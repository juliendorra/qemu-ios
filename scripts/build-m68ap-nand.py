#!/usr/bin/env python3
"""Construct an iPhone-2G (M68AP) sparse NAND page tree.

This reimplements the documented S5L8900 Whimory on-flash structures used by the
historical iPod-Touch-1G NAND generator (devos50/qemu-ios-generate-nand, tag
`it1g_nand_filesystem`, no explicit upstream license -- structures reimplemented
here with attribution, not copied), specialised for iPhone 2G (M68AP) iBoot
204.3.14.

The M68AP form uses its FIL "AND driver" signature, a production BBT, and four
active NAND banks. With the controller identification handshake modeled
correctly, M68AP iBoot reports four banks / 512 pages per subblock and uses the
same four-bank layout as the 4A102 kernel. N45AP remains the proven eight-bank
reference.

Output: a fresh bank0..bank7/*.page tree plus a JSON provenance sidecar. Never
mutates an installed or source NAND. A user-supplied decrypted root HFS+ image
may be placed via --hfs; without it the tree carries metadata only, which is
sufficient to pass WMR init (the narrow first milestone). Optionally packs the
result with pack-ipod-nand.py after the sparse tree validates.

See IPHONE_2G_BRINGUP_HANDOFF.md and AGENTS.md (firmware/artifact policy).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import firmware_profiles

# --- S5L8900 NAND geometry -----------------------------------------------------
BANKS = 8
M68AP_ACTIVE_BANKS = 4
PAGES_PER_BANK = 524288
BYTES_PER_PAGE = 2048
BYTES_PER_SPARE = 64
PAGES_PER_BLOCK = 128
PAGES_PER_SUBLOCK = 1024
BYTES_PER_SECTOR = 512

# --- Whimory constants (from wmr.h / ftl.h / vfl.h in the reference generator) --
WMR_MAX_VB = 18000
WMR_MAX_RESERVED_SIZE = 820
BAD_MARK_COMPRESS_SIZE = 8
VFL_BAD_MARK_INFO_TABLE_SIZE = WMR_MAX_VB // 8 // BAD_MARK_COMPRESS_SIZE  # 281
VFL_INFO_SECTION_SIZE = 4
VFL_CTX_SPARE_TYPE = 0x80

FTL_CXT_SECTION_SIZE = 3
FTL_SPARE_TYPE_CXT_INDEX = 0x43
FREE_SECTION_SIZE = 20
FREE_LIST_SIZE = 3
FREE_SECTION_START = FTL_CXT_SECTION_SIZE
LOG_SECTION_SIZE = FREE_SECTION_SIZE - FREE_LIST_SIZE
FTL_CXT_SECTION_START = 201
FTL_CTX_VBLK_IND = 0

# MAX_NUM_OF_MAP_TABLES = ceil(WMR_MAX_VB / (4*512)) * sizeof(uint16_t)
_sectors = 4 * BYTES_PER_SECTOR  # WMR_SECTORS_PER_PAGE_MIN * WMR_SECTOR_SIZE
_ceil_vb = WMR_MAX_VB // _sectors + (1 if WMR_MAX_VB % _sectors else 0)  # 9
MAX_NUM_OF_MAP_TABLES = _ceil_vb * 2   # 18   (u32 entries)
MAX_NUM_OF_EC_TABLES = _ceil_vb * 4    # 36   (u32 entries)
_logcxt_bytes = LOG_SECTION_SIZE * 0x100 * BANKS * 2  # WMR_MAX_PAGES_PER_BLOCK=0x100
MAX_NUM_OF_LOGCXT_MAPS = (_logcxt_bytes // _sectors +
                          (1 if _logcxt_bytes % _sectors else 0))  # 34
LOGCXT_ENTRY_SIZE = 20  # sizeof(LOGCxt), packed

# FIL "AND driver" signature words (iBoot compares bank0/page0 word0 to these)
SIG_M68AP = 0x43303033  # "300C"  iPhone 2G  iBoot-204.3.14
SIG_N45AP = 0x43303032  # "200C"  iPod Touch 1G  (regression reference only)

# GPT / MBR
GPT_HDR_SIG = b"EFI PART"
GPT_HDR_REVISION = 0x00010000
MBR_ADDRESS = 0x1BE
ROOT_PARTITION_FIRST_PAGE = 3  # LBA0=MBR, LBA1=GPT hdr, LBA2=entry array


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def crc32_ieee(buf: bytes) -> int:
    import zlib
    return zlib.crc32(buf) & 0xFFFFFFFF


class NandTree:
    """Collects sparse physical pages before flushing them to disk."""

    def __init__(self):
        # (bank, page) -> (data 2048, spare 64)
        self.pages: dict[tuple[int, int], tuple[bytes, bytes]] = {}
        self.page_links: dict[tuple[int, int], Path] = {}

    def write_page(self, bank: int, page_index: int, data: bytes | None,
                   spare: bytes | None) -> None:
        data = bytes(data or b"") .ljust(BYTES_PER_PAGE, b"\x00")[:BYTES_PER_PAGE]
        spare = bytes(spare or b"").ljust(BYTES_PER_SPARE, b"\x00")[:BYTES_PER_SPARE]
        key = (bank, page_index)
        if key in self.pages or key in self.page_links:
            raise SystemExit(f"duplicate physical page bank{bank}/{page_index}")
        self.pages[key] = (data, spare)

    def link_page(self, bank: int, page_index: int, source: Path) -> None:
        key = (bank, page_index)
        if key in self.pages or key in self.page_links:
            raise SystemExit(f"duplicate physical page bank{bank}/{page_index}")
        if source.stat().st_size != BYTES_PER_PAGE + BYTES_PER_SPARE:
            raise SystemExit(f"invalid reusable NAND page size: {source}")
        self.page_links[key] = source

    def flush(self, out: Path) -> int:
        for bank in range(BANKS):
            (out / f"bank{bank}").mkdir(parents=True, exist_ok=True)
        for (bank, page_index), (data, spare) in self.pages.items():
            with open(out / f"bank{bank}" / f"{page_index}.page", "wb") as fh:
                fh.write(data)
                fh.write(spare)
        for (bank, page_index), source in self.page_links.items():
            os.link(source, out / f"bank{bank}" / f"{page_index}.page")
        return len(self.pages) + len(self.page_links)


def get_physical_address(vpn: int, active_banks: int = BANKS) -> tuple[int, int]:
    """Virtual page number -> (bank, physical page).

    M68AP interleaves four active banks and N45AP interleaves eight. The sparse
    storage format always reserves eight bank directories.
    """
    bank = vpn % active_banks
    pages_per_subblock = active_banks * PAGES_PER_BLOCK
    pbi = vpn // pages_per_subblock
    pib = (vpn // active_banks) % PAGES_PER_BLOCK
    return bank, pbi * PAGES_PER_BLOCK + pib


# --- metadata builders (byte layouts verified against the N45AP tree) ----------

def build_fil_signature_page(signature: int) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    struct.pack_into("<I", page, 0, signature)
    return bytes(page)


def build_vfl_context_page() -> tuple[bytes, bytes]:
    """VFLMeta (2048) + VFLSpare (64). Layout of the it1g VFLCxt:
      +0    u32 dwGlobalCxtAge
      +4    u16 aFTLCxtVbn[3]
      +10   u16 wPadding
      +12   u32 dwCxtAge
      +16   u16 wCxtLocation, wNextCxtPOffset
      +20   u16 wNumOfInitBadBlk, wNumOfWriteFail, wNumOfEraseFail
      +26   u16 wBadMapTableMaxIdx, wReservedSecStart, wReservedSecSize
      +32   u16 aBadMapTable[820]        (1640 bytes -> +1672)
      +1672 u8  aBadMark[281]            (-> +1953, 1 pad byte -> +1954)
      +1954 u16 awInfoBlk[4]             (-> +1962)
      +1962 u16 wBadMapTableScrubIdx     (-> +1964 = sizeof VFLCxt)
    then abReserved[72], dwVersion, dwCheckSum, dwXorSum -> 2048 total.
    it1g sets awInfoBlk[0]=35, aBadMark[*]=0xff, aFTLCxtVbn=0; leaves the rest 0.
    """
    page = bytearray(BYTES_PER_PAGE)
    # aBadMark[281] = 0xff
    for i in range(VFL_BAD_MARK_INFO_TABLE_SIZE):
        page[1672 + i] = 0xFF
    # awInfoBlk[0] = 35
    struct.pack_into("<H", page, 1954, 35)
    # aFTLCxtVbn[0..2] = FTL_CTX_VBLK_IND (0) -- already zero
    spare = bytearray(BYTES_PER_SPARE)
    struct.pack_into("<I", spare, 0, 1)          # dwCxtAge = 1
    spare[9] = VFL_CTX_SPARE_TYPE                 # bSpareType = 0x80
    return bytes(page), bytes(spare)


def build_bbt_page(production: bool) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    page[0:16] = b"DEVICEINFOBBT\x00\x00\x00"
    if production:
        # Production ("all blocks good") BBT, needed by M68AP's Whimory2_1
        # VFL_Init (the it1g zero-fill marks every block bad, so VFL_Open's
        # context scan finds nothing and fails at _LoadVFLCxt line 768).
        # Page layout decoded from m68ap iBoot-204.3.14's loader at 0x18015fa0:
        # it memcmp()s the first 0x10 bytes against "DEVICEINFOBBT", then does
        # memmove(dst, page + 0x38, *(uint32 *)(page + 0x34)) — +0x34 is the
        # BBT byte count and +0x38 the bitmap (1 bit per block, 1 = good).
        # An earlier full-page 0xFF fill therefore put 0xFFFFFFFF in the count
        # and made that memmove read past the iBoot RAM window: the
        # post-FTL_Init Data Abort (DFAR=0x18100000). Only the bitmap may be
        # 0xFF; the count must be the real bitmap size and the rest zeros,
        # matching the N45AP page shape (marker + zeros, count 0).
        bbt_len = (PAGES_PER_BANK // PAGES_PER_BLOCK) // 8  # 4096 blocks -> 0x200
        struct.pack_into("<I", page, 0x34, bbt_len)
        page[0x38:0x38 + bbt_len] = b"\xFF" * bbt_len
    return bytes(page)


def build_ftl_meta_page() -> bytes:
    """FTLMeta: FTLCxt2 then abReserved, dwVersion, dwVersionNot -> 2048.
    Mirrors the reference field writes (free-VB list, empty logs, map-table
    pointers). The trailing dwVersion/dwVersionNot sit at the end of the page."""
    page = bytearray(BYTES_PER_PAGE)
    # FTLCxt2 is NOT a packed struct, so u32 members take 4-byte alignment.
    # Offsets (verified byte-for-byte against the N45AP FTL meta page):
    #  +0   u32 dwAge
    #  +4   u32 dwWriteAge
    #  +8   u16 wNumOfFreeVb
    #  +10  u16 wFreeVbListTail
    #  +12  u16 wWearLevelCounter
    #  +14  u16 awFreeVbList[20]                    (40 bytes -> +54)
    #  [+2 pad to 4-byte alignment -> +56]
    #  +56  u32 adwMapTablePtrs[18]                 (72  -> +128)
    #  +128 u32 adwECTablePtrs[36]                  (144 -> +272)
    #  +272 u32 adwLOGCxtMapPtrs[34]                (136 -> +408)
    #  +408 u32 pawMapTable, pawECCacheTable, pawLOGCxtMapTable (12 -> +420)
    #  +420 LOGCxt aLOGCxtTable[LOG_SECTION_SIZE+1] (20 each; wVbn at +4)
    # dwVersion / dwVersionNot occupy the last 8 bytes of the page.
    struct.pack_into("<H", page, 8, FREE_SECTION_SIZE)          # wNumOfFreeVb
    for i in range(FREE_SECTION_SIZE):
        struct.pack_into("<H", page, 14 + i * 2, FREE_SECTION_START + i)

    map_ptrs_off = _align_up(14 + FREE_SECTION_SIZE * 2, 4)      # 56
    for i in range(MAX_NUM_OF_MAP_TABLES):
        struct.pack_into("<I", page, map_ptrs_off + i * 4, i + 1)

    ec_ptrs_off = map_ptrs_off + MAX_NUM_OF_MAP_TABLES * 4       # 128
    logcxt_map_off = ec_ptrs_off + MAX_NUM_OF_EC_TABLES * 4      # 272
    cache_ptrs_off = logcxt_map_off + MAX_NUM_OF_LOGCXT_MAPS * 4  # 408
    logcxt_tbl_off = cache_ptrs_off + 3 * 4                      # 420
    for i in range(LOG_SECTION_SIZE + 1):
        struct.pack_into("<H", page, logcxt_tbl_off + i * LOGCXT_ENTRY_SIZE + 4,
                         0xFFFF)                                 # aLOGCxtTable[i].wVbn

    # FTLCtrlBlock[3] (+0x312) and FTLCtrlPage (+0x318). Field names and
    # offsets from openiBoot's plat-s5l8900/includes/s5l8900/ftl.h, whose
    # struct FTLCxt matches this page field-for-field.
    #
    # Leaving these zero is what made a written NAND unbootable. FTLCtrlPage is
    # the FTL's APPEND CURSOR into the control block: it saves a new context at
    # ++FTLCtrlPage. At zero, the very first save -- which iPhone OS 1.0 does
    # on its way into sleep -- lands at page 1 of the block, straight over the
    # 18 mapping tables written just below, and the FTL meta at the block's
    # last page still points its adwMapTablePtrs at them. The next cold boot
    # then reads a context header where it expects a mapping table: 1.0 wedges
    # in iBoot with no serial output at all, 1.1.4 gets through FTL init and
    # panics. Parking the cursor past the tables the format actually wrote
    # sends the first save to page 19, where it belongs.
    #
    # FTLCtrlBlock is the set of blocks the FTL rotates between when one fills;
    # all-zero named block 0 three times over.
    struct.pack_into("<3H", page, 0x312, FTL_CXT_SECTION_START,
                     FTL_CXT_SECTION_START + 1, FTL_CXT_SECTION_START + 2)
    struct.pack_into("<I", page, 0x318, MAX_NUM_OF_MAP_TABLES)

    struct.pack_into("<I", page, BYTES_PER_PAGE - 8, 0x46560000)
    struct.pack_into("<i", page, BYTES_PER_PAGE - 4, -0x46560001)
    return bytes(page)


def build_ftl_mapping_page(table_index: int) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    for j in range(1024):  # BYTES_PER_PAGE / sizeof(uint16_t)
        struct.pack_into("<H", page, j * 2, (table_index * 1024) + j + 1)
    return bytes(page)


def build_gpt_entry_page(partitions: list[tuple[int, int]]) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    for index, (lba_start, lba_end) in enumerate(partitions):
        off = index * 0x80
        # gpt_ent.ent_type[4] (Apple HFS GUID, reference-generator order)
        struct.pack_into("<I", page, off + 0, 0x48465300)
        struct.pack_into("<I", page, off + 4, 0x11AA0000)
        struct.pack_into("<I", page, off + 8, 0x300011AA)
        struct.pack_into("<I", page, off + 12, 0xACEC4365)
        # ent_uuid[16] at +16, then ent_lba_start(u64)+ ent_lba_end(u64)
        struct.pack_into("<Q", page, off + 32, lba_start)
        struct.pack_into("<Q", page, off + 40, lba_end)
    return bytes(page)


def build_gpt_header_page(entry_page: bytes, partition_count: int) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    page[0:8] = GPT_HDR_SIG
    struct.pack_into("<I", page, 8, GPT_HDR_REVISION)          # hdr_revision
    struct.pack_into("<I", page, 12, 0x5C)                     # hdr_size = 92
    # hdr_lba_table at +72 (matches gpt_hdr layout in gpt.h)
    struct.pack_into("<Q", page, 72, 2)                        # hdr_lba_table
    struct.pack_into("<I", page, 80, partition_count)         # hdr_entries
    struct.pack_into("<I", page, 84, 0x80)                     # hdr_entsz
    struct.pack_into("<I", page, 88,
                     crc32_ieee(entry_page[:partition_count * 0x80]))
    struct.pack_into("<I", page, 16, crc32_ieee(bytes(page[:0x5C])))  # hdr_crc_self
    return bytes(page)


def build_mbr_page(disk_partition_size: int) -> bytes:
    page = bytearray(BYTES_PER_PAGE)
    off = MBR_ADDRESS
    page[off + 4] = 0xEE                                       # sysid
    struct.pack_into("<I", page, off + 8, ROOT_PARTITION_FIRST_PAGE)
    struct.pack_into("<I", page, off + 12, disk_partition_size)
    page[510] = 0x55
    page[511] = 0xAA
    return bytes(page)


def valid_ftl_spare() -> bytes:
    # VFLSpare: dwCxtAge[0:4], dwReserved[4:8], cStatusMark[8], bSpareType[9],
    # eccMarker[10]. The it1g generate_nand.c sets ONLY eccMarker=0xFF on data
    # pages; the kernel-formatted N45AP data-block spares match (spare[10]=0xFF,
    # spare[8]=0x00). A previous value of 0x00FF00FF at offset 8 also set
    # cStatusMark (spare[8]) to 0xFF, diverging from the reference.
    spare = bytearray(BYTES_PER_SPARE)
    spare[10] = 0xFF  # eccMarker
    return bytes(spare)


# --- top-level construction ----------------------------------------------------

def build(tree: NandTree, signature: int, hfs_path: Path | None,
          data_hfs_path: Path | None,
          production_bbt: bool, active_banks: int = BANKS,
          reuse_hfs_pages_from: Path | None = None) -> dict:
    populated = {}
    if active_banks not in (M68AP_ACTIVE_BANKS, BANKS):
        raise SystemExit(f"unsupported active bank count: {active_banks}")
    # NB: do NOT gate the bank count on the signature word. The signature is
    # firmware-keyed and the interleave is board-keyed, and they cross: iPhone
    # OS 1.1.1 on M68AP carries the iPod's 0x43303032 signature but still needs
    # the four-bank M68AP interleave. main() enforces the board rule.
    pages_per_subblock = active_banks * PAGES_PER_BLOCK

    # 1. FIL signature (bank0/page0)
    tree.write_page(0, 0, build_fil_signature_page(signature), None)
    populated["fil_signature"] = {"bank": 0, "page": 0,
                                  "word0": f"0x{signature:08x}"}

    # 2. BBT: first page of the last physical block on every bank
    bbt_page_index = PAGES_PER_BANK - PAGES_PER_BLOCK  # 524160
    bbt = build_bbt_page(production_bbt)
    for bank in range(active_banks):
        tree.write_page(bank, bbt_page_index, bbt, None)
    populated["bbt"] = {"page_index": bbt_page_index,
                        "banks": active_banks}

    # 3. VFL context: physical block 35, page 0 (page index 35*128=4480) per bank
    vfl_page, vfl_spare = build_vfl_context_page()
    vfl_page_index = 35 * PAGES_PER_BLOCK
    for bank in range(active_banks):
        tree.write_page(bank, vfl_page_index, vfl_page, vfl_spare)
    populated["vfl_context"] = {"page_index": vfl_page_index,
                                "banks": active_banks}

    # 4. FTL context
    #    (a) CTX-index spare marker on first page of the FTL CXT block
    cxt_spare = bytearray(BYTES_PER_SPARE)
    cxt_spare[9] = FTL_SPARE_TYPE_CXT_INDEX
    cxt_spare[10] = 0xFF  # eccMarker
    bank, pn = get_physical_address(
        (FTL_CXT_SECTION_START + FTL_CTX_VBLK_IND) * pages_per_subblock,
        active_banks)
    tree.write_page(bank, pn, None, bytes(cxt_spare))
    populated["ftl_cxt_index"] = {"bank": bank, "page": pn}

    #    (b) logical->virtual mapping pages
    mapping = []
    for i in range(MAX_NUM_OF_MAP_TABLES):
        bank, pn = get_physical_address(
            FTL_CXT_SECTION_START * pages_per_subblock + i + 1,
            active_banks)
        tree.write_page(bank, pn, build_ftl_mapping_page(i), None)
        mapping.append({"bank": bank, "page": pn})
    populated["ftl_mapping_pages"] = mapping

    #    (c) FTL meta on the last page of the FTL CXT block
    meta_spare = bytearray(BYTES_PER_SPARE)
    meta_spare[9] = FTL_SPARE_TYPE_CXT_INDEX
    bank, pn = get_physical_address(
        (FTL_CXT_SECTION_START + FTL_CTX_VBLK_IND + 1) *
        pages_per_subblock - 1, active_banks)
    meta_page = build_ftl_meta_page()
    tree.write_page(bank, pn, meta_page, bytes(meta_spare))
    populated["ftl_meta"] = {"bank": bank, "page": pn}
    # 5. Optional filesystem payload (GPT/MBR/HFS). Not required for WMR init.
    if hfs_path is not None:
        populated["filesystem"] = _write_filesystem(
            tree, hfs_path, active_banks, reuse_hfs_pages_from,
            data_hfs_path)
    elif data_hfs_path is not None:
        raise SystemExit("--data-hfs requires --hfs")
    else:
        populated["filesystem"] = None

    return populated


def _write_filesystem(tree: NandTree, hfs_path: Path,
                      active_banks: int,
                      reuse_hfs_pages_from: Path | None = None,
                      data_hfs_path: Path | None = None) -> dict:
    size = hfs_path.stat().st_size
    if size % BYTES_PER_PAGE:
        raise SystemExit(
            f"HFS image size {size} is not a multiple of {BYTES_PER_PAGE}")
    pages_for_boot = size // BYTES_PER_PAGE

    spare = valid_ftl_spare()
    pages_per_subblock = active_banks * PAGES_PER_BLOCK
    base = (FTL_CXT_SECTION_START + 1) * pages_per_subblock
    with open(hfs_path, "rb") as fh:
        vpn = base + ROOT_PARTITION_FIRST_PAGE
        for index in range(pages_for_boot):
            bank, pn = get_physical_address(vpn, active_banks)
            data = fh.read(BYTES_PER_PAGE)
            if reuse_hfs_pages_from is None:
                tree.write_page(bank, pn, data, spare)
            else:
                old_base = (FTL_CXT_SECTION_START + 1) * PAGES_PER_SUBLOCK
                old_vpn = old_base + ROOT_PARTITION_FIRST_PAGE + index
                old_bank, old_pn = get_physical_address(old_vpn, BANKS)
                source = (reuse_hfs_pages_from / f"bank{old_bank}" /
                          f"{old_pn}.page")
                source_bytes = source.read_bytes()
                if source_bytes[:BYTES_PER_PAGE] != data or \
                        source_bytes[BYTES_PER_PAGE:] != spare:
                    raise SystemExit(
                        f"reusable page does not match HFS/spare: {source}")
                tree.link_page(bank, pn, source)
            vpn += 1

    partitions = [
        (ROOT_PARTITION_FIRST_PAGE,
         ROOT_PARTITION_FIRST_PAGE + pages_for_boot - 1),
    ]
    pages_for_data = 0
    if data_hfs_path is not None:
        data_size = data_hfs_path.stat().st_size
        if data_size % BYTES_PER_PAGE:
            raise SystemExit(
                f"data HFS image size {data_size} is not a multiple of "
                f"{BYTES_PER_PAGE}")
        pages_for_data = data_size // BYTES_PER_PAGE
        data_first_page = partitions[0][1] + 1
        with open(data_hfs_path, "rb") as fh:
            for index in range(pages_for_data):
                bank, pn = get_physical_address(
                    base + data_first_page + index, active_banks)
                tree.write_page(bank, pn, fh.read(BYTES_PER_PAGE), spare)
        partitions.append(
            (data_first_page, data_first_page + pages_for_data - 1))

    # GPT partition-entry array (LBA2)
    entry = build_gpt_entry_page(partitions)
    bank, pn = get_physical_address(base + 2, active_banks)
    tree.write_page(bank, pn, entry, spare)
    # GPT header (LBA1)
    bank, pn = get_physical_address(base + 1, active_banks)
    tree.write_page(
        bank, pn, build_gpt_header_page(entry, len(partitions)), spare)
    # MBR (LBA0)
    bank, pn = get_physical_address(base, active_banks)
    tree.write_page(
        bank, pn, build_mbr_page(partitions[-1][1] -
                                 ROOT_PARTITION_FIRST_PAGE + 1), spare)

    return {"hfs": str(hfs_path), "hfs_pages": pages_for_boot,
            "boot_partition_first_page": ROOT_PARTITION_FIRST_PAGE,
            "data_hfs": str(data_hfs_path) if data_hfs_path else None,
            "data_hfs_pages": pages_for_data,
            "data_partition_first_page": (
                partitions[1][0] if len(partitions) > 1 else None),
            "partition_count": len(partitions),
            "active_banks": active_banks,
            "reused_hfs_pages_from": (str(reuse_hfs_pages_from)
                                      if reuse_hfs_pages_from else None)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True,
                        help="output NAND directory (bank0..bank7 created here)")
    parser.add_argument("--signature", default=None,
                        help="override the FIL signature: 'm68ap' (0x43303033), "
                             "'n45ap' (0x43303032), or a raw 0x-hex word. "
                             "Default: taken from --ipsw-build's profile, which "
                             "is the correct source -- the signature is keyed to "
                             "the FIRMWARE, not the board (1.1.1 on M68AP uses "
                             "the iPod's 0x43303032)")
    parser.add_argument("--hfs", type=Path, default=None,
                        help="decrypted root HFS+ image (optional; not needed to "
                             "pass WMR init)")
    parser.add_argument("--data-hfs", type=Path, default=None,
                        help="optional writable HFS+ image for iPhone disk0s2 "
                             "(/private/var)")
    parser.add_argument("--active-banks", type=int, choices=(4, 8), default=None,
                        help="override filesystem/FTL interleave (default: "
                             "M68AP=4, N45AP=8)")
    parser.add_argument("--reuse-hfs-pages-from", type=Path, default=None,
                        help="eight-bank staged NAND carrying the same HFS; "
                             "verify and hard-link its immutable data pages")
    parser.add_argument("--ipsw-hash", default=None,
                        help="SHA-256 of the source IPSW, recorded in provenance")
    # Selects the firmware profile that supplies the FIL signature, and is
    # recorded in provenance. Required: the signature is firmware-keyed, and a
    # tree built under the wrong one fails WMR init without saying why.
    firmware_profiles.add_build_argument(parser)
    parser.add_argument("--device", default=None,
                        help="source device, recorded in provenance (default: "
                             "from the build profile)")
    parser.add_argument("--bbt", choices=("production", "zero", "auto"),
                        default="auto",
                        help="BBT fill: 'production' (0xFF, needed by M68AP), "
                             "'zero' (it1g/N45AP byte-exact), or 'auto' (by "
                             "the profile's board)")
    parser.add_argument("--pack", action="store_true",
                        help="also run pack-ipod-nand.py after the tree validates")
    parser.add_argument("--recipe", default=None,
                        help="name of the recipe that produced the input "
                             "filesystems, recorded in provenance. Without it "
                             "a tree cannot be told apart from one built with "
                             "an unpatched root, which boots to a BLACK SCREEN "
                             "rather than failing.")
    args = parser.parse_args()

    profile = firmware_profiles.get(args.build)

    # The FIL signature belongs to the FIRMWARE (000C for 1.0/1.0.x, 200C for
    # 1.1.1 and the iPod, 300C for 1.1.4); the bank topology and BBT style
    # belong to the BOARD (M68AP: four active banks + production BBT; N45AP:
    # eight banks + the it1g byte-exact zero BBT). Keying both off the
    # signature word conflated them and made 1.1.1-on-M68AP unbuildable.
    if args.signature is None:
        signature = profile.fil_signature
    elif args.signature == "m68ap":
        signature = SIG_M68AP
    elif args.signature == "n45ap":
        signature = SIG_N45AP
    else:
        signature = int(args.signature, 0)

    is_n45ap = profile.board == "n45ap"

    if args.bbt == "auto":
        production_bbt = not is_n45ap
    else:
        production_bbt = args.bbt == "production"

    active_banks = (args.active_banks if args.active_banks is not None else
                    (BANKS if is_n45ap else M68AP_ACTIVE_BANKS))
    if is_n45ap and active_banks != BANKS:
        raise SystemExit("N45AP construction requires --active-banks 8")

    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to write into non-empty directory: {out}")
    out.mkdir(parents=True, exist_ok=True)

    tree = NandTree()
    if args.reuse_hfs_pages_from is not None and args.hfs is None:
        raise SystemExit("--reuse-hfs-pages-from requires --hfs")
    populated = build(tree, signature, args.hfs, args.data_hfs, production_bbt,
                      active_banks, args.reuse_hfs_pages_from)
    count = tree.flush(out)

    manifest = {
        "constructor": "build-m68ap-nand.py",
        "constructor_revision": _git_rev(),
        "geometry": {
            "active_banks": active_banks, "storage_bank_slots": BANKS,
            "pages_per_bank": PAGES_PER_BANK,
            "bytes_per_page": BYTES_PER_PAGE, "bytes_per_spare": BYTES_PER_SPARE,
            "pages_per_block": PAGES_PER_BLOCK,
            "pages_per_sublock": active_banks * PAGES_PER_BLOCK,
        },
        "signature_word": f"0x{signature:08x}",
        "signature_ascii": struct.pack("<I", signature).decode("latin1"),
        "bbt_fill": "production_0xff" if production_bbt else "zero",
        "metadata_versions": {
            "vfl": "it1g", "ftl_dwVersion": "0x46560000",
            "fil_and_driver": f"0x{signature:08x}",
            "bank_geometry": f"{active_banks}-bank-interleave",
            "geometry_status": "proven-board-layout",
        },
        "populated_pages": populated,
        "page_count": count,
        "source": {
            "kind": "ipsw", "device": args.device or profile.device,
            "build": profile.build,
            "ipsw_sha256": args.ipsw_hash,
            "hfs_sha256": sha256_file(args.hfs) if args.hfs else None,
            "data_hfs_sha256": (
                sha256_file(args.data_hfs) if args.data_hfs else None),
        },
        "recipe": args.recipe,
        "guest_file_modifications": (
            "none (metadata-only)" if args.hfs is None
            else ("root and data HFS+ partitions placed; GPT/MBR synthesised"
                  if args.data_hfs else
                  "root HFS+ placed at boot partition; GPT/MBR synthesised")),
    }
    manifest_path = out / "nand-provenance.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {count} pages to {out}")
    print(f"signature word 0x{signature:08x} "
          f"({manifest['signature_ascii']!r}) at bank0/0.page")
    print(f"provenance: {manifest_path}")

    if args.pack:
        import subprocess
        script = Path(__file__).with_name("pack-ipod-nand.py")
        subprocess.run(["python3", str(script), str(out)], check=True)


def _git_rev() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            text=True).strip()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    main()
