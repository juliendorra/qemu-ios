#!/usr/bin/env python3
"""Hacktivate the M68AP (iPhone 2G) so SpringBoard renders the home screen.

Why this exists
---------------
A fresh-from-IPSW M68AP boots `[Unactivated]`; iPhone OS 1.x then shows an
activation screen (never the home screen) and SpringBoard never programs its
framebuffer base, so the panel stays black. N45AP (iPod Touch) ships a
pre-activated NAND (devos50: "I copied activation records from an actual
device ... modified the NAND filesystem to bypass various checks").

DEAD END (do not retry): injecting `/var/root/Library/Lockdown/data_ark.plist`
into the `--data-hfs` partition. Two cycles (journaled + fresh non-journaled)
stayed `[Unactivated]`. Ruled out carriage (disk0s2 mounts), path, timing
(/var mounts before lockdownd reads), and journaling. Root cause: macOS's
modern HFS+ writer produces a catalog the 2007-era iOS HFS driver will not
traverse for NEWLY-INSERTED files, so lockdownd's fopen fails.

ROBUST APPROACH (this script): byte-patch `/usr/libexec/lockdownd`
in place -- overwrite bytes inside the EXISTING file, same length, so the
HFS catalog and the file's extents are untouched and the guest reads the
patched bytes. The patch forces lockdownd's activation-state determination
to `Activated`. We locate lockdownd's Mach-O inside the raw root-HFS image by
signature and patch its file bytes directly (never through a macOS mount), so
the HFS-writer incompatibility above cannot bite.

Usage
-----
  # analyse only: find the patch site and print the disassembly
  scripts/hacktivate-m68ap.py analyse --lockdownd <extracted lockdownd>

  # patch a copy of the root HFS image in place
  scripts/hacktivate-m68ap.py patch \
      --root-hfs m68ap-artifacts/stage/filesystem-m68ap-readonly.img \
      --out /tmp/root-hacktivated.img

Then regenerate the NAND with `build-m68ap-nand.py --hfs <out>` and boot.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


# --- Mach-O (32-bit ARM) --------------------------------------------------

class MachO:
    def __init__(self, data: bytes):
        self.d = data
        if data[:4] != bytes.fromhex("cefaedfe"):
            raise ValueError("not a little-endian 32-bit Mach-O (MH_MAGIC)")
        self.sects = []  # (segname, sectname, vmaddr, size, fileoff)
        ncmds = struct.unpack("<I", data[16:20])[0]
        off = 28
        for _ in range(ncmds):
            cmd, cs = struct.unpack("<II", data[off:off + 8])
            if cmd == 1:  # LC_SEGMENT
                nsects = struct.unpack("<I", data[off + 48:off + 52])[0]
                so = off + 56
                for _k in range(nsects):
                    sn = data[so:so + 16].rstrip(b"\0").decode("latin1")
                    seg = data[so + 16:so + 32].rstrip(b"\0").decode("latin1")
                    addr, size, foff = struct.unpack("<III", data[so + 32:so + 44])
                    self.sects.append((seg, sn, addr, size, foff))
                    so += 68
            off += cs

    def vm_to_fo(self, vm):
        for _seg, _sn, addr, size, foff in self.sects:
            if size and addr <= vm < addr + size:
                return foff + (vm - addr)
        return None

    def fo_to_vm(self, fo):
        for _seg, _sn, addr, size, foff in self.sects:
            if size and foff <= fo < foff + size:
                return addr + (fo - foff)
        return None

    def cstr(self, vm):
        fo = self.vm_to_fo(vm)
        if fo is None:
            return None
        end = self.d.index(b"\0", fo)
        s = self.d[fo:end]
        return s.decode("latin1") if s.isascii() else None

    def find_cstring_vm(self, s: str):
        i = self.d.find(s.encode() + b"\0")
        return self.fo_to_vm(i) if i >= 0 else None

    def pool_xrefs(self, target_vm: int):
        """File offsets whose 4 LE bytes equal target_vm (literal-pool refs)."""
        needle = struct.pack("<I", target_vm)
        out = []
        start = 0
        while True:
            i = self.d.find(needle, start)
            if i < 0:
                break
            out.append(i)
            start = i + 1
        return out


# --- the patch ------------------------------------------------------------
#
# Analysis result (see `analyse`/`disasm`): lockdownd's activation-state
# determination is a large, path-dependent function. An empty-record device
# resolves to the CFString constant "Unactivated"; the plain-constant ref
# (0x109cf8) is only reached on the IMEI-mismatch path, which an empty device
# skips ("No IMEI in the activation record"), so a pool redirect there does not
# help. The robust, code-path-independent fix is to RENAME the "Unactivated"
# CFString to "Activated": wherever lockdownd reports the state, SpringBoard
# then reads "Activated" and renders the home screen.
#
# Two same-length raw-byte edits, applied by unique context so they land in the
# right HFS data block regardless of fragmentation (never via a macOS HFS
# mount, which is the write path that failed for data-ark injection):
#   1. the C string  "Unactivated\0"  ->  "Activated\0\0\0"   (12 bytes)
#   2. the CFString constant's length word  11 -> 9, located by the
#      <str-ptr=0x0009d8a0><len=0x0000000b> pair unique to that constant.

PATCH_DESCRIPTION = 'rename lockdownd "Unactivated" activation string -> "Activated"'

PATCHES = [
    # (name, find_bytes, replace_bytes, expected) - expected occurrence count.
    # Signatures carry enough surrounding context to be unique/scoped in the
    # whole root-FS image (the bare "Unactivated\0" occurs 4x across the FS).
    #  - The C string is patched once (unique 8-byte-prefixed signature).
    #  - The length field lives in TWO CFString constants that both point at
    #    the "Unactivated" string; both must become length 9 so SpringBoard's
    #    exact-string compare sees "Activated" (not "Activated\0\0").
    ("activation-string",
     b"tate\x00\x00\x00\x00Unactivated\x00",       # ...brick sTATE\0\0\0\0 + str
     b"tate\x00\x00\x00\x00Activated\x00\x00\x00", 1),
    ("cfstring-length",
     bytes.fromhex("b8f34f38" "c8070000" "a0d80900" "0b000000"),  # CFString const
     bytes.fromhex("b8f34f38" "c8070000" "a0d80900" "09000000"), 2),  # len 11->9
]


def apply_patches(image: bytes) -> bytes:
    buf = bytearray(image)
    for name, find, repl, expected in PATCHES:
        assert len(find) == len(repl), name
        n = image.count(find)
        if n != expected:
            raise SystemExit(
                f"patch {name!r}: expected {expected} occurrence(s), found {n} "
                f"(pattern {find!r})")
        start = 0
        for _ in range(n):
            i = buf.find(find, start)
            buf[i:i + len(repl)] = repl
            print(f"  patched {name}: image offset {i:#x}")
            start = i + len(repl)
    return bytes(buf)


def analyse(lockdownd: bytes):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
    m = MachO(lockdownd)
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    for name in ("Activated", "Unactivated", "The device was factory activated",
                 "Setting the activation state to %s",
                 "determine_activation_state"):
        vm = m.find_cstring_vm(name)
        print(f"string {name!r:45} vm={hex(vm) if vm else None}")
        if vm:
            xr = m.pool_xrefs(vm)
            print("   pool xrefs (file offs):",
                  [hex(x) for x in xr[:8]], "->",
                  [hex(m.fo_to_vm(x)) for x in xr[:8] if m.fo_to_vm(x)])
    return m


def disasm(lockdownd: bytes, start_vm: int, length: int):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
    import re
    m = MachO(lockdownd)
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    fo = m.vm_to_fo(start_vm)
    code = m.d[fo:fo + length]
    for ins in md.disasm(code, start_vm):
        ann = ""
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:
            mt = re.search(r"\[pc,?\s*#(-?0x[0-9a-f]+|-?\d+)\]", ins.op_str)
            if mt:
                pa = (ins.address + 8 + int(mt.group(1), 0)) & 0xffffffff
                pf = m.vm_to_fo(pa)
                if pf is not None:
                    val = struct.unpack("<I", m.d[pf:pf + 4])[0]
                    s = m.cstr(val)
                    ann = f"  ; =0x{val:08x}"
                    if s and s.isprintable() and len(s) >= 2:
                        ann += f' "{s[:38]}"'
        print(f"  {ins.address:08x} {ins.mnemonic:7} {ins.op_str}{ann}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyse", help="locate the activation patch site")
    a.add_argument("--lockdownd", type=Path, required=True)
    dd = sub.add_parser("disasm", help="disassemble a vm range")
    dd.add_argument("--lockdownd", type=Path, required=True)
    dd.add_argument("--vm", type=lambda x: int(x, 0), required=True)
    dd.add_argument("--len", type=lambda x: int(x, 0), default=0x120)
    p = sub.add_parser("patch", help="patch lockdownd in a raw root-HFS image")
    p.add_argument("--root-hfs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if args.cmd == "analyse":
        analyse(args.lockdownd.read_bytes())
        return 0
    if args.cmd == "disasm":
        disasm(args.lockdownd.read_bytes(), args.vm, args.len)
        return 0
    # patch mode: rename the Unactivated activation string in the root HFS
    print(f"hacktivate: {PATCH_DESCRIPTION}")
    image = args.root_hfs.read_bytes()
    patched = apply_patches(image)
    args.out.write_bytes(patched)
    print(f"wrote patched root HFS -> {args.out} ({len(patched)} bytes)")
    print("next: build-m68ap-nand.py --hfs", args.out,
          "--data-hfs <data> --signature m68ap --active-banks 4 --bbt production")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
