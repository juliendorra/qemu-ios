#!/usr/bin/env python3
"""Fixture tests for compare-s5l8900-service-matching.py."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("compare-s5l8900-service-matching.py")
SPEC = importlib.util.spec_from_file_location("compare_service_matching", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def event(kind: str, references: str, vtable: str = "0xc0190000") -> str:
    return (
        f"M68AP_FTL_TRACE service_match event={kind} pc=0xc0130000 "
        f"object=0xc0800000 vtable={vtable} references={references} "
        "state=0x0000001e iterator=0xc0900000 "
        "iterator_references=0x00000001\n")


class CompareServiceMatchingTests(unittest.TestCase):
    def write_log(self, profile: str, third_references: str, panic: bool) -> Path:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        with handle:
            handle.write(f"M68AP_FTL_TRACE kernel_profile={profile}\n")
            handle.write(
                f"M68AP_FTL_TRACE usb_device_start profile={profile} "
                "pc=0xc04c0000\n")
            handle.write(event("wait_entry", "0x00000000"))
            handle.write(event("enumeration_entry", "0x00000000"))
            handle.write(event("candidate", "0x00100012"))
            handle.write(event("candidate", "0x000c000d"))
            handle.write(event("candidate", third_references))
            handle.write(event("candidate", "0x00060008"))
            handle.write(event("matched", "0x00060008"))
            handle.write(event("iterator_release", "0x00060008"))
            if panic:
                handle.write("M68AP_FTL_TRACE pre_ftl_panic=0xc0019790\n")
            else:
                handle.write(event("enumeration_return", "0x00050006"))
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_finds_first_reference_divergence_and_matched_parity(self) -> None:
        n45 = MODULE.parse_first_sequence(
            self.write_log("n45ap", "0x00130016", False))
        m68 = MODULE.parse_first_sequence(
            self.write_log("m68ap", "0x00010002", True))
        result = MODULE.compare(n45, m68)
        self.assertEqual(result["comparison"]["first_divergence"]["ordinal"], 3)
        self.assertTrue(result["comparison"]["matched_reference_parity"])
        self.assertTrue(n45["enumeration_returned"])
        self.assertEqual(m68["panic"], "0xc0019790")


if __name__ == "__main__":
    unittest.main()
