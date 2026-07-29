#!/usr/bin/env python3
"""Does SpringBoard's menu-button handler actually RUN when HOME is pressed?

The in-app HOME failure on iPhone OS 1.0 has survived every black-box probe:
the interrupt is raised, read and acknowledged exactly as on 1.1.4 and the iPod
(byte-identical SYSIC sequence), nothing is displayed, and the guest returns to
its idle WFI loop. PC sampling cannot answer it -- stopping the vCPU 50x a
second prevents even 1.1.4 from completing the transition -- and the guest logs
nothing from userland.

So ask the binary directly. `SpringBoard` in the 1A543a root filesystem is a
32-bit ARM Mach-O with its ObjC metadata intact, so the implementation address
of `-[SpringBoard menuButtonUp:]` can be resolved from `__OBJC` without symbols
(see `resolve_imp`). Set a HARDWARE-INDEPENDENT gdbstub breakpoint there, press
HOME, and see whether it is reached.

That splits the remaining search space in one run:

  * breakpoint HIT  -> the event reaches SpringBoard, and the handler decides to
    do nothing. Both builds gate on
    `[[SBSyncController sharedInstance] isRestoring | isResetting |
    isSoftwareUpdating]` and on an early-return ivar (1.0 +0x40, 1.1.4 +0x44),
    so read those next.
  * breakpoint MISS -> the event never gets to SpringBoard, and the problem is
    below it, in the kernel's HID posting path.

iPhone OS 1.x has no ASLR, so the link-time address is the runtime address.

Usage:
  scripts/springboard-button-breakpoint.py --board m68ap-10
  scripts/springboard-button-breakpoint.py --board m68ap-114   # control: works
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

ROOTS = {
    "m68ap-10": REPO / "m68ap-artifacts/builds/1A543a/root.img",
    "m68ap-114": REPO / "m68ap-artifacts/builds/4A102/root.img",
}
SB = "System/Library/CoreServices/SpringBoard.app/SpringBoard"


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_imp(binary: Path, selector: bytes):
    """IMP address of an ObjC method, from the old-ABI __OBJC metadata.

    The method list entries are {SEL name; char *types; IMP imp}, and `name`
    holds the ADDRESS of the selector cstring -- so finding the cstring, mapping
    it to a vmaddr, and searching for that word finds the entry.
    """
    d = binary.read_bytes()
    out = subprocess.run(["otool", "-l", str(binary)],
                         capture_output=True, text=True).stdout
    segs, cur = [], None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"segname (\S+)", line)
        if m:
            cur = {"name": m.group(1)}
        for k in ("vmaddr", "vmsize", "fileoff", "filesize"):
            mm = re.match(rf"{k} (\S+)", line)
            if mm and cur and "name" in cur:
                cur[k] = int(mm.group(1), 0)
                if k == "filesize":
                    segs.append(cur.copy())

    def f2v(fo):
        for s in segs:
            if s.get("fileoff", 0) <= fo < s.get("fileoff", 0) + s.get("filesize", 0):
                return fo - s["fileoff"] + s["vmaddr"]
        return None

    fo = d.find(selector + b"\0")
    if fo < 0:
        return None
    va = f2v(fo)
    needle = struct.pack("<I", va)
    i = 0
    while True:
        i = d.find(needle, i)
        if i < 0:
            return None
        _types, imp = struct.unpack_from("<II", d, i + 4)
        if imp and imp < 0x400000:
            return imp
        i += 1


class Gdb:
    """Minimal RSP client: enough to set a breakpoint and wait for a stop."""

    def __init__(self, port, timeout=10):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.s.settimeout(timeout)

    def _send(self, body: str):
        csum = sum(body.encode()) & 0xFF
        self.s.sendall(f"${body}#{csum:02x}".encode())
        try:
            self.s.recv(1)          # '+'
        except socket.timeout:
            pass

    def _recv(self, timeout):
        self.s.settimeout(timeout)
        buf = b""
        try:
            while b"#" not in buf:
                c = self.s.recv(4096)
                if not c:
                    return None
                buf += c
        except socket.timeout:
            return None
        self.s.sendall(b"+")
        m = re.search(rb"\$([^#]*)#", buf)
        return m.group(1).decode("latin1") if m else None

    def set_break(self, addr, kind=4):
        self._send(f"Z0,{addr:x},{kind}")
        return self._recv(5)

    def cont(self):
        self._send("c")

    def wait_stop(self, timeout):
        return self._recv(timeout)

    def regs(self):
        """ARM 'g': r0-r15 (16 x 4 bytes), then FPA regs, then FPS, then CPSR."""
        self._send("g")
        raw = self._recv(10)
        if not raw or len(raw) < 128:
            return None
        w = [int.from_bytes(bytes.fromhex(raw[i * 8:(i + 1) * 8]), "little")
             for i in range(16)]
        cpsr = None
        if len(raw) >= 8 * (16 + 8 * 3 + 1 + 1):
            off = (16 + 8 * 3 + 1) * 8
            cpsr = int.from_bytes(bytes.fromhex(raw[off:off + 8]), "little")
        return w, cpsr

    def mem(self, addr, n):
        self._send(f"m{addr:x},{n:x}")
        raw = self._recv(10)
        if not raw or raw.startswith("E"):
            return None
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None


def library_map(mnt: Path):
    """address -> library name, from the guest's own Mach-O load addresses.

    iPhone OS 1.x prebinds its dylibs at fixed preferred addresses and has no
    ASLR, so a framework's link-time __TEXT vmaddr IS where it lives at runtime.
    That turns a raw caller address into a name without any guest cooperation.
    """
    out = []
    roots = [mnt / "System/Library/Frameworks",
             mnt / "System/Library/PrivateFrameworks",
             mnt / "usr/lib"]
    for r in roots:
        if not r.exists():
            continue
        for f in r.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            try:
                head = f.open("rb").read(4)
            except OSError:
                continue
            if head not in (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
                continue
            try:
                res = subprocess.run(["otool", "-l", str(f)],
                                     capture_output=True, text=True, timeout=20).stdout
            except Exception:
                continue
            seg = None
            for line in res.splitlines():
                line = line.strip()
                if line == "segname __TEXT":
                    seg = {}
                elif seg is not None and line.startswith("vmaddr "):
                    seg["a"] = int(line.split()[1], 0)
                elif seg is not None and line.startswith("vmsize "):
                    seg["s"] = int(line.split()[1], 0)
                    if seg.get("a"):
                        out.append((seg["a"], seg["a"] + seg["s"], f.name))
                    seg = None
    return sorted(out)


def whose(addr, libs):
    for lo, hi, name in libs:
        if lo <= addr < hi:
            return f"{name}+{addr - lo:#x}"
    if addr < 0x100000:
        return f"SpringBoard+{addr:#x}"
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=sorted(ROOTS), required=True)
    ap.add_argument("--logs", type=Path, default=Path("/tmp/sb-break"))
    ap.add_argument("--gdb-port", type=int, default=1234)
    ap.add_argument("--vnc-port", type=int, default=5930)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--wait", type=float, default=90)
    ap.add_argument("--break-addr", action="append", default=[],
                    help="extra address to break on, as NAME=0xADDR (e.g. a "
                         "GraphicsServices routing function). Repeatable.")
    ap.add_argument("--no-app", action="store_true",
                    help="press HOME from the home screen instead of from "
                         "inside an app -- isolates whether the frontmost app "
                         "is what changes the routing")
    args = ap.parse_args()

    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    app, icon = btn.BOARDS[args.board]

    # Resolve the IMPs from the build's own root filesystem.
    mnt = Path(f"/tmp/sb-break-root-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                    "-mountpoint", str(mnt), str(ROOTS[args.board])],
                   capture_output=True)
    binary = args.logs / "SpringBoard"
    args.logs.mkdir(parents=True, exist_ok=True)
    binary.write_bytes((mnt / SB).read_bytes())
    print("building the guest library address map ...")
    libs = library_map(mnt)
    print(f"  {len(libs)} images mapped")
    subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    imps = {sel.decode(): resolve_imp(binary, sel)
            for sel in (b"menuButtonDown:", b"menuButtonUp:")}
    for spec in args.break_addr:
        name, _, addr = spec.partition("=")
        imps[name] = int(addr, 0)
    for k, v in imps.items():
        print(f"  -[SpringBoard {k}] IMP = {v:#x}" if v else f"  {k}: NOT FOUND")
    if not all(imps.values()):
        return 2

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/sb-break-{os.getpid()}.sock"
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}",
           "-gdb", f"tcp::{args.gdb_port}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT)
    client = None
    try:
        client = lock.DisplayClient(args.vnc_port)
        client.start()
        time.sleep(3)
        q = btn.QMP(qmp_path)
        for _ in range(420):
            time.sleep(1)
            if "Touch input ready" in logp.read_bytes().decode("utf8", "replace"):
                break
        else:
            print("FAIL: no home screen; run is INVALID")
            return 1
        print("home screen up")

        dismiss = btn.DISMISS.get(args.board)
        if dismiss:
            print("dismissing the first-launch modal ...")
            btn.tap(q, *dismiss, 0.12)
            time.sleep(args.settle)
        if args.no_app:
            print("NOT opening an app -- pressing HOME from SpringBoard itself")
        else:
            print("opening an app ...")
            btn.tap(q, *icon, 0.12)
            time.sleep(args.settle)

        print(f"attaching gdbstub on :{args.gdb_port} ...")
        g = Gdb(args.gdb_port)
        for name, imp in imps.items():
            print(f"  breakpoint at {imp & ~1:#x} ({name}): {g.set_break(imp & ~1)}")
        g.cont()
        time.sleep(1)

        print("pressing HOME ...")
        btn.key(q, "home")
        stop = g.wait_stop(args.wait)
        if stop:
            print(f"\nBREAKPOINT HIT: {stop}")
            rc_ = g.regs()
            if rc_:
                w, cpsr = rc_
                print(f"  r0(self)={w[0]:#010x} r1(sel)={w[1]:#010x} "
                      f"r2(GSEvent)={w[2]:#010x}")
                print(f"  sp={w[13]:#010x} lr={w[14]:#010x} pc={w[15]:#010x}"
                      + (f" cpsr={cpsr:#010x}" if cpsr else ""))
                # At the first instruction of the IMP, LR is still the caller.
                print(f"\n  CALLER (lr): {w[14]:#010x}  {whose(w[14], libs)}")
                # Walk the r7 frame chain: [r7]=prev r7, [r7+4]=return address.
                fp = w[7]
                print("  frames:")
                for i in range(8):
                    blk = g.mem(fp, 8)
                    if not blk or len(blk) < 8:
                        break
                    prev, ret = struct.unpack("<II", blk)
                    if not ret or ret == 0xFFFFFFFF:
                        break
                    print(f"    #{i} {ret:#010x}  {whose(ret, libs)}")
                    if not prev or prev <= fp:
                        break
                    fp = prev
                report_txt = args.logs / "stack.txt"
                report_txt.write_text(f"lr={w[14]:#x} {whose(w[14], libs)}\n")
            print("=> the event REACHES SpringBoard; the handler decides to do "
                  "nothing.\n   Read the SBSyncController gates and the "
                  "early-return ivar next.")
        else:
            print(f"\nNO HIT in {args.wait}s.")
            print("=> the event never reaches SpringBoard's handler; the "
                  "problem is BELOW it,\n   in the kernel's HID posting path.")
    finally:
        if client:
            client.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
