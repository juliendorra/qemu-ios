#!/usr/bin/env python3
"""Boot the engine, let the kernel come up, then pmemsave a large chunk of
guest RAM so we can carve the AppleMRVL868x kext and disassemble readEEPROM.
S5L8900 RAM is based at physical 0x08000000."""
import json, os, shutil, signal, socket, subprocess, tempfile, time
from pathlib import Path

APP = Path("/Applications/iPod Touch.app/Contents")
QEMU = Path(os.environ.get("IPOD_QEMU",
            "/private/tmp/qemu-11-port/build-ipod/qemu-system-arm"))
PORT = int(os.environ.get("IPOD_QMP_PORT", "4492"))
OUT = Path(os.environ.get("DUMP", "/private/tmp/ram-dump.bin"))
BASE = 0x08000000
SIZE = 0x08000000   # 128 MB covers the kernelcache

qmp_id = 0; buf = b""
def cmd(conn, execute, arguments=None):
    global qmp_id, buf
    qmp_id += 1
    req = {"execute": execute, "id": qmp_id}
    if arguments is not None: req["arguments"] = arguments
    conn.sendall((json.dumps(req)+"\n").encode())
    while True:
        while b"\n" not in buf: buf += conn.recv(65536)
        line, buf = buf.split(b"\n", 1)
        r = json.loads(line)
        if r.get("id") != qmp_id: continue
        if "error" in r: raise RuntimeError(r["error"])
        return r.get("return")

def wait_for(path, marker, timeout=180):
    t = time.monotonic()
    while time.monotonic()-t < timeout:
        d = path.read_bytes() if path.exists() else b""
        if marker in d: return
        time.sleep(0.05)
    raise RuntimeError(f"no {marker!r}")

def main():
    nand = Path(tempfile.mkdtemp(prefix="ramdump-nand-", dir="/private/tmp"))/"nand"
    subprocess.run(["cp","-Rc",str(APP/"Resources/ipod_files/nand"),str(nand)],check=True)
    serial = OUT.with_suffix(".serial.log")
    proc = subprocess.Popen([
        str(QEMU),"-M",("iPod-Touch,"
          f"bootrom={APP/'Resources/ipod_files/bootrom_s5l8900'},"
          f"iboot={APP/'Resources/ipod_files/iboot_204_n45ap.bin'},"
          f"nand={nand}"),
        "-m","1G","-pflash",str(APP/"Resources/ipod_files/nor_n45ap.bin"),
        "-L",str(APP/"Resources/pc-bios"),"-display","sdl,gl=off",
        "-serial",f"file:{serial}","-monitor","none",
        "-qmp",f"tcp:127.0.0.1:{PORT},server=on,wait=off",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    conn=None
    try:
        for _ in range(500):
            if proc.poll() is not None: raise RuntimeError("qemu exited")
            try:
                conn=socket.create_connection(("127.0.0.1",PORT)); conn.recv(65536)
                cmd(conn,"qmp_capabilities"); break
            except OSError: time.sleep(0.02)
        print("booting; waiting for AppleMRVL868x probe...", flush=True)
        wait_for(serial, b"Reading EEPROM data", timeout=180)
        time.sleep(2)
        cmd(conn,"stop")
        print("stopped; dumping RAM...", flush=True)
        cmd(conn,"pmemsave",{"val":BASE,"size":SIZE,"filename":str(OUT)})
        print(f"dumped {OUT} ({OUT.stat().st_size} bytes)", flush=True)
    finally:
        if conn:
            try: cmd(conn,"quit")
            except Exception: proc.send_signal(signal.SIGINT)
        try: proc.wait(timeout=10)
        except Exception: proc.kill()
        shutil.rmtree(nand.parent, ignore_errors=True)

if __name__=="__main__": main()
