#!/usr/bin/env python3
"""Host-side certificate support for the S5L8900 HTTPS bridge.

The bridge CA is deliberately generated outside the repository and app bundle.
Only its public certificate is passed to the NAND patcher.  This module uses
the OpenSSL command-line tool because the system Python shipped by macOS does
not include a certificate-building package.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


CA_NOT_BEFORE = "20200101000000Z"
CA_NOT_AFTER = "20451231235959Z"
LEAF_NOT_BEFORE = "20200101000000Z"
LEAF_NOT_AFTER = "20451231235959Z"
CA_SUBJECT = "/O=S5L8900 Emulator/CN=S5L8900 HTTPS Bridge Root"
LEAF_CIPHERS = "AES128-SHA:AES256-SHA:DES-CBC3-SHA:RC4-SHA:RC4-MD5"
HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(map(str, command))} failed:\n{result.stdout}{result.stderr}"
        )
    return result


def find_openssl():
    requested = os.environ.get("S5L8900_OPENSSL")
    candidates = [
        requested,
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        shutil.which("openssl"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        help_text = subprocess.run(
            [candidate, "x509", "-help"], capture_output=True, text=True
        ).stderr
        if "-not_before" in help_text and "-not_after" in help_text:
            return candidate
    raise RuntimeError(
        "OpenSSL with x509 -not_before/-not_after support is required; "
        "set S5L8900_OPENSSL to an OpenSSL 3 executable"
    )


def normalize_hostname(hostname):
    try:
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("invalid TLS server name") from error
    if not HOST_RE.fullmatch(hostname):
        raise ValueError(f"invalid TLS server name: {hostname!r}")
    return hostname


def _chmod_private(path):
    path.chmod(0o600)


def ensure_ca(state_dir):
    """Create (once) and return paths for the per-install CA and leaf key."""
    state_dir = Path(state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    ca_key = state_dir / "bridge-ca.key"
    ca_pem = state_dir / "bridge-ca.pem"
    ca_der = state_dir / "bridge-ca.der"
    leaf_key = state_dir / "bridge-leaf.key"

    present = [ca_key.exists(), ca_pem.exists(), ca_der.exists()]
    if any(present) and not all(present):
        raise RuntimeError(
            f"incomplete CA state in {state_dir}; refusing to replace a key or certificate"
        )

    openssl = find_openssl()
    if not all(present):
        with tempfile.TemporaryDirectory(prefix="ca-new.", dir=state_dir) as temporary:
            temporary = Path(temporary)
            new_key = temporary / ca_key.name
            new_pem = temporary / ca_pem.name
            new_der = temporary / ca_der.name
            run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-noenc",
                    "-sha1",
                    "-keyout",
                    str(new_key),
                    "-out",
                    str(new_pem),
                    "-subj",
                    CA_SUBJECT,
                    "-set_serial",
                    "0x53554C38393030",
                    "-not_before",
                    CA_NOT_BEFORE,
                    "-not_after",
                    CA_NOT_AFTER,
                    "-addext",
                    "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext",
                    "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext",
                    "subjectKeyIdentifier=hash",
                ]
            )
            run(
                [
                    openssl,
                    "x509",
                    "-in",
                    str(new_pem),
                    "-outform",
                    "DER",
                    "-out",
                    str(new_der),
                ]
            )
            _chmod_private(new_key)
            os.replace(new_key, ca_key)
            os.replace(new_pem, ca_pem)
            os.replace(new_der, ca_der)
            ca_pem.chmod(0o644)
            ca_der.chmod(0o644)

    if not leaf_key.exists():
        temporary = state_dir / f".{leaf_key.name}.new"
        run(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(temporary),
            ]
        )
        _chmod_private(temporary)
        os.replace(temporary, leaf_key)

    _chmod_private(ca_key)
    _chmod_private(leaf_key)
    run([openssl, "verify", "-CAfile", str(ca_pem), str(ca_pem)])
    return {
        "state": state_dir,
        "openssl": openssl,
        "ca_key": ca_key,
        "ca_pem": ca_pem,
        "ca_der": ca_der,
        "leaf_key": leaf_key,
    }


def leaf_certificate(state_dir, hostname):
    """Return a cached leaf certificate for hostname, minting it if needed."""
    hostname = normalize_hostname(hostname)
    paths = ensure_ca(state_dir)
    cache = paths["state"] / "leaf-certificates"
    cache.mkdir(exist_ok=True, mode=0o700)
    cache.chmod(0o700)
    cache_name = hashlib.sha256(hostname.encode("ascii")).hexdigest() + ".pem"
    certificate = cache / cache_name
    if certificate.exists():
        return certificate, paths["leaf_key"], paths

    serial = "0x01" + hashlib.sha256(
        paths["ca_der"].read_bytes() + b"\0" + hostname.encode("ascii")
    ).hexdigest()[:38]
    with tempfile.TemporaryDirectory(prefix="leaf-new.", dir=cache) as temporary:
        temporary = Path(temporary)
        extension_file = temporary / "extensions.cnf"
        request = temporary / "leaf.csr"
        new_certificate = temporary / certificate.name
        extension_file.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName=DNS:{hostname}\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
        )
        run(
            [
                paths["openssl"],
                "req",
                "-new",
                "-key",
                str(paths["leaf_key"]),
                "-subj",
                f"/O=S5L8900 HTTPS Bridge/CN={hostname}",
                "-out",
                str(request),
            ]
        )
        run(
            [
                paths["openssl"],
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(paths["ca_pem"]),
                "-CAkey",
                str(paths["ca_key"]),
                "-set_serial",
                serial,
                "-not_before",
                LEAF_NOT_BEFORE,
                "-not_after",
                LEAF_NOT_AFTER,
                "-sha1",
                "-extfile",
                str(extension_file),
                "-out",
                str(new_certificate),
            ]
        )
        run(
            [
                paths["openssl"],
                "verify",
                "-CAfile",
                str(paths["ca_pem"]),
                "-verify_hostname",
                hostname,
                str(new_certificate),
            ]
        )
        new_certificate.chmod(0o644)
        os.replace(new_certificate, certificate)
    return certificate, paths["leaf_key"], paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--mint", metavar="HOSTNAME")
    args = parser.parse_args()
    if args.mint:
        certificate, _, paths = leaf_certificate(args.state, args.mint)
        print(certificate)
    else:
        paths = ensure_ca(args.state)
        print(paths["ca_pem"])


if __name__ == "__main__":
    main()
