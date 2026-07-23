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


def generate_with_hfs(sig: str, pages: int,
                      active_banks: int | None = None,
                      data_pages: int = 0) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"nand-{sig}-hfs-"))
    hfs = root / "fixture.img"
    hfs.write_bytes(b"".join(bytes([index]) * PAGE for index in range(pages)))
    out = root / "nand"
    command = [sys.executable, str(BUILDER), "--out", str(out),
               "--signature", sig, "--bbt", "auto", "--hfs", str(hfs)]
    if data_pages:
        data_hfs = root / "data-fixture.img"
        data_hfs.write_bytes(
            b"".join(bytes([0x80 + index]) * PAGE
                     for index in range(data_pages)))
        command.extend(["--data-hfs", str(data_hfs)])
    if active_banks is not None:
        command.extend(["--active-banks", str(active_banks)])
    subprocess.run(
        command,
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
    # Layout per m68ap iBoot-204.3.14's DEVICEINFOBBT loader (0x18015fa0):
    # +0x34 u32 BBT byte count, +0x38 bitmap (1 bit/block, 1 = good). A
    # full-page 0xFF fill corrupts the count (0xFFFFFFFF) and iBoot's
    # memmove Data Aborts after FTL_Init — the count MUST be the bitmap size.
    bbt_len = struct.unpack_from("<I", bbt, 0x34)[0]
    check(bbt_len == 0x200, "BBT count @0x34 == 0x200 (4096 blocks / 8)")
    check(all(x == 0xFF for x in bbt[0x38:0x38 + 0x200]),
          "BBT bitmap @0x38 is 0xFF-filled (production, all-good)")
    check(all(x == 0 for x in bbt[0x10:0x34]),
          "BBT header padding (0x10..0x34) is zero")
    check(all(x == 0 for x in bbt[0x238:PAGE]),
          "BBT tail after bitmap is zero")
    meta = out / "bank3" / "25855.page"
    check(meta.exists(), "M68AP FTL metadata is at bank3/25855")
    check(not (out / "bank7" / "25855.page").exists(),
          "M68AP has no inactive-bank FTL metadata copy")


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
    # M68AP iBoot reports and scans four active NAND banks.
    for b in range(4):
        check((out / f"bank{b}" / "4480.page").exists(),
              f"bank{b}/4480.page (VFL context) present")
    for b in range(4, 8):
        check(not (out / f"bank{b}" / "4480.page").exists(),
              f"bank{b}/4480.page absent outside M68AP active geometry")


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
    m68_subblock = bm.M68AP_ACTIVE_BANKS * bm.PAGES_PER_BLOCK
    bank, pn = bm.get_physical_address(
        (bm.FTL_CXT_SECTION_START + 1) * m68_subblock - 1,
        bm.M68AP_ACTIVE_BANKS)
    check((bank, pn) == (3, 25855),
          f"M68AP FTL meta lands at bank3/25855 (got {bank}/{pn})")


def test_m68ap_filesystem_uses_four_bank_interleave() -> None:
    print("M68AP four-bank filesystem interleave:")
    out = generate_with_hfs("m68ap", 16)
    page8 = (out / "bank3" / "25858.page").read_bytes()
    check(page8[:PAGE] == bytes([8]) * PAGE,
          "HFS page 8 is at bank3/25858")
    page4 = (out / "bank3" / "25857.page").read_bytes()
    check(page4[:PAGE] == bytes([4]) * PAGE,
          "bank3/25857 contains HFS page 4, not the N45AP page-8 placement")


def test_m68ap_can_hardlink_verified_eight_bank_pages() -> None:
    print("M68AP compact restaging verifies and hard-links HFS pages:")
    root = Path(tempfile.mkdtemp(prefix="nand-m68ap-reuse-"))
    hfs = root / "fixture.img"
    hfs.write_bytes(b"".join(bytes([index]) * PAGE for index in range(16)))
    source = root / "source"
    target = root / "target"
    subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(source),
         "--signature", "n45ap", "--bbt", "auto", "--hfs", str(hfs)],
        check=True, capture_output=True, text=True)
    subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(target),
         "--signature", "m68ap", "--bbt", "auto", "--hfs", str(hfs),
         "--reuse-hfs-pages-from", str(source), "--active-banks", "4"],
        check=True, capture_output=True, text=True)
    old_page8 = source / "bank3" / "25857.page"
    new_page8 = target / "bank3" / "25858.page"
    check(old_page8.stat().st_ino == new_page8.stat().st_ino,
          "four-bank HFS page 8 reuses the verified eight-bank page inode")


def test_m68ap_two_partition_layout() -> None:
    print("M68AP iPhone root + data partition layout:")
    out = generate_with_hfs("m68ap", 8, data_pages=4)
    sys.path.insert(0, str(HERE))
    import importlib.util
    spec = importlib.util.spec_from_file_location("bm_partitions", BUILDER)
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)
    base = ((bm.FTL_CXT_SECTION_START + 1) *
            bm.M68AP_ACTIVE_BANKS * bm.PAGES_PER_BLOCK)
    entry_bank, entry_page = bm.get_physical_address(
        base + 2, bm.M68AP_ACTIVE_BANKS)
    entries = (out / f"bank{entry_bank}" /
               f"{entry_page}.page").read_bytes()
    root_start, root_end = struct.unpack_from("<QQ", entries, 32)
    data_start, data_end = struct.unpack_from("<QQ", entries, 0x80 + 32)
    check((root_start, root_end) == (3, 10),
          "GPT entry 1 advertises disk0s1 root pages")
    check((data_start, data_end) == (11, 14),
          "GPT entry 2 advertises disk0s2 data pages")
    first_data_bank, first_data_page = bm.get_physical_address(
        base + data_start, bm.M68AP_ACTIVE_BANKS)
    first_data = (out / f"bank{first_data_bank}" /
                  f"{first_data_page}.page").read_bytes()
    check(first_data[:PAGE] == bytes([0x80]) * PAGE,
          "disk0s2 first HFS page uses four-bank interleave")


def main() -> int:
    for t in (test_n45ap_metadata_reproduces, test_m68ap_signature_and_bbt,
              test_geometry_and_layout, test_get_physical_address_matches_model,
              test_m68ap_filesystem_uses_four_bank_interleave,
              test_m68ap_can_hardlink_verified_eight_bank_pages,
              test_m68ap_two_partition_layout):
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
