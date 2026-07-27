#!/usr/bin/env python3
"""Assert the Apple boot logo is on screen for the whole early boot.

The regression test for the 2026-07-27 boot-logo fix, which needed BOTH:

  * `build-m68ap-nor.py` filling in the IMG2 +0x18 walk stride, without which
    iBoot enumerates one image and never reaches `logo`
    (`scripts/nor-image-store.py --check` covers that half statically), and
  * the LCD scanning out display window 2 while window 1 is unprogrammed,
    because iBoot draws into window 2 and window 1 is the kernel's.

Either regressing puts the screen back to black, and neither shows up in a
serial log -- the guest boots fine and says nothing about it. So the check has
to be on pixels, sampled EARLY: the whole iBoot era is over within a few
seconds, which is why `fb-snapshot.py --boot-wait` (default 180 s) never saw
it.

Judging: the logo covers ~2.17% of the panel. Black is ~0.003% (screendump is
never exactly zero). The default threshold sits between them, far from both.
This deliberately does not try to recognise the logo -- "is anything lit during
iBoot" is the property that was broken, and a proxy that cannot distinguish the
logo from some other early image is still a proxy that goes to 0.003% the
moment either fix regresses.

Test the INSTALLED BUNDLE by default, not the repo build (NEXT_SESSION_HANDOFF
ground rule 1): the NOR half of the fix lives in the bundle's firmware, so a
green repo build says nothing about what the user launches.

Usage:
  # the installed app bundles -- what the user actually runs
  python3 scripts/verify-boot-logo.py --app "/Applications/iPhone 2G (iOS 1.1.4).app"
  python3 scripts/verify-boot-logo.py --app "/Applications/iPod Touch.app"

  # the repo build, for a given firmware
  python3 scripts/verify-boot-logo.py --board m68ap --build 4A102
  python3 scripts/verify-boot-logo.py --board n45ap

  # keep the frames to look at
  python3 scripts/verify-boot-logo.py --app ... --save-frames /tmp/logo
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
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m68ap_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
IPOD_APP = Path("/Applications/iPod Touch.app")
IPOD_FILES = IPOD_APP / "Contents" / "Resources" / "ipod_files"
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"

# Measured on both boards, both fixes in place: the logo frame is 2.17% lit and
# a black frame is 0.003%. Three orders of magnitude apart, so the threshold is
# not delicate.
LOGO_PCT = 2.17
BLACK_PCT = 0.003
DEFAULT_THRESHOLD = 1.0


class QMP:
    """Minimal QMP client (same shape as fb-snapshot.py's)."""

    def __init__(self, path: str, timeout: float = 30.0):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(path)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        self.buf = b""
        self._read_obj()
        self.execute("qmp_capabilities")

    def _read_obj(self, timeout: float = 30.0) -> dict:
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
        while True:
            obj = self._read_obj()
            if "return" in obj or "error" in obj:
                return obj

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def lit_percent(ppm: Path) -> float:
    """Percentage of non-black pixels in a P6 PPM screendump."""
    data = ppm.read_bytes()
    # P6\n<w> <h>\n255\n<rgb...>
    idx = data.index(b"255\n") + 4
    px = data[idx:]
    lit = sum(1 for i in range(0, len(px) - 2, 3)
              if px[i] or px[i + 1] or px[i + 2])
    total = len(px) // 3
    return 100.0 * lit / total if total else 0.0


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the launcher AND the emulator it started.

    Killing only `proc` is not enough: the bundle's entry point is a shell
    script that runs qemu as a child and waits, so the emulator outlives the
    signal and keeps its per-launch NAND clone (~300 MB) alive. Paired with
    start_new_session at spawn, killing the process GROUP takes both.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def bundle_command(app: Path, sock: str) -> list[str]:
    """Launch the bundle through its own launcher, so the epoch file, NAND
    staging and firmware paths are resolved exactly as for a real user."""
    launcher = app / "Contents" / "MacOS" / "iPod Touch"
    if not launcher.exists():
        raise SystemExit(f"no launcher in {app}")
    return [str(launcher), "-display", "none",
            "-qmp", f"unix:{sock},server,nowait"]


def repo_command(args, sock: str, stage: Path) -> list[str]:
    """Launch the repo build directly against the staged artifacts."""
    if args.board == "n45ap":
        machine = (f"iPod-Touch,bootrom={m68ap_paths.BOOTROM},"
                   f"iboot={IPOD_FILES / 'iboot_204_n45ap.bin'},"
                   f"nand={stage / 'nand'}")
        nor = stage / "nor.bin"
        shutil.copy2(IPOD_FILES / "nor_n45ap.bin", nor)
        subprocess.run(["cp", "-Rc", str(IPOD_FILES / "nand"),
                        str(stage / "nand")], check=True)
    else:
        paths = m68ap_paths.get(args.build)
        paths.require("iboot_sb", "nor", "nand")
        machine = (f"iPhone-2G,bootrom={m68ap_paths.BOOTROM},"
                   f"iboot={paths.iboot_sb},nand={stage / 'nand'},"
                   f"epoch={paths.epoch}")
        nor = stage / "nor.bin"
        shutil.copy2(paths.nor, nor)
        subprocess.run(["cp", "-Rc", str(paths.nand), str(stage / "nand")],
                       check=True)
        for bank in range(8):
            (stage / "nand" / f"bank{bank}").mkdir(exist_ok=True)
    return [str(args.qemu), "-M", machine, "-m", "1G", "-pflash", str(nor),
            "-L", str(IPOD_APP / "Contents" / "Resources" / "pc-bios"),
            "-display", "none", "-qmp", f"unix:{sock},server,nowait"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--app", type=Path,
                     help="installed .app bundle to test (preferred)")
    src.add_argument("--board", choices=("m68ap", "n45ap"),
                     help="test the repo build for this board instead")
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    m68ap_paths.add_build_argument(ap, required=False)
    ap.add_argument("--first-sample", type=float, default=6.0,
                    help="seconds before the first frame (default 6)")
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"minimum lit %% to pass (default {DEFAULT_THRESHOLD}; "
                         f"logo is ~{LOGO_PCT}, black ~{BLACK_PCT})")
    ap.add_argument("--save-frames", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.board == "m68ap" and not args.build:
        ap.error("--board m68ap needs --build")

    work = Path(tempfile.mkdtemp(prefix="verify-boot-logo-"))
    # AF_UNIX sun_path is ~104 bytes on macOS; keep the socket short.
    sock = f"/tmp/vbl-{os.getpid()}.sock"
    if os.path.exists(sock):
        os.unlink(sock)

    try:
        if args.app:
            cmd = bundle_command(args.app, sock)
            label = str(args.app)
        else:
            cmd = repo_command(args, sock, work)
            label = (f"repo {args.board}"
                     + (f" {args.build}" if args.build else ""))

        # start_new_session puts the launcher in its own process group so the
        # whole tree can be killed together. The bundle entry point is a shell
        # script that RUNS qemu rather than exec'ing it, so killing just the
        # child we spawned leaves an orphaned emulator holding a ~300 MB NAND
        # clone -- one per run, until the volume fills. (It did.)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=(work / "stderr.log").open("wb"),
                                start_new_session=True)
        samples = []
        try:
            time.sleep(args.first_sample)
            qmp = QMP(sock)
            t0 = time.time()
            for i in range(args.samples):
                if i:
                    time.sleep(args.interval)
                ppm = work / f"frame{i:02d}.ppm"
                qmp.execute("screendump", filename=str(ppm))
                pct = lit_percent(ppm)
                samples.append({"t": round(args.first_sample + time.time() - t0, 1),
                                "lit_pct": round(pct, 3)})
                print(f"  t={samples[-1]['t']:5.1f}s  lit {pct:7.3f}%",
                      flush=True)
            qmp.close()
        finally:
            kill_tree(proc)

        worst = min(s["lit_pct"] for s in samples) if samples else -1.0
        ok = bool(samples) and worst >= args.threshold
        result = {"target": label, "samples": samples,
                  "worst_lit_pct": worst, "threshold": args.threshold,
                  "pass": ok}

        if args.save_frames:
            args.save_frames.mkdir(parents=True, exist_ok=True)
            for p in sorted(work.glob("frame*.ppm")):
                shutil.copy2(p, args.save_frames / p.name)

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"\n{label}: worst {worst:.3f}% vs threshold "
                  f"{args.threshold}% -> {'PASS' if ok else 'FAIL'}")
            if not ok:
                print("\nA black early boot means one of the two fixes "
                      "regressed:\n"
                      "  1. the NOR walk stride -- check it statically with\n"
                      "     scripts/nor-image-store.py <nor.bin> --check "
                      "--expect 7\n"
                      "  2. the LCD scanning out window 2 during iBoot --\n"
                      "     lcd_scanout_base() in hw/arm/ipod_touch_lcd.c\n"
                      "Confirm which with IT_FB_TRACE=1 (every LCD MMIO "
                      "access);\n"
                      "IT_LCD_TRACE only fires on base CHANGES and will stay "
                      "silent.")
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(sock):
            os.unlink(sock)


if __name__ == "__main__":
    sys.exit(main())
