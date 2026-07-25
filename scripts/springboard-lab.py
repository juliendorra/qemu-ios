#!/usr/bin/env python3
"""Parallel, self-judging SpringBoard/display bring-up experiments.

Same idea as `scripts/baseband-lab.py`, aimed at a different wall: M68AP
reaches SpringBoard, is [Activated] and registered, and STILL never programs a
framebuffer base -- the guest parks in the kernel wait-for-interrupt idle loop,
i.e. SpringBoard is blocked on an event that never arrives. Finding what it
waits on means comparing many boot configurations, and the only fast way is to
run them concurrently and let the harness judge each one.

What it does
------------
Boots one QEMU per VARIANT (M68AP configurations + an N45AP control that is
known to render), watches each until a decisive verdict, then collects the
evidence that discriminates the hypotheses:

  verdicts
    rendered   an LCD window base was programmed to a kernel framebuffer
               (0x0f400000 / 0x0f496000) or a framebuffer sampled non-black
    wedged     serial stopped growing AND every PC sample sits in the kernel
               idle loop -> a blocked thread, not a crawl
    crawling   serial stopped growing but PCs are spread -> still executing
    panicked   a kernel panic appeared
    timeout    --max-wall reached

  evidence per instance
    pc_histogram        where the guest actually is (idle vs spinning)
    lcd_bases           every window base the guest programmed
    framebuffers        non-black %% of the three candidate FB bases
    phase               last recognised boot marker (how far it got)
    springboard_lines   SpringBoard's own log lines
    driver_tail         the last IOKit attach/registration lines before the wedge
    markers             counted regexes (activation, registration, ...)

Then `--diff A=B` prints the driver/service tokens present in one instance's
post-SpringBoard serial and absent in the other's -- the differential that
points at the missing event (e.g. N45AP-renders vs M68AP-wedges).

Variants are declared in VARIANTS below (name -> board + env + artifact knobs)
so hypotheses are data, not code edits. NAND trees are built once per unique
artifact recipe and cached in the logs dir.

Example
-------
  python3 scripts/springboard-lab.py --logs /tmp/sblab \
      --variants n45ap-control m68ap-full m68ap-nobb m68ap-plain \
      --diff m68ap-full=n45ap-control

Host contention note: simultaneous boots perturb the USB-start window and can
flip the IOIpodUSBDevice::start race (see the baseband lab). Instances are
staggered by --stagger-secs (default 30).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
PC_BIOS = APP / "Resources" / "pc-bios"
QEMU = REPO / "build-ipod11" / "qemu-system-arm"
M68_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"
M68_IBOOT = REPO / "m68ap-artifacts" / "stage" / "iboot_204_m68ap_sbpatch.bin"
M68_NOR = REPO / "m68ap-artifacts" / "stage" / "nor_m68ap.bin"
M68_ROOT_HFS = REPO / "m68ap-artifacts" / "stage" / "filesystem-m68ap-readonly.img"
M68_DATA_DMG = REPO / "m68ap-artifacts" / "stage" / "data-m68ap.dmg"

KERNEL_FB_BASES = (0x0F400000, 0x0F496000)
FB_BASES = {"iboot_0x0fe00000": 0x0FE00000,
            "kernel_0x0f400000": 0x0F400000,
            "kernel_0x0f496000": 0x0F496000}
# The kernel idle loop (wait-for-interrupt); PCs here mean "nothing to run".
IDLE_PCS = {"c005a9cc", "c005a9c4", "c005a9c8", "c005a9d0"}

MARKERS = {
    "panic": rb"panic\(cpu",
    "springboard": rb"SpringBoard\[",
    "activated": rb"device is: \[Activated\]",
    "unactivated_flip": rb"activation state to Unactivated",
    "everregistered_missing": rb"didn't have a EverRegistered",
    "registered": rb"previously registered",
    "coresurface": rb"IOCoreSurfaceRootUserClient::attach",
    "mobilefb": rb"IOMobileFramebufferUserClient::attach",
    "multitouch": rb"AppleMultitouch",
    "usb_ready": rb"ready to start usb stack",
    "configuring_sb": rb"Configuring SpringBoard",
}
# Ordered boot phases; the last one seen is the instance's "phase".
PHASES = [
    ("iboot", rb"iBoot version"),
    ("kernel", rb"Darwin Kernel Version|BSD root"),
    ("launchd", rb"launchd\[1\]: BOOT_TIME"),
    ("springboard_start", rb"SpringBoard\["),
    ("activation_checked", rb"device is: \[(Un)?Activated\]"),
    ("registration_checked", rb"previously registered|EverRegistered"),
    ("fb_attached", rb"IOMobileFramebufferUserClient::attach"),
    ("coresurface", rb"IOCoreSurfaceRootUserClient::attach"),
    ("configuring", rb"Configuring SpringBoard"),
]

# --- hypothesis matrix ----------------------------------------------------
# board: n45ap | m68ap ; dataark/patch: M68AP artifact knobs ; env: extra env.
VARIANTS = {
    # the reference that DOES render
    "n45ap-control": dict(board="n45ap", env={}),
    # M68AP, everything we know how to give it
    "m68ap-full": dict(board="m68ap", dataark=True, patch=True,
                       env={"IT_M68AP_NO_BASEBAND": "1"}),
    # same but with the baseband stub attached
    "m68ap-full-bb": dict(board="m68ap", dataark=True, patch=True,
                          env={"IT_BASEBAND_H5": "1"}),
    # untouched artifacts: the [Unactivated] baseline
    "m68ap-plain": dict(board="m68ap", dataark=False, patch=False,
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    # data ark only (no binary patch)
    "m68ap-ark": dict(board="m68ap", dataark=True, patch=False,
                      env={"IT_M68AP_NO_BASEBAND": "1"}),
    # patch only (no data ark)
    "m68ap-patch": dict(board="m68ap", dataark=False, patch=True,
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- activation DURABILITY matrix (can the binary patch be dropped?) ---
    # All of these are data-ark-only (patch=False). The question each answers:
    # does [Activated] SURVIVE determine_activation_state's boot re-validation
    # (markers.unactivated_flip == 0), using only lockdownd's data-driven
    # levers? If one holds, hacktivation becomes pure data, like the iPod.
    "ark-minimal": dict(board="m68ap", dataark=True, patch=False,
                        ark_profile="minimal",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-factory": dict(board="m68ap", dataark=True, patch=False,
                        ark_profile="factory",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-unactsvc": dict(board="m68ap", dataark=True, patch=False,
                         ark_profile="unactsvc",
                         env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-all": dict(board="m68ap", dataark=True, patch=False,
                    ark_profile="all",
                    env={"IT_M68AP_NO_BASEBAND": "1"}),
}


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


class QMP:
    def __init__(self, path, timeout=10):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("qmp closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute, arguments=None):
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.s.sendall((json.dumps(msg) + "\n").encode())
        while True:
            r = self._read()
            if "return" in r or "error" in r:
                return r

    def hmp(self, line):
        return self.cmd("human-monitor-command",
                        {"command-line": line}).get("return", "")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def build_m68ap_nand(out: Path, dataark: bool, patch: bool, work: Path,
                     ark_profile: str = "minimal") -> Path:
    """Build (and cache) an M68AP NAND for a given artifact recipe."""
    if out.exists() and (out / "bank0").exists():
        return out
    work.mkdir(parents=True, exist_ok=True)
    root = M68_ROOT_HFS
    if patch:
        root = work / "root-patched.img"
        if not root.exists():
            run([sys.executable, str(REPO / "scripts" / "hacktivate-m68ap.py"),
                 "patch", "--root-hfs", str(M68_ROOT_HFS), "--out", str(root)])
    data = M68_DATA_DMG
    if dataark:
        data = work / "data-ark.img"
        if not data.exists():
            ark = work / "data_ark.plist"
            run([sys.executable, str(REPO / "scripts" / "hacktivate-m68ap.py"),
                 "build-dataark", "--out", str(ark), "--profile", ark_profile])
            size = M68_DATA_DMG.stat().st_size
            dmg = work / "data.dmg"
            if dmg.exists():
                dmg.unlink()
            run(["hdiutil", "create", "-sectors", str(size // 512), "-fs",
                 "Case-sensitive HFS+", "-volname", "var", "-layout", "NONE",
                 "-o", str(dmg)])
            run([sys.executable, str(REPO / "scripts" / "inject-guest-file.py"),
                 "--image", str(dmg), "--src", str(ark),
                 "--dest", "/root/Library/Lockdown/data_ark.plist"])
            cdr = work / "data-raw"
            run(["hdiutil", "convert", str(dmg), "-format", "UDTO",
                 "-o", str(cdr)])
            shutil.move(str(work / "data-raw.cdr"), str(data))
    run([sys.executable, str(REPO / "scripts" / "build-m68ap-nand.py"),
         "--out", str(out), "--signature", "m68ap", "--active-banks", "4",
         "--bbt", "production", "--hfs", str(root), "--data-hfs", str(data),
         "--device", "iPhone1,1", "--ipsw-build", "4A102"])
    return out


class Instance:
    def __init__(self, name, spec, args, cache: Path):
        self.name = name
        self.spec = spec
        self.args = args
        self.cache = cache
        self.dir = args.logs / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.serial = self.dir / "serial.log"
        self.stderr = self.dir / "stderr.log"
        self.qmp_path = Path(f"/tmp/sblab-{os.getpid()}-{name}.qmp")
        self.proc = None
        self.result = {"name": name, "spec": {k: v for k, v in spec.items()
                                              if k != "env"}}

    def stage(self):
        board = self.spec["board"]
        stage = self.dir / "stage"
        stage.mkdir(exist_ok=True)
        if board == "n45ap":
            nand = stage / "nand"
            if not nand.exists():
                run(["cp", "-Rc", str(IPOD_FILES / "nand"), str(nand)])
            nor = stage / "nor.bin"
            shutil.copy2(IPOD_FILES / "nor_n45ap.bin", nor)
            return (M68_BOOTROM, IPOD_FILES / "iboot_204_n45ap.bin", nand, nor,
                    "iPod-Touch")
        profile = self.spec.get("ark_profile", "minimal")
        recipe = f"m68ap-ark{int(self.spec.get('dataark', False))}" \
                 f"-{profile}-patch{int(self.spec.get('patch', False))}"
        shared = self.cache / recipe
        build_m68ap_nand(shared / "nand", self.spec.get("dataark", False),
                         self.spec.get("patch", False), shared, profile)
        nand = stage / "nand"
        if nand.exists():
            shutil.rmtree(nand)
        run(["cp", "-Rc", str(shared / "nand"), str(nand)])
        nor = stage / "nor.bin"
        shutil.copy2(M68_NOR, nor)
        return (M68_BOOTROM, M68_IBOOT, nand, nor, "iPhone-2G")

    def launch(self):
        bootrom, iboot, nand, nor, machine = self.stage()
        cmd = [str(self.args.qemu),
               "-M", f"{machine},bootrom={bootrom},iboot={iboot},nand={nand}",
               "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
               "-display", "none",
               "-serial", f"file:{self.serial}",
               "-qmp", f"unix:{self.qmp_path},server,nowait"]
        env = dict(os.environ)
        env["IT_LCD_TRACE"] = "1"
        env.update(self.spec.get("env", {}))
        (self.dir / "command.txt").write_text(
            " ".join(cmd) + "\n# env: " +
            json.dumps(self.spec.get("env", {})) + "\n")
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=self.stderr.open("wb"),
                                     stderr=subprocess.STDOUT)

    # --- evidence -------------------------------------------------------
    def serial_bytes(self):
        return self.serial.stat().st_size if self.serial.exists() else 0

    def text(self):
        return self.serial.read_bytes() if self.serial.exists() else b""

    def lcd_bases(self):
        if not self.stderr.exists():
            return []
        blob = self.stderr.read_text(errors="replace")
        return sorted(set(re.findall(r"base <- (0x0[0-9a-f]{7})", blob)))

    def rendered(self):
        for b in self.lcd_bases():
            if int(b, 16) in KERNEL_FB_BASES:
                return True
        return False

    def sample_pcs(self, n=8):
        pcs = []
        try:
            q = QMP(self.qmp_path)
        except Exception:
            return pcs
        try:
            for _ in range(n):
                t = q.hmp("info registers")
                m = re.search(r"R15=([0-9a-fA-F]{8})", t)
                if m:
                    pcs.append(m.group(1).lower())
                time.sleep(0.1)
        except Exception:
            pass
        finally:
            q.close()
        return pcs

    def framebuffers(self):
        out = {}
        try:
            q = QMP(self.qmp_path)
        except Exception:
            return out
        try:
            for label, base in FB_BASES.items():
                raw = self.dir / f"fb_{base:08x}.raw"
                q.cmd("pmemsave", {"val": base, "size": 320 * 480 * 4,
                                   "filename": str(raw)})
                d = raw.read_bytes()
                nb = sum(1 for i in range(0, len(d), 4)
                         if d[i] > 16 or d[i + 1] > 16 or d[i + 2] > 16)
                out[label] = round(100 * nb / (len(d) // 4), 1)
                raw.unlink(missing_ok=True)
        except Exception:
            pass
        finally:
            q.close()
        return out

    def phase(self, blob):
        seen = "none"
        for name, rx in PHASES:
            if re.search(rx, blob):
                seen = name
        return seen

    def collect(self, verdict, wall):
        blob = self.text()
        pcs = self.sample_pcs(self.args.pc_samples)
        lines = blob.decode("latin1", "replace").splitlines()
        sb = [l for l in lines if "SpringBoard[" in l]
        drivers = [l for l in lines
                   if re.search(r"::attach|Registering:|registerFunction", l)]
        self.result.update({
            "verdict": verdict,
            "wall_s": round(wall, 1),
            "serial_lines": len(lines),
            "phase": self.phase(blob),
            "markers": {k: len(re.findall(v, blob)) for k, v in MARKERS.items()},
            "lcd_bases": self.lcd_bases(),
            "rendered": self.rendered(),
            "pc_histogram": Counter(pcs).most_common(6),
            "pcs_all_idle": bool(pcs) and all(p in IDLE_PCS for p in pcs),
            "framebuffers": self.framebuffers(),
            "springboard_lines": sb[-6:],
            "driver_tail": drivers[-12:],
        })
        return self.result

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.qmp_path.unlink(missing_ok=True)

    def run(self):
        started = time.monotonic()
        self.launch()
        last_size, last_change = -1, time.monotonic()
        verdict = "timeout"
        try:
            while time.monotonic() - started < self.args.max_wall:
                time.sleep(self.args.poll_secs)
                blob = self.text()
                if re.search(MARKERS["panic"], blob):
                    verdict = "panicked"
                    break
                if self.rendered():
                    time.sleep(self.args.post_success)
                    verdict = "rendered"
                    break
                size = self.serial_bytes()
                if size != last_size:
                    last_size, last_change = size, time.monotonic()
                elif time.monotonic() - last_change > self.args.stall_secs:
                    pcs = self.sample_pcs(self.args.pc_samples)
                    verdict = ("wedged" if pcs and all(p in IDLE_PCS for p in pcs)
                               else "crawling")
                    break
            return self.collect(verdict, time.monotonic() - started)
        finally:
            self.kill()


def diff_instances(results, a_name, b_name):
    """Tokens in A's post-SpringBoard driver lines that are absent from B's."""
    by = {r["name"]: r for r in results if r}
    if a_name not in by or b_name not in by:
        return None
    def tokens(r):
        toks = set()
        for line in r.get("driver_tail", []):
            for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", line):
                toks.add(t)
        return toks
    ta, tb = tokens(by[a_name]), tokens(by[b_name])
    return {"only_in_" + a_name: sorted(ta - tb),
            "only_in_" + b_name: sorted(tb - ta)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--variants", nargs="+", default=["n45ap-control",
                                                      "m68ap-full",
                                                      "m68ap-plain"],
                    help=f"any of: {', '.join(VARIANTS)}")
    ap.add_argument("--diff", action="append", default=[], metavar="A=B",
                    help="print driver-token diff between two instances")
    ap.add_argument("--qemu", type=Path, default=QEMU)
    ap.add_argument("--max-wall", type=float, default=420)
    ap.add_argument("--stall-secs", type=float, default=60)
    ap.add_argument("--post-success", type=float, default=20)
    ap.add_argument("--poll-secs", type=float, default=3)
    ap.add_argument("--pc-samples", type=int, default=8)
    ap.add_argument("--stagger-secs", type=float, default=30)
    args = ap.parse_args()

    for v in args.variants:
        if v not in VARIANTS:
            ap.error(f"unknown variant {v!r}; known: {', '.join(VARIANTS)}")
    args.logs.mkdir(parents=True, exist_ok=True)
    cache = args.logs / "_cache"
    cache.mkdir(exist_ok=True)

    instances = [Instance(v, VARIANTS[v], args, cache) for v in args.variants]
    results = [None] * len(instances)

    def worker(i):
        time.sleep(i * args.stagger_secs)
        print(f"[{instances[i].name}] booting", flush=True)
        try:
            results[i] = instances[i].run()
            r = results[i]
            print(f"[{r['name']}] {r['verdict']} phase={r['phase']} "
                  f"bases={r['lcd_bases']} idle={r['pcs_all_idle']}", flush=True)
        except Exception as e:                      # keep the matrix going
            results[i] = {"name": instances[i].name, "verdict": f"error: {e}"}
            print(f"[{instances[i].name}] ERROR {e}", flush=True)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(len(instances))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for inst in instances:
            inst.kill()
        raise

    print("\n=== MATRIX ===")
    print(f"{'instance':16} {'verdict':10} {'phase':22} {'idle':5} "
          f"{'sb':3} {'bases'}")
    print("-" * 92)
    for r in results:
        if not r:
            continue
        print(f"{r['name']:16} {r.get('verdict',''):10} "
              f"{r.get('phase',''):22} {str(r.get('pcs_all_idle','')):5} "
              f"{r.get('markers',{}).get('springboard',0):<3} "
              f"{','.join(r.get('lcd_bases',[])) or '-'}")

    report = {"results": [r for r in results if r], "diffs": {}}
    for spec in args.diff:
        if "=" not in spec:
            continue
        a, b = spec.split("=", 1)
        d = diff_instances(results, a, b)
        if d:
            report["diffs"][spec] = d
            print(f"\n=== DIFF {a} vs {b} (driver/service tokens) ===")
            for k, v in d.items():
                print(f"  {k}: {', '.join(v[:25]) or '(none)'}")

    path = args.logs / "matrix.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
