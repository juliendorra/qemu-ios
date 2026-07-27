#!/usr/bin/env python3
"""Inspect and validate the IMG2 image store in an S5L8900 NOR image.

This is the tool that found the missing boot logo. QEMU does not parse the NOR
-- the guest does -- so a NOR can be structurally perfect to every check we
had, contain all seven images, and still be unreadable past the first one.

WHAT iBOOT ACTUALLY DOES, which is the thing to validate:

    next_header = this_header + (u32 at header+0x18) * 0x40

Not a scan for the '2gmI' magic, and not "add the padded data length". The
+0x18 field is an on-NOR *allocation* size in 0x40-byte units. The IPSW's own
containers ship 0xFFFFFFFF there (the field only means something once a NOR
programmer has chosen a layout), so a builder that copies containers verbatim
produces a store whose very first step lands 256 MiB past the end. iBoot then
enumerates image 0 and stops -- no `logo`, so no Apple logo for the whole boot.

Derived from the retail N45AP NOR, where +0x18 predicts the next header's
offset exactly for all six gaps. `--check` asserts that chain, which is the
regression test the builder lacked.

Usage:
  # what does iBoot see in this NOR?
  python3 scripts/nor-image-store.py <nor.bin>

  # fail (exit 1) if the walk chain, CRCs or magic are broken
  python3 scripts/nor-image-store.py <nor.bin> --check

  # compare a synthetic NOR against the retail reference
  python3 scripts/nor-image-store.py <nor.bin> --reference <nor_n45ap.bin>

  # machine-readable
  python3 scripts/nor-image-store.py <nor.bin> --json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

MAGIC = b"2gmI"          # "Img2" stored little-endian
HDR_LEN = 0x400          # header precedes the data
STORE_BASE = 0x10400     # first container in the 1 MiB NOR
ALIGN = 0x40             # every container starts on a 0x40 boundary

# Header field offsets.
OFF_TYPE = 0x04          # 4-char type, big-endian in the file
OFF_EPOCH = 0x0a         # uint16 security epoch
OFF_LEN_PADDED = 0x10    # uint32
OFF_LEN = 0x14           # uint32 payload length
OFF_STRIDE = 0x18        # uint32 allocation for THIS image, in 0x40 units
OFF_FLAGS2 = 0x1c        # uint32
OFF_CRC = 0x64           # uint32 CRC32 over header[0:0x64]

STRIDE_UNSET = 0xFFFFFFFF

# A store this large is a walk that has gone off the rails, not a real image.
MAX_IMAGES = 32


def parse_header(blob: bytes, off: int) -> dict | None:
    """Decode the IMG2 header at `off`, or None if there is not one there."""
    if off < 0 or off + HDR_LEN > len(blob):
        return None
    hdr = blob[off:off + HDR_LEN]
    if hdr[:4] != MAGIC:
        return None
    crc_stored = struct.unpack_from("<I", hdr, OFF_CRC)[0]
    crc_calc = zlib.crc32(hdr[:OFF_CRC]) & 0xFFFFFFFF
    stride_units = struct.unpack_from("<I", hdr, OFF_STRIDE)[0]
    return {
        "offset": off,
        "type": hdr[OFF_TYPE:OFF_TYPE + 4][::-1].decode("ascii", "replace"),
        "epoch": struct.unpack_from("<H", hdr, OFF_EPOCH)[0],
        "data_len": struct.unpack_from("<I", hdr, OFF_LEN)[0],
        "data_len_padded": struct.unpack_from("<I", hdr, OFF_LEN_PADDED)[0],
        "stride_units": stride_units,
        "stride_bytes": (None if stride_units == STRIDE_UNSET
                         else stride_units * ALIGN),
        "flags2": struct.unpack_from("<I", hdr, OFF_FLAGS2)[0],
        "crc": crc_stored,
        "crc_ok": crc_stored == crc_calc,
    }


def walk(blob: bytes, base: int = STORE_BASE) -> tuple[list[dict], list[str]]:
    """Follow the store exactly as iBoot does. Returns (images, problems).

    The walk stops the way iBoot's does: when the next computed address does
    not hold an IMG2 magic. That is the whole point -- a store that scans fine
    with a magic search can still terminate here after one image.
    """
    images: list[dict] = []
    problems: list[str] = []
    off = base
    seen: set[int] = set()

    while True:
        img = parse_header(blob, off)
        if img is None:
            break
        if off in seen:
            problems.append(f"walk loops back to {off:#x}")
            break
        seen.add(off)
        images.append(img)

        if not img["crc_ok"]:
            problems.append(f"{img['type']}@{off:#x}: header CRC mismatch")

        stride = img["stride_bytes"]
        if stride is None:
            problems.append(
                f"{img['type']}@{off:#x}: +0x18 is 0xFFFFFFFF (never filled "
                "in) -- iBoot's walk leaves the store here, so every later "
                "image is invisible to it")
            break
        if stride < HDR_LEN + img["data_len"]:
            problems.append(
                f"{img['type']}@{off:#x}: stride {stride:#x} is shorter than "
                f"the container ({HDR_LEN + img['data_len']:#x}) -- the next "
                "header would land inside this image's data")
        if stride % ALIGN:
            problems.append(f"{img['type']}@{off:#x}: stride {stride:#x} is "
                            f"not a multiple of {ALIGN:#x}")

        nxt = off + stride
        img["next"] = nxt
        if len(images) >= MAX_IMAGES:
            problems.append(f"more than {MAX_IMAGES} images; walk abandoned")
            break
        off = nxt

    if not images:
        problems.append(f"no IMG2 header at {base:#x}")
    return images, problems


def scan(blob: bytes, base: int = STORE_BASE, end: int | None = None) -> list[dict]:
    """Every IMG2 header physically present, found by magic search.

    Deliberately separate from walk(): the gap between what is PRESENT and what
    is REACHABLE is the diagnosis. `scan` finding seven while `walk` finds one
    means the payload is fine and the walk metadata is not.
    """
    end = len(blob) if end is None else end
    out = []
    for off in range(base, min(end, len(blob)) - 4, ALIGN):
        img = parse_header(blob, off)
        if img is not None:
            out.append(img)
    return out


def describe(img: dict) -> str:
    stride = img["stride_bytes"]
    stride_s = ("0xFFFFFFFF (UNSET)" if stride is None
                else f"{stride:#x} -> next @ {img['offset'] + stride:#x}")
    return (f"  {img['type']} @ {img['offset']:#08x}  "
            f"len {img['data_len']:#x}  epoch {img['epoch']}  "
            f"flags2 {img['flags2']:#010x}  crc {'ok' if img['crc_ok'] else 'BAD'}\n"
            f"        stride {stride_s}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nor", type=Path, help="NOR image to inspect")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=STORE_BASE,
                    help=f"image store base (default {STORE_BASE:#x})")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the walk is broken or incomplete")
    ap.add_argument("--expect", type=int, default=None,
                    help="with --check, require exactly this many reachable "
                         "images (the retail N45AP store has 7)")
    ap.add_argument("--reference", type=Path, default=None,
                    help="compare the reachable type list against this NOR")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    blob = args.nor.read_bytes()
    reachable, problems = walk(blob, args.base)
    present = scan(blob, args.base)

    hidden = [p for p in present
              if p["offset"] not in {r["offset"] for r in reachable}]
    if hidden:
        problems.append(
            f"{len(hidden)} image(s) present in the NOR but UNREACHABLE by "
            f"iBoot's walk: {', '.join(h['type'] for h in hidden)}")

    result = {
        "nor": str(args.nor),
        "store_base": args.base,
        "reachable": [{k: v for k, v in r.items()} for r in reachable],
        "present_count": len(present),
        "reachable_count": len(reachable),
        "hidden_types": [h["type"] for h in hidden],
        "problems": problems,
    }

    if args.reference:
        ref_reachable, _ = walk(args.reference.read_bytes(), args.base)
        ref_types = [r["type"] for r in ref_reachable]
        got_types = [r["type"] for r in reachable]
        result["reference"] = {
            "path": str(args.reference),
            "types": ref_types,
            "matches": ref_types == got_types,
        }
        if ref_types != got_types:
            problems.append(
                f"reachable types {got_types} != reference {ref_types}")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{args.nor}  ({len(blob):#x} bytes)")
        print(f"reachable by iBoot's walk: {len(reachable)}"
              f"   physically present: {len(present)}")
        for img in reachable:
            print(describe(img))
        for h in hidden:
            print(f"  [UNREACHABLE] {h['type']} @ {h['offset']:#08x}")
        if args.reference:
            r = result["reference"]
            print(f"\nreference {r['path']}: {r['types']}"
                  f"  {'MATCH' if r['matches'] else 'MISMATCH'}")
        if problems:
            print("\nproblems:")
            for p in problems:
                print(f"  ! {p}")
        else:
            print("\nno problems found")

    if args.check:
        if args.expect is not None and len(reachable) != args.expect:
            problems.append(
                f"expected {args.expect} reachable images, walk found "
                f"{len(reachable)}")
        if problems:
            if args.json:
                print(json.dumps({"problems": problems}, indent=2),
                      file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
