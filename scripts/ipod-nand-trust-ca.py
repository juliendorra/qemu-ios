#!/usr/bin/env python3
"""Inject a public bridge CA into the iPhone OS 1 system trust store.

This is a bundle-time NAND patch.  It reconstructs the HFSX filesystem into a
temporary image, updates the SQLite trust-store seed on that copy, checks the
filesystem, and writes only changed logical pages back to the staged NAND.
Every touched page is backed up and recorded so --revert restores the exact
pre-patch bytes.  The CA private key is neither accepted nor copied.
"""

import argparse
import hashlib
import importlib.util
import shutil
import tempfile
from pathlib import Path

# The guest-side half of this patch -- what a trusted root looks like in
# Security.framework's TrustStore.sqlite3 -- lives in its own module because
# the M68AP bundles need exactly it and none of the NAND plumbing below.
from guest_trust_store import (TRUST_STORE, inject_store, load_ca_certificate)


SCRIPT_DIR = Path(__file__).resolve().parent
DNS_PATCH = SCRIPT_DIR / "ipod-nand-restore-dns.py"
SPEC = importlib.util.spec_from_file_location("ipod_nand_dns", DNS_PATCH)
if SPEC is None or SPEC.loader is None:
    raise SystemExit(f"error: cannot load NAND support from {DNS_PATCH}")
NAND_SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NAND_SUPPORT)

NUM_BANKS = NAND_SUPPORT.NUM_BANKS
PAGE_DATA = NAND_SUPPORT.PAGE_DATA
PAGE_SPARE = NAND_SUPPORT.PAGE_SPARE
FILESYSTEM_START_VPN = NAND_SUPPORT.FILESYSTEM_START_VPN
FILESYSTEM_NUM_PAGES = NAND_SUPPORT.FILESYSTEM_NUM_PAGES
MANIFEST_NAME = "https-bridge-ca-patch-manifest.txt"
BACKUP_DIR_NAME = "https-bridge-ca-patch-backup"


def fail(message):
    raise SystemExit(f"error: {message}")


def write_back(nand, original, patched, ca_sha256):
    changed = []
    with original.open("rb") as before, patched.open("rb") as after:
        for index in range(FILESYSTEM_NUM_PAGES):
            if before.read(PAGE_DATA) != after.read(PAGE_DATA):
                changed.append(FILESYSTEM_START_VPN + index)
    if not changed:
        return changed

    backup_dir = nand / BACKUP_DIR_NAME
    backup_dir.mkdir(exist_ok=True)
    manifest_lines = [f"ca-sha256={ca_sha256}", f"trust-store={TRUST_STORE}"]
    with patched.open("rb") as after:
        for vpn in changed:
            after.seek((vpn - FILESYSTEM_START_VPN) * PAGE_DATA)
            new_data = after.read(PAGE_DATA)
            target = NAND_SUPPORT.page_path(nand, vpn)
            if target.is_file():
                previous = target.read_bytes()
                spare = previous[PAGE_DATA:]
                state = "modified"
                backup = backup_dir / f"{vpn}.page"
                if not backup.exists():
                    backup.write_bytes(previous)
            else:
                spare = b"\x00" * PAGE_SPARE
                state = "created"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(new_data + spare)
            manifest_lines.append(
                f"{state} vpn={vpn} file={target.relative_to(nand)}"
            )
    (nand / MANIFEST_NAME).write_text("\n".join(manifest_lines) + "\n")
    return changed


def revert(nand):
    manifest = nand / MANIFEST_NAME
    backup_dir = nand / BACKUP_DIR_NAME
    if not manifest.is_file():
        fail(f"no patch manifest found at {manifest}")
    for line in manifest.read_text().splitlines():
        if not line.startswith(("modified ", "created ")):
            continue
        state, vpn_field, file_field = line.split()
        vpn = int(vpn_field.split("=", 1)[1])
        target = nand / file_field.split("=", 1)[1]
        if state == "created":
            target.unlink(missing_ok=True)
            print(f"  removed {target}")
        else:
            backup = backup_dir / f"{vpn}.page"
            if not backup.is_file():
                fail(f"missing backup page {backup}")
            target.write_bytes(backup.read_bytes())
            print(f"  restored {target}")
    shutil.rmtree(backup_dir, ignore_errors=True)
    manifest.unlink()
    print("revert complete")


def manifest_ca_sha256(nand):
    manifest = nand / MANIFEST_NAME
    if not manifest.is_file():
        return None
    for line in manifest.read_text().splitlines():
        if line.startswith("ca-sha256="):
            return line.split("=", 1)[1]
    fail(f"malformed patch manifest at {manifest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nand", required=True, type=Path)
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    nand = args.nand.resolve()
    for bank in range(NUM_BANKS):
        if not (nand / f"bank{bank}").is_dir():
            fail(f"{nand} does not look like a NAND tree (missing bank{bank})")
    if args.revert:
        revert(nand)
        return
    if args.ca_cert is None:
        fail("--ca-cert is required unless --revert is used")

    workdir = Path(tempfile.mkdtemp(prefix="ipod-nand-trust-ca."))
    original = workdir / "volume-original.img"
    patched = workdir / "volume-patched.img"
    try:
        certificate, subject = load_ca_certificate(args.ca_cert, workdir)
        ca_sha256 = hashlib.sha256(certificate).hexdigest()
        previous_ca = manifest_ca_sha256(nand)
        if previous_ca is not None and previous_ca != ca_sha256:
            print("reverting previous bridge CA before rotation ...")
            revert(nand)

        print(f"reconstructing HFSX volume from {nand} ...")
        NAND_SUPPORT.reconstruct_volume(nand, original)
        NAND_SUPPORT.run(["cp", "-c", str(original), str(patched)])

        device, mount_point = NAND_SUPPORT.attach(patched, readonly=False)
        try:
            store = mount_point / TRUST_STORE
            if not store.is_file():
                fail(f"guest trust store missing: {TRUST_STORE}")
            changed_store, row_count = inject_store(store, certificate, subject)
            print(
                f"  {'updated' if changed_store else 'already present in'} "
                f"{TRUST_STORE} ({row_count} trusted roots)"
            )
        finally:
            NAND_SUPPORT.detach(device)

        if not changed_store:
            print(f"CA sha256: {ca_sha256}")
            return

        print("  comparing fsck_hfs complaints before/after ...")
        baseline = NAND_SUPPORT.fsck_complaints(original)
        after = NAND_SUPPORT.fsck_complaints(patched)
        new_complaints = after - baseline
        if new_complaints:
            fail(
                "patch introduced new fsck_hfs complaints:\n"
                + "\n".join(sorted(new_complaints))
            )
        print(
            f"  fsck_hfs: no new complaints "
            f"({len(baseline)} pre-existing warnings unchanged)"
        )
        changed = write_back(nand, original, patched, ca_sha256)
        print(f"patched {len(changed)} NAND pages (manifest: {nand / MANIFEST_NAME})")
        print(f"CA sha256: {ca_sha256}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
