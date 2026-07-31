#!/usr/bin/env python3
"""How much guest CPU goes to LayerKit software compositing? (MBX §4 gate)

MBX_HANDOFF.md §4: before writing any MBX device code, measure whether
software compositing is actually where the guest spends its time during
home-screen animations. If the share is small, MBX modelling is fidelity
work only and the performance argument evaporates.

Method: boot M68AP to the home screen (VNC refresh client attached, exactly
like lock-unlock-probe), then hammer QMP `info registers` on a second QMP
socket while driving animations on the first:

    idle      -- home screen, no input (baseline: should be ~all WFI)
    shimmer   -- sleep, wake to the lock screen, sample the continuous
                 "slide to unlock" text shimmer, then unlock again
    appzoom   -- repeated app-open / home-close zoom animations

Each sampled R15 is attributed by the firmware's own prebinding: every
framework/dylib on the (already mounted) root filesystem occupies a fixed
__TEXT [vmaddr, vmaddr+vmsize) that this script reads with `otool -l`.
Anything >= 0xC0000000 is kernel and reported by top-PC (symbolize the top
ones with scripts/kernel-addr-symbolize.py against the build's kernelcache).

Example:
    python3 scripts/mbx-composite-probe.py --build 4A102 \
        --root /private/tmp/m68_114 --logs /tmp/mbx-composite
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402

R15_RE = re.compile(r"R15=([0-9a-fA-F]{8})")
PSR_RE = re.compile(r"PSR=([0-9a-fA-F]{8})")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lockprobe = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")


# ---------------------------------------------------------------- ranges ----

def text_range(binary: Path):
    """(vmaddr, vmsize) of __TEXT, or None. otool copes with 1.x load cmds."""
    try:
        out = subprocess.run(["otool", "-l", str(binary)], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"segname __TEXT\n\s*vmaddr (0x[0-9a-f]+)\n"
                  r"\s*vmsize (0x[0-9a-f]+)", out)
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


def build_ranges(root: Path, cache: Path) -> list:
    """[[start, end, name], ...] for every prebound image on the root fs."""
    if cache.exists():
        return json.loads(cache.read_text())
    binaries = {}
    for fwdir in ("System/Library/Frameworks",
                  "System/Library/PrivateFrameworks"):
        base = root / fwdir
        if not base.is_dir():
            continue
        for fw in sorted(base.glob("*.framework")):
            b = fw / fw.stem
            if b.is_file():
                binaries[fw.stem] = b
    for dylib in sorted((root / "usr/lib").rglob("*.dylib")):
        if dylib.is_file() and not dylib.is_symlink():
            binaries[dylib.name] = dylib
    # SpringBoard (and every other app main binary) loads its __TEXT at
    # 0x1000 -- apps are not in the shared prebinding, so this bucket cannot
    # tell SpringBoard from the foreground app during the appzoom phase.
    sb = root / "System/Library/CoreServices/SpringBoard.app/SpringBoard"
    if sb.is_file():
        binaries["app-main(SpringBoard/fg-app)"] = sb
    ranges = []
    for name, path in binaries.items():
        tr = text_range(path)
        if tr and tr[0] > 0:
            ranges.append([tr[0], tr[0] + tr[1], name])
    ranges.sort()
    cache.write_text(json.dumps(ranges))
    print(f"prebinding map: {len(ranges)} images ({cache})")
    return ranges


def attribute(pc: int, ranges: list) -> str:
    if pc >= 0xC0000000:
        return "kernel"
    for start, end, name in ranges:
        if start <= pc < end:
            return name
    return "user-unmapped"


# --------------------------------------------------------------- sampler ----

class Sampler(threading.Thread):
    """Continuously read R15 over a dedicated QMP socket; tag by phase."""

    daemon = True

    def __init__(self, qmp_path: Path, hz: float = 100.0):
        super().__init__(daemon=True)
        self.qmp_path = qmp_path
        self.period = 1.0 / hz
        self.phase = None            # None = discard samples
        self.samples = []            # (phase, pc, mode)
        self._stop = threading.Event()

    def run(self):
        q = lockprobe.QMP(self.qmp_path)
        try:
            while not self._stop.is_set():
                phase = self.phase
                if phase is None:
                    time.sleep(0.05)
                    continue
                t0 = time.time()
                r = q.cmd("human-monitor-command",
                          {"command-line": "info registers"})
                regs = r.get("return", "") if isinstance(r, dict) else ""
                m15, mpsr = R15_RE.search(regs), PSR_RE.search(regs)
                if m15:
                    mode = (int(mpsr.group(1), 16) & 0x1F) if mpsr else 0
                    self.samples.append((phase, int(m15.group(1), 16), mode))
                dt = self.period - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
        finally:
            q.close()

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------ main ----

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    m68ap_paths.add_build_argument(ap)
    ap.add_argument("--root", type=Path, required=True,
                    help="mount point of this build's root.img "
                         "(hdiutil attach -readonly)")
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--qemu", type=Path,
                    default=REPO / "build-ipod11" / "qemu-system-arm")
    ap.add_argument("--boot-wait", type=float, default=200)
    ap.add_argument("--hz", type=float, default=100.0)
    ap.add_argument("--idle-secs", type=float, default=20)
    ap.add_argument("--shimmer-secs", type=float, default=25)
    ap.add_argument("--zoom-cycles", type=int, default=6)
    ap.add_argument("--icon", type=int, nargs=2, default=(40, 95),
                    metavar=("X", "Y"), help="home-screen icon to poke for "
                    "the app zoom phase (default: first icon)")
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    args.logs.mkdir(parents=True, exist_ok=True)
    # stage a clone so guest NAND writes never dirty the build artifact
    stage = args.logs / "stage"
    nand = stage / "nand"
    if nand.exists():
        import shutil
        shutil.rmtree(nand)
    stage.mkdir(exist_ok=True)
    subprocess.run(["cp", "-Rc", str(paths.nand), str(nand)], check=True)
    for b in range(8):
        (nand / f"bank{b}").mkdir(exist_ok=True)
    nor = stage / "nor.bin"
    subprocess.run(["cp", str(paths.nor), str(nor)], check=True)
    ranges = build_ranges(args.root, args.logs / "prebind-ranges.json")
    classify = lockprobe._classifier()

    qmp_drive = Path(f"/tmp/mbxprobe-{os.getpid()}-a.qmp")
    qmp_sample = Path(f"/tmp/mbxprobe-{os.getpid()}-b.qmp")
    serial = args.logs / "serial.log"
    stderr = args.logs / "stderr.log"
    vnc_port = 5998

    cmd = [str(args.qemu),
           "-M", (f"iPhone-2G,bootrom={m68ap_paths.BOOTROM},"
                  f"iboot={paths.iboot_sb},nand={nand}"),
           "-m", "1G", "-pflash", str(nor),
           # 1.1.4 boots are timing-sensitive without icount: the first run of
           # this probe panicked in IOIpodUSBDevice::start, the exact signature
           # BROWSER_WASM_STATUS.md records for fb-snapshot.py's missing
           # -icount. Deterministic instruction pacing also makes the sampled
           # shares reproducible run-to-run.
           "-icount", "1",
           "-L", str(lockprobe.PC_BIOS),
           "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_drive},server,nowait",
           "-qmp", f"unix:{qmp_sample},server,nowait",
           "-vnc", f"127.0.0.1:{vnc_port - 5900}"]
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)

    report = {"build": args.build, "hz": args.hz, "phases": {}}
    sampler = None
    try:
        client = lockprobe.DisplayClient(vnc_port)
        client.start()
        # icount boots are slow and variable: poll for the home screen (or a
        # lock screen we can slide away) rather than trusting a fixed wait.
        print(f"booting {args.build}: polling for the home screen "
              f"(deadline {args.boot_wait:.0f}s + grace) ...", flush=True)
        time.sleep(60)
        q = lockprobe.QMP(qmp_drive)
        deadline = time.time() + args.boot_wait + 300
        kind = {"kind": "blank"}
        while time.time() < deadline:
            d, kind = lockprobe.grab(q, args.logs, classify)
            if kind["kind"] == "home":
                break
            if kind["nonblack_pct"] > 5.0:
                # something rendered that is not home -- likely the lock
                # screen; try a slide and re-check
                lockprobe.slide(q)
                time.sleep(8)
                d, kind = lockprobe.grab(q, args.logs, classify)
                if kind["kind"] == "home":
                    break
            time.sleep(10)
        lockprobe.png(d, args.logs / "00-booted.png")
        print(f"booted: {kind}")
        report["booted"] = kind
        if kind["kind"] != "home":
            print("never reached the home screen; aborting")
            return 1

        sampler = Sampler(qmp_sample, args.hz)
        sampler.start()

        # -- phase 1: idle home screen ------------------------------------
        print(f"phase idle: {args.idle_secs:.0f}s, no input")
        sampler.phase = "idle"
        time.sleep(args.idle_secs)
        sampler.phase = None

        # -- phase 2: app open/close zoom ---------------------------------
        # This runs BEFORE the lock/shimmer phase: on the first run, waking
        # from the power/home cycle raised a telephony "Repair Needed" alert
        # over the lock screen and the re-unlock slide never landed, so the
        # "appzoom" samples were actually lock-screen samples.
        print(f"phase appzoom: {args.zoom_cycles} open/close cycles at "
              f"icon {tuple(args.icon)}")
        sampler.phase = "appzoom"
        for n in range(args.zoom_cycles):
            lockprobe._abs(q, args.icon[0], args.icon[1])
            q.cmd("input-send-event", {"events": [
                {"type": "btn", "data": {"down": True, "button": "left"}}]})
            time.sleep(0.12)
            q.cmd("input-send-event", {"events": [
                {"type": "btn", "data": {"down": False, "button": "left"}}]})
            time.sleep(3.0)
            if n == 0:
                d, kind = lockprobe.grab(q, args.logs, classify)
                lockprobe.png(d, args.logs / "04-in-app.png")
            lockprobe.key(q, "h")
            time.sleep(3.0)
        sampler.phase = None
        d, kind = lockprobe.grab(q, args.logs, classify)
        lockprobe.png(d, args.logs / "05-after-zoom.png")
        print(f"after appzoom: {kind}")
        report["after_zoom"] = kind

        # -- phase 3: lock-screen shimmer (last: waking can raise alerts
        # over the lock screen and strand the run there) ------------------
        lockprobe.key(q, "p")
        time.sleep(3)
        lockprobe.key(q, "h")
        time.sleep(4)
        d, kind = lockprobe.grab(q, args.logs, classify)
        lockprobe.png(d, args.logs / "06-lockscreen.png")
        print(f"lock screen for shimmer phase: {kind}")
        report["shimmer_screen"] = kind
        print(f"phase shimmer: {args.shimmer_secs:.0f}s on the lock screen")
        sampler.phase = "shimmer"
        time.sleep(args.shimmer_secs)
        sampler.phase = None
        d, kind = lockprobe.grab(q, args.logs, classify)
        lockprobe.png(d, args.logs / "07-final.png")
        print(f"final screen: {kind}")
        report["final_screen"] = kind
        q.close()
    finally:
        if sampler is not None:
            sampler.stop()
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for p in (qmp_drive, qmp_sample):
            p.unlink(missing_ok=True)

    # ------------------------------------------------------------ report ----
    raw = [{"phase": ph, "pc": pc, "mode": mode}
           for ph, pc, mode in (sampler.samples if sampler else [])]
    (args.logs / "samples.json").write_text(json.dumps(raw))
    for phase in ("idle", "shimmer", "appzoom"):
        pcs = [s for s in raw if s["phase"] == phase]
        mods = Counter(attribute(s["pc"], ranges) for s in pcs)
        kern = Counter(f"{s['pc']:08x}" for s in pcs if s["pc"] >= 0xC0000000)
        user = Counter((attribute(s["pc"], ranges), f"{s['pc']:08x}")
                       for s in pcs if s["pc"] < 0xC0000000)
        n = len(pcs) or 1
        report["phases"][phase] = {
            "samples": len(pcs),
            "by_module_pct": {m: round(100.0 * c / n, 1)
                              for m, c in mods.most_common(20)},
            "top_kernel_pcs": kern.most_common(10),
            "top_user_pcs": [[f"{m}:{a}", c]
                             for (m, a), c in user.most_common(15)],
        }
        print(f"\n=== {phase}: {len(pcs)} samples ===")
        for m, c in mods.most_common(12):
            print(f"  {100.0 * c / n:5.1f}%  {m}")
    (args.logs / "composite-probe.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(f"\nreport: {args.logs / 'composite-probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
