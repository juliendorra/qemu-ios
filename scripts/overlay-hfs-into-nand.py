#!/usr/bin/env python3
"""Drop an HFS partition image into an existing NAND tree as page overrides.

WHY. Changing one file in the M68AP `/var` used to mean rebuilding the whole
NAND (`build-m68ap-homescreen-nand.py`: minutes, ~900 MB of scratch, and the
disk is always nearly full). But the QEMU NAND model already reads
`bank<N>/<page>.page` in preference to the immutable `nand.pack` when
`IT_NAND_WRITABLE=1`, so a partition can be replaced by writing just that
partition's pages next to the pack. The data partition is 12288 pages (25 MB),
which takes about a second.

That turns "what does the guest do if /var looks like THIS?" into a loop you
can run many times an hour -- which is what T6 needs.

The spare bytes are copied from the pack's page at the same address, so the
FTL metadata the guest checks stays exactly as the builder wrote it.

Usage
-----
  # 1. get the pristine partition out of the shipped NAND
  scripts/extract-hfs-from-nand.py "<bundle>/nand" /tmp/var.img \\
      --active-banks 4 --partition data

  # 2. edit /tmp/var.img (hdiutil attach -readwrite, or inject-guest-file.py)

  # 3. build a throwaway tree: a SYMLINK to the pack plus empty bank dirs,
  #    then overlay the edited image
  scripts/overlay-hfs-into-nand.py --nand /tmp/nand-test --image /tmp/var.img \\
      --partition data --active-banks 4

  # 4. boot it (the tree is throwaway: the model may write into it)
  S5L8900_STAGE_NAND=0 S5L8900_NAND=/tmp/nand-test IT_NAND_WRITABLE=1 \\
      "/Applications/iPhone 2G (iOS 1.1.4).app/Contents/MacOS/iPod Touch"
"""
from __future__ import annotations

import argparse
import importlib.util
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "extract_hfs", Path(__file__).resolve().parent / "extract-hfs-from-nand.py")
_extract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_extract)

DATA_SIZE = _extract.DATA_SIZE
SPARE_SIZE = _extract.PACK_PAGE_SIZE - DATA_SIZE
NUM_BANKS = _extract.NUM_BANKS


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nand", type=Path, required=True,
                    help="NAND tree to write into (needs nand.pack, or a "
                         "symlink to one, plus bank<N> dirs; both are created "
                         "with --pack)")
    ap.add_argument("--pack", type=Path,
                    help="create <nand> as a tree symlinking this pack")
    ap.add_argument("--image", type=Path, required=True,
                    help="HFS partition image to write into those pages")
    ap.add_argument("--partition", choices=("boot", "data"), default="data")
    ap.add_argument("--active-banks", type=int, choices=(4, 8), default=4)
    args = ap.parse_args()

    if args.pack:
        args.nand.mkdir(parents=True, exist_ok=True)
        link = args.nand / "nand.pack"
        if not link.exists():
            link.symlink_to(args.pack.resolve())
        for bank in range(NUM_BANKS):
            (args.nand / f"bank{bank}").mkdir(exist_ok=True)

    pack = args.nand / "nand.pack"
    if not pack.exists():
        raise SystemExit(f"no nand.pack in {args.nand} (pass --pack)")
    reader = _extract.PackedPages(pack, args.active_banks)

    if args.partition == "data":
        first_vpn, block_size, total_blocks = _extract.data_volume_geometry(
            reader, args.active_banks)
    else:
        first_vpn, block_size, total_blocks = _extract.volume_geometry(
            reader, args.active_banks)
    capacity = (block_size * total_blocks) // DATA_SIZE

    size = args.image.stat().st_size
    if size % DATA_SIZE:
        raise SystemExit(f"image size {size} is not a multiple of {DATA_SIZE}")
    pages = size // DATA_SIZE
    if pages > capacity:
        raise SystemExit(f"image is {pages} pages, partition holds {capacity}")

    # The pack is keyed physically; read its spare for each page so the FTL
    # metadata (which the guest validates) is preserved byte for byte.
    with pack.open("rb") as fh:
        import mmap
        mapping = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        _, _, page_size, count = struct.unpack_from("<8sIII", mapping)
        data_offset = 20 + count * 4
        index = {struct.unpack_from("<I", mapping, 20 + i * 4)[0]: i
                 for i in range(count)}

        written = missing_spare = 0
        with args.image.open("rb") as image:
            for offset in range(pages):
                vpn = first_vpn + offset
                bank = vpn % args.active_banks
                page = vpn // args.active_banks
                entry = index.get(page * NUM_BANKS + bank)
                if entry is None:
                    # No base page here: the builder left the hole because the
                    # volume did not reach this far. Skip rather than invent
                    # FTL spare bytes -- an invented spare is worse than a
                    # hole, which the model already reads as an erased page.
                    image.seek(DATA_SIZE, 1)
                    missing_spare += 1
                    continue
                base = data_offset + entry * page_size
                spare = bytes(mapping[base + DATA_SIZE: base + page_size])
                out = args.nand / f"bank{bank}" / f"{page}.page"
                out.write_bytes(image.read(DATA_SIZE) + spare)
                written += 1
        mapping.close()

    print(f"partition {args.partition}: first_vpn={first_vpn} "
          f"capacity={capacity} pages")
    print(f"wrote {written} page overrides into {args.nand}"
          + (f" ({missing_spare} skipped: no base page)" if missing_spare
             else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
