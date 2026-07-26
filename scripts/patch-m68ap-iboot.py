#!/usr/bin/env python3
"""Apply the secure-boot bypass patch to an extracted M68AP iBoot image.

Why this is needed
------------------
iBoot-204.3.14 (iPhone1,1 1.1.4) is a RELEASE build that strictly enforces
image secure boot. Its `image_load` path verifies each NOR IMG2 image: signed
images (flags2 bit 1) are hash-checked, and UNSIGNED images are accepted only if
the security-config word at VA 0x18022fa0 has bit 4 set. That bit is never set
in this build (security_init at 0x18005a28 seeds the config to 0x002c0000 and
sets no path to bit 4), so unsigned images are always rejected.

The synthetic M68AP NOR that this project builds from a user-supplied IPSW
(scripts/build-m68ap-nor.py) carries the authentic Apple images, which are
effectively unsigned in our pipeline (the img2 signature/hash is not
reconstructed). With secure boot enforced, iBoot enumerates the device-tree
`dtre` image but refuses to LOAD it, printing:

    load_macho_image: failed to load device tree

and dropping to recovery. This is the same class of obstacle the bootrom
"pwnage" exploits remove on real hardware, and the same reason emulated legacy
iOS boots run a secure-boot-relaxed iBoot. (The alternative -- reconstructing
Apple's img2 GID signatures so the signed path passes -- is a much larger and
separately licensable effort.)

The patch
---------
One 2-byte Thumb edit at the unsigned-image decision helper (VA 0x18005984 in
4A102): its bit-4-clear return `movs r0, #0` (bytes 00 20) becomes
`movs r0, #1` (01 20), so the helper reports "allowed" for unsigned images.
Signed-image verification is untouched. Verified: with this patch (plus the
NOR IMG2-validator normalisation in build-m68ap-nor.py), m68ap iBoot loads the
device tree and proceeds through `gBootArgs.commandLine` to the kernel jump.

Finding the site across builds
------------------------------
The site is LOCATED BY PATTERN, not by a hardcoded offset. The 32 bytes ending
at the patch site contain the literal address of the security-config word
(0x18022fa0) followed by its load/test/branch, which makes them a unique and
stable fingerprint. Measured 2026-07-25, the identical 32-byte sequence occurs
exactly once in every 1.x m68ap iBoot, merely relocated:

    4A102  (1.1.4)  iBoot-204  site 0x5990
    3A109a (1.1.1)  iBoot-204  site 0x5930
    1A543a (1.0)    iBoot-159  site 0x5350

So the helper is byte-identical across the whole iPhone OS 1.x line and this
patch needs no per-build re-derivation -- only a unique match. If a future
image matches zero or several times, the tool refuses rather than guessing.

Policy: this rewrites Apple-derived firmware, so like the extracted images it is
never committed -- only this patch *tool* is. Operate on a staged copy; never
mutate the installed iboot except via install-iphone-firmware.py.

Usage:
  python3 scripts/patch-m68ap-iboot.py <iboot_204_m68ap.bin> <out.bin>
  python3 scripts/patch-m68ap-iboot.py --verify <iboot.bin>   # check state only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ORIG = bytes.fromhex("0020")   # movs r0, #0  (reject: unsigned not allowed)
PATCHED = bytes.fromhex("0120")  # movs r0, #1  (allow unsigned)

# --- iBoot-159 only: the unsigned flash-image return value --------------------
#
# iBoot-159's flash-image loader tests IMG2 flags2 bit 1 ("signed") and, when it
# is CLEAR, runs a path that hardcodes the return value to -1:
#
#     0x1800843e  ldr r3,[r4,#0x1c] / lsls r2,r3,#0x1e / bpl <unsigned path>
#     0x180084a4  movs r4,#1
#     0x180084a6  rsbs r4,r4,#0        ; r4 = -1  <- the failure
#     0x180084b2  cmp r4,#0 / bge <return r4>
#     0x180084b6  <security-config helper> ; consulted, but r4 is already -1
#
# Apple's own all_flash containers ship with bit 1 CLEAR (the IPSW dtre header
# is 0x40000000), so every NOR image we build takes this path and iBoot returns
# "load_macho_image: failed to load device tree" no matter what the security
# config says -- which is why relaxing the config helper alone is not enough on
# 1.0/1.0.x, and why forcing the image validator to report "trusted" does not
# help either.
#
# One instruction: movs r4,#1 -> movs r4,#0. The following rsbs then computes
# -0 == 0, cmp/bge returns 0, and the load succeeds. The destination address and
# size were already stored by 0x1800842e, so nothing else is needed.
#
# Located by the 20 bytes starting 2 before the site; measured unique in both
# iBoot-159 builds (1C28 and 1A543a, both at 0x84a4) and ABSENT from every
# iBoot-204 image, so it is skipped automatically on 1.1.x.
UNSIGNED_LOCATOR = bytes.fromhex("01e0" "0124" "6442" "19a8" "0021" "1022"
                                 "0ff0" "98eb" "002c" "09da")
UNSIGNED_SITE_OFF = 2
UNSIGNED_ORIG = bytes.fromhex("0124")     # movs r4, #1  (-> -1, reject)
UNSIGNED_PATCHED = bytes.fromhex("0024")  # movs r4, #0  (-> 0, accept)

# The 32 bytes immediately PRECEDING the patch site, ending exactly at it.
# Read from 4A102 at file 0x5970..0x5990 and confirmed byte-identical in 3A109a
# and in 1A543a's iBoot-159. Contains the literal 0x18022fa0 (security-config
# address, stored little-endian as a0 2f 02 18) plus its load/test/branch, which
# is what makes it unique.
LOCATOR = bytes.fromhex(
    "1340202213430b60201c10bd"        # tail of the preceding function
    "a02f0218"                        # &security_config == 0x18022fa0
    "fffff3df01280bd1074a1368d90601d4"  # cmp r0,#1 / ldr / tst bit 4 / bmi
)


def find_site(buf: bytes) -> int:
    """Return the file offset of the 2-byte return value, or raise."""
    hits = []
    start = 0
    while True:
        i = buf.find(LOCATOR, start)
        if i < 0:
            break
        hits.append(i + len(LOCATOR))
        start = i + 1
    if len(hits) != 1:
        raise SystemExit(
            f"secure-boot helper located {len(hits)} times (expected exactly "
            f"1); this does not look like an m68ap iBoot 1.x image, so "
            f"refusing to patch")
    return hits[0]


def classify(buf: bytes, site: int) -> str:
    win = buf[site:site + 2]
    if win == ORIG:
        return "unpatched"
    if win == PATCHED:
        return "patched"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iboot", type=Path, help="extracted raw m68ap iBoot")
    ap.add_argument("out", type=Path, nargs="?", help="output patched iBoot")
    ap.add_argument("--verify", action="store_true",
                    help="only report patch state, do not write")
    args = ap.parse_args()

    buf = bytearray(args.iboot.read_bytes())
    site = find_site(buf)
    state = classify(buf, site)
    if state == "unknown":
        raise SystemExit(
            f"{args.iboot}: secure-boot helper found at 0x{site:x} but its "
            f"return is {buf[site:site+2].hex()} (expected {ORIG.hex()} or "
            f"{PATCHED.hex()}); refusing to patch")

    if args.verify:
        print(f"{args.iboot}: {state} (site 0x{site:x})")
        return 0

    if not args.out:
        raise SystemExit("output path required (or use --verify)")
    if state == "patched":
        print(f"{args.iboot}: already patched; copying through")
    else:
        buf[site:site + 2] = PATCHED

    # iBoot-159 only; absent from iBoot-204, where it is silently skipped.
    hits = []
    start = 0
    while True:
        i = bytes(buf).find(UNSIGNED_LOCATOR, start)
        if i < 0:
            break
        hits.append(i + UNSIGNED_SITE_OFF)
        start = i + 1
    if len(hits) > 1:
        raise SystemExit(f"unsigned-image site found {len(hits)} times; "
                         f"refusing to patch")
    if hits:
        at = hits[0]
        if bytes(buf[at:at + 2]) == UNSIGNED_PATCHED:
            print(f"  unsigned flash-image acceptance: already patched at "
                  f"0x{at:x}")
        else:
            buf[at:at + 2] = UNSIGNED_PATCHED
            print(f"  unsigned flash-image acceptance (iBoot-159) at 0x{at:x}: "
                  f"{UNSIGNED_ORIG.hex()} -> {UNSIGNED_PATCHED.hex()}")

    args.out.write_bytes(buf)
    print(f"wrote {args.out} (secure-boot bypass at 0x{site:x}: "
          f"{ORIG.hex()} -> {PATCHED.hex()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
