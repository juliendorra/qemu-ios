#!/usr/bin/env python3
"""iPhone-2G (M68AP) machine smoke test.

Boots the `iPhone-2G` machine with the n45ap (iPod Touch 1G) firmware
images -- no real m68ap dumps exist in this repo -- and verifies the
board-id divergence points behave as documented in IPHONE_2G.md.
It passes `epoch=2` because the board's default SYSIC epoch is now the
real M68AP value (3), which n45ap iBoot rejects in miu_init:

  1. The full Darwin kernel boots (not just iBoot) with no panic.
  2. AppleISL29003 matches and starts against the ALS stub.
  3. The multitouch controller runs in Zephyr1 mode, so the n45ap
     Zephyr2 driver reports "Could not detect HBPP" (expected mismatch;
     real m68ap firmware would load the Z1 driver instead).
  4. `-M help` still lists both machines.

Every wait carries its own timeout and the whole script sits under a
SIGALRM watchdog that kills QEMU and exits, so this test can never hang
a caller (the reason it exists: an untimed boot wait once wedged a
session for two hours).

Usage:
  python3 scripts/iphone-smoke-test.py
  IPOD_QEMU=build/qemu-system-arm python3 scripts/iphone-smoke-test.py
"""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

APP_DEFAULT = Path("/Applications/iPod Touch.app/Contents")
WATCHDOG_SECONDS = 420

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--app", type=Path,
                    default=Path(os.environ.get("IPOD_APP", APP_DEFAULT)),
                    help="application Contents directory (firmware source)")
parser.add_argument("--qemu", type=Path,
                    default=os.environ.get("IPOD_QEMU"),
                    help="engine binary (default: the installed app engine)")
parser.add_argument("--nand", type=Path,
                    default=os.environ.get("IPOD_NAND"),
                    help="NAND directory (default: fresh disposable clone)")
parser.add_argument("--logs", type=Path, default=None,
                    help="log directory (default: timestamped in /private/tmp)")
args = parser.parse_args()

APP = args.app
QEMU = Path(args.qemu) if args.qemu else APP / "MacOS/qemu-system-arm"
LOGS = args.logs or Path(tempfile.mkdtemp(prefix="iphone-smoke-",
                                          dir="/private/tmp"))
LOGS.mkdir(parents=True, exist_ok=True)

failures = []
process = None


def watchdog(signum, frame):
    print(f"FAIL  watchdog: still running after {WATCHDOG_SECONDS}s, "
          "killing QEMU", flush=True)
    if process and process.poll() is None:
        process.kill()
    sys.exit(2)


signal.signal(signal.SIGALRM, watchdog)
signal.alarm(WATCHDOG_SECONDS)


def check(name, condition, detail=""):
    verdict = "PASS" if condition else "FAIL"
    print(f"{verdict}  {name}{'  (' + detail + ')' if detail else ''}",
          flush=True)
    if not condition:
        failures.append(name)


def wait_for(path, marker, timeout=30):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        data = path.read_bytes() if path.exists() else b""
        if data.find(marker) >= 0:
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def scan_for_panics(*paths):
    benign = ("panic fail count", "iopanicplatform", "sdio-crc-panic",
              "panic-")
    hits = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            lowered = line.lower()
            if any(t in lowered for t in ("panic", "data abort",
                                          "assertion failed")):
                if not any(b in lowered for b in benign):
                    hits.append(line.strip())
    return hits


# both machine types must stay registered
listing = subprocess.run([str(QEMU), "-M", "help"], capture_output=True,
                         text=True, timeout=30).stdout
check("machine list has iPhone-2G", "iPhone-2G" in listing)
check("machine list has iPod-Touch", "iPod-Touch" in listing)

nand = args.nand
temp_nand = None
if nand is None:
    temp_nand = Path(tempfile.mkdtemp(prefix="iphone-smoke-nand-")) / "nand"
    print("cloning disposable NAND from the application resources...",
          flush=True)
    subprocess.run(["cp", "-Rc", str(APP / "Resources/ipod_files/nand"),
                    str(temp_nand)], check=True)
    nand = temp_nand

serial = LOGS / "iphone-serial.log"
stderr = LOGS / "iphone-stderr.log"
stderr_handle = stderr.open("wb")
process = subprocess.Popen([
    str(QEMU),
    # epoch=2: the iPhone-2G board now reports the real M68AP SYSIC epoch (3)
    # by default, which makes n45ap iBoot panic in miu_init and reset-loop via
    # the (now functional) watchdog. Overriding the epoch keeps this synthetic
    # n45ap-firmware-on-M68AP boot alive for the divergence checks below.
    "-M", (
        "iPhone-2G,epoch=2,"
        f"bootrom={APP / 'Resources/ipod_files/bootrom_s5l8900'},"
        f"iboot={APP / 'Resources/ipod_files/iboot_204_n45ap.bin'},"
        f"nand={nand}"
    ),
    "-m", "1G",
    "-pflash", str(APP / "Resources/ipod_files/nor_n45ap.bin"),
    "-L", str(APP / "Resources/pc-bios"),
    "-display", "none",
    "-serial", f"file:{serial}",
    "-monitor", "none",
], stdout=subprocess.DEVNULL, stderr=stderr_handle)

try:
    check("Darwin kernel boots",
          wait_for(serial, b"Darwin Kernel Version", 180))
    check("AppleISL29003 probes the ALS stub",
          wait_for(serial, b"AppleISL29003", 60))
    check("multitouch is in Zephyr1 mode (n45ap Z2 driver mismatches)",
          wait_for(serial, b"Could not detect HBPP", 60),
          "expected with n45ap firmware; drop this check for real m68ap dumps")
    # let the boot settle a little so late panics land in the logs
    time.sleep(5)
    hits = scan_for_panics(serial, stderr)
    check("no panics in serial/stderr", not hits,
          "; ".join(hits[:3]) if hits else "")
finally:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    stderr_handle.close()
    if temp_nand is not None:
        shutil.rmtree(temp_nand.parent, ignore_errors=True)

print(f"logs: {LOGS}", flush=True)
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}",
          flush=True)
    sys.exit(1)
print("all checks passed", flush=True)
