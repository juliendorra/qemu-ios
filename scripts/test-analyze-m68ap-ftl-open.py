#!/usr/bin/env python3
"""Fixture-free unit tests for analyze-m68ap-ftl-open.py."""

from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("analyze-m68ap-ftl-open.py")
SPEC = importlib.util.spec_from_file_location("analyze_m68ap_ftl_open", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AnalyzeM68APFTLOpenTests(unittest.TestCase):
    def test_lzss_literal_run(self):
        self.assertEqual(MODULE.decode_lzss(b"\xffabcdefgh", 8), b"abcdefgh")

    def test_parse_segment_and_address_translation(self):
        header = struct.pack("<7I", MODULE.MH_MAGIC, 12, 6, 2, 1, 56, 0)
        command = struct.pack(
            "<II16sIIIIIIII", MODULE.LC_SEGMENT, 56,
            b"__PRELINK\0\0\0\0\0\0\0", 0xC029B000, 0x1000,
            0x100, 0x800, 7, 3, 0, 0)
        segments = MODULE.parse_segments(header + command)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].name, "__PRELINK")
        self.assertEqual(MODULE.file_to_va(segments, 0x180), 0xC029B080)
        self.assertEqual(MODULE.va_to_file(segments, 0xC029B080), 0x180)

    def test_ftl_meta_page_fields(self):
        page = bytearray(2112)
        struct.pack_into("<II", page, 0x7F8,
                         MODULE.FTL_VERSION, MODULE.FTL_VERSION_NOT)
        page[0x809] = 0x43
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ftl-meta.page"
            path.write_bytes(page)
            result = MODULE.inspect_ftl_meta(path)
        self.assertEqual(result["dwVersion_at_0x7f8"], "0x46560000")
        self.assertEqual(result["dwVersionNot_at_0x7fc"], "0xb9a9ffff")
        self.assertEqual(result["spare_type"], "0x43")


if __name__ == "__main__":
    unittest.main()
