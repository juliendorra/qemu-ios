#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("compare-s5l8900-startup.py")
SPEC = importlib.util.spec_from_file_location("startup_compare", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class StartupCompareTests(unittest.TestCase):
    def test_parses_milestones_and_focus(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serial.log"
            path.write_text(
                "AppleS5L8900XGPIOIC::start(gpio) <1>\n"
                "AppleS5L8900XSDIO::start(sdio) <1>\n"
                "BSD root: disk0s1, major 14, minor 1\n"
                "launchd[1]: BOOT_TIME: 1 2\n"
                "Configuring SpringBoard for N45AP\n"
            )
            parsed = MODULE.parse_serial(path)
        self.assertEqual(parsed["service_starts"], [
            "AppleS5L8900XGPIOIC", "AppleS5L8900XSDIO"])
        self.assertTrue(parsed["milestones"]["root"])
        self.assertTrue(parsed["milestones"]["launchd"])
        self.assertTrue(parsed["milestones"]["springboard"])
        self.assertEqual(len(parsed["focus_events"]), 2)

    def test_reports_ordered_difference(self):
        left = {"service_starts": ["GPIO", "SDIO", "USB"]}
        right = {"service_starts": ["GPIO", "Baseband", "SDIO"]}
        result = MODULE.compare(left, right)
        self.assertGreaterEqual(result["shared_start_count"], 2)
        self.assertTrue(result["differences"])


if __name__ == "__main__":
    unittest.main()
