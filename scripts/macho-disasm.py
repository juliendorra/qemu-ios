#!/usr/bin/env python3
"""Disassemble a named symbol out of a 32-bit ARM Mach-O.

Written for the iPhone OS 1.x userland, where the interesting behaviour is not
in this tree at all -- it is in Apple's binaries, and several of them still ship
full symbols.  That has now settled three questions that guesswork could not:

* `0x46` was a retry counter, not an opcode (`AppleMultitouchSPI.kext`, 1A543a);
* iPhone OS 1.0 reads a frame with two COMMAND-LESS transfers;
* the deliberate upward finger projection is `SBFingerProjection`, 3.5
  typographic points, and the sensor->screen scale is applied in
  `MultitouchHID.plugin`, not in the kext (see TOUCH_INVESTIGATION.md).

Why not otool/objdump: the Apple objdump on a modern host prints nothing at all
for these `arm_v6` files, and otool's ARM disassembler is gone.  capstone works
fine; the only real work is the Mach-O walk and resolving the pc-relative
literal pools, which is where every constant of interest lives.

    scripts/macho-disasm.py <binary> --find Sensor          # search symbols
    scripts/macho-disasm.py <binary> --sections
    scripts/macho-disasm.py <binary> _MTHMLiteInit [0] [len]
    scripts/macho-disasm.py <binary> 0x41cf0 arm 0x120      # raw address

`ldr rX,[pc,#N]` operands are resolved and printed with their value and, where
it matches, the nearest symbol -- that is how the 3.5-point default and the
25.4/72 points-to-mm constant were read.  Note the printed `-> symbol` for a
literal is only meaningful when the literal really is an address; for a
pc-RELATIVE offset (the `ldr rX,[pc,#N]` + `add rX,pc,rX` pair that addresses
globals in position-independent code) the target is `literal + addr_of_add + 8`,
which this tool does not fold for you.

Mount an IPSW root filesystem first, e.g.

    hdiutil attach -readonly -nobrowse -mountpoint /tmp/m68 \
        m68ap-artifacts/builds/4A102/root.img
"""
from __future__ import annotations
import struct, sys, bisect
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN

MH_MAGIC = 0xFEEDFACE
LC_SEGMENT, LC_SYMTAB = 0x1, 0x2
N_ARM_THUMB_DEF = 0x0008


class Macho:
    def __init__(self, data: bytes):
        self.d = data
        assert struct.unpack_from("<I", data)[0] == MH_MAGIC, "not a 32-bit Mach-O"
        ncmds = struct.unpack_from("<I", data, 16)[0]
        off = 28
        self.sections = []          # (name, addr, size, fileoff)
        self.segments = []          # (name, vmaddr, vmsize, fileoff)
        self.syms = []              # (addr, name, thumb)
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<II", data, off)
            if cmd == LC_SEGMENT:
                segname = data[off + 8:off + 24].split(b"\0")[0].decode()
                vmaddr, vmsize, fo, fsize = struct.unpack_from("<IIII", data, off + 24)
                nsects = struct.unpack_from("<I", data, off + 48)[0]
                self.segments.append((segname, vmaddr, vmsize, fo))
                so = off + 56
                for _ in range(nsects):
                    sn = data[so:so + 16].split(b"\0")[0].decode()
                    saddr, ssize, soff = struct.unpack_from("<III", data, so + 32)
                    self.sections.append((f"{segname},{sn}", saddr, ssize, soff))
                    so += 68
            elif cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", data, off + 8)
                strs = data[stroff:stroff + strsize]
                for i in range(nsyms):
                    n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from(
                        "<IBBHI", data, symoff + i * 12)
                    if n_strx == 0 or n_value == 0:
                        continue
                    name = strs[n_strx:strs.index(b"\0", n_strx)].decode(errors="replace")
                    self.syms.append((n_value, name, bool(n_desc & N_ARM_THUMB_DEF)))
            off += size
        self.syms.sort()
        self._addrs = [s[0] for s in self.syms]

    def off_for(self, addr):
        for _, saddr, ssize, soff in self.sections:
            if saddr <= addr < saddr + ssize:
                return soff + (addr - saddr)
        return None

    def read(self, addr, n):
        o = self.off_for(addr)
        return None if o is None else self.d[o:o + n]

    def sym(self, name):
        for a, n, t in self.syms:
            if n == name or n == "_" + name:
                return a, t
        return None, None

    def nearest(self, addr):
        i = bisect.bisect_right(self._addrs, addr) - 1
        if i < 0:
            return None
        a, n, _ = self.syms[i]
        return f"{n}+{addr - a:#x}" if addr != a else n

    def find(self, pattern):
        return [(a, n, t) for a, n, t in self.syms if pattern.lower() in n.lower()]


def size_of(m: Macho, addr):
    i = bisect.bisect_right(m._addrs, addr)
    while i < len(m.syms) and m.syms[i][0] == addr:
        i += 1
    return (m.syms[i][0] - addr) if i < len(m.syms) else 0x400


def disasm(m: Macho, addr, thumb, n=None, limit=400):
    n = n or size_of(m, addr)
    code = m.read(addr & ~1, n)
    md = Cs(CS_ARCH_ARM, (CS_MODE_THUMB if thumb else CS_MODE_ARM) | CS_MODE_LITTLE_ENDIAN)
    md.detail = False
    out = []
    for ins in md.disasm(code, addr & ~1):
        line = f"{ins.address:#010x}  {ins.mnemonic:<8} {ins.op_str}"
        # resolve pc-relative literal loads, which is where the constants live
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:
            try:
                delta = int(ins.op_str.split("#")[-1].rstrip("]!"), 0)
                pc = (ins.address + (4 if thumb else 8)) & ~3
                lit = pc + delta
                v = m.read(lit, 4)
                if v:
                    val = struct.unpack("<I", v)[0]
                    line += f"    ; [{lit:#x}] = {val:#x} ({val})"
                    tgt = m.nearest(val)
                    if tgt:
                        line += f" -> {tgt}"
            except Exception:
                pass
        if ins.mnemonic in ("bl", "blx", "b") and ins.op_str.startswith("#"):
            try:
                t = m.nearest(int(ins.op_str[1:], 0))
                if t:
                    line += f"    ; {t}"
            except Exception:
                pass
        out.append(line)
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    path, what = sys.argv[1], sys.argv[2]
    m = Macho(open(path, "rb").read())
    if what == "--find":
        for a, n, t in m.find(sys.argv[3]):
            print(f"{a:#010x} {'T' if t else 'A'} {n}")
        raise SystemExit(0)
    if what == "--sections":
        for s in m.sections:
            print(s)
        raise SystemExit(0)
    if what.startswith("0x"):
        addr = int(what, 0)
        thumb = len(sys.argv) > 3 and sys.argv[3] == "thumb"
    else:
        addr, thumb = m.sym(what)
        if addr is None:
            raise SystemExit(f"no symbol {what}")
    n = int(sys.argv[4], 0) if len(sys.argv) > 4 else None
    print(f"; {what} at {addr:#x} ({'thumb' if thumb else 'arm'})")
    for line in disasm(m, addr, thumb, n):
        print(line)
