#!/usr/bin/env python3
"""Stage-0 Wi-Fi harness: boot the dev engine with IPOD_SDIO_TRACE=1,
wait for SpringBoard + settle time, optionally drive taps, then quit and
summarize the [sdio] trace lines from stderr."""
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path("/Applications/iPod Touch.app/Contents")
QEMU = Path(os.environ.get("IPOD_QEMU",
            "/private/tmp/qemu-11-port/build-ipod/qemu-system-arm"))

# Board profile: default iPod-Touch (N45AP). Set S5L8900_PROFILE=iphone-2g to
# target the iPhone-2G (M68AP) firmware tree instead. Mirrors the launcher.
PROFILE = os.environ.get("S5L8900_PROFILE", "ipod-touch")
if PROFILE == "ipod-touch":
    FW_SUBDIR, MACHINE, IBOOT, NOR = ("ipod_files", "iPod-Touch",
                                      "iboot_204_n45ap.bin", "nor_n45ap.bin")
elif PROFILE == "iphone-2g":
    FW_SUBDIR, MACHINE, IBOOT, NOR = ("iphone_files", "iPhone-2G",
                                      "iboot_204_m68ap.bin", "nor_m68ap.bin")
else:
    sys.exit(f"Unsupported S5L8900_PROFILE: {PROFILE} "
             "(expected ipod-touch or iphone-2g)")
FW = APP / "Resources" / FW_SUBDIR
PORT = int(os.environ.get("IPOD_QMP_PORT", "4491"))
QMP_SOCKET = os.environ.get("IPOD_QMP_SOCKET")
QMP_STDIO = os.environ.get("IPOD_QMP_STDIO") == "1"
SETTLE = float(os.environ.get("SETTLE", "45"))
TAP_DELAY = float(os.environ.get("TAP_DELAY", "2"))
TAPS = [tuple(map(int, item.split(",")))
        for item in os.environ.get("TAPS", "").split(";") if item]
ACTIONS = json.loads(os.environ.get("ACTIONS_JSON", "[]"))
INTERACTIVE = os.environ.get("INTERACTIVE") == "1"
LOGS = Path(os.environ.get("LOGS") or tempfile.mkdtemp(
    prefix="sdio-trace-", dir="/private/tmp"))
LOGS.mkdir(parents=True, exist_ok=True)

qmp_id = 0
qmp_buffer = b""


