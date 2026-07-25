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


def without_addressbook(root: Path, work: Path, drop: bool) -> Path:
    """OPT-IN ONLY (--drop-addressbook): remove com.apple.AddressBook.

    Not the default: removing a daemon degrades the device (no Contacts), and
    the packaged image should be complete. It exists because the daemon costs
    ~90% of a host core until task T6 lands -- pick your trade-off knowingly.

    AddressBook creates its SQLite database on first run. On a GENERATED NAND
    the guest's writes never take effect -- the FTL cannot write to the tree we
    construct (measured: /private/var is mounted read-WRITE and the kernel
    still lands zero pages in the NAND model). So the daemon creates its
    tables, reads them back, finds "no such table: ABPerson", and retries about
    250 times a second FOREVER. That is what pegs the emulated CPU at ~98%
    while the iPod idles at 11-15%.

    The iPod is unaffected because its NAND is a real device dump whose /var
    already contains what daemons expect.

    THE REAL FIX is writable storage on the generated NAND (task T6); this is
    a stopgap that costs Contacts and nothing else. --keep-addressbook opts
    out for anyone working on T6.
    """
    if not keep:
        return root
    out = work / "root-noab.img"
    if out.exists():
        return out
    print("[2b/4] removing com.apple.AddressBook (SHORTCUT -- see T6)")
    tmp = work / "root-noab.tmp.img"
    shutil.copy2(root, tmp)
    with attached(tmp, readonly=False) as mnt:
        victim = (Path(mnt) / "System" / "Library" / "LaunchDaemons" /
                  "com.apple.AddressBook.plist")
        if victim.exists():
            victim.unlink()
        else:
            print("      (already absent)")
    tmp.rename(out)
    root.unlink(missing_ok=True)
    return out


def data_partition(work: Path) -> Path:
    """/var: the minimal directory skeleton PLUS the activation data ark.

    The skeleton is not cosmetic. A /var without `/var/mobile/Library` sends
    com.apple.AddressBook into an endless SQLite retry loop -- it can neither
    open nor create its database ("no such table: ABPerson", "error 5 creating
    properties table: database is locked") -- which pegs the emulated CPU at
    ~98% forever. The iPod does not have this problem, and NOT because it
    ships a database: its /var/mobile/Library simply EXISTS and is writable,
    so the daemon creates the file once and goes quiet. (Reference checked:
    the iPod's own /var has no AddressBook database either.)

    Use --minimal, never --full: 56 dirs + chmod 1777 stops launchd starting
    at all (measured; see build-m68ap-var.py).
    """
    out = work / "data-var.img"
    if out.exists():
        return out
    print(f"[3/4] data partition: minimal /var skeleton + {ARK_PROFILE!r} ark")
    ark = work / "data_ark.plist"
    run([sys.executable, SCRIPTS / "hacktivate-m68ap.py", "build-dataark",
         "--out", ark, "--profile", ARK_PROFILE])
    # The databases the first boot after a restore would have created. Our
    # guest cannot create them (writes do not reach the generated NAND), and
    # without them com.apple.AddressBook retries ~250x/s forever. Schema comes
    # from the firmware's own SQL -- see seed-guest-databases.py. They are
    # handed to the /var builder so they are written WHILE the volume is
    # constructed: a file added to a finished image is not reliably traversed
    # by the 2007 HFS driver.
    seed = work / "seed"
    run([sys.executable, SCRIPTS / "seed-guest-databases.py",
         "--root-hfs", ROOT_HFS, "--out", seed])
    run([sys.executable, SCRIPTS / "build-m68ap-var.py",
         "--out", out, "--data-ark", ark, "--seed-dir", seed])
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
    ap.add_argument("--drop-addressbook", action="store_true",
                    help="remove com.apple.AddressBook instead of seeding its "
                         "database (last-resort fallback; costs Contacts)")
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
    root = without_addressbook(root, work, args.drop_addressbook)
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
