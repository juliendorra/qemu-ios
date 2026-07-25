#!/usr/bin/env python3
"""Build the M68AP NAND that boots iPhone OS 1.1.4 to the SpringBoard HOME SCREEN.

This is the *product* recipe: one command, from the staged artifacts to a NAND
tree an app bundle can ship. `springboard-lab.py` builds the same thing for
experiments (with knobs); this script fixes the knobs at the combination that
was measured to reach the home screen on 2026-07-25, so packaging cannot drift
from the verified configuration.

What it applies, and why each part is needed
--------------------------------------------
1. **lockdownd activation patch** (`hacktivate-m68ap.py patch`) — no data-only
   ark survives `determine_activation_state`'s boot re-validation, because a
   genuine Apple-signed, device-bound activation record cannot be synthesised.
   One patch in the activation *authority*, not per-consumer patches.
2. **`LK_ENABLE_MBX2D=0`** in `com.apple.SpringBoard.plist` — LayerKit
   otherwise composites through the PowerVR MBX, which the emulator only
   stubs, and SpringBoard tight-polls forever. devos50's iPod image ships the
   same setting. (Shortcut: the clean fix is to model MBX 2D — task T2.)
3. **Reference-shaped data ark** (`--profile reference-reg`) — key names and
   TYPES read off the iPod's own activated device: CFBooleans where a naive
   ark writes CFNumbers, plus the international/SIM/timezone keys without
   which the device has never been "set up" and shows connect-to-iTunes.
   `EverRegistered = True` is the value a registered phone has.

No Apple-signed material is copied: the reference contributed key names,
types and generic values only.

Usage
-----
  scripts/build-m68ap-homescreen-nand.py --out /tmp/nand-homescreen
  scripts/build-m68ap-homescreen-nand.py --out … --keep-work   # debugging
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import (NAND_TREE_BYTES, ROOT_HFS_BYTES, DATA_HFS_BYTES,
                           attached, human, require_free_bytes)

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
STAGE = REPO / "m68ap-artifacts" / "stage"
ROOT_HFS = STAGE / "filesystem-m68ap-readonly.img"
DATA_DMG = STAGE / "data-m68ap.dmg"

ARK_PROFILE = "reference-reg"
SB_PLIST = "System/Library/LaunchDaemons/com.apple.SpringBoard.plist"


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def patched_root(work: Path) -> Path:
    out = work / "root-patched.img"
    if not out.exists():
        print("[1/4] lockdownd activation patch")
        run([sys.executable, SCRIPTS / "hacktivate-m68ap.py", "patch",
             "--root-hfs", ROOT_HFS, "--out", out])
    return out


def with_software_compositing(root: Path, work: Path) -> Path:
    out = work / "root-mbx2d.img"
    if out.exists():
        return out
    print("[2/4] SpringBoard LK_ENABLE_MBX2D=0 (software compositing)")
    tmp = work / "root-mbx2d.tmp.img"      # hdiutil types images by extension
    shutil.copy2(root, tmp)
    with attached(tmp, readonly=False) as mnt:
        plist = Path(mnt) / SB_PLIST
        job = plistlib.loads(plist.read_bytes())
        job.setdefault("EnvironmentVariables", {})["LK_ENABLE_MBX2D"] = "0"
        plist.write_bytes(plistlib.dumps(job, fmt=plistlib.FMT_BINARY))
        print(f"      set in {plist.name}: "
              f"{job['EnvironmentVariables']}")
    tmp.rename(out)
    root.unlink(missing_ok=True)           # intermediate; disk is scarce here
    return out


def data_partition(work: Path) -> Path:
    out = work / "data-ark.img"
    if out.exists():
        return out
    print(f"[3/4] data partition with the {ARK_PROFILE!r} ark")
    ark = work / "data_ark.plist"
    run([sys.executable, SCRIPTS / "hacktivate-m68ap.py", "build-dataark",
         "--out", ark, "--profile", ARK_PROFILE])
    dmg = work / "data.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-sectors", str(DATA_DMG.stat().st_size // 512),
         "-fs", "Case-sensitive HFS+", "-volname", "var", "-layout", "NONE",
         "-o", dmg], stdout=subprocess.DEVNULL)
    run([sys.executable, SCRIPTS / "inject-guest-file.py",
         "--image", dmg, "--src", ark,
         "--dest", "/root/Library/Lockdown/data_ark.plist"],
        stdout=subprocess.DEVNULL)
    raw = work / "data-raw"
    run(["hdiutil", "convert", dmg, "-format", "UDTO", "-o", raw],
        stdout=subprocess.DEVNULL)
    shutil.move(str(work / "data-raw.cdr"), str(out))
    dmg.unlink(missing_ok=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True,
                    help="NAND tree to create (must not exist)")
    ap.add_argument("--work", type=Path,
                    help="scratch dir (default: <out>.work, removed on success)")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    for needed in (ROOT_HFS, DATA_DMG):
        if not needed.exists():
            raise SystemExit(f"missing staged artifact: {needed}\n"
                             f"(see BUILD.md — the IPSW-derived images are not "
                             f"committed)")
    if args.out.exists():
        raise SystemExit(f"refusing to overwrite: {args.out}")

    work = args.work or Path(str(args.out) + ".work")
    work.mkdir(parents=True, exist_ok=True)
    require_free_bytes(work.parent,
                       NAND_TREE_BYTES + 2 * ROOT_HFS_BYTES + DATA_HFS_BYTES,
                       "the M68AP home-screen NAND")

    root = with_software_compositing(patched_root(work), work)
    data = data_partition(work)

    print("[4/4] building the NAND tree")
    run([sys.executable, SCRIPTS / "build-m68ap-nand.py",
         "--out", args.out, "--signature", "m68ap", "--active-banks", "4",
         "--bbt", "production", "--hfs", root, "--data-hfs", data,
         "--device", "iPhone1,1", "--ipsw-build", "4A102"])

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    size = sum(f.stat().st_size for f in args.out.rglob("*") if f.is_file())
    print(f"\nhome-screen NAND ready: {args.out} ({human(size)})")
    print("boot it with -M iPhone-2G,…,nand=<this tree> plus the "
          "secure-boot-patched iBoot (iboot_204_m68ap_sbpatch.bin).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