class PipeConnection:
    """Small socket-compatible wrapper for QMP over QEMU stdio."""

    def __init__(self, proc):
        self.proc = proc

    def sendall(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv(self, size):
        return os.read(self.proc.stdout.fileno(), size)

    def close(self):
        pass


def command(conn, execute, arguments=None):
    global qmp_id, qmp_buffer
    qmp_id += 1
    req = {"execute": execute, "id": qmp_id}
    if arguments is not None:
        req["arguments"] = arguments
    conn.sendall((json.dumps(req) + "\n").encode())
    while True:
        while b"\n" not in qmp_buffer:
            chunk = conn.recv(65536)
            if not chunk:
                raise RuntimeError("QMP channel closed")
            qmp_buffer += chunk
        line, qmp_buffer = qmp_buffer.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            print(f"ignoring non-QMP stdout: {line!r}", flush=True)
            continue
        if resp.get("id") != qmp_id:
            continue
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("return")


def wait_for(path, marker, start=0, timeout=120):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        data = path.read_bytes() if path.exists() else b""
        off = data.find(marker, start)
        if off >= 0:
            return off
        time.sleep(0.05)
    raise RuntimeError(f"did not observe {marker!r} in {path}")


def tap(conn, x, y):
    """Tap a 320x480 guest-screen coordinate through QMP."""
    abs_x = round(x * 0x7fff / 319)
    abs_y = round(y * 0x7fff / 479)
    command(conn, "input-send-event", {"events": [
        {"type": "abs", "data": {"axis": "x", "value": abs_x}},
        {"type": "abs", "data": {"axis": "y", "value": abs_y}},
        {"type": "btn", "data": {"button": "left", "down": True}},
    ]})
    time.sleep(0.1)
    command(conn, "input-send-event", {"events": [
        {"type": "btn", "data": {"button": "left", "down": False}},
    ]})


def drag(conn, x1, y1, x2, y2, steps=12):
    """Drag between two guest-screen coordinates through QMP."""
    def move(x, y, down=None):
        events = [
            {"type": "abs", "data": {"axis": "x",
                                     "value": round(x * 0x7fff / 319)}},
            {"type": "abs", "data": {"axis": "y",
                                     "value": round(y * 0x7fff / 479)}},
        ]
        if down is not None:
            events.append({"type": "btn",
                           "data": {"button": "left", "down": down}})
        command(conn, "input-send-event", {"events": events})

    move(x1, y1, True)
    for step in range(1, steps + 1):
        move(x1 + (x2 - x1) * step / steps,
             y1 + (y2 - y1) * step / steps)
        time.sleep(0.02)
    command(conn, "input-send-event", {"events": [
        {"type": "btn", "data": {"button": "left", "down": False}},
    ]})


def press_key(conn, qcode):
    """Press one of the machine's physical-button key bindings."""
    for down in (True, False):
        command(conn, "input-send-event", {"events": [{
            "type": "key",
            "data": {"key": {"type": "qcode", "data": qcode},
                     "down": down},
        }]})
        time.sleep(0.1)


def screendump(conn, index):
    command(conn, "human-monitor-command", {"command-line":
            f"screendump {LOGS}/screen-{index}.ppm"})


def perform_action(conn, action, screen_index):
    if "wait" in action:
        delay = float(action["wait"])
        print(f"wait: {delay}s", flush=True)
        time.sleep(delay)
        return screen_index
    if "tap" in action:
        x, y = action["tap"]
        print(f"tap: ({x}, {y})", flush=True)
        tap(conn, x, y)
    elif "drag" in action:
        x1, y1, x2, y2 = action["drag"]
        print(f"drag: ({x1}, {y1}) -> ({x2}, {y2})", flush=True)
        drag(conn, x1, y1, x2, y2, int(action.get("steps", 12)))
    elif "key" in action:
        print(f"key: {action['key']}", flush=True)
        press_key(conn, action["key"])
    else:
        raise ValueError(f"unknown action: {action!r}")
    screen_index += 1
    time.sleep(float(action.get("after", TAP_DELAY)))
    screendump(conn, screen_index)
    return screen_index


def main():
    persistent_root = os.environ.get("IPOD_TEST_ROOT")
    temp_root = (Path(persistent_root) if persistent_root else
                 Path(tempfile.mkdtemp(prefix="sdio-trace-nand-",
                                       dir="/private/tmp")))
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_nand = temp_root / "nand"
    temp_pflash = temp_root / NOR
    if not temp_nand.exists():
        subprocess.run(["cp", "-Rc", str(FW / "nand"),
                        str(temp_nand)], check=True)
    if not temp_pflash.exists():
        shutil.copy2(FW / NOR, temp_pflash)
    serial = LOGS / "serial.log"
    stderr_path = LOGS / "stderr.log"
    env = dict(os.environ,
               IPOD_SDIO_TRACE=os.environ.get("IPOD_SDIO_TRACE", "1"))
    qmp_endpoint = ("stdio" if QMP_STDIO else
                    f"unix:{QMP_SOCKET},server=on,wait=off" if QMP_SOCKET else
                    f"tcp:127.0.0.1:{PORT},server=on,wait=off")
    stderr_handle = stderr_path.open("wb")
    qemu_args = [
        str(QEMU),
        "-M", (f"{MACHINE},"
               f"bootrom={FW / 'bootrom_s5l8900'},"
               f"iboot={FW / IBOOT},"
               f"nand={temp_nand}"),
        "-m", "1G",
        "-drive", f"if=pflash,format=raw,file={temp_pflash}",
        "-L", str(APP / "Resources/pc-bios"),
        "-display", "sdl,gl=off",
        "-serial", f"file:{serial}",
        "-monitor", "none",
        "-qmp", qmp_endpoint,
    ]
    qemu_args += shlex.split(os.environ.get("EXTRA_QEMU_ARGS", ""))
    proc = subprocess.Popen(qemu_args,
       stdin=subprocess.PIPE if QMP_STDIO else None,
       stdout=subprocess.PIPE if QMP_STDIO else subprocess.DEVNULL,
       stderr=stderr_handle, env=env)
    conn = None
    try:
        for _ in range(500):
            if proc.poll() is not None:
                raise RuntimeError(f"QEMU exited {proc.returncode}")
            try:
                if QMP_STDIO:
                    conn = PipeConnection(proc)
                elif QMP_SOCKET:
                    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    conn.connect(QMP_SOCKET)
                else:
                    conn = socket.create_connection(("127.0.0.1", PORT))
                conn.recv(65536)
                command(conn, "qmp_capabilities")
                break
            except OSError:
                time.sleep(0.02)
        else:
            raise RuntimeError("QMP not available")
        print("booting...", flush=True)
        wait_for(serial, b"Configuring SpringBoard for N45AP", timeout=180)
        print("SpringBoard configured; settling "
              f"{SETTLE}s for driver matching...", flush=True)
        time.sleep(SETTLE)
        screendump(conn, 0)
        screen_index = 0
        for index, (x, y) in enumerate(TAPS, 1):
            print(f"tap {index}: ({x}, {y})", flush=True)
            tap(conn, x, y)
            time.sleep(TAP_DELAY)
            screendump(conn, index)
            screen_index = index
        for action in ACTIONS:
            screen_index = perform_action(conn, action, screen_index)
        while INTERACTIVE:
            print("action JSON (or quit)> ", end="", flush=True)
            line = sys.stdin.readline()
            if not line or line.strip() == "quit":
                break
            screen_index = perform_action(conn, json.loads(line),
                                          screen_index)
        time.sleep(0.5)
    finally:
        if conn:
            try:
                command(conn, "quit")
            except Exception:
                proc.send_signal(signal.SIGINT)
            conn.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr_handle.close()
        if not persistent_root:
            shutil.rmtree(temp_nand.parent, ignore_errors=True)

    lines = [l for l in stderr_path.read_text(errors="replace").splitlines()
             if l.startswith(("[sdio]", "[mv8686]"))]
    print(f"\n{len(lines)} SDIO/card trace lines -> {LOGS}/stderr.log")
    for l in lines[:200]:
        print(l)
    if len(lines) > 200:
        print(f"... {len(lines) - 200} more")


if __name__ == "__main__":
    main()
