#!/usr/bin/env python3
"""Restore the mDNSResponder LaunchDaemon in an iPod Touch 1G NAND tree.

The distributed n45ap NAND image was pruned by its original author: ten of the
sixteen /System/Library/LaunchDaemons property lists were deleted, including
com.apple.mDNSResponder.plist.  On iPhone OS 1 every hostname lookup
(getaddrinfo/gethostbyname via libinfo's mdns module) is brokered by the
mDNSResponder daemon, so without its launchd job Safari fails hostname
resolution locally before a single DNS packet is emitted.  The daemon binary
/usr/sbin/mDNSResponder is still present in the image; only the job plist is
missing.  This tool puts the plist back.

How it works:
  1. The QEMU NAND tree stores one file per physical page (bank<N>/<page>.page,
     2048 data + 64 spare bytes).  The guest filesystem is a single HFSX
     volume mapped linearly: logical offset = (page*8 + bank - 206851) * 2048.
     The emulator never reads back guest page writes (*_new.page), so the
     original pages are authoritative.
  2. The volume is reconstructed into a temporary raw image, attached
     read-write with hdiutil, and the plist (byte-identical to the Apple
     original recovered from surviving free-space blocks of the same image)
     is written into /System/Library/LaunchDaemons.
  3. After detaching, the new catalog record's BSD ownership is rewritten to
     root:wheel in the raw image (launchctl skips "dubious" non-root plists;
     hdiutil mounts without owners), and the result is verified with
     fsck_hfs.
  4. Only the 2048-byte logical pages that actually changed are written back
     to the NAND tree.  A manifest listing the touched pages and backups of
     their prior contents is stored next to the tree, making the patch
     reversible with --revert.

The tool never modifies the source tree unless it *is* the --nand target;
point --nand at a staged copy for experiments.  Idempotent: a second run
detects the plist and exits without changes.
"""

import argparse
import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

NUM_BANKS = 8
PAGE_DATA = 2048
PAGE_SPARE = 64
PAGE_SIZE = PAGE_DATA + PAGE_SPARE
FILESYSTEM_START_VPN = 206851  # mirrors FILESYSTEM_START_VPN in ipod_touch_nand.h
FILESYSTEM_NUM_PAGES = 132854
PLIST_NAME = "com.apple.mDNSResponder.plist"
LAUNCH_DAEMONS = "System/Library/LaunchDaemons"
MANIFEST_NAME = "mdnsresponder-patch-manifest.txt"
BACKUP_DIR_NAME = "mdnsresponder-patch-backup"

