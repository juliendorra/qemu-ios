#!/usr/bin/env python3
"""Build the M68AP NAND that boots an iPhone OS 1.x build to the HOME SCREEN.

This is the *product* recipe: one command, from a build's staged artifacts to a
NAND tree an app bundle can ship. `springboard-lab.py` builds the same thing for
experiments (with knobs); this script fixes the knobs at the combination that
was measured to reach the home screen on 2026-07-25, so packaging cannot drift
from the verified configuration.

The firmware build is always explicit (`--build`) and every path comes from
`m68ap_paths.py`, so 1.0, 1.0.2, 1.1.1 and 1.1.4 are built the same way. This
recipe previously hardcoded 4A102, which made "the M68AP NAND" mean 1.1.4 and
left no way to package another build without editing the source.

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
3. **HTTPS-bridge CA in the guest trust store** — the launcher starts a local
   TLS bridge for this bundle (ports 18543/18542), but Safari rejects every
   certificate it mints unless the bridge's root is trusted. The iPod gets the
   same row via `ipod-nand-trust-ca.py`; that tool's NAND plumbing cannot be
   reused here (it reconstructs a single-partition device dump page by page,
   and this NAND is generated with the real two-partition layout), so the row
   goes in while the root image is still a mounted filesystem. Only the public
   certificate is read. Disable with `--no-bridge-ca`.
4. **Reference-shaped data ark** (`--profile reference-reg`) — key names and
   TYPES read off the iPod's own activated device: CFBooleans where a naive
   ark writes CFNumbers, plus the international/SIM/timezone keys without
   which the device has never been "set up" and shows connect-to-iTunes.
   `EverRegistered = True` is the value a registered phone has.

No Apple-signed material is copied: the reference contributed key names,
types and generic values only.

Usage
-----
  scripts/build-m68ap-homescreen-nand.py --build 1A543a
  scripts/build-m68ap-homescreen-nand.py --build 4A102 --out /tmp/nand-test
  scripts/build-m68ap-homescreen-nand.py --build 1A543a --keep-work
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import (NAND_TREE_BYTES, ROOT_HFS_BYTES, DATA_HFS_BYTES,
                           attached, human, require_free_bytes)

import guest_trust_store
import ipod_tls_common
import m68ap_paths

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Set once in main() from --build. There is no default build: this recipe used
# to hardcode 4A102, which made "the M68AP NAND" silently mean 1.1.4 and left
# no way to package 1.0 without editing the script. See m68ap_paths.py.
PATHS: "m68ap_paths.BuildPaths | None" = None

# /var partition size when the build has no data.dmg to measure. This is OUR
# choice, not a firmware constant -- on real hardware /var is whatever the
# restore leaves after the root partition -- so it is stated here rather than
# inferred from whichever file happens to sit in a staging directory.
DEFAULT_VAR_BYTES = 24 * 1024 * 1024


def root_hfs() -> Path:
    return PATHS.root

ARK_PROFILE = "reference-reg"
SEED_DATABASES = [True]
SB_PLIST = "System/Library/LaunchDaemons/com.apple.SpringBoard.plist"

# Where ipod-app-launcher.sh keeps this profile's bridge CA. The default MUST
# match the launcher's, because a NAND that trusts some other CA is a NAND
# whose HTTPS bridge silently does not work.
BRIDGE_PROFILE = "iphone-2g"
BRIDGE_STATE_DIR = (Path.home() / "Library" / "Application Support" /
                    "S5L8900 HTTPS Bridge" / BRIDGE_PROFILE)
# sha256 of the injected CA certificate, for the provenance sidecar. None when
# --no-bridge-ca.
BRIDGE_CA_SHA256 = [None]


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def patched_root(work: Path) -> Path:
    out = work / "root-patched.img"
    if not out.exists():
        print("[1/5] lockdownd activation patch")
        run([sys.executable, SCRIPTS / "hacktivate-m68ap.py", "patch",
             "--root-hfs", root_hfs(), "--out", out])
    return out


def with_software_compositing(root: Path, work: Path) -> Path:
    out = work / "root-mbx2d.img"
    if out.exists():
        return out
    print("[2/5] SpringBoard LK_ENABLE_MBX2D=0 (software compositing)")
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


def bridge_ca_certificate(state_dir: Path, explicit: Path | None) -> Path:
    """Return the public certificate to trust, generating the CA if needed.

    With no --ca-cert this calls the launcher's own generator against the
    launcher's own state directory, so the CA baked into the NAND is the one
    the bridge will present at run time. `ensure_ca` reuses an existing CA and
    only mints one when the directory is empty, so re-packaging does not
    invalidate a bundle built earlier on this host.
    """
    if explicit is not None:
        return explicit
    paths = ipod_tls_common.ensure_ca(state_dir)
    return paths["ca_der"]


def with_bridge_ca(root: Path, work: Path, ca_cert: Path | None,
                   state_dir: Path, enabled: bool) -> Path:
    """Add the HTTPS-bridge root to the guest's system trust store.

    Edited IN PLACE, unlike the steps around it: the root image here is already
    a private intermediate, and this machine's disk is routinely near full, so
    a third 280 MB copy buys nothing. A marker file carries the resume
    behaviour the other steps get from their output filename.
    """
    if not enabled:
        print("[3/5] HTTPS-bridge CA: SKIPPED (--no-bridge-ca); Safari will "
              "reject the bridge's certificates")
        return root
    print("[3/5] HTTPS-bridge CA -> guest system trust store")
    certificate_path = bridge_ca_certificate(state_dir, ca_cert)
    certificate, subject = guest_trust_store.load_ca_certificate(
        certificate_path, work)
    sha256 = hashlib.sha256(certificate).hexdigest()
    BRIDGE_CA_SHA256[0] = sha256
    marker = work / "bridge-ca.sha256"
    if marker.exists() and marker.read_text().strip() == sha256:
        print("      (already injected in this work directory)")
        return root
    print(f"      CA: {certificate_path}")
    with attached(root, readonly=False) as mnt:
        changed, rows = guest_trust_store.inject_into_volume(
            mnt, certificate, subject)
        print(f"      {'added to' if changed else 'already in'} "
              f"{guest_trust_store.TRUST_STORE.name}: {rows} trusted roots")
    marker.write_text(sha256 + "\n")
    print(f"      CA sha256: {sha256}")
    return root


def without_addressbook(root: Path, work: Path, drop: bool) -> Path:
    """OPT-IN ONLY (--drop-addressbook): remove com.apple.AddressBook.

    OBSOLETE as a workaround (T6 is fixed -- see data_partition() below): the
    daemon no longer spins, because /var now carries the root filesystem's own
    template. Kept only as a bisecting tool, and never the default: removing a
    daemon degrades the device (no Contacts).

    Retained because the reasoning was wrong in an instructive way. The claim
    here used to be "the guest's writes never take effect -- the FTL cannot
    write to the tree we construct". That was false twice over: the guest does
    issue page writes, AND writability was never the issue. AddressBook was
    failing CREATE TABLE with SQLITE_BUSY because /var had none of the
    directories the OS expects. Dropping the daemon took CPU from ~98% to ~9%
    and thereby made a symptom disappear while the cause stayed put -- which is
    exactly how a stopgap buys silence instead of understanding.
    """
    if not drop:
        return root
    out = work / "root-noab.img"
    if out.exists():
        return out
    print("[3b/5] removing com.apple.AddressBook (SHORTCUT -- see T6)")
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
    """/var: the root filesystem's OWN /private/var template + the data ark.

    The skeleton is not cosmetic, and a hand-written one is not enough. With
    the 8-directory minimal list, `com.apple.AddressBook` failed CREATE TABLE
    with SQLITE_BUSY ("error 5 creating properties table: database is locked")
    and retried ~250x/s forever, pegging the host CPU at ~98% (T6). Seeding
    the database did not help: the daemon was not failing to READ, it was
    failing to WRITE into a /var that has none of the directories the OS
    expects (`tmp` 1777, `run`, `preferences`, `logs`, `db/timezone`, ...).

    `--template-from` copies those from the root image, which is exactly what
    the restore ramdisk does on a real device. Measured on the packaged
    bundle: home screen, zero SQLite errors, 6-10% idle CPU.

    Never pass --full to the /var builder: that is a different, hand-written
    56-entry list plus chmods, and it stops launchd starting at all.
    """
    out = work / "data-var.img"
    if out.exists():
        return out
    print(f"[4/5] data partition: /var from the root template + "
          f"{ARK_PROFILE!r} ark")
    ark = work / "data_ark.plist"
    run([sys.executable, SCRIPTS / "hacktivate-m68ap.py", "build-dataark",
         "--out", ark, "--profile", ARK_PROFILE])
    # The databases the first boot after a restore would have created. Kept
    # even though T6 turned out not to be about them -- the guest reads them
    # fine (proved by corrupting the SQLite magic and watching SpringBoard
    # report SQLITE_CORRUPT with the path), and a device that ships with its
    # schema already present skips one first-run rebuild. Schema comes from the
    # firmware's own SQL -- see seed-guest-databases.py. They are handed to the
    # /var builder so they are written WHILE the volume is constructed.
    cmd = [sys.executable, SCRIPTS / "build-m68ap-var.py",
           "--out", out, "--data-ark", ark,
           "--template-from", root_hfs()]
    # Size from this build's own data.dmg when it was extracted, otherwise the
    # stated default -- never from another firmware's staging directory.
    if PATHS.data.exists():
        cmd += ["--size-from", PATHS.data]
    else:
        print(f"      (no {PATHS.data.name} for {PATHS.build}; "
              f"/var sized at {human(DEFAULT_VAR_BYTES)})")
        cmd += ["--size", str(DEFAULT_VAR_BYTES)]
    if not SEED_DATABASES[0]:
        print("      (databases NOT seeded: --no-seed-databases)")
    else:
        seed = work / "seed"
        run([sys.executable, SCRIPTS / "seed-guest-databases.py",
             "--root-hfs", root_hfs(), "--out", seed])
        cmd += ["--seed-dir", seed]
    run(cmd)
    return out


def declare_recipe(out: Path, args) -> None:
    """Record this recipe's guest-side modifications in the NAND sidecar.

    Additive: build-m68ap-nand.py has already written nand-provenance.json with
    the geometry, signature and partition hashes. This adds what only the
    recipe knows -- which guest files it altered and why -- under a `recipe`
    key, and amends `guest_file_modifications` so the summary line cannot
    describe a patched image as untouched.
    """
    sidecar = out / "nand-provenance.json"
    if not sidecar.exists():
        print(f"WARNING: no {sidecar.name} to annotate; the NAND will carry no "
              "record of the guest modifications made here", file=sys.stderr)
        return
    manifest = json.loads(sidecar.read_text())
    steps = [
        "lockdownd activation patch (hacktivate-m68ap.py patch)",
        "SpringBoard LK_ENABLE_MBX2D=0 -- forces software compositing, "
        "because the MBX 2D block is a stub (task T2)",
        "/var built from the root filesystem's own /private/var template, "
        "plus a reference-shaped data ark",
    ]
    if SEED_DATABASES[0]:
        steps.append("AddressBook databases pre-created")
    if args.drop_addressbook:
        steps.append("com.apple.AddressBook REMOVED (--drop-addressbook)")
    recipe = {
        "constructor": "build-m68ap-homescreen-nand.py",
        "build": PATHS.build,
        "version": PATHS.version,
        "steps": steps,
    }
    # The CA sha256 is what lets packaging (and a human) tell whether this NAND
    # trusts the bridge CA that is actually on this host, rather than one from
    # another machine whose private key nobody here holds.
    if BRIDGE_CA_SHA256[0] is not None:
        steps.insert(2, "local HTTPS-bridge CA added to the system trust store "
                        "(Security.framework/TrustStore.sqlite3)")
        recipe["bridge_ca_sha256"] = BRIDGE_CA_SHA256[0]
        recipe["bridge_ca_profile"] = BRIDGE_PROFILE
    else:
        steps.insert(2, "HTTPS-bridge CA NOT added (--no-bridge-ca): the "
                        "launcher's TLS bridge will be rejected by Safari")
    manifest["recipe"] = recipe
    base = manifest.get("guest_file_modifications", "")
    manifest["guest_file_modifications"] = (
        f"{base}; plus the home-screen recipe (see `recipe`)" if base
        else "home-screen recipe (see `recipe`)")
    sidecar.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"declared {len(steps)} guest modification(s) in {sidecar}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    m68ap_paths.add_build_argument(ap)
    ap.add_argument("--out", type=Path,
                    help="NAND tree to create (must not exist). Default: this "
                         "build's canonical nand/ directory.")
    ap.add_argument("--work", type=Path,
                    help="scratch dir (default: <out>.work, removed on success)")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--no-seed-databases", action="store_true",
                    help="do NOT pre-create the AddressBook databases. With "
                         "writable storage the daemon should create its own; "
                         "this exists to test that (T6).")
    ap.add_argument("--drop-addressbook", action="store_true",
                    help="remove com.apple.AddressBook instead of seeding its "
                         "database (last-resort fallback; costs Contacts)")
    ap.add_argument("--no-bridge-ca", action="store_true",
                    help="do NOT trust the local HTTPS bridge's root. Safari "
                         "then rejects every certificate the bridge mints.")
    ap.add_argument("--ca-cert", type=Path,
                    help="public CA certificate to trust (PEM or DER). "
                         "Default: the launcher's own, from --https-state.")
    ap.add_argument("--https-state", type=Path, default=None,
                    help=f"HTTPS bridge state directory the CA is taken from "
                         f"(default: {BRIDGE_STATE_DIR}, i.e. the one "
                         f"ipod-app-launcher.sh uses for {BRIDGE_PROFILE})")
    args = ap.parse_args()

    state_dir = (args.https_state
                 or Path(os.environ.get("S5L8900_HTTPS_STATE_DIR", "")
                         or BRIDGE_STATE_DIR))

    global PATHS
    PATHS = m68ap_paths.get(args.build)
    print(f"building for {m68ap_paths.describe(args.build)}")
    PATHS.require("root")

    out = args.out or PATHS.nand
    if out.exists():
        raise SystemExit(f"refusing to overwrite: {out}")

    work = args.work or Path(str(out) + ".work")
    work.mkdir(parents=True, exist_ok=True)
    require_free_bytes(work.parent,
                       NAND_TREE_BYTES + 2 * ROOT_HFS_BYTES + DATA_HFS_BYTES,
                       "the M68AP home-screen NAND")

    SEED_DATABASES[0] = not args.no_seed_databases
    root = with_software_compositing(patched_root(work), work)
    root = with_bridge_ca(root, work, args.ca_cert, state_dir,
                          not args.no_bridge_ca)
    root = without_addressbook(root, work, args.drop_addressbook)
    data = data_partition(work)

    print("[5/5] building the NAND tree")
    # --build carries the FIL/WMR signature word, which is firmware-keyed:
    # 000C for 1.0/1.0.2, 200C for 1.1.1, 300C for 1.1.4. Hardcoding it here
    # was what pinned this recipe to one firmware.
    run([sys.executable, SCRIPTS / "build-m68ap-nand.py",
         "--out", out, "--active-banks", "4",
         "--bbt", "production", "--hfs", root, "--data-hfs", data,
         "--device", PATHS.profile.device, "--build", PATHS.build,
         # Stamped into nand-provenance.json: without it, a tree built from an
         # unpatched root is indistinguishable from this one and boots to a
         # black screen instead of the home screen.
         "--recipe", "home-screen",
         # The product ships a PACKED NAND: the launcher clones the tree on
         # every start, and cloning 100k+ page files takes minutes where the
         # single pack file is instant (BUILD.md). The browser port consumes
         # the same pack.
         "--pack"])

    # build-m68ap-nand.py only knows it placed two HFS+ partitions; the guest
    # modifications THIS recipe made are invisible to it, and its
    # `guest_file_modifications` line would otherwise be the whole record. A
    # bundle carrying the activation patch and a forced-software-compositing
    # SpringBoard must not be describable as stock firmware -- declaring what
    # was changed in a guest image is the repository's firmware policy
    # (AGENTS.md), and it is also the only after-the-fact answer to "which
    # recipe is in this NAND?".
    declare_recipe(out, args)

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\nhome-screen NAND ready for {PATHS.build} "
          f"(iPhone OS {PATHS.version}): {out} ({human(size)})")
    # The epoch is firmware-keyed, so it is printed with the command: booting
    # this build under another's wedges in iBoot with an empty serial log.
    print(f"\nboot it with:\n"
          f"  -M iPhone-2G,bootrom={PATHS.bootrom.name},"
          f"iboot={PATHS.iboot_sb.name},nand={out.name},"
          f"epoch={PATHS.epoch}\n"
          f"  -pflash {PATHS.nor.name}\n"
          f"or simply: scripts/fb-snapshot.py --board m68ap "
          f"--build {PATHS.build} --logs <dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
