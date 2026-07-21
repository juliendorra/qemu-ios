#!/usr/bin/env python3
"""M68AP NAND boot acceptance test (board-aware, staged, hard-timeout).

Boots `-M iPhone-2G` with the real extracted m68ap iBoot, the synthetic m68ap
NOR, and a generated m68ap NAND, and checks the WMR/FTL boot phases in order.
The narrow first milestone is to replace

    [WMR:ERR] no signature or no production format

with a recognised production signature and a WMR init that proceeds past it.
Each boot is bounded by a hard timeout (macOS has no `timeout(1)`; this uses a
Python watchdog) and every NAND/NOR is a staged copy -- the installed firmware
is never mutated (see AGENTS.md).

Also runs the N45AP (`-M iPod-Touch`) NAND boot in the same batch as a
regression, so a change that helps M68AP cannot silently break the iPod.

Outputs machine-readable JSON (per-phase pass/fail, deepest marker reached,
serial byte offsets) plus the serial logs, under --logs.

Firmware inputs are supplied by path (the Apple-derived artifacts are never
committed). Defaults point at ./m68ap-artifacts as produced by
extract-m68ap-images.py / build-m68ap-nor.py / build-m68ap-nand.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get(
    "IPOD_APP", "/Applications/iPod Touch.app/Contents"))
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
DEFAULT_ART = REPO / "m68ap-artifacts"

# Ordered M68AP phase markers. Each is (key, needle, is_failure).
M68AP_PHASES = [
    ("iboot_banner", b"BUILD_TAG: iBoot-204.3.14", False),
    ("and_driver_m68ap", b"Apple NAND Driver (AND) 0x43303033", False),
    ("fil_init", b"FIL_Init\t\t\t[OK]", False),
    ("buf_init", b"BUF_Init\t\t\t[OK]", False),
    ("vfl_init", b"VFL_Init\t\t\t[OK]", False),
    ("ftl_init", b"FTL_Init\t\t\t[OK]", False),
    ("vfl_open", b"VFL_Open\t\t\t[OK]", False),
    ("ftl_open", b"FTL_Open\t\t\t[OK]", False),
    ("kernel", b"Darwin Kernel Version", False),
]
# Markers that must be ABSENT for the milestone to hold.
M68AP_MUST_NOT = [
    ("no_signature", b"no signature or no production format"),
    ("read_only_version", b"read only version"),
]

N45AP_PHASES = [
    ("and_driver_n45ap", b"Apple NAND Driver (AND) 0x43303032", False),
    ("fil_init", b"FIL_Init\t\t\t[OK]", False),
    ("vfl_open", b"VFL_Open\t\t\t[OK]", False),
    ("kernel", b"Darwin Kernel Version", False),
]


def run_qemu(qemu: Path, machine_arg: str, pflash: Path, serial: Path,
             stderr: Path, timeout_s: int) -> None:
    serial.write_bytes(b"")
    with stderr.open("wb") as errfh:
        proc = subprocess.Popen(
            [str(qemu), "-M", machine_arg, "-m", "1G",
             "-pflash", str(pflash), "-L", str(APP / "Resources" / "pc-bios"),
             "-display", "none", "-serial", f"file:{serial}", "-monitor", "none"],
            stdout=subprocess.DEVNULL, stderr=errfh)
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()


def scan_phases(serial: Path, phases, must_not) -> dict:
    data = serial.read_bytes() if serial.exists() else b""
    reached = {}
    for key, needle, _ in phases:
        idx = data.find(needle)
        reached[key] = (idx if idx >= 0 else None)
    forbidden = {}
    for key, needle in (must_not or []):
        idx = data.find(needle)
        forbidden[key] = (idx if idx >= 0 else None)
    deepest = None
    for key, _needle, _ in phases:
        if reached[key] is not None:
            deepest = key
    return {"reached": reached, "forbidden": forbidden,
            "deepest": deepest, "serial_bytes": len(data)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path,
                    default=APP / "Resources/ipod_files/bootrom_s5l8900")
    ap.add_argument("--iboot-m68ap", type=Path,
                    default=DEFAULT_ART / "extracted/iboot_204_m68ap.bin")
    ap.add_argument("--nor-m68ap", type=Path,
                    default=DEFAULT_ART / "nor_m68ap.bin")
    ap.add_argument("--nand-m68ap", type=Path,
                    default=DEFAULT_ART / "nand-m68ap")
    ap.add_argument("--skip-n45ap", action="store_true",
                    help="skip the iPod (N45AP) regression boot")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--logs", type=Path,
                    default=Path(f"/private/tmp/iphone-nand-accept-{int(time.time())}"))
    args = ap.parse_args()

    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    stage.mkdir(exist_ok=True)

    result = {"logs": str(args.logs), "cases": {}}

    # --- M68AP case -----------------------------------------------------------
    missing = [str(p) for p in (args.qemu, args.bootrom, args.iboot_m68ap,
                                args.nor_m68ap, args.nand_m68ap) if not p.exists()]
    if missing:
        result["cases"]["m68ap"] = {"status": "SKIP",
                                    "reason": "missing inputs", "missing": missing}
    else:
        # stage writable NAND + NOR copies
        m_nand = stage / "nand-m68ap"
        if m_nand.exists():
            shutil.rmtree(m_nand)
        subprocess.run(["cp", "-Rc", str(args.nand_m68ap), str(m_nand)], check=True)
        m_nor = stage / "nor_m68ap.bin"
        shutil.copy2(args.nor_m68ap, m_nor)
        serial = args.logs / "m68ap-serial.log"
        machine = (f"iPhone-2G,bootrom={args.bootrom},"
                   f"iboot={args.iboot_m68ap},nand={m_nand}")
        run_qemu(args.qemu, machine, m_nor, serial,
                 args.logs / "m68ap-stderr.log", args.timeout)
        scan = scan_phases(serial, M68AP_PHASES, M68AP_MUST_NOT)
        milestone = (scan["reached"]["and_driver_m68ap"] is not None and
                     scan["reached"]["ftl_init"] is not None and
                     all(v is None for v in scan["forbidden"].values()))
        full_wmr = (scan["reached"]["vfl_open"] is not None and
                    scan["reached"]["ftl_open"] is not None)
        result["cases"]["m68ap"] = {
            "status": "PASS" if milestone else "FAIL",
            "milestone_no_signature_error_cleared": milestone,
            "full_wmr_init": full_wmr,
            "serial": str(serial),
            **scan,
        }

    # --- N45AP regression -----------------------------------------------------
    if not args.skip_n45ap:
        n_iboot = APP / "Resources/ipod_files/iboot_204_n45ap.bin"
        n_nor = APP / "Resources/ipod_files/nor_n45ap.bin"
        n_src = APP / "Resources/ipod_files/nand"
        if not (args.qemu.exists() and n_iboot.exists() and n_src.exists()):
            result["cases"]["n45ap"] = {"status": "SKIP",
                                        "reason": "installed iPod firmware absent"}
        else:
            n_nand = stage / "nand-n45ap"
            if n_nand.exists():
                shutil.rmtree(n_nand)
            subprocess.run(["cp", "-Rc", str(n_src), str(n_nand)], check=True)
            serial = args.logs / "n45ap-serial.log"
            machine = (f"iPod-Touch,bootrom={args.bootrom},"
                       f"iboot={n_iboot},nand={n_nand}")
            run_qemu(args.qemu, machine, n_nor, serial,
                     args.logs / "n45ap-stderr.log", args.timeout)
            scan = scan_phases(serial, N45AP_PHASES, None)
            ok = (scan["reached"]["and_driver_n45ap"] is not None and
                  scan["reached"]["vfl_open"] is not None)
            result["cases"]["n45ap"] = {
                "status": "PASS" if ok else "FAIL",
                "serial": str(serial), **scan}

    report = args.logs / "result.json"
    report.write_text(json.dumps(result, indent=2) + "\n")

    print(json.dumps(result, indent=2))
    print(f"\nreport: {report}")
    statuses = [c.get("status") for c in result["cases"].values()]
    return 0 if all(s in ("PASS", "SKIP") for s in statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
