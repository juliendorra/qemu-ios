#!/usr/bin/env python3
"""Turn a NAND page-fetch trace into the browser port's delivery numbers.

Records a boot's demand-fault set (produced by `IT_NAND_TRACE_PAGES=<path>`,
see hw/arm/ipod_touch_nand.c) and answers the question the chunked-delivery
design turns on: **how much of the base pack does a cold boot actually touch?**

If a boot touches most of the pack, lazy loading buys little and a single
compressed pack is the simpler design. If it touches a fraction, chunking wins
that fraction back on every first visit.

It also emits the ordered chunk list that becomes the manifest's `prefetch`
field, so startup is not serialized on demand-faults.

Usage:
    IT_NAND_TRACE_PAGES=/tmp/boot.trace ./build-ipod11/qemu-system-arm ...
    scripts/wasm/analyze-nand-trace.py /tmp/boot.trace path/to/nand.pack
    scripts/wasm/analyze-nand-trace.py /tmp/boot.trace nand.pack --json
    scripts/wasm/analyze-nand-trace.py /tmp/boot.trace nand.pack \\
        --prefetch prefetch.json

Caveat: with IT_NAND_WRITABLE=1 the trace also counts pages served from
guest-written files rather than from the pack. Those are overlay reads in the
browser, so the pack working set reported here is a slight over-estimate.
"""

from __future__ import annotations

import argparse
import array
import json
import lzma
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


def read_trace(path: Path) -> array.array:
    data = path.read_bytes()
    if len(data) % 4:
        print(f"warning: trace length {len(data)} is not a multiple of 4; "
              "ignoring the trailing partial record", file=sys.stderr)
        data = data[: len(data) - (len(data) % 4)]
    vpns = array.array("I")
    vpns.frombytes(data)
    if sys.byteorder != "little":
        vpns.byteswap()
    return vpns


def read_pack_index(path: Path) -> tuple[int, list[int]]:
    with path.open("rb") as handle:
        magic, version, stride, count = HEADER.unpack(handle.read(HEADER.size))
        if magic != MAGIC:
            raise SystemExit(f"not a NAND pack: {path}")
        if version != 1:
            raise SystemExit(f"unsupported pack version {version}")
        index = array.array("I")
        index.frombytes(handle.read(count * 4))
        if sys.byteorder != "little":
            index.byteswap()
    return stride, list(index)


def compressed_size(block: bytes) -> int:
    if brotli is not None:
        return len(brotli.compress(block, quality=11))
    return len(lzma.compress(block, preset=6))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("trace", type=Path)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--pages-per-chunk", type=int,
                        default=DEFAULT_PAGES_PER_CHUNK)
    parser.add_argument("--prefetch", type=Path,
                        help="write the ordered first-touch chunk list here")
    parser.add_argument("--no-compress", action="store_true",
                        help="skip measuring the touched bytes (much faster)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    fetches = read_trace(args.trace)
    stride, index = read_pack_index(args.pack)
    slot_of = {vpn: slot for slot, vpn in enumerate(index)}
    payload_base = HEADER.size + len(index) * 4
    total_chunks = (len(index) + args.pages_per_chunk - 1) // args.pages_per_chunk

    touched_pages = set()
    missing = 0            # fetched pages absent from the pack (erased reads)
    chunk_order: list[int] = []
    seen_chunks = set()

    for vpn in fetches:
        slot = slot_of.get(vpn)
        if slot is None:
            missing += 1
            continue
        touched_pages.add(slot)
        chunk = slot // args.pages_per_chunk
        if chunk not in seen_chunks:
            seen_chunks.add(chunk)
            chunk_order.append(chunk)

    result = {
        "trace": str(args.trace),
        "pack": str(args.pack),
        "packPages": len(index),
        "packSize": args.pack.stat().st_size,
        "fetches": len(fetches),
        "fetchesNotInPack": missing,
        "touchedPages": len(touched_pages),
        "touchedPagesPct": len(touched_pages) / len(index) * 100,
        "pagesPerChunk": args.pages_per_chunk,
        "totalChunks": total_chunks,
        "touchedChunks": len(seen_chunks),
        "touchedChunksPct": len(seen_chunks) / total_chunks * 100,
    }

    if not args.no_compress:
        raw = 0
        compressed = 0
        with args.pack.open("rb") as handle:
            for chunk in sorted(seen_chunks):
                start = chunk * args.pages_per_chunk
                pages = min(args.pages_per_chunk, len(index) - start)
                handle.seek(payload_base + start * stride)
                block = handle.read(pages * stride)
                raw += len(block)
                compressed += compressed_size(block)
        result["touchedBytes"] = raw
        result["touchedBytesCompressed"] = compressed
        result["compressor"] = "brotli" if brotli else "lzma"

    if args.prefetch:
        args.prefetch.write_text(json.dumps({
            "pagesPerChunk": args.pages_per_chunk,
            "chunks": chunk_order,
        }, indent=2) + "\n")

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    mb = 1024 * 1024
    print(f"trace           {result['fetches']:,} page fetches")
    if missing:
        print(f"                {missing:,} of them not in the pack "
              "(erased/absent pages)")
    print(f"pack            {result['packPages']:,} pages, "
          f"{result['packSize'] / mb:.1f} MiB")
    print(f"touched pages   {result['touchedPages']:,} "
          f"({result['touchedPagesPct']:.1f}% of the pack)")
    print(f"touched chunks  {result['touchedChunks']:,} of "
          f"{result['totalChunks']:,} ({result['touchedChunksPct']:.1f}%)")
    if "touchedBytes" in result:
        print(f"first-boot cost {result['touchedBytes'] / mb:.1f} MiB raw -> "
              f"{result['touchedBytesCompressed'] / mb:.1f} MiB "
              f"({result['compressor']})")
        print(f"vs whole pack   {result['packSize'] / mb:.1f} MiB raw")
    if args.prefetch:
        print(f"prefetch list   {len(chunk_order):,} chunks -> {args.prefetch}")


if __name__ == "__main__":
    main()
