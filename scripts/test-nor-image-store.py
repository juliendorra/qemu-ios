#!/usr/bin/env python3
"""Fixture tests for nor-image-store.py. No Apple payloads required.

Every NOR here is synthesised in-memory from zero-filled bodies, so this runs
anywhere. What is being tested is the WALK -- the thing that actually broke:
that a store is judged by whether iBoot can reach each image via the +0x18
stride, not by whether the images are physically present.

Run: python3 scripts/test-nor-image-store.py
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "nor-image-store.py"

STORE_BASE = 0x10400
ALIGN = 0x40
HDR = 0x400
NOR_SIZE = 0x100000
STRIDE_UNSET = 0xFFFFFFFF

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def align_up(v: int, a: int) -> int:
    return (v + a - 1) & ~(a - 1)


def make_header(img_type: str, data_len: int, stride_units: int,
                epoch: int = 3, flags2: int = 0x01000000,
                crc_valid: bool = True) -> bytes:
    hdr = bytearray(HDR)
    hdr[0:4] = b"2gmI"
    hdr[4:8] = img_type.encode()[::-1]
    struct.pack_into("<H", hdr, 0x0a, epoch)
    struct.pack_into("<I", hdr, 0x10, data_len)
    struct.pack_into("<I", hdr, 0x14, data_len)
    struct.pack_into("<I", hdr, 0x18, stride_units)
    struct.pack_into("<I", hdr, 0x1c, flags2)
    crc = zlib.crc32(bytes(hdr[:0x64])) & 0xFFFFFFFF
    struct.pack_into("<I", hdr, 0x64, crc if crc_valid else crc ^ 0xFFFFFFFF)
    return bytes(hdr)


def build_nor(images, fill_stride: bool = True, tmp: Path = None) -> Path:
    """images: [(type, data_len)] laid out 0x40-packed from STORE_BASE."""
    nor = bytearray(b"\xFF" * NOR_SIZE)
    off = STORE_BASE
    for img_type, data_len in images:
        container_len = HDR + data_len
        stride = align_up(container_len, ALIGN)
        units = (stride // ALIGN) if fill_stride else STRIDE_UNSET
        nor[off:off + HDR] = make_header(img_type, data_len, units)
        off = off + stride
    path = Path(tempfile.mkstemp(suffix=".bin", dir=tmp)[1])
    path.write_bytes(bytes(nor))
    return path


def run(path: Path, *extra) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, str(TOOL), str(path), "--json", *extra],
                       capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}


SEVEN = [("dtre", 0x8be8), ("batC", 0x101e1), ("logo", 0x1c3a),
         ("nsrv", 0x4695), ("batl", 0xc829), ("batL", 0xe9d2),
         ("recm", 0xb594)]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 1. A correctly strided store: all seven reachable, no problems.
        good = build_nor(SEVEN, fill_stride=True, tmp=tmp)
        rc, r = run(good, "--check", "--expect", "7")
        check("good store: 7 reachable", r.get("reachable_count") == 7,
              str(r.get("reachable_count")))
        check("good store: no problems", r.get("problems") == [],
              str(r.get("problems")))
        check("good store: --check exits 0", rc == 0, f"rc={rc}")
        check("good store: types in order",
              [i["type"] for i in r["reachable"]] == [t for t, _ in SEVEN])

        # 2. The actual bug: stride left at 0xFFFFFFFF. All seven are PRESENT,
        #    exactly one is REACHABLE. This is the case that shipped.
        bad = build_nor(SEVEN, fill_stride=False, tmp=tmp)
        rc, r = run(bad, "--check", "--expect", "7")
        check("unset stride: only 1 reachable", r.get("reachable_count") == 1,
              str(r.get("reachable_count")))
        check("unset stride: still 7 present", r.get("present_count") == 7,
              str(r.get("present_count")))
        check("unset stride: logo reported hidden",
              "logo" in r.get("hidden_types", []), str(r.get("hidden_types")))
        check("unset stride: --check exits 1", rc == 1, f"rc={rc}")
        check("unset stride: names the field",
              any("0xFFFFFFFF" in p for p in r.get("problems", [])),
              str(r.get("problems")))

        # 3. A stride that lands inside the image's own data is caught even
        #    though the walk still finds a header at the target.
        nor = bytearray(b"\xFF" * NOR_SIZE)
        nor[STORE_BASE:STORE_BASE + HDR] = make_header("dtre", 0x8000, 0x20)
        short = tmp / "short.bin"
        short.write_bytes(bytes(nor))
        rc, r = run(short, "--check")
        check("short stride: flagged",
              any("shorter than" in p for p in r.get("problems", [])),
              str(r.get("problems")))

        # 4. A corrupt header CRC is reported but does not stop the walk --
        #    enumeration does not check it, and pretending otherwise would
        #    mislead the next investigation.
        nor = bytearray(b"\xFF" * NOR_SIZE)
        stride = align_up(HDR + 0x1000, ALIGN)
        nor[STORE_BASE:STORE_BASE + HDR] = make_header(
            "dtre", 0x1000, stride // ALIGN, crc_valid=False)
        off2 = STORE_BASE + stride
        nor[off2:off2 + HDR] = make_header("logo", 0x1000, stride // ALIGN)
        badcrc = tmp / "badcrc.bin"
        badcrc.write_bytes(bytes(nor))
        rc, r = run(badcrc, "--check")
        check("bad CRC: reported", any("CRC" in p for p in r.get("problems", [])),
              str(r.get("problems")))
        check("bad CRC: walk still reaches image 2",
              r.get("reachable_count") == 2, str(r.get("reachable_count")))

        # 5. --expect enforces the count even when the walk itself is clean.
        rc, r = run(good, "--check", "--expect", "8")
        check("--expect mismatch exits 1", rc == 1, f"rc={rc}")

        # 6. --reference compares the reachable type list.
        rc, r = run(bad, "--reference", str(good))
        check("--reference detects mismatch",
              r.get("reference", {}).get("matches") is False,
              str(r.get("reference")))

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
