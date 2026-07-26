#!/usr/bin/env python3
"""Gate 0: verify a user-supplied iPhone1,1 IPSW against its firmware profile.

Every later gate (NOR build, WMR init, kernel boot, SpringBoard) costs minutes
and produces confusing failures when the artifacts are simply from the wrong
build. This check costs seconds and runs before any of them. It answers one
question: "are these images really build X, and is our profile for X right?"

It measures, from the IPSW itself:

  * the 8900 container format byte      (0x03 encrypted / 0x04 plaintext)
  * the security epoch                  (8900 header +0x3e, IMG2 header +0x0a)
  * the bootloader build string         ("iBoot-159" / "iBoot-204")
  * the FIL/WMR signature constant      the bootloader compares bank0/page0
                                        word0 against
  * optionally the IPSW's SHA-1

and compares each against scripts/firmware_profiles.py. A mismatch is an error,
not a warning -- see IPHONE_OS_1X_VERSIONS.md for how the expected values were
derived.

Usage:
  python3 scripts/iphone-firmware-acceptance.py --build 1A543a <unpacked IPSW dir>
  python3 scripts/iphone-firmware-acceptance.py --build 4A102 --ipsw <file.ipsw> <dir>
  python3 scripts/iphone-firmware-acceptance.py --all-known <dir-of-unpacked-ipsws>

No firmware is written or copied; this only reads.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import firmware_profiles

ALL_FLASH = "Firmware/all_flash/all_flash.m68ap.production"
IBOOT = "iBoot.m68ap.RELEASE.img2"
CONTAINER_HEADER_LEN = 0x800

_failures: list[str] = []


def check(cond: bool, msg: str, detail: str = "") -> bool:
    print(("  ok   " if cond else " FAIL  ") + msg + (f"  [{detail}]" if detail else ""))
    if not cond:
        _failures.append(msg)
    return cond


def decrypt_payload(raw: bytes) -> bytes:
    """Return the plaintext 8900 payload, whichever format it is stored in."""
    size = struct.unpack("<I", raw[0x0c:0x10])[0]
    if raw[7] == firmware_profiles.FORMAT_PLAINTEXT:
        return raw[CONTAINER_HEADER_LEN:CONTAINER_HEADER_LEN + size]
    body = raw[CONTAINER_HEADER_LEN:CONTAINER_HEADER_LEN + size - size % 16]
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-nopad",
         "-K", firmware_profiles.GID_KEY, "-iv", "0" * 32],
        input=body, stdout=subprocess.PIPE, check=True)
    return proc.stdout


def signature_constants(blob: bytes) -> collections.Counter:
    """Count FIL-signature-shaped words ('000C', '200C', '300C', ...).

    The signature is stored little-endian, so the word 0x43303033 appears in the
    binary as the ASCII bytes "300C". Scanning for that shape finds it without
    needing to know where the comparison lives.
    """
    return collections.Counter(
        m.group().decode() for m in re.finditer(rb"[0-9A-Z]0{2}C", blob))


def verify(root: Path, profile, ipsw_path: Path | None) -> None:
    print(f"\n=== {profile.build} ({profile.version}, {profile.device}) "
          f"against {root} ===")

    all_flash = root / ALL_FLASH
    if not all_flash.is_dir():
        check(False, f"{ALL_FLASH} present")
        return

    iboot_path = all_flash / IBOOT
    if not iboot_path.is_file():
        check(False, f"{IBOOT} present")
        return
    raw = iboot_path.read_bytes()

    check(raw[:4] == b"8900", "iBoot is an 8900 container", raw[:7].decode("latin1"))

    fmt = raw[7]
    check(fmt == profile.img_format, "8900 format byte matches profile",
          f"got {fmt:#04x}, want {profile.img_format:#04x}")

    epoch = struct.unpack("<H", raw[0x3e:0x40])[0]
    check(epoch == profile.epoch, "security epoch matches profile",
          f"got {epoch}, want {profile.epoch}")

    payload = decrypt_payload(raw)
    check(payload[:4] == b"2gmI", "payload unwraps to an IMG2 container",
          payload[:4].decode("latin1", "replace"))
    check(payload[4:8] == b"tobi", "IMG2 type is iBoot ('tobi')")

    img2_epoch = struct.unpack("<H", payload[0x0a:0x0c])[0]
    check(img2_epoch == profile.epoch, "IMG2 header epoch matches profile",
          f"got {img2_epoch}, want {profile.epoch}")

    body = payload[0x400:]
    versions = {m.group().decode() for m in re.finditer(rb"iBoot-[0-9]+", body)}
    check(versions == {profile.iboot}, "bootloader build matches profile",
          f"got {sorted(versions) or 'none'}, want {profile.iboot}")

    sigs = signature_constants(body)
    want_sig = profile.fil_signature_bytes.decode("ascii")
    check(list(sigs) == [want_sig],
          "FIL/WMR signature constant matches profile",
          f"got {dict(sigs) or 'none'}, want {want_sig}")

    if profile.root_dmg:
        check((root / profile.root_dmg).is_file(),
              f"root filesystem {profile.root_dmg} present")

    if ipsw_path is not None and profile.ipsw_sha1:
        digest = hashlib.sha1(ipsw_path.read_bytes()).hexdigest()
        check(digest == profile.ipsw_sha1, "IPSW SHA-1 matches profile",
              f"got {digest[:12]}…")

    for note in profile.notes:
        print(f"  note  {note}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    firmware_profiles.add_build_argument(ap)
    ap.add_argument("--ipsw", type=Path, default=None,
                    help="the .ipsw file itself, to additionally check its SHA-1")
    ap.add_argument("--all-known", action="store_true",
                    help="treat the path as a directory of unpacked IPSWs and "
                         "verify every build whose directory is present "
                         "(auto-detected by Restore.plist)")
    ap.add_argument("root", type=Path,
                    help="unpacked IPSW directory (or its parent, with --all-known)")
    args = ap.parse_args()

    if args.all_known:
        found = 0
        for child in sorted(p for p in args.root.iterdir() if p.is_dir()):
            plist = child / "Restore.plist"
            if not plist.is_file():
                continue
            text = plist.read_text(errors="replace")
            m = re.search(r"<key>ProductBuildVersion</key>\s*<string>([^<]+)<",
                          text)
            if not m:
                continue
            try:
                profile = firmware_profiles.get(m.group(1))
            except KeyError as exc:
                print(f"\n=== {child}: {exc}")
                _failures.append(str(exc))
                continue
            verify(child, profile, None)
            found += 1
        if not found:
            print(f"no unpacked IPSWs found under {args.root}")
            return 1
    else:
        verify(args.root, firmware_profiles.get(args.build), args.ipsw)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("all firmware acceptance checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
