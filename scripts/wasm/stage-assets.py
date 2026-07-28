#!/usr/bin/env python3
"""Stage a bootable firmware set for the browser build and hash it.

The browser app never guesses where firmware lives.  This tool takes explicit
source paths (or one packaged .app bundle), copies exactly the four artifacts
the machine needs into a content-described asset directory, and writes the
asset manifest the web shell consumes:

    web/public/assets/<set-id>/
        bootrom_s5l8900
        iboot.bin
        nor.bin
        nand.pack
    web/public/assets/<set-id>/asset-manifest.json

The manifest records size and SHA-256 for every artifact plus the machine
arguments needed to launch it, so the frontend has no board knowledge compiled
into it and a mismatched or truncated download is a hard error rather than a
mysterious boot failure.

Firmware is never committed: web/public/assets/ is git-ignored.

Examples:
    scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G (iOS 1.1.4).app" \\
        --board m68ap --firmware 1.1.4 --set-id m68ap-114-v1

    scripts/wasm/stage-assets.py --board m68ap --firmware 1.1.4 \\
        --bootrom .../bootrom_s5l8900 --iboot .../iboot_204_m68ap.bin \\
        --nor .../nor_m68ap.bin --nand-dir m68ap-artifacts/stage/nand-m68ap-fresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACK_TOOL = REPO / "scripts" / "pack-ipod-nand.py"

# Each board's QEMU machine type and the number of NAND banks its firmware
# expects.  Kept here (not in the frontend) so board facts live with the tools.
BOARDS = {
    "m68ap": {
        "machine": "iPhone-2G",
        "product_type": "iPhone1,1",
        "description": "iPhone 2G (M68AP), S5L8900",
    },
    "n45ap": {
        "machine": "iPod-Touch",
        "product_type": "iPod1,1",
        "description": "iPod touch 1G (N45AP), S5L8900",
    },
}

# Where a packaged .app keeps each board's artifacts, and the filenames used.
APP_LAYOUT = {
    "m68ap": ("iphone_files", "iboot_204_m68ap.bin", "nor_m68ap.bin"),
    "n45ap": ("ipod_files", "iboot_204_n45ap.bin", "nor_n45ap.bin"),
}

CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_from_app(app: Path, board: str) -> dict[str, Path]:
    subdir, iboot_name, nor_name = APP_LAYOUT[board]
    files = app / "Contents" / "Resources" / subdir
    if not files.is_dir():
        raise SystemExit(f"not a packaged emulator bundle: {files} is missing")
    return {
        "bootrom": files / "bootrom_s5l8900",
        "iboot": files / iboot_name,
        "nor": files / nor_name,
        "nand": files / "nand",
    }


def ensure_pack(nand: Path, work: Path) -> Path:
    """Return a nand.pack for `nand`, building one from a page tree if needed."""
    if nand.is_file():
        return nand
    if not nand.is_dir():
        raise SystemExit(f"NAND source not found: {nand}")
    existing = nand / "nand.pack"
    if existing.is_file():
        return existing
    work.parent.mkdir(parents=True, exist_ok=True)
    print(f"packing {nand} -> {work}")
    subprocess.run(
        [sys.executable, str(PACK_TOOL), str(nand), "--output", str(work)],
        check=True,
    )
    return work


def copy_if_changed(source: Path, destination: Path) -> None:
    if (
        destination.exists()
        and destination.stat().st_size == source.stat().st_size
        and sha256_of(destination) == sha256_of(source)
    ):
        print(f"  {destination.name}: unchanged")
        return
    print(f"  {destination.name}: copying {source}")
    shutil.copyfile(source, destination)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--board", choices=sorted(BOARDS), required=True)
    parser.add_argument(
        "--firmware", required=True,
        help="iPhone OS version this asset set boots, e.g. 1.1.4",
    )
    parser.add_argument(
        "--set-id",
        help="asset set directory name (default: <board>-<firmware>-v1)",
    )
    parser.add_argument(
        "--from-app", type=Path,
        help="packaged emulator .app to take all four artifacts from",
    )
    parser.add_argument("--bootrom", type=Path)
    parser.add_argument("--iboot", type=Path)
    parser.add_argument("--nor", type=Path)
    parser.add_argument(
        "--nand", type=Path, help="an existing nand.pack",
    )
    parser.add_argument(
        "--nand-dir", type=Path,
        help="a bank0..bankN page tree; packed with scripts/pack-ipod-nand.py",
    )
    parser.add_argument(
        "--out", type=Path, default=REPO / "web" / "public" / "assets",
        help="asset root (default: web/public/assets)",
    )
    parser.add_argument(
        "--machine-args", default="",
        help="extra -M options, e.g. 'epoch=2' (comma separated)",
    )
    args = parser.parse_args()

    board = BOARDS[args.board]
    set_id = args.set_id or f"{args.board}-{args.firmware.replace('.', '')}-v1"

    sources: dict[str, Path] = {}
    if args.from_app:
        sources = resolve_from_app(args.from_app.resolve(), args.board)
    for key, value in (
        ("bootrom", args.bootrom),
        ("iboot", args.iboot),
        ("nor", args.nor),
        ("nand", args.nand or args.nand_dir),
    ):
        if value is not None:
            sources[key] = value.resolve()

    missing = [key for key in ("bootrom", "iboot", "nor", "nand") if key not in sources]
    if missing:
        raise SystemExit(
            "missing source artifacts: " + ", ".join(missing) +
            "\npass them explicitly or use --from-app"
        )

    out = (args.out / set_id).resolve()
    out.mkdir(parents=True, exist_ok=True)

    pack = ensure_pack(sources["nand"], out / "nand.pack")

    print(f"staging {set_id} into {out}")
    staged = {
        "bootrom": ("bootrom_s5l8900", sources["bootrom"]),
        "iboot": ("iboot.bin", sources["iboot"]),
        "nor": ("nor.bin", sources["nor"]),
        "nand": ("nand.pack", pack),
    }
    assets = {}
    for key, (name, source) in staged.items():
        destination = out / name
        if source.resolve() != destination.resolve():
            copy_if_changed(source, destination)
        size = destination.stat().st_size
        digest = sha256_of(destination)
        assets[key] = {
            "url": f"./{name}",
            "size": size,
            "sha256": digest,
            "format": "ipod-nand-pack-v1" if key == "nand" else "raw",
        }
        print(f"  {name}: {size} bytes, sha256 {digest[:16]}...")

    manifest = {
        "schemaVersion": 1,
        "assetSet": set_id,
        "board": args.board.upper(),
        "machine": board["machine"],
        "productType": board["product_type"],
        "description": f"{board['description']}, iPhone OS {args.firmware}",
        "firmware": args.firmware,
        "delivery": "bundled",
        # The frontend builds its QEMU argv from this, so board/firmware
        # knowledge stays in the manifest rather than in JavaScript.
        "machineOptions": [
            option for option in args.machine_args.split(",") if option
        ],
        "assets": assets,
    }
    manifest_path = out / "asset-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
