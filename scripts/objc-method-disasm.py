#!/usr/bin/env python3
"""Disassemble one old-ABI ObjC method and name every selector it sends.

These binaries are 32-bit ARM Mach-O from iPhone OS 1.x. `otool -tV` will
disassemble them but shows only raw literal-pool loads, so an
`objc_msgSend` reads as `bl 0x85478` with no hint of WHICH message. The
old ABI makes that recoverable statically:

    ldr r1, [pc, #N]      ; N resolves to a word in __OBJC __message_refs
    ldr r1, [r1]          ; that word points at the selector cstring
    bl  _objc_msgSend

so following two pointers turns each call site into a name. Class references
(`__cls_refs`) resolve the same way and give the receiver.

Usage:
  scripts/objc-method-disasm.py <binary> 0x6bd8            # to the next `pop {..pc}`
  scripts/objc-method-disasm.py <binary> 0x6bd8 --end 0x6cd0
"""
from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path


def sections(binary: Path):
    """[(vmaddr, size, fileoff, name)] for every section, for addr->file."""
    out = subprocess.run(["otool", "-l", str(binary)],
                         capture_output=True, text=True).stdout
    secs, cur = [], {}
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"sectname (\S+)", line)
        if m:
            cur = {"name": m.group(1)}
        for k in ("addr", "size", "offset"):
            mm = re.match(rf"{k} (\S+)", line)
            if mm and cur.get("name"):
                cur[k] = int(mm.group(1), 0)
                if k == "offset":
                    secs.append(dict(cur))
    return secs


class Image:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.d = self.path.read_bytes()
        self.secs = sections(self.path)

    def off(self, va):
        for s in self.secs:
            if s.get("addr", 0) <= va < s.get("addr", 0) + s.get("size", 0):
                return va - s["addr"] + s["offset"]
        return None

    def word(self, va):
        o = self.off(va)
        if o is None or o + 4 > len(self.d):
            return None
        return struct.unpack_from("<I", self.d, o)[0]

    def cstr(self, va):
        o = self.off(va)
        if o is None:
            return None
        e = self.d.find(b"\0", o)
        s = self.d[o:e]
        try:
            t = s.decode("ascii")
        except UnicodeDecodeError:
            return None
        return t if t and all(32 <= c < 127 for c in s) else None

    def deref_name(self, va):
        """__message_refs / __cls_refs entry -> the name it points at."""
        p = self.word(va)
        return self.cstr(p) if p else None


LDR_PC = re.compile(r"ldr\s+(\w+), \[pc, #(0x[0-9a-f]+|\d+)\]")


def disasm(img: Image, start: int, end: int | None):
    out = subprocess.run(["otool", "-tV", str(img.path)],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"^([0-9a-f]{8})\t([0-9a-f]{8})\t(.*)$", line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        if addr < start:
            continue
        if end is not None and addr >= end:
            break
        rows.append((addr, m.group(2), m.group(3).strip()))
        if end is None and re.search(r"^pop\s+\{.*pc\}", m.group(3).strip()):
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", type=Path)
    ap.add_argument("start", type=lambda s: int(s, 0))
    ap.add_argument("--end", type=lambda s: int(s, 0), default=None)
    args = ap.parse_args()

    img = Image(args.binary)
    rows = disasm(img, args.start, args.end)
    # A register holds a name once an `ldr rN,[pc,#..]` gave it one; the
    # following `ldr rN,[rN]` keeps it, and objc_msgSend consumes r0/r1.
    named: dict[str, str] = {}
    for addr, _raw, text in rows:
        note = ""
        m = LDR_PC.search(text)
        if m:
            reg, imm = m.group(1), int(m.group(2), 0)
            lit = img.word(addr + 8 + imm)
            if lit is not None:
                nm = img.deref_name(lit) or img.cstr(lit)
                if nm:
                    named[reg] = nm
                    note = f"      ; {reg} = &\"{nm}\""
        mm = re.match(r"ldr\s+(\w+), \[(\w+)\]$", text)
        if mm and mm.group(2) in named:
            named[mm.group(1)] = named[mm.group(2)]
            note = f"      ; {mm.group(1)} = \"{named[mm.group(1)]}\""
        if "_objc_msgSend" in text:
            note = (f"      ; [{named.get('r0', 'r0')} "
                    f"{named.get('r1', '?sel')}]")
        print(f"{addr:08x}  {text}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
