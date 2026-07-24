#!/usr/bin/env python3
"""Characterise the observer-free M68AP boot freeze (it stalls at configd).

Boots -M iPhone-2G with no plugin, lets it reach the freeze, then samples the
guest CPU state over time via QMP so we can tell a true deadlock (PC pinned in a
small loop, or the idle/WFI loop) from a slow crawl (PC ranging over code). Also
records serial line growth per sample, so "is anything still printing" is
visible alongside "is the CPU moving".

Example:
    python3 scripts/m68ap-freeze-probe.py \
        --iboot-m68ap m68ap-artifacts/stage/iboot_204_m68ap_sbpatch.bin \
        --nor-m68ap   m68ap-artifacts/stage/nor_m68ap.bin \
        --nand-m68ap  m68ap-artifacts/stage/nand-m68ap-fresh \
        --boot-wait 160 --samples 40 --interval 2 --logs /tmp/m68ap-freeze
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
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
DEFAULT_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"

R15_RE = re.compile(r"R15=([0-9a-fA-F]{8})")
PSR_RE = re.compile(r"PSR=([0-9a-fA-F]{8})")


class QMP:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self._read_obj()
        self.execute("qmp_capabilities")

    def _read_obj(self, timeout: float = 10.0) -> dict:
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def execute(self, cmd: str, **args):
        req = {"execute": cmd}
        if args:
            req["arguments"] = args
        self.sock.sendall((json.dumps(req) + "\n").encode())
        while True:
            obj = self._read_obj()
            if "return" in obj or "error" in obj:
                return obj

    def hmp(self, line: str) -> str:
        r = self.execute("human-monitor-command", **{"command-line": line})
        return r.get("return", "") if isinstance(r, dict) else ""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def serial_lines(path: Path) -> int:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path, default=DEFAULT_BOOTROM)
    ap.add_argument("--iboot-m68ap", type=Path, required=True)
    ap.add_argument("--nor-m68ap", type=Path, required=True)
    ap.add_argument("--nand-m68ap", type=Path, required=True)
    ap.add_argument("--boot-wait", type=int, default=160)
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--logs", type=Path, required=True)
    args = ap.parse_args()

    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    stage.mkdir(exist_ok=True)
    nand = stage / "nand"
    if nand.exists():
        shutil.rmtree(nand)
    subprocess.run(["cp", "-Rc", str(args.nand_m68ap), str(nand)], check=True)
    for b in range(8):
        (nand / f"bank{b}").mkdir(exist_ok=True)
    nor = stage / "nor.bin"
    shutil.copy2(args.nor_m68ap, nor)
    bootrom = stage / "bootrom"
    shutil.copy2(args.bootrom, bootrom)
    iboot = stage / "iboot.bin"
    shutil.copy2(args.iboot_m68ap, iboot)

    sock_path = f"/tmp/m68freeze-{os.getpid()}.sock"
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    serial = args.logs / "serial.log"
    serial.write_bytes(b"")
    machine = f"iPhone-2G,bootrom={bootrom},iboot={iboot},nand={nand}"
    cmd = [str(args.qemu), "-M", machine, "-m", "1G", "-pflash", str(nor),
           "-L", str(APP / "Resources" / "pc-bios"),
           "-display", "none", "-serial", f"file:{serial}",
           "-qmp", f"unix:{sock_path},server,nowait"]
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, stdout=(args.logs / "monitor.log").open("wb"),
                            stderr=(args.logs / "stderr.log").open("wb"))

    result: dict = {"boot_wait_s": args.boot_wait, "samples": []}
    try:
        for _ in range(80):
            if os.path.exists(sock_path):
                break
            time.sleep(0.2)
        print(f"booting M68AP for {args.boot_wait}s to reach the freeze ...",
              flush=True)
        time.sleep(args.boot_wait)
        qmp = QMP(sock_path)
        t0 = time.time()
        for i in range(args.samples):
            regs = qmp.hmp("info registers")
            m15 = R15_RE.search(regs)
            mpsr = PSR_RE.search(regs)
            pc = m15.group(1) if m15 else None
            psr = mpsr.group(1) if mpsr else None
            sl = serial_lines(serial)
            sample = {"i": i, "t": round(time.time() - t0, 1),
                      "pc": pc, "psr": psr, "serial_lines": sl}
            result["samples"].append(sample)
            print(json.dumps(sample), flush=True)
            time.sleep(args.interval)
        qmp.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()

    pcs = [s["pc"] for s in result["samples"] if s["pc"]]
    counts = Counter(pcs)
    result["distinct_pcs"] = len(counts)
    result["top_pcs"] = counts.most_common(10)
    result["serial_grew"] = (result["samples"][-1]["serial_lines"] >
                             result["samples"][0]["serial_lines"]
                             if result["samples"] else False)
    (args.logs / "freeze.json").write_text(json.dumps(result, indent=2) + "\n")
    print("\n=== SUMMARY ===")
    print(f"distinct PCs across {len(pcs)} samples: {result['distinct_pcs']}")
    print(f"top PCs: {result['top_pcs']}")
    print(f"serial grew during sampling: {result['serial_grew']}")
    print(f"report: {args.logs / 'freeze.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
