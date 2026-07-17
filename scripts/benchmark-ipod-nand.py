#!/usr/bin/env python3
"""Compare cold boot from legacy page files and an optional NAND base pack."""

import argparse
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time


MILESTONES = {
    "kernel": b"Darwin Kernel Version",
    "touch": b"downloaded 49128 bytes of firmware data",
    "springboard": b"Configuring SpringBoard for N45AP",
}


def run_trial(args: argparse.Namespace, mode: str, trial: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ipod-nand-{mode}-", dir="/private/tmp") as temporary:
        nand = Path(temporary) / "nand"
        log = Path(temporary) / "serial.log"
        subprocess.run(["cp", "-cR", str(args.nand), str(nand)], check=True)
        if mode == "legacy":
            (nand / "nand.pack").unlink(missing_ok=True)

        command = [
            str(args.qemu),
            "-M", (
                f"iPod-Touch,bootrom={args.firmware / 'bootrom_s5l8900'},"
                f"iboot={args.firmware / 'iboot_204_n45ap.bin'},nand={nand}"
            ),
            "-m", "1G",
            "-pflash", str(args.firmware / "nor_n45ap.bin"),
            "-L", str(args.pc_bios),
            "-display", "none",
            "-serial", f"file:{log}",
            "-monitor", "none",
        ]
        started = time.monotonic()
        process = subprocess.Popen(command)
        found = {}
        serial = b""
        offset = 0
        try:
            while process.poll() is None and time.monotonic() - started < args.timeout:
                if log.exists():
                    with log.open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read()
                    offset += len(chunk)
                    serial += chunk
                    elapsed = time.monotonic() - started
                    for name, marker in MILESTONES.items():
                        if name not in found and marker in serial:
                            found[name] = round(elapsed, 3)
                if "springboard" in found:
                    break
                time.sleep(0.025)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if "springboard" not in found:
            tail = serial[-4000:].decode(errors="replace")
            raise RuntimeError(f"{mode} trial {trial} failed:\n{tail}")
        result = {"mode": mode, "trial": trial, "seconds": found}
        print(json.dumps(result), flush=True)
        return result


def main() -> None:
    default_app = Path("/Applications/iPod Touch.app/Contents")
    parser = argparse.ArgumentParser()
    parser.add_argument("nand", type=Path)
    parser.add_argument("--qemu", type=Path, default=Path("build-release/qemu-system-arm"))
    parser.add_argument("--firmware", type=Path, default=default_app / "Resources/ipod_files")
    parser.add_argument("--pc-bios", type=Path, default=default_app / "Resources/pc-bios")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.nand = args.nand.resolve()
    args.qemu = args.qemu.resolve()

    results = []
    for trial in range(1, args.trials + 1):
        order = ("legacy", "packed") if trial % 2 else ("packed", "legacy")
        for mode in order:
            results.append(run_trial(args, mode, trial))
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
