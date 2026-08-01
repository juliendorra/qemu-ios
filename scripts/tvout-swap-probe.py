#!/usr/bin/env python3
"""Name the code that polls (and writes) the TVOut swap-device field.

T1 (MBX_HANDOFF.md) wants the derived 4-byte always-zero window deleted, which
first requires knowing WHO reads it. The window's MMIO handlers log guest
pc/lr under IT_FB_TRACE since 2026-07-31, so one instrumented boot answers:

  * which AppleMBX (or other kext) function polls the field at +0x160,
  * whether anything ever WRITES the field (writes used to be dropped
    silently),
  * what MBX register conversation surrounds the teardown (IT_MBX_TRACE=all),
  * what the guest's 0x130 enable mask is at that moment -- i.e. whether an
    event/IRQ could even be delivered, or whether completion must be
    memory-side.

Boots M68AP to the home screen exactly like mbx-composite-probe (VNC refresh
client, icount, staged NAND clone), then symbolizes every distinct [TVOUT-WA]
pc/lr against the build's kernelcache.

Example:
    python3 scripts/tvout-swap-probe.py --build 4A102 \
        --kernelcache /tmp/kc114r.raw --logs /tmp/tvout-swap
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
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lockprobe = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")

WA_RD = re.compile(r"\[TVOUT-WA\] rd \+0x(\d+) pc=0x([0-9a-f]{8}) "
                   r"lr=0x([0-9a-f]{8})")
WA_WR = re.compile(r"\[TVOUT-WA\] wr \+0x([0-9a-f]+) = 0x([0-9a-f]{8}) "
                   r"pc=0x([0-9a-f]{8}) lr=0x([0-9a-f]{8})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    m68ap_paths.add_build_argument(ap)
    ap.add_argument("--kernelcache", type=Path,
                    help="decrypted kernelcache for symbolization "
                         "(scripts/extract-kernelcache.py output)")
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--qemu", type=Path,
                    default=REPO / "build-ipod11" / "qemu-system-arm")
    ap.add_argument("--deadline", type=float, default=600,
                    help="give up if no home screen after this many seconds")
    ap.add_argument("--mode", choices=("window", "sdo"), default="window",
                    help="window: today's shipped config, instrumented. "
                         "sdo: THE T1 ACCEPTANCE RUN -- swap-device window "
                         "REMOVED (IT_TVOUT_WA=0) and the modelled SDO frame "
                         "interrupt ON (IT_TVOUT_SDO=1); also watches the "
                         "real [swapdev+0x160] field in guest RAM (sparse "
                         "polls, never around input -- see dismiss-latency's "
                         "polling trap)")
    ap.add_argument("--settle", type=float, default=60,
                    help="seconds to keep running after the home screen, so "
                         "post-boot teardown traffic is captured too")
    ap.add_argument("--keep-stage", action="store_true",
                    help="keep the staged NAND clone (~300 MB) after the run; "
                         "by default it is deleted, per the repo's "
                         "disk-hygiene rule")
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    args.logs.mkdir(parents=True, exist_ok=True)
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

    classify = lockprobe._classifier()
    qmp_path = Path(f"/tmp/tvoutprobe-{os.getpid()}.qmp")
    serial = args.logs / "serial.log"
    stderr = args.logs / "stderr.log"
    vnc_port = 5997

    cmd = [str(args.qemu),
           "-M", (f"iPhone-2G,bootrom={m68ap_paths.BOOTROM},"
                  f"iboot={paths.iboot_sb},nand={nand}"),
           "-m", "1G", "-pflash", str(nor),
           "-icount", "1",
           "-L", str(lockprobe.PC_BIOS),
           "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{vnc_port - 5900}"]
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    env["IT_FB_TRACE"] = "1"
    env["IT_MBX_TRACE"] = "all"
    if args.mode == "sdo":
        env["IT_TVOUT_WA"] = "0"
        env["IT_TVOUT_SDO"] = "1"
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)
    kind = {"kind": "blank"}
    field_log = []
    try:
        client = lockprobe.DisplayClient(vnc_port)
        client.start()
        print(f"booting {args.build} instrumented "
              f"(IT_FB_TRACE, IT_MBX_TRACE=all) ...", flush=True)
        time.sleep(60)
        q = lockprobe.QMP(qmp_path)

        field_pa = None

        def watch_field():
            """One sparse look at the real [swapdev+0x160] word (sdo mode)."""
            nonlocal field_pa
            if args.mode != "sdo":
                return
            if field_pa is None:
                m = re.search(rb"Added swap device: AppleH1TVOut\s+id: "
                              rb"([0-9a-f]{8})", serial.read_bytes())
                if not m:
                    return
                va = int(m.group(1), 16)
                field_pa = (va - 0xC0000000) + 0x08000000 + 0x160
                print(f"swap device announced at VA 0x{va:08x}; watching "
                      f"field PA 0x{field_pa:08x}")
            raw = args.logs / "_field.raw"
            q.cmd("pmemsave", {"val": field_pa, "size": 4,
                               "filename": str(raw)})
            v = int.from_bytes(raw.read_bytes(), "little")
            raw.unlink(missing_ok=True)
            field_log.append({"t": round(time.time() - t0, 1),
                              "value": f"{v:08x}"})
            print(f"  [swapdev+0x160] = 0x{v:08x}")

        t0 = time.time()
        deadline = time.time() + args.deadline
        while time.time() < deadline:
            watch_field()
            d, kind = lockprobe.grab(q, args.logs, classify)
            if kind["kind"] == "home":
                break
            if kind["nonblack_pct"] > 5.0:
                lockprobe.slide(q)
                time.sleep(8)
                d, kind = lockprobe.grab(q, args.logs, classify)
                if kind["kind"] == "home":
                    break
            time.sleep(10)
        watch_field()
        print(f"screen: {kind}")
        lockprobe.png(d, args.logs / "screen.png")
        if kind["kind"] == "home":
            time.sleep(args.settle)
        q.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        qmp_path.unlink(missing_ok=True)
        # Disk hygiene, the repo convention (M68AP_RENDER_HANDOFF "Traps"):
        # a NAND tree is ~300 MB and these probes stage one per run. Leaving
        # them behind filled this machine's data volume to 100% on
        # 2026-08-01 and broke an app install mid-flight. Inside the finally
        # so an early return or a crash still cleans up. --keep-stage keeps
        # it when a run needs the guest's own writes for post-mortem.
        if not args.keep_stage:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


    text = stderr.read_bytes().decode("latin1", "replace")
    rd_sites = Counter()
    wr_sites = Counter()
    for line in text.splitlines():
        m = WA_RD.search(line)
        if m:
            rd_sites[(m.group(2), m.group(3))] += 1
            continue
        m = WA_WR.search(line)
        if m:
            wr_sites[(f"{m.group(3)}/{m.group(4)}", m.group(2))] += 1

    report = {"build": args.build, "mode": args.mode, "screen": kind,
              "swap_field_watch": field_log,
              "read_sites": [[f"pc={pc} lr={lr}", n]
                             for (pc, lr), n in rd_sites.most_common()],
              "write_sites": [[f"pclr={pclr} val={v}", n]
                              for (pclr, v), n in wr_sites.most_common()]}
    print(f"\n{len(rd_sites)} distinct read sites, "
          f"{len(wr_sites)} distinct write sites")
    for (pc, lr), n in rd_sites.most_common():
        print(f"  rd  pc=0x{pc} lr=0x{lr}  x{n}")
    for (pclr, v), n in wr_sites.most_common():
        print(f"  wr  {pclr} val=0x{v}  x{n}")

    if args.kernelcache and rd_sites:
        addrs = sorted({a for pc, lr in list(rd_sites) for a in (pc, lr)})
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "kernel-addr-symbolize.py"),
             str(args.kernelcache)] + [f"0x{a}" for a in addrs],
            capture_output=True, text=True)
        print(r.stdout)
        (args.logs / "symbolized.txt").write_text(r.stdout)

    (args.logs / "tvout-swap-probe.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(f"report: {args.logs / 'tvout-swap-probe.json'}")
    return 0 if kind["kind"] == "home" else 1


if __name__ == "__main__":
    raise SystemExit(main())
