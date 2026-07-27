#!/usr/bin/env python3
"""Move legacy M68AP artifacts into the canonical per-build layout.

The tree grew one directory per firmware, each shaped differently
(`stage/` = 1.1.4, `stage-1.0/`, `extracted-1.0.2/`, IPSWs loose at the top),
with the version encoded in filenames. `scripts/m68ap_paths.py` defines one
shape for every build instead; this moves what already exists into it.

Everything is a RENAME, never a copy: these are hundred-megabyte trees and the
whole point is to not need the disk space twice. The only exception is the boot
ROM, which is 64 KiB and shared by every build.

Nothing is overwritten. A destination that already exists is reported and
skipped, so re-running after a partial migration is safe.

NAND directories are verified before moving: each carries a
`nand-provenance.json` naming the build it was generated for, and a tree whose
provenance disagrees with its destination is refused rather than filed under
the wrong firmware.

Usage:
    scripts/migrate-m68ap-artifacts.py            # dry run: print the plan
    scripts/migrate-m68ap-artifacts.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import m68ap_paths as layout  # noqa: E402

ARTIFACTS = layout.ARTIFACTS

# (source, destination, expected NAND build or None)
#
# Sources that do not exist are simply skipped: not every checkout has every
# build staged.
MOVES: list[tuple[str, str, str | None]] = [
    # --- iPhone OS 1.1.4 -------------------------------------------------
    ("stage/filesystem-m68ap-readonly.img", "builds/4A102/root.img", None),
    ("stage/data-m68ap.dmg", "builds/4A102/data.dmg", None),
    ("stage/iboot_204_m68ap.bin", "builds/4A102/iboot.bin", None),
    ("stage/iboot_204_m68ap_sbpatch.bin", "builds/4A102/iboot-sb.bin", None),
    ("stage/nor_m68ap.bin", "builds/4A102/nor.bin", None),
    # The product NAND is the FULL tree; `stage/nand` is a 37-page
    # metadata-only diagnostic that predates it and keeps its own name.
    ("stage/nand-m68ap-fresh", "builds/4A102/nand", "4A102"),
    ("stage/nand", "builds/4A102/nand-metadata-only", "4A102"),
    ("iPhone1,1_1.1.4_4A102_Restore.ipsw",
     "builds/4A102/ipsw/iPhone1,1_1.1.4_4A102_Restore.ipsw", None),
    ("extracted", "builds/4A102/ipsw/extracted", None),

    # --- iPhone OS 1.0 ---------------------------------------------------
    ("stage-1.0/filesystem-m68ap-readonly.img", "builds/1A543a/root.img", None),
    ("stage-1.0/iboot_159_m68ap_sbpatch.bin", "builds/1A543a/iboot-sb.bin", None),
    ("stage-1.0/nor_m68ap.bin", "builds/1A543a/nor.bin", None),
    ("stage-1.0/nand", "builds/1A543a/nand", "1A543a"),
    ("iPhone1,1_1.0_1A543a_Restore.ipsw",
     "builds/1A543a/ipsw/iPhone1,1_1.0_1A543a_Restore.ipsw", None),
    ("extracted-1.0", "builds/1A543a/ipsw/extracted", None),

    # --- iPhone OS 1.0.2 -------------------------------------------------
    ("iPhone1,1_1.0.2_1C28_Restore.ipsw",
     "builds/1C28/ipsw/iPhone1,1_1.0.2_1C28_Restore.ipsw", None),
    ("extracted-1.0.2", "builds/1C28/ipsw/extracted", None),

    # --- iPhone OS 1.1.1 -------------------------------------------------
    ("iPhone1,1_1.1.1_3A109a_Restore.ipsw",
     "builds/3A109a/ipsw/iPhone1,1_1.1.1_3A109a_Restore.ipsw", None),
]

# Copied, not moved: small and genuinely shared by every build.
COPIES: list[tuple[str, str]] = [
    ("appdbg/bootrom_s5l8900", "shared/bootrom_s5l8900"),
]

# Left alone on purpose, with the reason shown in the report.
KEPT = {
    "appdbg": "debug run directory, not a build input",
    "stage-full": "old full-boot experiment",
    "nand-m68ap": "pre-pipeline NAND experiment",
    "nand-n45ap-check": "iPod cross-check, not an M68AP build",
    "unpacked": "ad-hoc IPSW unpacking scratch",
    "ipsw": "ad-hoc IPSW unpacking scratch",
    "nor_m68ap.bin": "loose copy; the per-build NOR is authoritative",
}


def nand_build_of(path: Path) -> str | None:
    provenance = path / "nand-provenance.json"
    if not provenance.is_file():
        return None
    try:
        return json.loads(provenance.read_text()).get("source", {}).get("build")
    except (ValueError, OSError):
        return None


def human(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true",
                        help="perform the moves (default: print the plan)")
    args = parser.parse_args()

    if not ARTIFACTS.is_dir():
        raise SystemExit(f"no artifact directory at {ARTIFACTS}")

    planned: list[tuple[Path, Path, bool]] = []
    problems: list[str] = []

    for source_name, destination_name, expect_build in MOVES:
        source = ARTIFACTS / source_name
        destination = ARTIFACTS / destination_name
        if not source.exists():
            continue
        if destination.exists():
            print(f"  skip   {destination_name} already exists")
            continue
        if expect_build is not None:
            found = nand_build_of(source)
            if found is None:
                problems.append(
                    f"{source_name}: no nand-provenance.json; refusing to file "
                    f"it as {expect_build}")
                continue
            if found != expect_build:
                problems.append(
                    f"{source_name}: provenance says build {found}, but the "
                    f"destination is {expect_build}")
                continue
        planned.append((source, destination, False))

    for source_name, destination_name in COPIES:
        source = ARTIFACTS / source_name
        destination = ARTIFACTS / destination_name
        if not source.exists() or destination.exists():
            continue
        planned.append((source, destination, True))

    if problems:
        print("\nrefusing to migrate:")
        for problem in problems:
            print(f"  ! {problem}")

    if not planned:
        print("\nnothing to migrate")
    else:
        print(f"\n{'applying' if args.apply else 'planned'} "
              f"({len(planned)} items):")
        for source, destination, copy in planned:
            verb = "copy" if copy else "move"
            print(f"  {verb}   {source.relative_to(ARTIFACTS)}"
                  f"  ->  {destination.relative_to(ARTIFACTS)}"
                  f"  ({human(size_of(source))})")
            if not args.apply:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if copy:
                shutil.copy2(source, destination)
            else:
                source.rename(destination)

    print("\nleft in place:")
    for name, why in KEPT.items():
        if (ARTIFACTS / name).exists():
            print(f"  {name}: {why}")

    if not args.apply and planned:
        print("\nthis was a dry run; re-run with --apply")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
