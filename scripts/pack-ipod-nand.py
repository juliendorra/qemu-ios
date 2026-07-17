#!/usr/bin/env python3
"""Build an immutable iPod NAND base pack without modifying its source tree."""

import argparse
import os
from pathlib import Path
import struct
import tempfile


MAGIC = b"IPODNAND"
VERSION = 1
NUM_BANKS = 8
PAGE_SIZE = 2048 + 64
HEADER = struct.Struct("<8sIII")
U32 = struct.Struct("<I")


def collect_pages(nand: Path) -> list[tuple[int, Path]]:
    pages: list[tuple[int, Path]] = []
    for bank in range(NUM_BANKS):
        bank_dir = nand / f"bank{bank}"
        if not bank_dir.is_dir():
            raise SystemExit(f"missing NAND bank directory: {bank_dir}")
        for path in bank_dir.glob("*.page"):
            if path.name.endswith("_new.page"):
                continue
            try:
                page = int(path.stem)
            except ValueError as error:
                raise SystemExit(f"invalid NAND page name: {path}") from error
            if path.stat().st_size != PAGE_SIZE:
                raise SystemExit(f"invalid NAND page size: {path}")
            pages.append((page * NUM_BANKS + bank, path))
    pages.sort(key=lambda item: item[0])
    if any(left[0] == right[0] for left, right in zip(pages, pages[1:])):
        raise SystemExit("duplicate NAND virtual page number")
    return pages


def build_pack(nand: Path, output: Path) -> None:
    pages = collect_pages(nand)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as packed:
            packed.write(HEADER.pack(MAGIC, VERSION, PAGE_SIZE, len(pages)))
            for vpn, _ in pages:
                packed.write(U32.pack(vpn))
            for _, path in pages:
                with path.open("rb") as page_file:
                    packed.write(page_file.read())
            packed.flush()
            os.fsync(packed.fileno())
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    print(f"packed {len(pages)} pages into {output} ({output.stat().st_size} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("nand", type=Path, help="directory containing bank0-bank7")
    parser.add_argument(
        "--output", type=Path,
        help="output path (default: NAND directory/nand.pack)",
    )
    args = parser.parse_args()
    nand = args.nand.resolve()
    build_pack(nand, args.output.resolve() if args.output else nand / "nand.pack")


if __name__ == "__main__":
    main()
