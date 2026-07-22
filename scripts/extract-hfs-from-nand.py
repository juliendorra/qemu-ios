#!/usr/bin/env python3
"""Reconstruct the boot HFS volume from a generated S5L8900 NAND base."""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path


PACK_HEADER = struct.Struct("<8sIII")
PACK_MAGIC = b"IPODNAND"
PACK_VERSION = 1
PACK_PAGE_SIZE = 2048 + 64
DATA_SIZE = 2048
PAGES_PER_BLOCK = 128
FTL_CXT_SECTION_START = 201
BOOT_PARTITION_FIRST_PAGE = 3


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PackedPages:
    def __init__(self, path: Path):
        self.path = path
        self.file = path.open("rb")
        self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        magic, version, page_size, count = PACK_HEADER.unpack_from(self.mapping)
        if magic != PACK_MAGIC or version != PACK_VERSION:
            raise SystemExit(f"unsupported NAND pack header: {path}")
        if page_size != PACK_PAGE_SIZE:
            raise SystemExit(f"unsupported NAND pack page size: {page_size}")
        descriptor_end = PACK_HEADER.size + count * 4
        expected = descriptor_end + count * page_size
        if expected != len(self.mapping):
            raise SystemExit(f"truncated or extended NAND pack: {path}")
        self.data_offset = descriptor_end
        self.page_size = page_size
        self.index = {
            struct.unpack_from("<I", self.mapping, PACK_HEADER.size + i * 4)[0]: i
            for i in range(count)
        }

    def read_data(self, vpn: int) -> bytes:
        try:
            index = self.index[vpn]
        except KeyError as error:
            raise SystemExit(f"missing NAND virtual page {vpn}") from error
        offset = self.data_offset + index * self.page_size
        return self.mapping[offset:offset + DATA_SIZE]

    def close(self) -> None:
        self.mapping.close()
        self.file.close()


class SparsePages:
    def __init__(self, path: Path, active_banks: int):
        self.path = path
        self.active_banks = active_banks

    def read_data(self, vpn: int) -> bytes:
        bank = vpn % self.active_banks
        page = vpn // self.active_banks
        path = self.path / f"bank{bank}" / f"{page}.page"
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise SystemExit(f"missing NAND page: {path}") from error
        if len(data) != PACK_PAGE_SIZE:
            raise SystemExit(f"invalid NAND page size: {path}")
        return data[:DATA_SIZE]

    def close(self) -> None:
        pass


def volume_geometry(reader, active_banks: int) -> tuple[int, int, int]:
    pages_per_subblock = active_banks * PAGES_PER_BLOCK
    first_vpn = ((FTL_CXT_SECTION_START + 1) * pages_per_subblock +
                 BOOT_PARTITION_FIRST_PAGE)
    prefix = reader.read_data(first_vpn) + reader.read_data(first_vpn + 1)
    header = prefix[1024:1536]
    signature = header[:2]
    if signature not in (b"H+", b"HX"):
        raise SystemExit(
            f"invalid HFS volume signature {signature!r} at VPN {first_vpn}")
    block_size = struct.unpack_from(">I", header, 40)[0]
    total_blocks = struct.unpack_from(">I", header, 44)[0]
    size = block_size * total_blocks
    if block_size < 512 or block_size & (block_size - 1):
        raise SystemExit(f"invalid HFS allocation block size: {block_size}")
    if not size or size % DATA_SIZE:
        raise SystemExit(f"invalid HFS volume size: {size}")
    return first_vpn, block_size, total_blocks


def extract(reader, output: Path, active_banks: int) -> dict:
    first_vpn, block_size, total_blocks = volume_geometry(reader, active_banks)
    size = block_size * total_blocks
    page_count = size // DATA_SIZE
    digest = hashlib.sha256()
    with output.open("wb") as destination:
        for index in range(page_count):
            data = reader.read_data(first_vpn + index)
            destination.write(data)
            digest.update(data)
    return {
        "first_vpn": first_vpn,
        "active_banks": active_banks,
        "block_size": block_size,
        "total_blocks": total_blocks,
        "page_count": page_count,
        "output_size": size,
        "output_sha256": digest.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nand", type=Path,
                        help="NAND directory or IPODNAND pack")
    parser.add_argument("output", type=Path, help="new HFS image path")
    parser.add_argument("--active-banks", type=int, choices=(4, 8), default=8)
    args = parser.parse_args()

    source = args.nand.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite output: {output}")
    if source.is_dir():
        pack = source / "nand.pack"
        reader = PackedPages(pack) if pack.is_file() else SparsePages(
            source, args.active_banks)
        source_artifact = pack if pack.is_file() else source
    elif source.is_file():
        reader = PackedPages(source)
        source_artifact = source
    else:
        raise SystemExit(f"NAND source not found: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = extract(reader, output, args.active_banks)
    finally:
        reader.close()
    result.update({
        "source": str(source_artifact),
        "source_sha256": (sha256_file(source_artifact)
                          if source_artifact.is_file() else None),
        "output": str(output),
    })
    manifest = output.with_suffix(output.suffix + ".json")
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
