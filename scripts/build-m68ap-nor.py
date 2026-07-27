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
import zlib

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


# IMG2 header field offsets used by the "loadable" promotion below.
IMG2_STRIDE_OFF = 0x18      # uint32 on-NOR allocation for this image, in 0x40s
IMG2_FLAGS2_OFF = 0x1c      # uint32 flags word
IMG2_FLAGS2_LOADABLE = 0x01000000   # bit 24: image may be loaded/booted
IMG2_FLAGS2_EXTCKSUM = 0x40000000   # bit 30: extended +0x60/+0x68 sub-checksum
IMG2_HDR_CRC_OFF = 0x64     # uint32 CRC32 over header bytes [0:0x64]


def set_stride(hdr, stride):
    """Fill in +0x18, the field iBoot's image-store walk uses to step from one
    IMG2 header to the next.

    This is how iBoot finds image N+1: not by scanning for the '2gmI' magic and
    not by adding the padded data length, but as
    `next_header = this_header + (u32 at +0x18) * 0x40`. Derived from the real
    N45AP NOR, where the field predicts the next header's offset exactly for all
    six gaps:

        dtre @0x10400 +0x18=0x215 -> 0x215*0x40 = 0x8540 -> batC @0x18940
        batC @0x18940 +0x18=0x435 -> 0x435*0x40 = 0x10d40 -> logo @0x29680
        logo @0x29680 +0x18=0x095 -> 0x095*0x40 = 0x2540 -> nsrv @0x2bbc0
        nsrv @0x2bbc0 +0x18=0x135 -> 0x135*0x40 = 0x4d40 -> batl @0x30900
        batl @0x30900 +0x18=0x355 -> 0x355*0x40 = 0xd540 -> batL @0x3de40
        batL @0x3de40 +0x18=0x3d5 -> 0x3d5*0x40 = 0xf540 -> recm @0x4d380

    The IPSW's own IMG2 containers ship 0xFFFFFFFF here -- the field describes an
    on-NOR allocation, so it is only meaningful once a NOR programmer has chosen
    the layout. Leaving it at 0xFFFFFFFF sends the walk 256 MiB past the end of
    the store on the very first step, so iBoot enumerates dtre and nothing else:
    no `logo` entry, hence no Apple logo drawn for the whole boot. Every image
    the store holds after the first one was invisible to iBoot.

    The last image's stride just has to land on erased (0xFF) NOR to terminate
    the walk, which its own allocation does.
    """
    struct.pack_into("<I", hdr, IMG2_STRIDE_OFF, stride // 0x40)


def promote_loadable(container, stride):
    """Normalise the IMG2 +0x1c flags to the value m68ap iBoot's image_load path
    requires, and recompute the header CRC.

    m68ap iBoot-204.3.14 builds a *normalised* RAM copy of each NOR IMG2 header
    at enumeration time and validates THAT copy on load (validator VA
    0x18008478, reached via image_load 0x180088cc). Two facts, both confirmed by
    live lldb probing of the dtre load (see IPHONE_2G_BRINGUP_HANDOFF.md):

    - The load path REQUIRES flags2 bit 24 (0x180084aa: `lsls r1, flags2, #7;
      bpl <reject>`). Enumeration skips this check, so an image with bit 24 clear
      registers but fails to load, and iBoot prints
      "load_macho_image: failed to load device tree" for the dtre image.
    - iBoot's RAM copy CLEARS flags2 bit 30 (the extended +0x60 checksum flag):
      a NOR +0x1c of 0x41000000 becomes 0x01000000 in RAM. But it COPIES the
      NOR's +0x64 CRC verbatim. The validator then recomputes CRC32 over the
      RAM header (with +0x1c = 0x01000000) and compares it to that copied CRC,
      so the CRC must be computed over the bit-30-CLEARED flags, not the raw
      IPSW value (0x40000000, bit 30 set).

    So: clear bit 30, set bit 24 (matching the normalised RAM value), then
    recompute the +0x64 CRC32 over header[0:0x64] (standard zlib CRC, verified
    against the N45AP NOR). This makes the NOR header self-consistent for
    enumeration AND identical to iBoot's RAM copy for the load-time validator,
    and it makes the validator take the no-extended-checksum path.

    The +0x18 walk stride (see set_stride) is filled in here too, since both
    edits share the one CRC recomputation.
    """
    hdr = bytearray(container[:0x400])
    flags2 = struct.unpack_from("<I", hdr, IMG2_FLAGS2_OFF)[0]
    new_flags2 = (flags2 & ~IMG2_FLAGS2_EXTCKSUM) | IMG2_FLAGS2_LOADABLE
    struct.pack_into("<I", hdr, IMG2_FLAGS2_OFF, new_flags2)
    set_stride(hdr, stride)
    crc = zlib.crc32(bytes(hdr[:IMG2_HDR_CRC_OFF])) & 0xFFFFFFFF
    struct.pack_into("<I", hdr, IMG2_HDR_CRC_OFF, crc)
    return bytes(hdr) + container[0x400:]


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
        # Every container starts on a 0x40 boundary, so the distance to the next
        # header is just this container's length rounded up to ALIGN -- that is
        # the allocation the +0x18 walk stride has to advertise.
        stride = align_up(len(container), ALIGN)
        container = promote_loadable(container, stride)
        img_type = container[4:8][::-1].decode("ascii", "replace")
        epoch = struct.unpack("<H", container[0xa:0xc])[0]
        end = offset + len(container)
        if end > STORE_END:
            sys.exit(f"image store overflow placing {stem} "
                     f"({hex(offset)}..{hex(end)} > {hex(STORE_END)})")
        nor[offset:end] = container
        placed.append((img_type, offset, len(container), epoch, stride))
        offset = align_up(end, ALIGN)
        assert offset == placed[-1][1] + stride, "stride must reach the next header"

    with open(args.out, "wb") as fh:
        fh.write(nor)

    print(f"wrote {args.out} ({nor_size} bytes)")
    for img_type, off, length, epoch, stride in placed:
        print(f"  {img_type} @ {hex(off)}  {hex(length)} bytes  epoch={epoch}"
              f"  next @ {hex(off + stride)}")


if __name__ == "__main__":
    main()
