#!/usr/bin/env python3
"""Generate the golden IPODNAND pack fixture used by tests/unit/test-nand-pack.

The real packs are 215-300 MiB and cannot live in git, but the pack-access seam
(hw/arm/ipod_touch_nand_pack.c) has to be provable: the browser serves records
from compressed chunks while native memcpy's out of a mapped file, and the two
must return the same bytes for the same virtual page.

So this writes a TINY pack -- same layout, same 2,112-byte stride -- with
deterministic pseudo-random content, plus a golden.json of per-VPN SHA-256
digests. Regenerating it must be a no-op; if the fixture changes, either the
generator or the layout changed, and that is exactly what the test should
notice.

    scripts/wasm/make-pack-fixture.py            # rewrite tests/data/nand-pack
    scripts/wasm/make-pack-fixture.py --check    # verify it is up to date
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

MAGIC = b"IPODNAND"
VERSION = 1
BYTES_PER_PAGE = 2048
BYTES_PER_SPARE = 64
STRIDE = BYTES_PER_PAGE + BYTES_PER_SPARE
NUM_BANKS = 8
HEADER = struct.Struct("<8sIII")
U32 = struct.Struct("<I")

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "data" / "nand-pack"

# A sparse, deliberately irregular set of (bank, page) pairs: the pack only
# carries pages that exist, so gaps in the VPN space are the normal case and
# the lookup has to return "absent" rather than a neighbouring page.
PAGES = [(bank, page) for page in (0, 1, 2, 5, 17, 64, 65, 1023)
         for bank in (0, 3, 7)]


def record(vpn: int) -> bytes:
    """Deterministic 2,112-byte record, distinct for every VPN."""
    out = bytearray()
    counter = 0
    while len(out) < STRIDE:
        out += hashlib.sha256(f"ipodnand-fixture:{vpn}:{counter}".encode()).digest()
        counter += 1
    del out[STRIDE:]
    # Make the page/spare boundary visible: a mis-sized stride shows up as a
    # digest mismatch rather than as subtly shifted data.
    out[BYTES_PER_PAGE:BYTES_PER_PAGE + 4] = b"SPRE"
    return bytes(out)


def build() -> tuple[bytes, dict]:
    vpns = sorted(page * NUM_BANKS + bank for bank, page in PAGES)
    body = b"".join(record(vpn) for vpn in vpns)
    pack = HEADER.pack(MAGIC, VERSION, STRIDE, len(vpns))
    pack += b"".join(U32.pack(vpn) for vpn in vpns)
    pack += body

    golden = {
        "stride": STRIDE,
        "pagesPerChunk": 5,          # deliberately not a divisor of len(vpns)
        "entries": [
            {"vpn": vpn, "sha256": hashlib.sha256(record(vpn)).hexdigest()}
            for vpn in vpns
        ],
        # VPNs the pack does NOT carry, including ones inside its range.
        # (VPN = page * 8 + bank, so page 0 contributes 0, 3 and 7 -- the
        # absent set has to be derived, not guessed.)
        "absent": sorted(set(range(0, 24)) - set(vpns)) + [100, 8185, 8192,
                                                          0xFFFFFFFF],
    }
    return pack, golden


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed fixture differs")
    args = parser.parse_args()

    pack, golden = build()
    pack_path = FIXTURE_DIR / "tiny.pack"
    golden_path = FIXTURE_DIR / "golden.json"
    # The index file is what a chunked browser boot downloads up front: header
    # plus one u32 per page, no payload.
    index_path = FIXTURE_DIR / "tiny.pack.idx"
    index = pack[:HEADER.size + len(golden["entries"]) * 4]
    golden_text = json.dumps(golden, indent=2) + "\n"

    if args.check:
        for path, expected in ((pack_path, pack), (index_path, index),
                               (golden_path, golden_text.encode())):
            if not path.exists() or path.read_bytes() != expected:
                sys.exit(f"fixture out of date: {path}")
        print(f"fixture up to date ({len(pack):,} B, "
              f"{len(golden['entries'])} pages)")
        return

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes(pack)
    index_path.write_bytes(index)
    golden_path.write_text(golden_text)
    print(f"wrote {pack_path} ({len(pack):,} B, {len(golden['entries'])} pages)")
    print(f"wrote {index_path} ({len(index):,} B)")
    print(f"wrote {golden_path}")


if __name__ == "__main__":
    main()