# Byte-identical to the Apple original from the Snowbird3A101a.N45 image
# (recovered from free-space blocks of the distributed NAND; 780 bytes).
PLIST_CONTENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>com.apple.mDNSResponder</string>
\t<key>OnDemand</key>
\t<false/>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/usr/sbin/mDNSResponder</string>
\t\t<string>-launchd</string>
\t</array>
\t<key>MachServices</key>
\t<dict>
\t\t<key>com.apple.mDNSResponder</key>
\t\t<true/>
\t</dict>
\t<key>Sockets</key>
\t<dict>
\t\t<key>Listeners</key>
\t\t<dict>
\t\t\t<key>SockFamily</key>
\t\t\t<string>Unix</string>
\t\t\t<key>SockPathName</key>
\t\t\t<string>/var/run/mDNSResponder</string>
\t\t\t<key>SockPathMode</key>
\t\t\t<integer>438</integer>
\t\t</dict>
\t</dict>
\t<key>ServiceIPC</key>
\t<true/>
</dict>
</plist>
"""


def fail(message: str) -> "sys.NoReturn":
    raise SystemExit(f"error: {message}")


def page_path(nand: Path, vpn: int) -> Path:
    return nand / f"bank{vpn % NUM_BANKS}" / f"{vpn // NUM_BANKS}.page"


def read_logical_page(nand: Path, vpn: int) -> bytes:
    path = page_path(nand, vpn)
    if not path.is_file():
        return b"\x00" * PAGE_DATA
    data = path.read_bytes()
    if len(data) != PAGE_SIZE:
        fail(f"unexpected page size for {path}: {len(data)}")
    return data[:PAGE_DATA]


def reconstruct_volume(nand: Path, image: Path) -> None:
    with image.open("wb") as out:
        for vpn in range(FILESYSTEM_START_VPN,
                         FILESYSTEM_START_VPN + FILESYSTEM_NUM_PAGES):
            out.write(read_logical_page(nand, vpn))


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        fail(f"{' '.join(command)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def attach(image: Path, readonly: bool) -> tuple[str, Path]:
    command = ["hdiutil", "attach", "-imagekey",
               "diskimage-class=CRawDiskImage", "-nobrowse"]
    if readonly:
        command.append("-readonly")
    command.append(str(image))
    output = run(command).stdout
    device = None
    mount_point = None
    for line in output.splitlines():
        parts = line.split("\t")
        parts = [part.strip() for part in parts if part.strip()]
        if parts and parts[0].startswith("/dev/disk"):
            device = parts[0]
            if len(parts) > 1 and parts[-1].startswith("/Volumes/"):
                mount_point = Path(parts[-1])
    if device is None or mount_point is None:
        fail(f"could not parse hdiutil attach output:\n{output}")
    return device, mount_point


def detach(device: str) -> None:
    run(["hdiutil", "detach", device])


def hfs_timestamp_is_recent(seconds_since_1904: int) -> bool:
    # 0xE0000000 is mid-2023 in the HFS epoch; anything newer than that is a
    # record this tool created, anything older is 2007/2022 free-space debris.
    return seconds_since_1904 >= 0xE0000000


def find_all(image: Path, needle: bytes) -> list[int]:
    """Offsets of needle in image, streaming in chunks to bound memory."""
    offsets = []
    chunk_size = 8 * 1024 * 1024
    overlap = len(needle) - 1
    position = 0
    tail = b""
    with image.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buffer = tail + chunk
            base = position - len(tail)
            start = 0
            while True:
                index = buffer.find(needle, start)
                if index < 0:
                    break
                offsets.append(base + index)
                start = index + 1
            tail = buffer[-overlap:] if overlap else b""
            position += len(chunk)
    return offsets


def fix_catalog_ownership(image: Path) -> int:
    """Set uid/gid of freshly created catalog records for PLIST_NAME to 0/0."""
    name_utf16 = PLIST_NAME.encode("utf-16-be")
    patched = 0
    candidates = find_all(image, name_utf16)
    with image.open("r+b") as f:
        for index in candidates:
            window_start = max(0, index - 8)
            f.seek(window_start)
            data = bytearray(f.read(8 + len(name_utf16) + 64))
            local = index - window_start
            patch = fix_record_in_window(data, local, index)
            if patch is None:
                continue
            f.seek(window_start)
            f.write(bytes(data))
            patched += 1
    return patched


def fix_record_in_window(data: bytearray, index: int, absolute: int):
    """Rewrite ownership of a catalog record whose name sits at buffer offset
    index; the buffer holds [index-8, index+len(name)+64) of the image.
    Returns True if the buffer was modified, None if this occurrence is not a
    freshly created live catalog record."""
    name_utf16 = PLIST_NAME.encode("utf-16-be")
    # HFSPlusCatalogKey: keyLength(2) parentID(4) nameLength(2) name...
    name_length_offset = index - 2
    key_start = name_length_offset - 4 - 2
    if key_start < 0:
        return None
    (key_length,) = struct.unpack_from(">H", data, key_start)
    (name_length,) = struct.unpack_from(">H", data, name_length_offset)
    if name_length != len(PLIST_NAME):
        return None
    if key_length != 6 + 2 * name_length:
        return None
    record = index + len(name_utf16)
    (record_type,) = struct.unpack_from(">H", data, record)
    if record_type != 0x0002:  # file record
        return None
    (create_date,) = struct.unpack_from(">I", data, record + 12)
    if not hfs_timestamp_is_recent(create_date):
        return None
    owner_offset = record + 32
    uid, gid = struct.unpack_from(">II", data, owner_offset)
    mode = struct.unpack_from(">BBH", data, owner_offset + 8)[2]
    if mode & 0o170000 != 0o100000:
        fail(f"unexpected mode 0o{mode:o} in new catalog record")
    struct.pack_into(">II", data, owner_offset, 0, 0)
    struct.pack_into(">BBH", data, owner_offset + 8, 0, 0, 0o100644)
    print(f"  catalog record at 0x{absolute + len(name_utf16):x}: "
          f"uid {uid} gid {gid} mode 0o{mode:o} -> root:wheel 0o100644")
    return True


def fsck_complaints(image: Path) -> set[str]:
    """Return fsck_hfs's set of complaint lines for the image.

    The 2007-era mkfs that produced this volume predates folder counts, so
    fsck_hfs warns about every directory even on the pristine image.  Callers
    therefore compare complaint sets before/after patching instead of
    requiring a clean bill of health.
    """
    device = None
    output = run(["hdiutil", "attach", "-imagekey",
                  "diskimage-class=CRawDiskImage", "-nomount",
                  str(image)]).stdout
    for line in output.splitlines():
        token = line.split()[0] if line.split() else ""
        if token.startswith("/dev/disk"):
            device = token
    if device is None:
        fail(f"could not parse hdiutil attach -nomount output:\n{output}")
    try:
        result = subprocess.run(["fsck_hfs", "-fn", device],
                                capture_output=True, text=True)
        lines = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith(("**", "The volume", "Executing")):
                continue
            # Parenthesized lines are per-folder value details of the
            # preceding complaint; folder-count values legitimately change
            # when a directory is modified.  Compare complaint titles only.
            if line.startswith("("):
                continue
            if line:
                lines.add(line)
        return lines
    finally:
        detach(device)


def write_back(nand: Path, original: Path, patched: Path) -> list[int]:
    changed = []
    with original.open("rb") as before, patched.open("rb") as after:
        for index in range(FILESYSTEM_NUM_PAGES):
            old = before.read(PAGE_DATA)
            new = after.read(PAGE_DATA)
            if old != new:
                changed.append(FILESYSTEM_START_VPN + index)
    if not changed:
        return changed

    backup_dir = nand / BACKUP_DIR_NAME
    backup_dir.mkdir(exist_ok=True)
    manifest_lines = []
    with patched.open("rb") as after:
        for vpn in changed:
            after.seek((vpn - FILESYSTEM_START_VPN) * PAGE_DATA)
            new_data = after.read(PAGE_DATA)
            target = page_path(nand, vpn)
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
            manifest_lines.append(f"{state} vpn={vpn} file={target.relative_to(nand)}")
    manifest = nand / MANIFEST_NAME
    manifest.write_text("\n".join(manifest_lines) + "\n")
    return changed


def revert(nand: Path) -> None:
    manifest = nand / MANIFEST_NAME
    backup_dir = nand / BACKUP_DIR_NAME
    if not manifest.is_file():
        fail(f"no patch manifest found at {manifest}")
    for line in manifest.read_text().splitlines():
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nand", required=True, type=Path,
                        help="NAND tree containing bank0..bank7 (patched in place)")
    parser.add_argument("--revert", action="store_true",
                        help="undo a previous patch using its manifest")
    args = parser.parse_args()

    nand = args.nand.resolve()
    for bank in range(NUM_BANKS):
        if not (nand / f"bank{bank}").is_dir():
            fail(f"{nand} does not look like a NAND tree (missing bank{bank})")

    if args.revert:
        revert(nand)
        return

    plistlib.loads(PLIST_CONTENT)  # sanity: embedded plist must parse

    workdir = Path(tempfile.mkdtemp(prefix="ipod-nand-dns."))
    original = workdir / "volume-original.img"
    patched = workdir / "volume-patched.img"
    try:
        print(f"reconstructing HFSX volume from {nand} ...")
        reconstruct_volume(nand, original)
        # APFS clone: the patched copy only costs space for changed blocks.
        run(["cp", "-c", str(original), str(patched)])

        device, mount_point = attach(patched, readonly=False)
        try:
            subprocess.run(["mdutil", "-i", "off", str(mount_point)],
                           capture_output=True)
            daemons = mount_point / LAUNCH_DAEMONS
            if not daemons.is_dir():
                fail(f"{daemons} missing from guest filesystem")
            target = daemons / PLIST_NAME
            if target.exists():
                print(f"{PLIST_NAME} already present; nothing to do")
                return
            if not (mount_point / "usr/sbin/mDNSResponder").is_file():
                fail("/usr/sbin/mDNSResponder missing from guest filesystem")
            target.write_bytes(PLIST_CONTENT)
            os.chmod(target, 0o644)
            print(f"  wrote {LAUNCH_DAEMONS}/{PLIST_NAME} "
                  f"({len(PLIST_CONTENT)} bytes)")
        finally:
            detach(device)

        patched_records = fix_catalog_ownership(patched)
        if patched_records != 1:
            fail(f"expected to fix exactly 1 catalog record, found {patched_records}")

        print("  comparing fsck_hfs complaints before/after ...")
        baseline = fsck_complaints(original)
        after = fsck_complaints(patched)
        new_complaints = after - baseline
        if new_complaints:
            fail("patch introduced new fsck_hfs complaints:\n"
                 + "\n".join(sorted(new_complaints)))
        print(f"  fsck_hfs: no new complaints "
              f"({len(baseline)} pre-existing warnings unchanged)")

        changed = write_back(nand, original, patched)
        print(f"patched {len(changed)} NAND pages "
              f"(manifest: {nand / MANIFEST_NAME})")
        digest = hashlib.sha256(PLIST_CONTENT).hexdigest()
        print(f"plist sha256: {digest}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
