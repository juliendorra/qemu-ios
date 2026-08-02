#!/usr/bin/env python3
"""Park the FTL's append cursor past the mapping tables in an existing NAND.

`build-m68ap-nand.py` used to leave two fields of the FTL meta page zero:

    uint16_t FTLCtrlBlock[3];   // +0x312   the blocks the FTL rotates between
    uint32_t FTLCtrlPage;       // +0x318   the append cursor

(names and offsets from openiBoot `plat-s5l8900/includes/s5l8900/ftl.h`, whose
`struct FTLCxt` matches this page field-for-field.)

The FTL saves a new context at `++FTLCtrlPage`. With the cursor at zero the
first save lands at page 1 of the control block -- on top of the 18 mapping
tables the format wrote at pages 1..18 -- while the meta at the block's last
page still points `adwMapTablePtrs` at them. The next COLD boot then reads a
context header where it expects a mapping table: iPhone OS 1.0 wedges in iBoot
with no serial output at all, 1.1.4 gets through FTL init and panics. iPhone OS
1.0 does its first save on the way into sleep, so a device only has to idle
once to become unbootable.

The generator is fixed, but regenerating a NAND means rebuilding the whole
filesystem image. This patches the two fields in place instead, in both the
loose page file and `nand.pack` -- the pack matters because the read-only path
(the default, and what the shipped app uses) reads the pack, not the page file.

    python3 scripts/fix-ftl-ctrl-cursor.py m68ap-artifacts/builds/1A543a/nand

Idempotent: a NAND that already carries a non-zero cursor is left alone.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

BYTES_PER_PAGE = 2048
BYTES_PER_SPARE = 64
PAGE_STRIDE = BYTES_PER_PAGE + BYTES_PER_SPARE

FTL_CXT_SECTION_START = 201
MAX_NUM_OF_MAP_TABLES = 18
FTL_META_PAGE = 25855          # same page number at 4 and 8 active banks
FTL_VERSION = 0x46560000       # versionLower, identifies the meta page

OFF_CTRL_BLOCK = 0x312
OFF_CTRL_PAGE = 0x318

PACK_MAGIC = b"IPODNAND"
PACK_HEADER = 20
PACK_BANKS = 8                 # the pack's vpn is always page * 8 + bank


def patched(page: bytes) -> bytes | None:
    """Return the fixed page, or None if it does not need fixing."""
    if struct.unpack_from("<I", page, BYTES_PER_PAGE - 8)[0] != FTL_VERSION:
        return None
    cursor = struct.unpack_from("<I", page, OFF_CTRL_PAGE)[0]
    if cursor != 0:
        return None
    out = bytearray(page)
    struct.pack_into("<3H", out, OFF_CTRL_BLOCK, FTL_CXT_SECTION_START,
                     FTL_CXT_SECTION_START + 1, FTL_CXT_SECTION_START + 2)
    struct.pack_into("<I", out, OFF_CTRL_PAGE, MAX_NUM_OF_MAP_TABLES)
    return bytes(out)


def fix_loose(nand: Path) -> list[str]:
    done = []
    for bank_dir in sorted(nand.glob("bank*")):
        page_file = bank_dir / f"{FTL_META_PAGE}.page"
        if not page_file.is_file():
            continue
        blob = page_file.read_bytes()
        fixed = patched(blob[:BYTES_PER_PAGE])
        if fixed is None:
            continue
        page_file.write_bytes(fixed + blob[BYTES_PER_PAGE:])
        done.append(f"{bank_dir.name}/{page_file.name}")
    return done


def fix_pack(nand: Path) -> list[str]:
    pack = nand / "nand.pack"
    if not pack.is_file():
        return []
    blob = bytearray(pack.read_bytes())
    if bytes(blob[:8]) != PACK_MAGIC:
        raise SystemExit(f"{pack} is not a NAND pack")
    entry_count = struct.unpack_from("<I", blob, 16)[0]
    entries = PACK_HEADER
    data = entries + entry_count * 4
    done = []
    for bank in range(PACK_BANKS):
        vpn = FTL_META_PAGE * PACK_BANKS + bank
        # entries are sorted; a linear scan over a few hundred thousand u32s is
        # not worth a bisect here, but do it anyway since the array IS sorted
        low, high = 0, entry_count
        while low < high:
            mid = (low + high) // 2
            if struct.unpack_from("<I", blob, entries + mid * 4)[0] < vpn:
                low = mid + 1
            else:
                high = mid
        if low >= entry_count:
            continue
        if struct.unpack_from("<I", blob, entries + low * 4)[0] != vpn:
            continue
        off = data + low * PAGE_STRIDE
        fixed = patched(bytes(blob[off:off + BYTES_PER_PAGE]))
        if fixed is None:
            continue
        blob[off:off + BYTES_PER_PAGE] = fixed
        done.append(f"pack vpn {vpn} (bank{bank})")
    if done:
        pack.write_bytes(bytes(blob))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nand", type=Path, nargs="+",
                    help="NAND directory (containing bank0../nand.pack)")
    args = ap.parse_args()

    total = 0
    for nand in args.nand:
        if not nand.is_dir():
            raise SystemExit(f"not a directory: {nand}")
        done = fix_loose(nand) + fix_pack(nand)
        total += len(done)
        if done:
            print(f"{nand}: parked the FTL cursor at page "
                  f"{MAX_NUM_OF_MAP_TABLES} in {len(done)} place(s)")
            for item in done:
                print(f"    {item}")
        else:
            print(f"{nand}: nothing to do (already fixed, or no FTL meta page)")
    return 0 if total else 0


if __name__ == "__main__":
    sys.exit(main())
