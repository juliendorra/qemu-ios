#!/usr/bin/env python3
"""One artifact layout for every iPhone OS build on M68AP.

The tree used to have exactly one firmware in it, so "the staging directory"
meant 1.1.4 and filenames carried the bootloader version
(`iboot_204_m68ap_sbpatch.bin`). Adding a second build by adding a second
differently-shaped directory (`stage-1.0/`) does not scale and makes 1.1.4 the
implicit default -- which is how a NAND gets built with one firmware's
filesystem and another firmware's signature word, a mistake that does not
announce itself.

Every build now lives in the same shape, and the DIRECTORY names the version so
the FILENAMES do not have to:

    m68ap-artifacts/
      shared/
        bootrom_s5l8900          SoC-wide, identical for every build
      builds/
        1A543a/                  iPhone OS 1.0
          ipsw/                  the retail IPSW and images extracted from it
          root.img               decrypted (and recipe-patched) root filesystem
          data.dmg               /var template
          iboot.bin              raw iBoot for this build
          iboot-sb.bin           secure-boot-patched iBoot
          nor.bin
          nand/                  generated tree + nand.pack
          build.json             provenance for this build directory
        1C28/                    iPhone OS 1.0.2
        3A109a/                  iPhone OS 1.1.1
        4A102/                   iPhone OS 1.1.4

There is deliberately **no default build**. Every entry point takes the build
explicitly, because all four are equally "the right version" and picking one
silently is the failure mode this module exists to remove.

Use `add_build_argument()` so every tool spells the option the same way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import firmware_profiles

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "m68ap-artifacts"
BUILDS = ARTIFACTS / "builds"
SHARED = ARTIFACTS / "shared"

# The one file that is genuinely not per-build: the S5L8900 boot ROM is burned
# into the SoC and is the same silicon on every iPhone 2G and iPod touch 1G.
BOOTROM = SHARED / "bootrom_s5l8900"

# Canonical names inside a build directory. Version-neutral on purpose.
NAMES = {
    "ipsw": "ipsw",
    "root": "root.img",
    "data": "data.dmg",
    "iboot": "iboot.bin",
    "iboot_sb": "iboot-sb.bin",
    "nor": "nor.bin",
    "nand": "nand",
    "pack": "nand/nand.pack",
    "provenance": "build.json",
}


class BuildPaths:
    """Resolved paths for one firmware build. Nothing here is guessed."""

    def __init__(self, build: str):
        self.profile = firmware_profiles.get(build)
        self.build = self.profile.build
        self.version = self.profile.version
        self.dir = BUILDS / self.build

    def __getattr__(self, name: str) -> Path:
        try:
            return self.dir / NAMES[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self) -> str:
        return f"<BuildPaths {self.build} ({self.version}) at {self.dir}>"

    @property
    def bootrom(self) -> Path:
        return BOOTROM

    @property
    def epoch(self) -> int:
        return self.profile.epoch

    def require(self, *names: str) -> None:
        """Fail with one legible message listing everything that is missing."""
        missing = [(name, getattr(self, name)) for name in names
                   if not getattr(self, name).exists()]
        if not missing:
            return
        lines = [f"missing artifacts for build {self.build} "
                 f"(iPhone OS {self.version}):"]
        lines += [f"  {name}: {path}" for name, path in missing]
        lines.append("")
        lines.append("These are IPSW-derived and are never committed. "
                     "See BUILD.md for how to produce them,")
        lines.append("and scripts/migrate-m68ap-artifacts.py if you still have "
                     "the old stage*/ directories.")
        raise SystemExit("\n".join(lines))


def get(build: str) -> BuildPaths:
    return BuildPaths(build)


def known_builds() -> list[str]:
    """Every M68AP firmware build, in release order. Excludes the iPod."""
    return firmware_profiles.m68ap_builds()


# One spelling of --build for the whole tree; the profile layer owns it.
add_build_argument = firmware_profiles.add_build_argument


def describe(build: str) -> str:
    profile = firmware_profiles.get(build)
    return (f"{profile.build} (iPhone OS {profile.version}, "
            f"{profile.iboot}, epoch {profile.epoch}, "
            f"FIL {profile.fil_signature:#x})")


NAMED = ("ipsw", "root", "data", "iboot", "iboot_sb", "nor", "nand", "pack",
         "provenance")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Show the canonical artifact layout for a build.")
    add_build_argument(parser)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable, for shell consumers")
    args = parser.parse_args()
    paths = get(args.build)

    if args.json:
        import json
        json.dump({
            "build": paths.build,
            "version": paths.version,
            "epoch": paths.epoch,
            "dir": str(paths.dir),
            "bootrom": str(paths.bootrom),
            **{name: str(getattr(paths, name)) for name in NAMED},
        }, sys.stdout, indent=2)
        print()
        raise SystemExit(0)

    print(describe(args.build))
    print(f"  dir      {paths.dir}")
    for name in NAMED:
        path = getattr(paths, name)
        print(f"  {name:<9}{path.relative_to(REPO)}"
              f"{'' if path.exists() else '   (missing)'}")
    print(f"  bootrom  {paths.bootrom.relative_to(REPO)}"
          f"{'' if paths.bootrom.exists() else '   (missing)'}")
