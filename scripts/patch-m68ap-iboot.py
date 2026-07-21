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
One 2-byte Thumb edit at the unsigned-image decision helper (VA 0x18005984):
its bit-4-clear return `movs r0, #0` (file 0x5990, bytes 00 20) becomes
`movs r0, #1` (01 20), so the helper reports "allowed" for unsigned images.
Signed-image verification is untouched. Verified: with this patch (plus the
NOR IMG2-validator normalisation in build-m68ap-nor.py), m68ap iBoot loads the
device tree and proceeds through `gBootArgs.commandLine` to the kernel jump.

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

# VA 0x18005984 helper: bit-4-clear reject at VA 0x18005990 == file 0x5990.
PATCH_OFF = 0x5990
ORIG = bytes.fromhex("0020")   # movs r0, #0  (reject: unsigned not allowed)
PATCHED = bytes.fromhex("0120")  # movs r0, #1  (allow unsigned)

# Sanity anchor: VA 0x18005984 is `cmp r0, #1` (0x2801, file 0x5984).
ANCHOR_OFF = 0x5984
ANCHOR = bytes.fromhex("0128")


def classify(buf: bytes) -> str:
    if buf[ANCHOR_OFF:ANCHOR_OFF + 2] != ANCHOR:
        return "unknown"
    win = buf[PATCH_OFF:PATCH_OFF + 2]
    if win == ORIG:
        return "unpatched"
    if win == PATCHED:
        return "patched"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iboot", type=Path, help="extracted iboot_204_m68ap.bin")
    ap.add_argument("out", type=Path, nargs="?", help="output patched iBoot")
    ap.add_argument("--verify", action="store_true",
                    help="only report patch state, do not write")
    args = ap.parse_args()

    buf = bytearray(args.iboot.read_bytes())
    state = classify(buf)
    if state == "unknown":
        raise SystemExit(
            f"{args.iboot}: not a recognised m68ap iBoot-204.3.14 "
            f"(anchor/opcode mismatch at 0x{ANCHOR_OFF:x}/0x{PATCH_OFF:x}); "
            f"refusing to patch")

    if args.verify:
        print(f"{args.iboot}: {state}")
        return 0 if state in ("patched", "unpatched") else 1

    if not args.out:
        raise SystemExit("output path required (or use --verify)")
    if state == "patched":
        print(f"{args.iboot}: already patched; copying through")
    else:
        buf[PATCH_OFF:PATCH_OFF + 2] = PATCHED
    args.out.write_bytes(buf)
    print(f"wrote {args.out} (secure-boot bypass at 0x{PATCH_OFF:x}: "
          f"{ORIG.hex()} -> {PATCHED.hex()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
