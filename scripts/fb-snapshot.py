#!/usr/bin/env python3
"""Boot a board, pause it after a fixed time, and see WHERE rendered pixels are.

The S5L8900 LCD scans out whatever ``w1_framebuffer_base`` points at.  iBoot
draws its logo at 0x0fe00000; the kernel/SpringBoard/CoreSurface render into
0x0f400000 and 0x0f496000.  If the OS renders a frame but the scanout base is
never re-pointed at it, ``screendump`` (which follows the scanout) shows black
even though the pixels exist in guest RAM.

This tool pauses the guest and dumps all three candidate bases straight from
guest physical memory over an HMP *socket* (synchronous request/response, so no
race with a stdio pipe), measures how much of each is non-black, and renders
each to a PNG-ish PPM so the content can be inspected.  It answers "did the OS
render, and to which buffer" independently of what the LCD is scanning out.

Example:
    python3 scripts/fb-snapshot.py --board n45ap --boot-wait 180 \
        --logs /tmp/fbsnap-n45ap
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m68ap_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
DEFAULT_PLUGIN = REPO / "build-ipod11" / "contrib" / "plugins" / \
    "libm68ap-ftl-trace.dylib"
DEFAULT_BOOTROM = m68ap_paths.BOOTROM

FB_W, FB_H, FB_BPP = 320, 480, 4
FB_SIZE = FB_W * FB_H * FB_BPP
BASES = {"iboot_0x0fe00000": 0x0fe00000,
         "kernel_0x0f400000": 0x0f400000,
         "kernel_0x0f496000": 0x0f496000}


class QMP:
    """Minimal QMP client. Structured JSON avoids the HMP readline echo that
    mangles rapid socket writes."""

    def __init__(self, path: str, wait: float = 20.0):
        # Wait for the socket rather than assuming it is there: QEMU may still
        # be starting, and a bare FileNotFoundError here reads like a guest
        # failure when it is really a race or a swept directory.
        deadline = time.time() + wait
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.25)
        if not os.path.exists(path):
            raise SystemExit(
                f"QMP socket never appeared at {path}.\n"
                "The guest may have exited, or the directory was cleaned "
                "mid-run. Check stderr.log in the logs directory.")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self._read_obj()               # server greeting
        self.execute("qmp_capabilities")

    def _read_obj(self, timeout: float = 10.0) -> dict:
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("QMP socket closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def execute(self, cmd: str, **args) -> dict:
        req = {"execute": cmd}
        if args:
            req["arguments"] = args
        self.sock.sendall((json.dumps(req) + "\n").encode())
        # Read objects until the matching return/error (skip async events).
        while True:
            obj = self._read_obj()
            if "return" in obj or "error" in obj:
                return obj

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def measure(path: Path) -> tuple[float, int]:
    try:
        data = path.read_bytes()
    except OSError:
        return (-1.0, 0)
    if not data:
        return (-1.0, 0)
    nz = sum(1 for b in data if b)
    return (nz / len(data) * 100.0, len(data))


def raw_to_ppm(raw: Path, ppm: Path) -> None:
    """Interpret a raw BGRA/ARGB dump as 320x480 and write a P6 PPM (RGB)."""
    try:
        data = raw.read_bytes()
    except OSError:
        return
    if len(data) < FB_SIZE:
        return
    out = bytearray(b"P6\n%d %d\n255\n" % (FB_W, FB_H))
    # LCD draws32 uses B,G,R ordering in this model; map to R,G,B.
    for i in range(0, FB_W * FB_H * FB_BPP, FB_BPP):
        b, g, r = data[i], data[i + 1], data[i + 2]
        out += bytes((r, g, b))
    ppm.write_bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("m68ap", "n45ap"), required=True)
    ap.add_argument("--epoch", type=int, default=None,
                    help="SYSIC security epoch override (per FIRMWARE: 1.1.4=3, "
                         "1.1.1=2, 1.0=0). Default: the board's own.")
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path, default=DEFAULT_BOOTROM)
    ap.add_argument("--iboot-n45ap", type=Path,
                    default=IPOD_FILES / "iboot_204_n45ap.bin")
    ap.add_argument("--nor-n45ap", type=Path,
                    default=IPOD_FILES / "nor_n45ap.bin")
    ap.add_argument("--nand-n45ap", type=Path, default=IPOD_FILES / "nand")
    ap.add_argument("--iboot-m68ap", type=Path)
    ap.add_argument("--nor-m68ap", type=Path)
    ap.add_argument("--nand-m68ap", type=Path)
    ap.add_argument("--stabilize-root-domain", action="store_true")
    ap.add_argument("--observer-plugin", type=Path, default=DEFAULT_PLUGIN)
    ap.add_argument("--observer", action="store_true",
                    help="load the observer plugin (required for m68ap retain).")
    ap.add_argument("--boot-wait", type=int, default=180)
    ap.add_argument("--samples", type=int, default=1,
                    help="after boot-wait, take this many live samples of the "
                    "scanout vs the kernel FB, spaced --sample-interval apart, "
                    "to catch the awake window before the panel auto-sleeps.")
    ap.add_argument("--sample-interval", type=float, default=5.0)
    # -icount is not a nicety on M68AP: without it QEMU_CLOCK_VIRTUAL follows
    # wall clock, drivers see their own start() taking seconds, and the kernel
    # takes timeout paths no real device takes. 1.1.4 panics in
    # IOIpodUSBDevice::start / the USB wrangler -- intermittently on a fast
    # host, reliably on a loaded one -- and then renders NOTHING, which reads as
    # "this NAND is broken". ipod-app-launcher.sh already passes shift=1 for the
    # iphone-2g profile; this makes the same setting available here.
    ap.add_argument("--icount", type=int, default=None, metavar="SHIFT",
                    help="run with -icount shift=SHIFT (use 1 for m68ap)")
    ap.add_argument("--logs", type=Path, required=True)
    m68ap_paths.add_build_argument(ap, required=False)
    args = ap.parse_args()

    if args.board == "m68ap":
        # --build fills the artifact paths and the security epoch from the
        # canonical layout, so an M68AP run cannot silently mix one firmware's
        # NAND with another's epoch. Explicit paths still win.
        if args.build:
            paths = m68ap_paths.get(args.build)
            paths.require("iboot_sb", "nor", "nand")
            if args.iboot_m68ap is None:
                args.iboot_m68ap = paths.iboot_sb
            if args.nor_m68ap is None:
                args.nor_m68ap = paths.nor
            if args.nand_m68ap is None:
                args.nand_m68ap = paths.nand
            if args.epoch is None:
                args.epoch = paths.epoch
            if args.bootrom == DEFAULT_BOOTROM and not args.bootrom.exists():
                args.bootrom = paths.bootrom
            print(f"[fb-snapshot] {m68ap_paths.describe(args.build)}")
        for name in ("iboot_m68ap", "nor_m68ap", "nand_m68ap"):
            if getattr(args, name) is None:
                ap.error(f"--{name.replace('_', '-')} is required for m68ap "
                         "(or pass --build)")

    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    stage.mkdir(exist_ok=True)
    if args.board == "n45ap":
        src_nand, src_nor = args.nand_n45ap, args.nor_n45ap
        machine_type, iboot, profile = "iPod-Touch", args.iboot_n45ap, "n45ap"
    else:
        src_nand, src_nor = args.nand_m68ap, args.nor_m68ap
        machine_type, iboot, profile = "iPhone-2G", args.iboot_m68ap, "m68ap"

    staged_nand = stage / "nand"
    if staged_nand.exists():
        shutil.rmtree(staged_nand)
    subprocess.run(["cp", "-Rc", str(src_nand), str(staged_nand)], check=True)
    for bank in range(8):
        (staged_nand / f"bank{bank}").mkdir(exist_ok=True)
    staged_nor = stage / "nor.bin"
    shutil.copy2(src_nor, staged_nor)

    # AF_UNIX sun_path is capped at ~104 bytes on macOS, so the socket cannot
    # live under the (long) scratchpad logs dir; use a short name.
    #
    # /var/tmp, NOT /tmp: on a long run (boot-wait of several minutes) the
    # socket disappeared from /tmp while QEMU was still running, and the
    # connect then failed with a bare FileNotFoundError that looked like a
    # guest hang. /var/tmp is not swept.
    sock_dir = "/var/tmp" if os.path.isdir("/var/tmp") else "/tmp"
    sock_path = f"{sock_dir}/fbsnap-{args.board}-{os.getpid()}.sock"
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    serial = args.logs / "serial.log"
    serial.write_bytes(b"")
    machine = (f"{machine_type},bootrom={args.bootrom},"
               f"iboot={iboot},nand={staged_nand}")
    if args.epoch is not None:
        # The security epoch is firmware-keyed, not board-keyed: M68AP defaults
        # to 3 (1.1.4), but 1.1.1's images carry 2 and 1.0's carry 0. Booting
        # them without this wedges in iBoot with an empty serial log.
        machine += f",epoch={args.epoch}"
    cmd = [str(args.qemu), "-M", machine, "-m", "1G",
           "-pflash", str(staged_nor),
           "-L", str(APP / "Resources" / "pc-bios"),
           "-display", "none", "-serial", f"file:{serial}",
           "-qmp", f"unix:{sock_path},server,nowait"]
    if args.icount is not None:
        cmd += ["-icount", f"shift={args.icount}"]
    if args.observer and args.observer_plugin.is_file():
        spec = (f"{args.observer_plugin},profile={profile},"
                f"service-observer-only=true,trace-details=false,"
                f"log={args.logs / 'observer.log'}")
        if args.stabilize_root_domain:
            spec += ",stabilize-root-domain=true"
        cmd.extend(["-plugin", spec, "-d", "plugin",
                    "-D", str(args.logs / "trace.log")])

    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, stdout=(args.logs / "monitor.log").open("wb"),
                            stderr=(args.logs / "stderr.log").open("wb"))
    result: dict = {"board": args.board, "boot_wait_s": args.boot_wait}
    try:
        # Wait for the monitor socket to appear, then for the boot window.
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.2)
        print(f"booting {args.board} for {args.boot_wait}s ...", flush=True)
        time.sleep(args.boot_wait)

        qmp = QMP(sock_path)
        frames = args.logs / "frames"
        frames.mkdir(exist_ok=True)
        timeline = []
        best = {"screenout_nonzero_pct": -1.0}
        t0 = time.time()
        for i in range(max(1, args.samples)):
            elapsed = round(args.boot_wait + time.time() - t0, 1)
            shot = frames / f"s{i:03d}_screenout.ppm"
            qmp.execute("screendump", filename=str(shot))
            sd_nz, _ = measure(shot)
            sample = {"sample": i, "elapsed_s": elapsed,
                      "screenout_nonzero_pct": round(sd_nz, 3), "bases": {}}
            for name, base in BASES.items():
                raw = frames / f"s{i:03d}_{name}.raw"
                qmp.execute("pmemsave", val=base, size=FB_SIZE,
                            filename=str(raw))
                nz, _ = measure(raw)
                sample["bases"][name] = round(nz, 3)
                raw.unlink(missing_ok=True)   # keep timeline, not the RAM
            timeline.append(sample)
            print(json.dumps(sample), flush=True)
            # Keep the frame with the most on-screen content (the awake frame).
            if sd_nz > best["screenout_nonzero_pct"]:
                best = sample
                shutil.copy2(shot, args.logs / "best_screenout.ppm")
            if i < args.samples - 1:
                time.sleep(args.sample_interval)
        # Detailed final RAM breakdown (paused for consistency).
        qmp.execute("stop")
        result["bases"] = {}
        for name, base in BASES.items():
            raw = args.logs / f"{name}.raw"
            qmp.execute("pmemsave", val=base, size=FB_SIZE, filename=str(raw))
            nz, n = measure(raw)
            result["bases"][name] = {"addr": hex(base),
                                     "nonzero_pct": round(nz, 3), "bytes": n}
            if nz > 0:
                raw_to_ppm(raw, args.logs / f"{name}.ppm")
        result["timeline"] = timeline
        result["max_screenout_nonzero_pct"] = max(
            (s["screenout_nonzero_pct"] for s in timeline), default=-1.0)
        # Convert the best on-screen frame to PNG-viewable if it had content.
        best_ppm = args.logs / "best_screenout.ppm"
        if best_ppm.exists() and best["screenout_nonzero_pct"] > 0:
            pass  # already a valid P6 PPM from screendump
        qmp.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()

    (args.logs / "fb-snapshot.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"\nreport: {args.logs / 'fb-snapshot.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
