#!/usr/bin/env python3
"""Build a synthetic iPhone-2G (M68AP) NOR image from decrypted IMG2 containers.

The S5L8900 NOR the guest reads directly (QEMU does not parse it) is, for the
early-boot images, a flat sequence of IMG2 containers in a fixed "image store"
region, plus a SysCfg/nvram block near the top. There is no IMG2 superblock;
iBoot scans the store. See the "NOR layout facts" section of
IPHONE_2G_BRINGUP_HANDOFF.md.

This builder starts from a REAL N45AP NOR (for its SysCfg block and overall
geometry) and rewrites only the image-store region with the authentic M68AP
IMG2 containers emitted by extract-m68ap-images.py (--> <out>/nor-containers/).
Those carry security epoch 3, which the M68AP iBoot requires; the N45AP images
it replaces carry epoch 2 and are otherwise "Ignoring image with mismatching
security epoch".

Policy: takes user-supplied firmware at run time; commits nothing derived from
Apple firmware.

Usage:
  python3 scripts/build-m68ap-nor.py \
      --template data/nor_n45ap.bin \
      --containers <extract-out>/nor-containers \
      --out iphone_files/nor_m68ap.bin
"""
import argparse
import os
import struct
import sys

# Image-store region in the 1 MiB NOR: the first container starts here, and the
# SysCfg block sits well above the last one. These match the observed N45AP
# layout; the store must not grow past STORE_END.
STORE_BASE = 0x10400
STORE_END = 0xF0000       # SysCfg lives at ~0xFC000; keep clear of it
ALIGN = 0x40              # every container starts on a 0x40 boundary (observed)

# The containers in N45AP-store order, keyed by extract-m68ap-images.py source
# stem (case-safe: 'batterylow0'/'batterylow1' rather than 'batl'/'batL').
STORE_ORDER = [
    "DeviceTree.m68ap",
    "batterycharging",
    "applelogo",
    "needservice",
    "batterylow0",
    "batterylow1",
    "recoverymode",
]


def align_up(value, alignment):
    return (value + alignment - 1) & ~(alignment - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", required=True,
                        help="a real N45AP NOR to source SysCfg/geometry from")
    parser.add_argument("--containers", required=True,
                        help="directory of <type>.img2c from extract-m68ap-images.py")
    parser.add_argument("--out", required=True, help="output nor_m68ap.bin")
    args = parser.parse_args()

    nor = bytearray(open(args.template, "rb").read())
    nor_size = len(nor)

    # Blank the old image store region (leave the LLB region and SysCfg intact).
    for i in range(STORE_BASE, min(STORE_END, nor_size)):
        nor[i] = 0xFF  # erased NOR reads as 0xFF

    offset = STORE_BASE
    placed = []
    for stem in STORE_ORDER:
        path = os.path.join(args.containers, f"{stem}.img2c")
        if not os.path.exists(path):
            print(f"skip {stem}: {path} missing")
            continue
        container = open(path, "rb").read()
        if container[:4] != b"2gmI":
            sys.exit(f"{path}: not an IMG2 container")
        img_type = container[4:8][::-1].decode("ascii", "replace")
        epoch = struct.unpack("<H", container[0xa:0xc])[0]
        end = offset + len(container)
        if end > STORE_END:
            sys.exit(f"image store overflow placing {stem} "
                     f"({hex(offset)}..{hex(end)} > {hex(STORE_END)})")
        nor[offset:end] = container
        placed.append((img_type, offset, len(container), epoch))
        offset = align_up(end, ALIGN)

    with open(args.out, "wb") as fh:
        fh.write(nor)

    print(f"wrote {args.out} ({nor_size} bytes)")
    for img_type, off, length, epoch in placed:
        print(f"  {img_type} @ {hex(off)}  {hex(length)} bytes  epoch={epoch}")


if __name__ == "__main__":
    main()
