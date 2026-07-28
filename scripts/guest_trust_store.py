#!/usr/bin/env python3
"""Insert a public CA into an iPhone OS 1.x system trust store.

This is the part of `ipod-nand-restore-dns.py`'s sibling
`ipod-nand-trust-ca.py` that is about the GUEST, not about the NAND: how
Security.framework's `TrustStore.sqlite3` stores a trusted root, and how to
add one to it without disturbing the 110 factory rows.

It was factored out when the M68AP bundles needed the same trust. The iPod's
NAND plumbing does NOT port -- it reconstructs a single-partition device dump
page by page, and an M68AP NAND is generated with the real two-partition
iPhone layout -- but this half ports unchanged, because both devices ship the
same store:

    build   iPhone OS   schema                                rows
    1A543a  1.0         tsettings(sha1,subj,tset,data)        110
    4A102   1.1.4       tsettings(sha1,subj,tset,data)        110

`validate_store()` is what keeps that claim honest: it asserts the exact
`CREATE TABLE` text rather than trusting the table to look familiar, so a
build with a different Security.framework fails loudly instead of getting a
row it will not read.

The CA private key is neither accepted nor copied; `load_ca_certificate()`
refuses a file that contains one.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from ipod_tls_common import find_openssl

# Relative to the volume root of the guest's *root* partition.
TRUST_STORE = Path("System/Library/Frameworks/Security.framework/TrustStore.sqlite3")

# tsettings.tset holds a plist of per-certificate trust overrides. An empty
# array means "no overrides": trust this root for everything, exactly like the
# factory rows.
TRUST_SETTINGS = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array/>
</plist>
"""

EXPECTED_SCHEMA = (
    "CREATE TABLE tsettings(sha1 BLOB NOT NULL DEFAULT '',"
    "subj BLOB NOT NULL DEFAULT '',tset BLOB,data BLOB,PRIMARY KEY(sha1))"
)


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
    """Return (DER bytes, subject DER) for a self-signed CA certificate."""
    source = Path(path).resolve()
    raw = source.read_bytes()
    if b"PRIVATE KEY" in raw:
        fail("--ca-cert must contain only a public certificate, not a private key")
    openssl = find_openssl()
    der = Path(workdir) / "bridge-ca.der"
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
    if schema is None or schema[0] != EXPECTED_SCHEMA:
        fail("unexpected iPhone OS trust-store schema")


def inject_store(database, certificate, subject):
    """Add `certificate` as a trusted root. Returns (changed, row count)."""
    digest = hashlib.sha1(certificate).digest()
    connection = sqlite3.connect(database)
    try:
        # DELETE journalling keeps the store one file: a -wal sidecar would be
        # invisible to the 2007 SQLite in the guest.
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


def inject_into_volume(mount_point, certificate, subject):
    """Inject into the trust store of an already-mounted root volume."""
    store = Path(mount_point) / TRUST_STORE
    if not store.is_file():
        fail(f"guest trust store missing: {TRUST_STORE}")
    return inject_store(store, certificate, subject)
