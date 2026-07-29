#!/usr/bin/env python3
"""Dump a 32-bit ARM Mach-O's LC_SYMTAB.

`nm` returns NOTHING for iPhone OS 1.x binaries -- it cannot parse this old ARM
Mach-O -- which made them look stripped.  They are not.  GraphicsServices, for
one, carries 360 defined symbols, and since the OS prebinds its dylibs and has
no ASLR, the link-time addresses in the symbol table ARE the runtime addresses.

Usage:
  scripts/macho-symbols.py <binary> [--grep RE] [--defined-only]
  scripts/macho-symbols.py --build 1A543a --lib GraphicsServices --grep 'GSEvent|Purple|Port'
"""
from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

LC_SYMTAB = 0x02
MH_MAGIC = 0xFEEDFACE
MH_CIGAM = 0xCEFAEDFE

# N_TYPE mask bits of nlist.n_type
N_STAB, N_TYPE, N_SECT = 0xE0, 0x0E, 0x0E


def symbols(data: bytes):
    """[(address, name, is_defined)] from LC_SYMTAB of a 32-bit Mach-O."""
    magic, = struct.unpack_from("<I", data, 0)
    if magic == MH_CIGAM:
        end = ">"
    elif magic == MH_MAGIC:
        end = "<"
    else:
        raise ValueError(f"not a 32-bit Mach-O (magic {magic:#x})")
    ncmds, = struct.unpack_from(end + "I", data, 16)
    off = 28
    out = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from(end + "II", data, off)
        if cmd == LC_SYMTAB:
            symoff, nsyms, stroff, strsize = struct.unpack_from(
                end + "IIII", data, off + 8)
            for i in range(nsyms):
                o = symoff + i * 12
                if o + 12 > len(data):
                    break
                strx, ntype, _nsect, _desc, value = struct.unpack_from(
                    end + "IBBHI", data, o)
                if ntype & N_STAB:          # debug entry, not a real symbol
                    continue
                s = stroff + strx
                if not (stroff <= s < stroff + strsize):
                    continue
                e = data.find(b"\0", s)
                name = data[s:e].decode("latin1")
                if not name:
                    continue
                out.append((value, name, (ntype & N_TYPE) == N_SECT))
        off += cmdsize
    return out


LIB_DIRS = ["System/Library/Frameworks", "System/Library/PrivateFrameworks",
            "usr/lib"]


def find_lib(mnt: Path, name: str) -> Path | None:
    for d in LIB_DIRS:
        for p in (mnt / d).rglob(name):
            if p.is_file() and not p.is_symlink():
                return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", nargs="?", type=Path)
    ap.add_argument("--build", help="1A543a | 4A102 -- mount that root.img")
    ap.add_argument("--lib", default="GraphicsServices")
    ap.add_argument("--grep", help="regex over symbol names")
    ap.add_argument("--defined-only", action="store_true")
    args = ap.parse_args()

    mnt = None
    path = args.binary
    if args.build:
        img = REPO / f"m68ap-artifacts/builds/{args.build}/root.img"
        mnt = Path(f"/tmp/machosym-{args.build}")
        mnt.mkdir(parents=True, exist_ok=True)
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                        "-mountpoint", str(mnt), str(img)], capture_output=True)
        path = find_lib(mnt, args.lib)
        if not path:
            print(f"{args.lib} not found under {img}", file=sys.stderr)
            return 2
    if not path:
        ap.error("give a binary or --build")

    try:
        syms = symbols(Path(path).read_bytes())
    finally:
        if mnt:
            subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    rx = re.compile(args.grep) if args.grep else None
    n = 0
    for value, name, defined in sorted(syms):
        if args.defined_only and not defined:
            continue
        if rx and not rx.search(name):
            continue
        print(f"{value:#010x}  {'D' if defined else 'U'}  {name}")
        n += 1
    print(f"-- {n} shown, {len(syms)} total", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
