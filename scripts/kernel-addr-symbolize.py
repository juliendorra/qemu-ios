#!/usr/bin/env python3
"""Which kext is this kernel address in, and what is the function called?

iPhone OS 1.x kernelcaches prelink their kexts into one Mach-O and describe them
with `kmod_info` structs -- there is no `_PrelinkExecutableLoadAddr` plist, so the
usual modern trick does not apply. This walks the kmod_info list, finds the kext
whose [address, address+size) contains the query, then parses THAT kext's own
Mach-O header (it is a complete Mach-O sitting inside the cache) and its
`LC_SYMTAB` to name the function. 1.0's kexts keep their full C++ symbols, which
is what makes this worth doing.

    struct kmod_info {          /* 32-bit, 168 bytes */
        next, info_version, id, name[64], version[64],
        reference_count, reference_list, address, size, hdr_size, start, stop
    };

Usage:
  scripts/kernel-addr-symbolize.py /tmp/kc10.raw 0xc0336010 0xc0335638
  scripts/kernel-addr-symbolize.py /tmp/kc10.raw 0xc0336010 --disasm 0x40
  scripts/kernel-addr-symbolize.py /tmp/kc10.raw --list
"""
from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

KMOD_SIZE = 168


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def segments(path: Path):
    out = subprocess.run(["otool", "-l", str(path)],
                         capture_output=True, text=True).stdout
    segs, cur = [], None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"segname (\S+)", line)
        if m:
            cur = {"name": m.group(1)}
            continue
        if cur is None:
            continue
        for k in ("vmaddr", "vmsize", "fileoff", "filesize"):
            mm = re.match(rf"{k} (\S+)", line)
            if mm:
                cur[k] = int(mm.group(1), 0)
                if k == "filesize":
                    segs.append(dict(cur))
    return segs


def kmods(data: bytes, prelink):
    """[(name, address, size)] from the kmod_info structs."""
    lo, hi = prelink
    out = []
    for o in range(0, len(data) - KMOD_SIZE, 4):
        info_version, = struct.unpack_from("<i", data, o + 4)
        if info_version != 1:
            continue
        name = data[o + 12:o + 76].split(b"\0")[0]
        if not name.startswith(b"com.apple"):
            continue
        try:
            name.decode("ascii")
        except UnicodeDecodeError:
            continue
        addr, size = struct.unpack_from("<II", data, o + 148)
        if not (lo <= addr < hi) or not (0 < size < 0x200000):
            continue
        out.append((name.decode(), addr, size))
    # de-duplicate, keep the first sighting of each name
    seen, res = set(), []
    for n, a, s in out:
        if n in seen:
            continue
        seen.add(n)
        res.append((n, a, s))
    return sorted(res, key=lambda x: x[1])


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kernelcache", type=Path)
    ap.add_argument("addrs", nargs="*", type=lambda s: int(s, 0))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--disasm", type=lambda s: int(s, 0), default=0,
                    help="also disassemble this many bytes around each address")
    args = ap.parse_args()

    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    data = args.kernelcache.read_bytes()
    segs = segments(args.kernelcache)
    pre = next((s for s in segs if s["name"] == "__PRELINK"), None)
    if not pre:
        print("no __PRELINK segment")
        return 2
    skew = pre["vmaddr"] - pre["fileoff"]
    print(f"__PRELINK {pre['vmaddr']:#x}+{pre['vmsize']:#x}  "
          f"file skew {skew:#x}")

    ks = kmods(data, (pre["vmaddr"], pre["vmaddr"] + pre["vmsize"]))
    print(f"{len(ks)} kexts found via kmod_info")
    if args.list:
        for n, a, s in ks:
            print(f"  {a:#010x}..{a + s:#010x}  {n}")
        return 0

    def v2f(va):
        for s in segs:
            if s.get("vmaddr", 0) <= va < s.get("vmaddr", 0) + s.get("filesize", 0):
                return va - s["vmaddr"] + s["fileoff"]
        return va - skew           # __PRELINK and friends

    for va in args.addrs:
        owner = None
        for n, a, s in ks:
            if a <= va < a + s:
                owner = (n, a, s)
        print(f"\n=== {va:#010x} ===")
        if not owner:
            print("  not inside any kext (kernel proper?)")
            continue
        n, a, s = owner
        print(f"  kext: {n}  ({a:#x}..{a + s:#x}, +{va - a:#x})")
        # The kext is itself a Mach-O at `a`.
        fo = v2f(a)
        blob = data[fo:fo + s]
        try:
            syms = sorted((v, nm) for v, nm, d in msym.symbols(blob) if d and v)
        except Exception as e:
            syms = []
            print(f"  (no parsable symbol table: {e})")
        best = None
        for v, nm in syms:
            if v <= va:
                best = (v, nm)
            else:
                break
        if best:
            print(f"  symbol: {best[1]} + {va - best[0]:#x}   ({best[0]:#x})")
        if args.disasm:
            start = va - args.disasm // 2
            off = v2f(start)
            tmp = Path("/tmp/_kdis.bin")
            tmp.write_bytes(data[off:off + args.disasm])
            r = subprocess.run(
                ["otool", "-arch", "arm", "-t", "-v", "-Q", str(tmp)],
                capture_output=True, text=True)
            print(f"  disassembly around {start:#x} "
                  f"(raw bytes, addresses are file-relative):")
            for line in r.stdout.splitlines()[2:]:
                m = re.match(r"^([0-9a-f]+)\t(.*)$", line.strip())
                if m:
                    print(f"    {start + int(m.group(1), 16):#010x}  {m.group(2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
