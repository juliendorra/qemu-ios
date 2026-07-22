#!/usr/bin/env python3

import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("extract-hfs-from-nand.py")
SPEC = importlib.util.spec_from_file_location("extract_hfs", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ExtractHfsTests(unittest.TestCase):
    def test_extracts_volume_declared_by_header(self):
        active_banks = 8
        first_vpn = ((MODULE.FTL_CXT_SECTION_START + 1) *
                     active_banks * MODULE.PAGES_PER_BLOCK +
                     MODULE.BOOT_PARTITION_FIRST_PAGE)
        pages = []
        for index in range(4):
            data = bytearray([index] * MODULE.DATA_SIZE)
            if index == 0:
                data[1024:1026] = b"HX"
                struct.pack_into(">I", data, 1024 + 40, 4096)
                struct.pack_into(">I", data, 1024 + 44, 2)
            pages.append((first_vpn + index,
                          bytes(data) + bytes(64)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack = root / "nand.pack"
            with pack.open("wb") as destination:
                destination.write(MODULE.PACK_HEADER.pack(
                    MODULE.PACK_MAGIC, MODULE.PACK_VERSION,
                    MODULE.PACK_PAGE_SIZE, len(pages)))
                for vpn, _ in pages:
                    destination.write(struct.pack("<I", vpn))
                for _, page in pages:
                    destination.write(page)
            output = root / "root.hfs"
            reader = MODULE.PackedPages(pack)
            try:
                result = MODULE.extract(reader, output, active_banks)
            finally:
                reader.close()
            self.assertEqual(result["output_size"], 8192)
            self.assertEqual(output.read_bytes()[:16], bytes(16))
            self.assertEqual(output.read_bytes()[2048:2064], bytes([1]) * 16)


if __name__ == "__main__":
    unittest.main()
