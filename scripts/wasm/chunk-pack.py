#!/usr/bin/env python3
"""Split an IPODNAND pack into content-addressed, Brotli-compressed chunks.

This is what turns a 215.6 MiB download into 18.6 MiB: a cold boot of iPhone
OS 1.0 touches 19.1% of the pack, so ship the pack in pieces and fetch only
the pieces the guest asks for (measurements in BROWSER_WASM_STATUS.md).

Output layout (format `ipod-nand-chunks-v1`):

    nand.pack.idx        header + index, no payload -- downloaded up front
    chunk-hashes.bin     32-byte SHA-256 per chunk, in chunk order
    chunk-config.txt     pagesPerChunk / base, read by the emulator
    chunk-manifest.json  the same, for the JS loader, plus sizes and prefetch
    chunks/<sha256>      the chunk payload, stored ALREADY Brotli-compressed

Chunks are stored compressed and served with `Content-Encoding: br`, so the
browser decompresses them on the way in and the emulator never carries a
Brotli decoder. scripts/wasm/serve.py does this; a CDN does it natively.

Chunks are content-addressed, so identical chunks collapse to one file. That
is not theoretical: a full scan of the 1.1.4 pack found 197 of 1,201 chunks
(16.4%) duplicated, one of them 198 times.

    scripts/wasm/chunk-pack.py <nand.pack> --out web/chunked/1A543a \\
        --pages-per-chunk 62 --prefetch prefetch.json --base /chunked/1A543a/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

import brotli

HEADER = struct.Struct("<8sIII")
MAGIC = b"IPODNAND"
VERSION = 1
DEFAULT_PAGES_PER_CHUNK = 62      # 130,944 B; chosen from our own measurements
DEFAULT_QUALITY = 11


def read_header(handle) -> tuple[int, int]:
    magic, version, stride, count = HEADER.unpack(handle.read(HEADER.size))
    if magic != MAGIC:
        raise SystemExit("not a NAND pack (bad magic)")
    if version != VERSION:
        raise SystemExit(f"unsupported pack version {version}")
    return stride, count


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pages-per-chunk", type=int,
                        default=DEFAULT_PAGES_PER_CHUNK)
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                        help="Brotli quality (11 = best, and slow: ~20 min "
                             "for a 215 MiB pack)")
    parser.add_argument("--base", default="chunks/",
                        help="URL prefix the emulator prepends to a chunk hash")
    parser.add_argument("--prefetch", type=Path,
                        help="prefetch.json from analyze-nand-trace.py "
                             "--prefetch; its chunk order is copied into the "
                             "manifest")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a non-empty output directory")
    args = parser.parse_args()

    chunks_dir = args.out / "chunks"
    if chunks_dir.exists() and any(chunks_dir.iterdir()) and not args.force:
        raise SystemExit(f"{chunks_dir} is not empty (use --force)")
    chunks_dir.mkdir(parents=True, exist_ok=True)

    with args.pack.open("rb") as handle:
        stride, count = read_header(handle)
        index = handle.read(count * 4)
        if len(index) != count * 4:
            raise SystemExit("truncated pack index")

        # The index file: what a browser downloads before it can boot at all.
        (args.out / "nand.pack.idx").write_bytes(
            HEADER.pack(MAGIC, VERSION, stride, count) + index)

        per_chunk = args.pages_per_chunk
        total_chunks = (count + per_chunk - 1) // per_chunk
        hashes: list[str] = []
        sizes: list[int] = []
        raw_total = 0
        stored_total = 0
        unique: dict[str, int] = {}

        for chunk in range(total_chunks):
            slots = min(per_chunk, count - chunk * per_chunk)
            block = handle.read(slots * stride)
            if len(block) != slots * stride:
                raise SystemExit(f"truncated pack payload at chunk {chunk}")
            digest = hashlib.sha256(block).hexdigest()
            raw_total += len(block)
            if digest in unique:
                # Content addressing: an identical chunk is stored once and
                # every reference to it is a cache hit after the first.
                sizes.append(sizes[unique[digest]])
            else:
                compressed = brotli.compress(block, quality=args.quality)
                (chunks_dir / digest).write_bytes(compressed)
                unique[digest] = chunk
                sizes.append(len(compressed))
                stored_total += len(compressed)
            hashes.append(digest)
            if chunk % 50 == 0 or chunk == total_chunks - 1:
                print(f"\r  chunk {chunk + 1}/{total_chunks} "
                      f"({len(unique)} unique, {stored_total / 1048576:.1f} MiB "
                      "stored)", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)

    (args.out / "chunk-hashes.bin").write_bytes(
        b"".join(bytes.fromhex(h) for h in hashes))
    # The emulator reads this instead of the JSON: two integers and a string
    # need no JSON parser inside the device model.
    (args.out / "chunk-config.txt").write_text(
        f"pagesPerChunk={per_chunk}\n"
        f"chunks={total_chunks}\n"
        f"entries={count}\n"
        f"stride={stride}\n"
        f"base={args.base}\n")

    prefetch: list[int] = []
    if args.prefetch:
        loaded = json.loads(args.prefetch.read_text())
        if loaded.get("pagesPerChunk") != per_chunk:
            raise SystemExit(
                f"prefetch list was computed for {loaded.get('pagesPerChunk')} "
                f"pages per chunk, not {per_chunk}; re-run "
                "analyze-nand-trace.py with --pages-per-chunk "
                f"{per_chunk}")
        prefetch = loaded["chunks"]

    prefetch_bytes = sum(sizes[c] for c in prefetch)
    manifest = {
        "format": "ipod-nand-chunks-v1",
        "pack": args.pack.name,
        "packSize": args.pack.stat().st_size,
        "stride": stride,
        "pages": count,
        "pagesPerChunk": per_chunk,
        "chunks": total_chunks,
        "uniqueChunks": len(unique),
        "base": args.base,
        "encoding": "br",
        "storedBytes": stored_total,
        "rawBytes": raw_total,
        "hashes": hashes,
        "sizes": sizes,
        "prefetch": prefetch,
        "prefetchBytes": prefetch_bytes,
    }
    (args.out / "chunk-manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")

    mb = 1048576
    print(f"pack          {raw_total / mb:.1f} MiB, {count:,} pages, "
          f"stride {stride}")
    print(f"chunks        {total_chunks:,} of {per_chunk} pages "
          f"({len(unique):,} unique, "
          f"{(1 - len(unique) / total_chunks) * 100:.1f}% deduplicated)")
    print(f"stored        {stored_total / mb:.1f} MiB brotli q{args.quality} "
          f"({stored_total / raw_total * 100:.1f}%)")
    if prefetch:
        print(f"prefetch      {len(prefetch):,} chunks = "
              f"{prefetch_bytes / mb:.1f} MiB -- what a cold boot downloads")
    print(f"index         {(args.out / 'nand.pack.idx').stat().st_size / mb:.2f}"
          " MiB (always downloaded)")
    print(f"output        {args.out}")

    if shutil.disk_usage(args.out).free < 512 * mb:
        print("warning: less than 512 MiB free on this volume", file=sys.stderr)


if __name__ == "__main__":
    main()
