#!/usr/bin/env python3
"""Fixture tests for inspect-apple-device-tree.py."""

from __future__ import annotations

import importlib.util
import struct
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("inspect-apple-device-tree.py")
SPEC = importlib.util.spec_from_file_location("inspect_apple_dt", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def prop(name: str, value: bytes) -> bytes:
    key = name.encode().ljust(32, b"\0")
    return key + struct.pack("<I", len(value)) + value.ljust(
        (len(value) + 3) & ~3, b"\0")


class DeviceTreeTests(unittest.TestCase):
    def test_resolves_function_parent(self) -> None:
        parent = (struct.pack("<II", 2, 0) + prop("name", b"gpio\0") +
                  prop("AAPL,phandle", struct.pack("<I", 0x4040e0)))
        child = (struct.pack("<II", 2, 0) + prop("name", b"sdio\0") +
                 prop("function-device_reset",
                      struct.pack("<I", 0x4040e0) + b"GPIO"))
        root = struct.pack("<II", 1, 2) + prop("name", b"device-tree\0")
        nodes = MODULE.parse_tree(root + parent + child)
        by_phandle = {node["phandle"]: node["path"] for node in nodes
                      if node["phandle"] is not None}
        self.assertEqual(by_phandle[0x4040e0], "/device-tree/gpio")
        value = nodes[2]["properties"]["function-device_reset"]
        self.assertEqual(MODULE.u32(value, 0), 0x4040e0)

    def test_rejects_truncated_tree(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            MODULE.parse_tree(struct.pack("<II", 1, 0))


if __name__ == "__main__":
    unittest.main()
