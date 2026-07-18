#!/usr/bin/env python3
"""iPod Touch 1G sleep/wake acceptance test.

Runs the full user-visible button matrix against a packaged or development
engine, in one QEMU boot, and reports timings with a PASS/FAIL verdict:

  1. Cold boot to SpringBoard and touch readiness, then a 60 Hz drag.
  2. Lock phase: Power, then Home two seconds later. The device is still
     awake with the panel dark; Home must relight the display immediately
     (no reboot, no queued wake) and touch must work right away.
  3. Late press: Power, then Home ~18.5 s later, near the deep-sleep
     commit. The device must end up awake either way the race resolves.
  4. Untouched sleep: Power, wait for the guest's own OOCSHDWN, the
     pre-warmed wake boot, and the RUN_STATE_SUSPENDED park. Home must
     complete the retained wake in about a second, and a drag must work.
  5. A second sleep/park/wake cycle.
  6. With --timed, a separate boot is left untouched until the guest's
     idle timer sleeps it (~80 s), then woken from the park.

The serial and stderr logs are scanned for panics with the known false
positives filtered ("Panic Fail Count", IOPanicPlatform, sdio-crc-panic).

By default the test runs the installed application engine and clones a
disposable NAND from the application resources (APFS clone, deleted
afterwards). Point IPOD_QEMU/IPOD_NAND (or --qemu/--nand) elsewhere to
test a development build; a caller-supplied NAND is never deleted.

Usage:
  python3 scripts/ipod-acceptance-test.py            # main matrix
  python3 scripts/ipod-acceptance-test.py --timed    # also timed sleep
  IPOD_QEMU=/path/to/qemu-system-arm python3 scripts/ipod-acceptance-test.py

The manual lock-phase scenario exists because harnesses that only press
Home after OOCSHDWN missed a swallowed-button bug for a full release; see
SLEEP_WAKE_INVESTIGATION.md Phase 27.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

APP_DEFAULT = Path("/Applications/iPod Touch.app/Contents")

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
parser.add_argument("--timed", action="store_true",
                    help="also run the untouched timed-sleep boot (~3 min)")
parser.add_argument("--port", type=int,
                    default=int(os.environ.get("IPOD_QMP_PORT", "4489")))
parser.add_argument("--logs", type=Path, default=None,
                    help="log directory (default: timestamped in /private/tmp)")
args = parser.parse_args()

APP = args.app
QEMU = Path(args.qemu) if args.qemu else APP / "MacOS/qemu-system-arm"
LOGS = args.logs or Path(tempfile.mkdtemp(prefix="ipod-acceptance-",
                                          dir="/private/tmp"))
LOGS.mkdir(parents=True, exist_ok=True)

qmp_id = 0
qmp_buffer = b""
failures = []


def command(connection, execute, arguments=None):
    global qmp_id, qmp_buffer
    qmp_id += 1
    request = {"execute": execute, "id": qmp_id}
    if arguments is not None:
        request["arguments"] = arguments
    connection.sendall((json.dumps(request) + "\n").encode())
    while True:
        while b"\n" not in qmp_buffer:
            qmp_buffer += connection.recv(65536)
        line, qmp_buffer = qmp_buffer.split(b"\n", 1)
        response = json.loads(line)
        if response.get("id") != qmp_id:
            continue
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("return")


def send_key(connection, qcode):
    for down in (True, False):
        command(connection, "input-send-event", {"events": [{
            "type": "key",
            "data": {"down": down, "key": {"type": "qcode", "data": qcode}},
        }]})
        time.sleep(0.08)


def pointer(connection, x, y, down=None):
    events = [
        {"type": "abs", "data": {"axis": "x", "value": x}},
        {"type": "abs", "data": {"axis": "y", "value": y}},
    ]
    if down is not None:
        events.append({"type": "btn",
                       "data": {"down": down, "button": "left"}})
    command(connection, "input-send-event", {"events": events})


def drag(connection):
    y = int((1.0 - 0.14) * 32768)
    x0 = int(0.18 * 32768)
    x1 = int(0.86 * 32768)
    pointer(connection, x0, y, True)
    for step in range(1, 43):
        pointer(connection, x0 + (x1 - x0) * step // 42, y)
        time.sleep(1 / 60)
    pointer(connection, x1, y, False)


def wait_for(path, marker, start=0, timeout=30):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        data = path.read_bytes() if path.exists() else b""
        offset = data.find(marker, start)
        if offset >= 0:
            return time.monotonic(), offset
        time.sleep(0.025)
    tail = (path.read_bytes()[-3000:].decode(errors="replace")
            if path.exists() else "")
    raise RuntimeError(f"did not observe {marker!r} in {path.name}\n{tail}")


def absent(path, marker, start=0):
    data = path.read_bytes() if path.exists() else b""
    return data.find(marker, start) < 0


def screendump_brightness(connection, path):
    command(connection, "human-monitor-command",
            {"command-line": f"screendump {path}"})
    time.sleep(0.3)
    parts = path.read_bytes().split(b"\n", 3)
    sample = parts[3][::997]
    return max(sample) if sample else 0


def check(name, condition, detail=""):
    verdict = "PASS" if condition else "FAIL"
    print(f"{verdict}  {name}{'  (' + detail + ')' if detail else ''}",
          flush=True)
    if not condition:
        failures.append(name)


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


class Engine:
    def __init__(self, label, nand):
        self.serial = LOGS / f"{label}-serial.log"
        self.stderr = LOGS / f"{label}-stderr.log"
        self.label = label
        self.stderr_handle = self.stderr.open("wb")
        self.process = subprocess.Popen([
            str(QEMU),
            "-M", (
                "iPod-Touch,"
                f"bootrom={APP / 'Resources/ipod_files/bootrom_s5l8900'},"
                f"iboot={APP / 'Resources/ipod_files/iboot_204_n45ap.bin'},"
                f"nand={nand}"
            ),
            "-m", "1G",
            "-pflash", str(APP / "Resources/ipod_files/nor_n45ap.bin"),
            "-L", str(APP / "Resources/pc-bios"),
            "-display", "sdl,gl=off",
            "-serial", f"file:{self.serial}",
            "-monitor", "none",
            "-qmp", f"tcp:127.0.0.1:{args.port},server=on,wait=off",
        ], stdout=subprocess.DEVNULL, stderr=self.stderr_handle)
        self.connection = None
        for _ in range(500):
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with {self.process.returncode}")
            try:
                self.connection = socket.create_connection(
                    ("127.0.0.1", args.port))
                self.connection.recv(65536)
                command(self.connection, "qmp_capabilities")
                return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("QMP did not become available")

    def brightness(self, name):
        # HMP screendump treats spaces as argument separators.
        safe = name.replace(" ", "-")
        return screendump_brightness(self.connection,
                                     LOGS / f"{self.label}-{safe}.ppm")

    def close(self):
        if self.connection:
            self.connection.close()
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.stderr_handle.close()


def run_drag(engine, name, timeout=6):
    mark = engine.stderr.stat().st_size
    drag(engine.connection)
    try:
        _, down = wait_for(engine.stderr, b"[TOUCH] mouse DOWN", mark,
                           timeout)
        wait_for(engine.stderr, b"[TOUCH] mouse UP", down, timeout)
        check(name, True)
    except RuntimeError:
        check(name, False, "drag not consumed by the guest")


def sleep_and_park(engine, name):
    mark = engine.stderr.stat().st_size
    send_key(engine.connection, "p")
    wait_for(engine.stderr, b"[PMU] OOCSHDWN=", mark, 40)
    _, _ = wait_for(engine.stderr, b"Pre-warmed wake parked", mark, 25)
    check(f"{name}: guest slept and pre-warm parked", True)
    return mark


def parked_wake(engine, name):
    time.sleep(2)
    mark = engine.stderr.stat().st_size
    pressed = time.monotonic()
    send_key(engine.connection, "h")
    ready, _ = wait_for(engine.stderr, b"Retained touch input ready", mark,
                        20)
    elapsed = ready - pressed
    bright = engine.brightness(f"{name}-wake")
    check(f"{name}: parked wake usable", bright >= 32 and elapsed < 6,
          f"{elapsed:.2f}s, brightness {bright}")


def main_matrix(nand):
    engine = Engine("main", nand)
    try:
        started = time.monotonic()
        wait_for(engine.serial, b"Configuring SpringBoard for N45AP",
                 timeout=120)
        cold_ready, _ = wait_for(engine.stderr, b"[LCD] Touch input ready",
                                 timeout=60)
        print(f"cold_input_ready_seconds {cold_ready - started:.3f}")
        time.sleep(1)
        check("cold boot reaches a lit home screen",
              engine.brightness("cold") >= 32)
        run_drag(engine, "cold 60 Hz drag")

        # Lock phase: Power, Home two seconds later. Still awake; must
        # relight instantly without a reboot or a queued wake.
        mark = engine.stderr.stat().st_size
        send_key(engine.connection, "p")
        time.sleep(2.0)
        locked = engine.brightness("locked")
        send_key(engine.connection, "h")
        time.sleep(4.0)
        relit = engine.brightness("relit")
        check("lock: panel dark after Power", locked < 32,
              f"brightness {locked}")
        check("lock: Home relights immediately, no reboot",
              relit >= 32 and absent(engine.stderr, b"[PMU] OOCSHDWN=", mark)
              and absent(engine.stderr, b"queued during OOCSHDWN", mark),
              f"brightness {relit}")
        run_drag(engine, "lock: touch works right after relight")

        # Late press: Home ~18.5 s after Power, near the sleep commit.
        time.sleep(3)
        mark = engine.stderr.stat().st_size
        send_key(engine.connection, "p")
        time.sleep(18.5)
        send_key(engine.connection, "h")
        deadline = time.monotonic() + 15
        outcome = None
        while time.monotonic() < deadline and outcome is None:
            if not absent(engine.stderr, b"Retained touch input ready",
                          mark):
                outcome = "woke via retained path"
            elif absent(engine.stderr, b"[PMU] OOCSHDWN=", mark) and \
                    engine.brightness("late") >= 32:
                outcome = "relit before commit"
            else:
                time.sleep(0.5)
        check("late press near sleep commit ends awake", outcome is not None,
              outcome or "still dark")

        # Two untouched sleep/park/wake cycles.
        time.sleep(4)
        sleep_and_park(engine, "cycle 1")
        parked_wake(engine, "cycle 1")
        run_drag(engine, "cycle 1: post-wake drag")
        time.sleep(2)
        sleep_and_park(engine, "cycle 2")
        parked_wake(engine, "cycle 2")
        run_drag(engine, "cycle 2: post-wake drag")

        hits = scan_for_panics(engine.serial, engine.stderr)
        check("no panic, data abort, or assertion", not hits,
              "; ".join(hits[:3]))
    finally:
        engine.close()


def timed_matrix(nand):
    engine = Engine("timed", nand)
    try:
        wait_for(engine.serial, b"Configuring SpringBoard for N45AP",
                 timeout=120)
        wait_for(engine.stderr, b"[LCD] Touch input ready", timeout=60)
        mark = engine.stderr.stat().st_size
        wait_for(engine.stderr, b"[PMU] OOCSHDWN=", mark, 180)
        wait_for(engine.stderr, b"Pre-warmed wake parked", mark, 25)
        check("timed sleep: guest idle-slept and parked", True)
        parked_wake(engine, "timed")
        hits = scan_for_panics(engine.serial, engine.stderr)
        check("timed sleep: no panic", not hits, "; ".join(hits[:3]))
    finally:
        engine.close()


print(f"engine: {QEMU}")
print(f"logs:   {LOGS}")
temp_nand = None
if args.nand:
    nand = Path(args.nand)
else:
    temp_nand = Path(tempfile.mkdtemp(prefix="ipod-acceptance-nand-",
                                      dir="/private/tmp")) / "nand"
    print("cloning disposable NAND from the application resources...",
          flush=True)
    subprocess.run(["cp", "-Rc", str(APP / "Resources/ipod_files/nand"),
                    str(temp_nand)], check=True)
    nand = temp_nand

try:
    main_matrix(nand)
    if args.timed:
        timed_matrix(nand)
finally:
    if temp_nand is not None:
        shutil.rmtree(temp_nand.parent, ignore_errors=True)

if failures:
    print(f"\nRESULT: FAIL ({len(failures)} failing checks)")
    sys.exit(1)
print("\nRESULT: PASS")
