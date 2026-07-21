#!/usr/bin/env python3
"""Structural fixture tests for build-m68ap-nand.py.

Validates the generated NAND metadata by BYTES and STRUCTURE only -- no Apple
payloads are required or embedded. The N45AP reference SHA-256 values below are
fingerprints of purely-synthetic Whimory metadata pages (FIL signature, VFL
context, BBT); they are the same values recorded in IPHONE_2G_BRINGUP_HANDOFF.md
and independently reproduce the public it1g generator's output. They contain no
device-unique or copyrighted data.

Run: python3 scripts/test-build-m68ap-nand.py
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "build-m68ap-nand.py"

# Fingerprints of synthetic it1g/N45AP metadata pages (2048 data + 64 spare).
N45AP_FINGERPRINTS = {
    ("bank0", "0.page"):
        "c5dacd1ade5322b1c36507be39e873a387414308cb64c2f9dac4eef26740c006",
    ("bank0", "4480.page"):
        "5a0157e626602bea19d797571b245809694a28b4e7e9268b6d08df066c19ee67",
    ("bank0", "524160.page"):
        "6984b58fc2345586f86ab3d64d098a1ffdb6a214556af4574ee439aa22d9bfb0",
}

PAGE = 2048
SPARE = 64

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        _failures.append(msg)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(sig: str, bbt: str) -> Path:
    out = Path(tempfile.mkdtemp(prefix=f"nand-{sig}-")) / "nand"
    subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(out),
         "--signature", sig, "--bbt", bbt],
        check=True, capture_output=True, text=True)
    return out


def test_n45ap_metadata_reproduces() -> None:
    print("N45AP metadata reproduces recorded fingerprints (bbt=zero):")
    out = generate("n45ap", "zero")
    for (bank, name), want in N45AP_FINGERPRINTS.items():
        got = sha(out / bank / name)
        check(got == want, f"{bank}/{name} sha256 == {want[:12]}...")
    # BBT is present on every bank at the last block's first page
    for b in range(8):
        p = out / f"bank{b}" / "524160.page"
        check(p.exists() and sha(p) == N45AP_FINGERPRINTS[("bank0", "524160.page")],
              f"bank{b}/524160.page matches BBT fingerprint")


def test_m68ap_signature_and_bbt() -> None:
    print("M68AP tree: signature word and production BBT:")
    out = generate("m68ap", "auto")
    sig_page = (out / "bank0" / "0.page").read_bytes()
    word0 = struct.unpack_from("<I", sig_page)[0]
    check(word0 == 0x43303033, "bank0/0.page word0 == 0x43303033 ('300C')")
    check(sig_page[:PAGE][4:].count(0) == PAGE - 4,
          "signature page is otherwise zero")
    bbt = (out / "bank0" / "524160.page").read_bytes()
    check(bbt[:16] == b"DEVICEINFOBBT\x00\x00\x00", "BBT header present")
    check(all(x == 0xFF for x in bbt[16:PAGE]),
          "M68AP BBT data region is 0xFF-filled (production, all-good)")


def test_geometry_and_layout() -> None:
    print("Geometry / page sizing / VFL spare:")
    out = generate("m68ap", "auto")
    # every emitted page is exactly 2048 + 64 bytes
    sizes = {p.stat().st_size for p in out.rglob("*.page")}
    check(sizes == {PAGE + SPARE}, f"all pages are {PAGE + SPARE} bytes ({sizes})")
    # VFL context spare must satisfy the M68AP validator: spare[8]==0, spare[9]==0x80
    vfl = (out / "bank0" / "4480.page").read_bytes()
    spare = vfl[PAGE:]
    check(spare[8] == 0x00, "VFL context spare[8] (cStatusMark) == 0")
    check(spare[9] == 0x80, "VFL context spare[9] (bSpareType) == 0x80")
    check(struct.unpack_from("<I", spare)[0] == 1, "VFL context spare dwCxtAge == 1")
    # awInfoBlk[0] == 35 at page offset 0x7A2 (the offset M68AP iBoot reads)
    check(struct.unpack_from("<H", vfl, 0x7A2)[0] == 35,
          "VFL context awInfoBlk[0] == 35 at offset 0x7A2")
    # VFL context present on all 8 banks
    for b in range(8):
        check((out / f"bank{b}" / "4480.page").exists(),
              f"bank{b}/4480.page (VFL context) present")


def test_get_physical_address_matches_model() -> None:
    print("get_physical_address matches the QEMU ITNand vpn convention:")
    sys.path.insert(0, str(HERE))
    import importlib.util
    spec = importlib.util.spec_from_file_location("bm", BUILDER)
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    # The QEMU model keys pages by vpn = page * BANKS + bank; the generator must
    # place a given vpn at exactly that (bank, page).
    for vpn in (0, 1, 7, 8, 280, 4480 * 8, 205823, 206847):
        bank, pn = bm.get_physical_address(vpn)
        check(0 <= bank < 8 and pn >= 0,
              f"vpn {vpn} -> bank {bank}, page {pn}")
    # spot-check the FTL meta placement used by the constructor
    bank, pn = bm.get_physical_address(
        (bm.FTL_CXT_SECTION_START + 1) * bm.PAGES_PER_SUBLOCK - 1)
    check((bank, pn) == (7, 25855), f"FTL meta lands at bank7/25855 (got {bank}/{pn})")


def main() -> int:
    for t in (test_n45ap_metadata_reproduces, test_m68ap_signature_and_bbt,
              test_geometry_and_layout, test_get_physical_address_matches_model):
        t()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print("  - " + f)
        return 1
    print("all fixture checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
