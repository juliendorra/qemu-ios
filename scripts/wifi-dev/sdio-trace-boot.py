#!/usr/bin/env python3
"""Stage-0 Wi-Fi harness: boot the dev engine with IPOD_SDIO_TRACE=1,
wait for SpringBoard + settle time, optionally drive taps, then quit and
summarize the [sdio] trace lines from stderr."""
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

APP = Path("/Applications/iPod Touch.app/Contents")
QEMU = Path(os.environ.get("IPOD_QEMU",
            "/private/tmp/qemu-11-port/build-ipod/qemu-system-arm"))
PORT = int(os.environ.get("IPOD_QMP_PORT", "4491"))
SETTLE = float(os.environ.get("SETTLE", "45"))
LOGS = Path(os.environ.get("LOGS") or tempfile.mkdtemp(
    prefix="sdio-trace-", dir="/private/tmp"))
LOGS.mkdir(parents=True, exist_ok=True)

qmp_id = 0
qmp_buffer = b""


def command(conn, execute, arguments=None):
    global qmp_id, qmp_buffer
    qmp_id += 1
    req = {"execute": execute, "id": qmp_id}
    if arguments is not None:
        req["arguments"] = arguments
    conn.sendall((json.dumps(req) + "\n").encode())
    while True:
        while b"\n" not in qmp_buffer:
            qmp_buffer += conn.recv(65536)
        line, qmp_buffer = qmp_buffer.split(b"\n", 1)
        resp = json.loads(line)
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


def main():
    temp_nand = Path(tempfile.mkdtemp(prefix="sdio-trace-nand-",
                                      dir="/private/tmp")) / "nand"
    subprocess.run(["cp", "-Rc", str(APP / "Resources/ipod_files/nand"),
                    str(temp_nand)], check=True)
    serial = LOGS / "serial.log"
    stderr_path = LOGS / "stderr.log"
    env = dict(os.environ, IPOD_SDIO_TRACE="1")
    stderr_handle = stderr_path.open("wb")
    proc = subprocess.Popen([
        str(QEMU),
        "-M", ("iPod-Touch,"
               f"bootrom={APP / 'Resources/ipod_files/bootrom_s5l8900'},"
               f"iboot={APP / 'Resources/ipod_files/iboot_204_n45ap.bin'},"
               f"nand={temp_nand}"),
        "-m", "1G",
        "-pflash", str(APP / "Resources/ipod_files/nor_n45ap.bin"),
        "-L", str(APP / "Resources/pc-bios"),
        "-display", "sdl,gl=off",
        "-serial", f"file:{serial}",
        "-monitor", "none",
        "-qmp", f"tcp:127.0.0.1:{PORT},server=on,wait=off",
    ], stdout=subprocess.DEVNULL, stderr=stderr_handle, env=env)
    conn = None
    try:
        for _ in range(500):
            if proc.poll() is not None:
                raise RuntimeError(f"QEMU exited {proc.returncode}")
            try:
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
        command(conn, "human-monitor-command",
                {"command-line": f"screendump {LOGS}/home.ppm"})
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
        shutil.rmtree(temp_nand.parent, ignore_errors=True)

    lines = [l for l in stderr_path.read_text(errors="replace").splitlines()
             if l.startswith("[sdio]")]
    print(f"\n{len(lines)} [sdio] trace lines -> {LOGS}/stderr.log")
    for l in lines[:200]:
        print(l)
    if len(lines) > 200:
        print(f"... {len(lines) - 200} more")


if __name__ == "__main__":
    main()
