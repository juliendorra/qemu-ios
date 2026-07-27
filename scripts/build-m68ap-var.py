#!/usr/bin/env python3
"""Build the M68AP data partition (disk0s2 = /private/var) with a real skeleton.

Why this exists
---------------
The generated M68AP NAND ships an EMPTY /var: `build-m68ap-nand.py --data-hfs`
was given a blank HFS image, because we construct the NAND ourselves instead of
running Apple's restore, and it is the restore ramdisk that lays down /var on a
real device. N45AP does not have this problem -- its NAND is a device dump with
a fully populated /var.

An empty /var is not cosmetic. Observed consequences (see
IPHONE_2G_BRINGUP_HANDOFF.md):
  * `configd: updateConfiguration(): no preferences.` and no network/AirPort
    setup at all, where N45AP does the whole `Setup:/Network/Interface/en0/
    AirPort` dance and saves a configuration;
  * `lockdown: _load_international_settings: Could not load languages list`;
  * `crashreporterd cannot create log directory '/Library/Logs'`;
  * `mDNSResponder: Couldn't read user-specified Computer Name`;
  * and, critically, **SpringBoard runs as `mobile` (uid 501) whose home is
    `/var/mobile`** -- a directory that does not exist, so it cannot create its
    own preferences/state. That is the leading explanation for M68AP reaching
    SpringBoard and then never programming a framebuffer.

This tool creates the directory skeleton (and optionally injects the lockdown
data ark) so the guest has somewhere to write.

MEASURED RESULTS (2026-07-25, springboard-lab.py) -- read before using:
  * `--minimal` (8 dirs, no chmod) boots normally: launchd 13 lines, configd
    45, SpringBoard 2 -- identical to an empty /var. It does NOT unblock
    rendering, and configd STILL logs `updateConfiguration(): no preferences.`
    because the directories are empty: configd wants preference *files*, not
    just a path. **So an empty /var is NOT the render blocker.**
  * The FULL skeleton (56 dirs + chmod 1777 on tmp/run) is actively HARMFUL:
    /dev/disk0s2 mounts and then launchd never starts at all (0 launchd,
    0 configd, 0 SpringBoard lines even at a 700 s cap). Something in the
    extra entries or the chmod through an owner-less hdiutil mount breaks
    early userland. DO NOT use the full set until that is bisected.
Default is therefore `--minimal`; pass `--full` deliberately.

THE ANSWER (2026-07-26): neither list. **The root filesystem carries the
authoritative template at `/private/var`** -- 73 directories with their real
modes, including `tmp` (1777), `run`, `preferences`, `logs`, `db/timezone`,
`Keychains`, `vm`, `mobile/Media` and `mobile/Library`. On a real device the
restore ramdisk lays that template onto the data partition; `--template-from
<root image>` does the same thing here, and it is the default for the product
recipe.

What it fixed, measured on the packaged bundle: `com.apple.AddressBook` had
been failing `CREATE TABLE` with SQLITE_BUSY ("database is locked") and
retrying ~250x/s forever, pegging a host core at ~98%. With the template the
guest reaches the HOME SCREEN with **zero** SQLite errors and idles at 6-10%
CPU -- better than the iPod's 11-15%. The 56-entry hand-written `--full` list
is not the same thing and is still the harmful one; do not confuse them.

Ownership: hdiutil mounts without owners, so entries land as the invoking uid.
On this host that happens to be 501, which is exactly `mobile` -- correct for
/var/mobile by luck. Paths that must be root-owned are rewritten explicitly
(`--fix-owners`, the ipod-nand-restore-dns.py catalog technique), because root
bypasses permissions anyway but daemons do check some of these.

Usage:
  scripts/build-m68ap-var.py --out /tmp/data-var.img \
      [--template-from m68ap-artifacts/builds/<BUILD>/root.img] \
      (--size-from m68ap-artifacts/builds/<BUILD>/data.dmg | --size BYTES) \
      [--data-ark /tmp/data_ark.plist] [--fix-owners]
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import attached, human, require_free_bytes

REPO = Path(__file__).resolve().parent.parent

# The iPhone OS 1.x /var skeleton. Paths are relative to the partition root
# (which the guest mounts at /private/var). `mobile` entries are the ones that
# must be owned by uid 501; everything else is root's.
MOBILE_DIRS = [
    "mobile",
    "mobile/Library",
    "mobile/Library/Preferences",
    "mobile/Library/Caches",
    "mobile/Library/Logs",
    "mobile/Library/SMS",
    "mobile/Library/Safari",
    "mobile/Library/AddressBook",
    "mobile/Library/Calendar",
    "mobile/Library/Notes",
    "mobile/Library/Maps",
    "mobile/Library/Keyboard",
    "mobile/Library/WebKit",
    "mobile/Library/Cookies",
    "mobile/Library/Voicemail",
    "mobile/Library/CallHistory",
    "mobile/Library/ConfigurationProfiles",
    "mobile/Media",
    "mobile/Media/DCIM",
    "mobile/Media/Photos",
    "mobile/Media/Recordings",
    "mobile/Media/iTunes_Control",
    "mobile/Media/iTunes_Control/iTunes",
    "mobile/Media/iTunes_Control/Music",
    "mobile/Applications",
    "mobile/Documents",
]
ROOT_DIRS = [
    "root",
    "root/Library",
    "root/Library/Preferences",
    "root/Library/Preferences/SystemConfiguration",
    "root/Library/Lockdown",
    "root/Library/Lockdown/activation_records",
    "root/Library/Caches",
    "root/Library/Logs",
    "db",
    "db/timezone",
    "logs",
    "logs/AppleSupport",
    "logs/CrashReporter",
    "Managed Preferences",
    "Managed Preferences/mobile",
    "preferences",
    "preferences/SystemConfiguration",
    "run",
    "tmp",
    "empty",
    "spool",
    "backups",
    "cache",
    "folders",
    "Keychains",
    "MobileDevice",
    "MobileDevice/ProvisioningProfiles",
    "wireless",
    "wireless/Library",
]


# The smallest set that addresses the observed failures: SpringBoard's home
# (it runs as mobile/501) and the preference directories configd and lockdownd
# complained about. Nothing else, so a regression can be attributed.
MINIMAL_DIRS = [
    "mobile",
    "mobile/Library",
    "mobile/Library/Preferences",
    "root",
    "root/Library",
    "root/Library/Preferences",
    "root/Library/Preferences/SystemConfiguration",
    "root/Library/Lockdown",
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True,
                    help="output raw HFS image for --data-hfs")
    size_group = ap.add_mutually_exclusive_group(required=True)
    size_group.add_argument("--size-from", type=Path,
                            help="take the image size from this file, e.g. "
                                 "the build's own data.dmg")
    size_group.add_argument("--size", type=int,
                            help="image size in bytes")
    ap.add_argument("--seed-dir", type=Path,
                    help="copy every *.sqlitedb here into "
                         "mobile/Library/AddressBook/ WHILE the volume is "
                         "being built. Files added to an already-built image "
                         "are not always traversed by the 2007 HFS driver, so "
                         "seeded databases must be written during construction "
                         "-- the same reason the data ark is copied here and "
                         "not injected afterwards.")
    ap.add_argument("--template-from", type=Path,
                    help="root filesystem image whose /private/var skeleton is "
                         "copied in first (modes preserved). This is what the "
                         "restore ramdisk does on a real device, and it is the "
                         "fix for the AddressBook SQLITE_BUSY spin that pegged "
                         "the host CPU at 98%% (T6).")
    ap.add_argument("--data-ark", type=Path,
                    help="also inject this binary plist at "
                         "root/Library/Lockdown/data_ark.plist")
    ap.add_argument("--full", action="store_true",
                    help="create the FULL 56-entry skeleton. Measured to break "
                         "early userland (launchd never starts) -- kept only "
                         "for bisecting that regression. Default is the "
                         "minimal, known-harmless set.")
    ap.add_argument("--fix-owners", action="store_true",
                    help="rewrite non-mobile entries to root:wheel")
    args = ap.parse_args()

    # Required and explicit: sizing /var from whichever data.dmg happened to
    # be in a shared staging directory silently tied every build to 1.1.4's.
    size = args.size if args.size else args.size_from.stat().st_size
    require_free_bytes(args.out.parent, size * 3, "the /var image")
    work = args.out.with_suffix(".build.dmg")
    for p in (work, args.out):
        if p.exists():
            p.unlink()
    subprocess.run(["hdiutil", "create", "-sectors", str(size // 512),
                    "-fs", "Case-sensitive HFS+", "-volname", "var",
                    "-layout", "NONE", "-o", str(work)],
                   check=True, capture_output=True, text=True)
    built = work.with_suffix(".dmg") if work.suffix != ".dmg" else work

    mnt = Path(f"/tmp/m68ap-var-{args.out.stem}")
    mnt.mkdir(exist_ok=True)
    made = []
    dirs = (ROOT_DIRS + MOBILE_DIRS) if args.full else MINIMAL_DIRS
    with attached(built, mountpoint=mnt, readonly=False):
        if args.template_from:
            # The restore ramdisk's job, done here: copy the root filesystem's
            # OWN /private/var template (modes included -- /var/tmp is 1777).
            # `ditto` preserves them; a plain mkdir loop does not, and the
            # missing skeleton is what made every /var-writing daemon fail.
            with attached(args.template_from, readonly=True) as root_mnt:
                template = Path(root_mnt) / "private" / "var"
                if not template.is_dir():
                    raise SystemExit(f"no /private/var in {args.template_from}")
                subprocess.run(["ditto", str(template), str(mnt)], check=True)
                made += [str(p.relative_to(mnt))
                         for p in sorted(mnt.rglob("*"))]
        for rel in dirs:
            (mnt / rel).mkdir(parents=True, exist_ok=True)
            made.append(rel)
        # /var/tmp is world-writable on a real device; several daemons need
        # it. Skipped in --minimal: chmod through an owner-less hdiutil mount
        # is one of the suspects for perturbing early userland.
        if args.full:
            for rel in ("tmp", "run"):
                (mnt / rel).chmod(0o1777)
        if args.seed_dir:
            target = mnt / "mobile/Library/AddressBook"
            target.mkdir(parents=True, exist_ok=True)
            for db in sorted(args.seed_dir.glob("*.sqlitedb")):
                shutil.copy(db, target / db.name)
                made.append(f"mobile/Library/AddressBook/{db.name}")
        if args.data_ark:
            dest = mnt / "root/Library/Lockdown/data_ark.plist"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(args.data_ark, dest)
            made.append("root/Library/Lockdown/data_ark.plist")
        subprocess.run(["sync"])
    try:
        mnt.rmdir()
    except OSError:
        pass

    if args.fix_owners:
        spec = importlib.util.spec_from_file_location(
            "inject", Path(__file__).with_name("inject-guest-file.py"))
        inject = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inject)
        names = sorted({Path(r).name for r in ROOT_DIRS})
        n = inject.rewrite_owner_records(built, names)
        print(f"rewrote {n} record(s) to root:wheel")

    # Flatten to a raw image the NAND builder can consume page-aligned.
    cdr = args.out.with_suffix(".cdr")
    subprocess.run(["hdiutil", "convert", str(built), "-format", "UDTO",
                    "-o", str(args.out.with_suffix(""))],
                   check=True, capture_output=True, text=True)
    if cdr.exists():
        cdr.replace(args.out)
    built.unlink(missing_ok=True)
    print(f"wrote /var image with {len(made)} entries "
          f"({human(args.out.stat().st_size)}) -> {args.out}")
    print("next: build-m68ap-nand.py --data-hfs", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
