#!/usr/bin/env python3
"""Measure a NAND base pack the way the browser build would ship it.

The browser port delivers the NAND as fixed-size, content-addressed,
individually compressed chunks (see BROWSER_WASM_IMPLEMENTATION_PLAN.md,
"Chunked, compressed asset delivery"). This tool reports what that costs for a
given pack, so delivery decisions are made on measurements rather than on
Infinite Mac's numbers, which were taken on Mac disk images and do not
necessarily transfer.

A chunk is a fixed number of PAGES, not bytes: the 2,112-byte page stride
divides no power of two, and a page count keeps page-to-chunk mapping pure
arithmetic. 124 pages = 261,888 B, just under Infinite Mac's 256 KiB.

Usage:
    scripts/wasm/measure-pack.py <nand.pack>
    scripts/wasm/measure-pack.py <nand.pack> --full          # every chunk
    scripts/wasm/measure-pack.py <nand.pack> --json          # machine-readable
    scripts/wasm/measure-pack.py a.pack b.pack --cross       # sharing between
                                                             # two versions

Brotli is used when the `brotli` module is importable; otherwise zlib and lzma
bracket the expected result (Brotli lands near lzma in practice).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import random
import struct
import sys
import zlib
from pathlib import Path

HEADER = struct.Struct("<8sIII")
MAGIC = b"IPODNAND"
DEFAULT_PAGES_PER_CHUNK = 124

try:
    import brotli  # type: ignore
except ImportError:
    brotli = None


def read_header(handle) -> tuple[int, int]:
    magic, version, stride, count = HEADER.unpack(handle.read(HEADER.size))
    if magic != MAGIC:
        raise SystemExit(f"not a NAND pack (magic {magic!r})")
    if version != 1:
        raise SystemExit(f"unsupported pack version {version}")
    return stride, count


def chunk_bytes(handle, payload_base: int, stride: int, count: int,
                index: int, pages_per_chunk: int) -> bytes:
    start = index * pages_per_chunk
    pages = min(pages_per_chunk, count - start)
    handle.seek(payload_base + start * stride)
    return handle.read(pages * stride)


def compress_sizes(block: bytes) -> dict[str, int]:
    sizes = {
        "zlib": len(zlib.compress(block, 9)),
        "lzma": len(lzma.compress(block, preset=6)),
    }
    if brotli is not None:
        sizes["brotli"] = len(brotli.compress(block, quality=11))
    return sizes


def measure(path: Path, pages_per_chunk: int, sample: int | None) -> dict:
    with path.open("rb") as handle:
        stride, count = read_header(handle)
        payload_base = HEADER.size + count * 4
        total_chunks = (count + pages_per_chunk - 1) // pages_per_chunk

        if sample is None or sample >= total_chunks:
            indexes = list(range(total_chunks))
            sampled = False
        else:
            random.seed(7)  # deterministic: reruns are comparable
            indexes = sorted(random.sample(range(total_chunks), sample))
            sampled = True

        raw = 0
        totals: dict[str, int] = {}
        digests: dict[str, int] = {}
        uniform = 0

        for index in indexes:
            block = chunk_bytes(handle, payload_base, stride, count,
                                index, pages_per_chunk)
            if not block:
                continue
            raw += len(block)
            for name, size in compress_sizes(block).items():
                totals[name] = totals.get(name, 0) + size
            digest = hashlib.sha256(block).hexdigest()
            digests[digest] = digests.get(digest, 0) + 1
            if len(set(block)) <= 1:
                uniform += 1

    file_size = path.stat().st_size
    result = {
        "path": str(path),
        "fileSize": file_size,
        "pages": count,
        "pageStride": stride,
        "indexSize": count * 4,
        "payloadSize": count * stride,
        "pagesPerChunk": pages_per_chunk,
        "chunkSize": pages_per_chunk * stride,
        "totalChunks": total_chunks,
        "measuredChunks": len(indexes),
        "sampled": sampled,
        "rawMeasured": raw,
        "compression": {
            name: {
                "ratio": size / raw,
                "projectedTotal": int(file_size * size / raw),
            }
            for name, size in sorted(totals.items())
        },
        "uniformChunks": uniform,
        "distinctChunks": len(digests),
        "duplicateChunks": len(indexes) - len(digests),
    }
    return result


def chunk_digests(path: Path, pages_per_chunk: int) -> set[str]:
    with path.open("rb") as handle:
        stride, count = read_header(handle)
        payload_base = HEADER.size + count * 4
        total = (count + pages_per_chunk - 1) // pages_per_chunk
        digests = set()
        for index in range(total):
            block = chunk_bytes(handle, payload_base, stride, count,
                                index, pages_per_chunk)
            if block:
                digests.add(hashlib.sha256(block).hexdigest())
    return digests


def human(count: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{count} B"
        count /= 1024
    return str(count)


def report(result: dict) -> None:
    print(f"{result['path']}")
    print(f"  size            {human(result['fileSize'])}"
          f" ({result['fileSize']} bytes)")
    print(f"  pages           {result['pages']:,} x {result['pageStride']} B")
    print(f"  index/payload   {human(result['indexSize'])}"
          f" / {human(result['payloadSize'])}")
    print(f"  chunk           {result['pagesPerChunk']} pages ="
          f" {human(result['chunkSize'])}")
    print(f"  chunks          {result['totalChunks']:,}"
          f" ({result['measuredChunks']:,} measured"
          f"{', sampled' if result['sampled'] else ', all'})")
    for name, data in result["compression"].items():
        print(f"  {name:<15} {data['ratio'] * 100:.1f}%"
              f"  -> {human(data['projectedTotal'])} whole pack")
    if brotli is None:
        print("  (brotli module not installed; lzma is the closest proxy)")
    print(f"  uniform chunks  {result['uniformChunks']}"
          f" / {result['measuredChunks']}")
    print(f"  duplicates      {result['duplicateChunks']}"
          f" / {result['measuredChunks']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("packs", type=Path, nargs="+")
    parser.add_argument("--pages-per-chunk", type=int,
                        default=DEFAULT_PAGES_PER_CHUNK)
    parser.add_argument("--sample", type=int, default=120,
                        help="chunks to sample (default 120; deterministic)")
    parser.add_argument("--full", action="store_true",
                        help="measure every chunk (slow, exact)")
    parser.add_argument("--cross", action="store_true",
                        help="report chunk sharing between the given packs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    sample = None if args.full else args.sample
    results = [measure(pack, args.pages_per_chunk, sample)
               for pack in args.packs]

    cross = None
    if args.cross:
        if len(args.packs) < 2:
            raise SystemExit("--cross needs at least two packs")
        # Full digest sets: sampling cannot answer a sharing question.
        sets = {str(pack): chunk_digests(pack, args.pages_per_chunk)
                for pack in args.packs}
        union: set[str] = set()
        for digests in sets.values():
            union |= digests
        cross = {
            "perPack": {name: len(digests) for name, digests in sets.items()},
            "union": len(union),
            "sumIfUnshared": sum(len(d) for d in sets.values()),
        }
        cross["saved"] = cross["sumIfUnshared"] - cross["union"]

    if args.json:
        json.dump({"packs": results, "cross": cross}, sys.stdout, indent=2)
        print()
        return

    for result in results:
        report(result)
        print()
    if cross:
        print("cross-version chunk sharing (full scan)")
        for name, size in cross["perPack"].items():
            print(f"  {size:,} distinct chunks  {name}")
        print(f"  {cross['sumIfUnshared']:,} chunks if stored separately")
        print(f"  {cross['union']:,} chunks stored once")
        print(f"  {cross['saved']:,} chunks saved"
              f" ({cross['saved'] / cross['sumIfUnshared'] * 100:.1f}%)")


if __name__ == "__main__":
    main()
