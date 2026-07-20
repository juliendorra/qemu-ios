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
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from ipod_tls_common import find_openssl


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
TRUST_STORE = Path("System/Library/Frameworks/Security.framework/TrustStore.sqlite3")
MANIFEST_NAME = "https-bridge-ca-patch-manifest.txt"
BACKUP_DIR_NAME = "https-bridge-ca-patch-backup"
TRUST_SETTINGS = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array/>
</plist>
"""


def fail(message):
    raise SystemExit(f"error: {message}")


def _tlv(data, offset):
    if offset + 2 > len(data):
        fail("truncated DER certificate")
    tag = data[offset]
    first_length = data[offset + 1]
    if first_length < 0x80:
        header = 2
        length = first_length
    else:
        count = first_length & 0x7F
        if count == 0 or count > 4 or offset + 2 + count > len(data):
            fail("unsupported DER length")
        header = 2 + count
        length = int.from_bytes(data[offset + 2:offset + 2 + count], "big")
    value = offset + header
    end = value + length
    if end > len(data):
        fail("truncated DER value")
    return tag, value, end


def certificate_subject_der(certificate):
    """Extract the raw contents of the Subject Name from an X.509 DER cert.

    Security.framework's tsettings.subj omits the outer SEQUENCE tag/length;
    the factory rows start directly with the first SET in the Name.
    """
    tag, certificate_value, certificate_end = _tlv(certificate, 0)
    if tag != 0x30 or certificate_end != len(certificate):
        fail("CA certificate is not one DER X.509 sequence")
    tag, tbs_value, _ = _tlv(certificate, certificate_value)
    if tag != 0x30:
        fail("CA certificate has no TBSCertificate sequence")
    offset = tbs_value
    tag, _, end = _tlv(certificate, offset)
    if tag == 0xA0:  # explicit version
        offset = end
    for expected in (0x02, 0x30, 0x30, 0x30):  # serial, signature, issuer, validity
        tag, _, offset = _tlv(certificate, offset)
        if tag != expected:
            fail("unexpected TBSCertificate field while locating subject")
    subject_start = offset
    tag, subject_value, subject_end = _tlv(certificate, subject_start)
    if tag != 0x30:
        fail("CA certificate subject is not a DER Name sequence")
    return certificate[subject_value:subject_end]


def load_ca_certificate(path, workdir):
    source = Path(path).resolve()
    raw = source.read_bytes()
    if b"PRIVATE KEY" in raw:
        fail("--ca-cert must contain only a public certificate, not a private key")
    openssl = find_openssl()
    der = workdir / "bridge-ca.der"
    result = subprocess.run(
        [openssl, "x509", "-in", str(source), "-outform", "DER", "-out", str(der)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        fail(f"cannot parse CA certificate:\n{result.stderr}")
    verify = subprocess.run(
        [openssl, "verify", "-CAfile", str(source), str(source)],
        capture_output=True,
        text=True,
    )
    if verify.returncode:
        fail(f"CA certificate is not self-verifying:\n{verify.stdout}{verify.stderr}")
    details = subprocess.run(
        [openssl, "x509", "-in", str(source), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if "CA:TRUE" not in details:
        fail("certificate is not marked as a CA")
    certificate = der.read_bytes()
    return certificate, certificate_subject_der(certificate)


def validate_store(connection):
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tsettings'"
    ).fetchone()
    expected = "CREATE TABLE tsettings(sha1 BLOB NOT NULL DEFAULT '',subj BLOB NOT NULL DEFAULT '',tset BLOB,data BLOB,PRIMARY KEY(sha1))"
    if schema is None or schema[0] != expected:
        fail("unexpected iPhone OS trust-store schema")


def inject_store(database, certificate, subject):
    digest = hashlib.sha1(certificate).digest()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        validate_store(connection)
        current = connection.execute(
            "SELECT data FROM tsettings WHERE sha1=?", (digest,)
        ).fetchone()
        if current is not None and current[0] == certificate:
            return False, connection.execute("SELECT count(*) FROM tsettings").fetchone()[0]
        # The subject is stable across per-install CA rotations.  Remove a
        # stale bridge root with the same subject before inserting this one.
        connection.execute("DELETE FROM tsettings WHERE subj=?", (subject,))
        connection.execute(
            "INSERT INTO tsettings(sha1,subj,tset,data) VALUES(?,?,?,?)",
            (digest, subject, TRUST_SETTINGS, certificate),
        )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fail(f"SQLite integrity check failed: {integrity}")
        stored = connection.execute(
            "SELECT subj,tset,data FROM tsettings WHERE sha1=?", (digest,)
        ).fetchone()
        if stored != (subject, TRUST_SETTINGS, certificate):
            fail("CA trust-store row did not round-trip exactly")
        return True, connection.execute("SELECT count(*) FROM tsettings").fetchone()[0]
    finally:
        connection.close()


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
