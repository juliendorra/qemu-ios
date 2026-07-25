#!/usr/bin/env python3
"""Inject a file into an S5L8900 guest HFS+ image, reproducibly.

This generalises the file-injection technique proven by
`scripts/ipod-nand-restore-dns.py` (which restores the mDNSResponder launchd
plist into the N45AP root volume, and whose injected file the guest reads —
Safari resolves DNS through it). The point here is a *stable, repeatable* tool:
put an arbitrary host file at an arbitrary guest path inside an HFS+ image, so
higher-level tools (e.g. the M68AP data-ark hacktivation) don't hand-roll the
steps each time.

Two facts learned the hard way (see IPHONE_2G_BRINGUP_HANDOFF.md):

1. **The guest DOES read macOS-written HFS files.** An earlier belief that the
   2007-era iOS HFS driver can't traverse a modern-macOS catalog was wrong; the
   DNS restore and the data-ark injection both prove otherwise. So a plain
   `hdiutil attach -readwrite` + copy is sufficient for the *file to be seen*.

2. **Ownership can matter for the *consumer*, not the visibility.** hdiutil
   mounts without owners, so a copied file lands as the invoking uid, not
   root:wheel. `root` still *reads* it (lockdownd loads a uid-501 data ark
   fine), but some consumers reject non-root files (launchctl skips "dubious"
   non-root plists). `--root-owned` rewrites the new records' BSD ownership to
   root:wheel directly in the raw image (the DNS tool's technique), for those
   cases.

Also: content format matters at the consumer, not here — e.g. lockdownd's
data_ark.plist must be a *binary* plist; injecting XML makes it log
"Could not load". This tool copies bytes verbatim; produce the right format
upstream.

Usage:
  scripts/inject-guest-file.py \
      --image  <hfs image, e.g. an empty /var partition>       \
      --src    <host file to inject>                            \
      --dest   /root/Library/Lockdown/data_ark.plist           \
      [--root-owned]  [--out <new image>]  [--verify]

`--dest` is relative to the volume root (the partition), e.g. for the M68AP
data partition (disk0s2 = /private/var) use `/root/...` to land at
`/var/root/...`. With no `--out`, the image is edited in place.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import attached


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def hfs_timestamp_is_recent(create_date: int) -> bool:
    # HFS+ dates are seconds since 1904-01-01; "recent" = created in this run.
    # Anything within the last hour of wall time (converted) is ours.
    HFS_EPOCH_OFFSET = 2082844800  # 1970 - 1904 in seconds
    now_hfs = int(time.time()) + HFS_EPOCH_OFFSET
    return 0 < now_hfs - create_date < 3600


def rewrite_owner_records(image: Path, names: list[str]) -> int:
    """Rewrite the BSD owner of freshly created HFS+ catalog records whose file
    names are in `names` to root:wheel. Mirrors ipod-nand-restore-dns.py's
    fix_record_in_window, generalised to arbitrary names and to file records.
    Returns the number of records rewritten."""
    data = bytearray(image.read_bytes())
    fixed = 0
    for name in names:
        needle = name.encode("utf-16-be")
        start = 0
        while True:
            i = data.find(needle, start)
            if i < 0:
                break
            start = i + 2
            # HFSPlusCatalogKey: keyLength(2) parentID(4) nameLength(2) name...
            name_length_offset = i - 2
            key_start = name_length_offset - 4 - 2
            if key_start < 0:
                continue
            (key_length,) = struct.unpack_from(">H", data, key_start)
            (name_length,) = struct.unpack_from(">H", data, name_length_offset)
            if name_length != len(name) or key_length != 6 + 2 * name_length:
                continue
            record = i + len(needle)
            if record + 40 > len(data):
                continue
            (record_type,) = struct.unpack_from(">H", data, record)
            if record_type not in (0x0001, 0x0002):  # folder or file
                continue
            (create_date,) = struct.unpack_from(">I", data, record + 12)
            if not hfs_timestamp_is_recent(create_date):
                continue
            owner_offset = record + 32
            uid, gid = struct.unpack_from(">II", data, owner_offset)
            mode = struct.unpack_from(">BBH", data, owner_offset + 8)[2]
            ftype = mode & 0o170000
            newmode = (0o040755 if record_type == 0x0001 else 0o100644)
            struct.pack_into(">II", data, owner_offset, 0, 0)
            struct.pack_into(">BBH", data, owner_offset + 8, 0, 0, newmode)
            print(f"  {name}: uid {uid} gid {gid} mode 0o{mode:o} "
                  f"-> root:wheel 0o{newmode:o}")
            fixed += 1
    if fixed:
        image.write_bytes(bytes(data))
    return fixed


def attach_rw(image: Path, mountpoint: Path) -> str:
    out = run(["hdiutil", "attach", "-readwrite", "-mountpoint",
               str(mountpoint), str(image)]).stdout
    for line in out.splitlines():
        tok = line.split()[0] if line.split() else ""
        if tok.startswith("/dev/disk"):
            return tok
    raise SystemExit(f"could not parse hdiutil attach output:\n{out}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", type=Path, required=True,
                    help="HFS+ image to inject into (a partition volume)")
    ap.add_argument("--src", type=Path, required=True,
                    help="host file to inject")
    ap.add_argument("--dest", required=True,
                    help="guest path relative to the volume root, e.g. "
                         "/root/Library/Lockdown/data_ark.plist")
    ap.add_argument("--out", type=Path,
                    help="write to a new image (default: edit in place)")
    ap.add_argument("--root-owned", action="store_true",
                    help="rewrite the new file/dir records to root:wheel "
                         "(needed by consumers that reject non-root files)")
    ap.add_argument("--verify", action="store_true",
                    help="fsck_hfs the result")
    args = ap.parse_args()

    target = args.out or args.image
    if args.out:
        shutil.copy2(args.image, args.out)

    dest = args.dest.lstrip("/")
    mnt = Path(f"/tmp/inject-guest-{os.getpid()}-{int(time.time())}")
    mnt.mkdir(exist_ok=True)
    created = []
    # `attached()` guarantees the detach even on failure: a leaked mount pins
    # its backing image's space and is a real disk-filler (lab_workspace.py).
    with attached(target, mountpoint=mnt, readonly=False):
        full = mnt / dest
        for parent in list(full.parents)[:-1]:
            if mnt in parent.parents and not parent.exists():
                created.append(parent.name)
        full.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(args.src, full)
        created.append(full.name)
        subprocess.run(["sync"])
        print(f"injected {args.src} -> /{dest}")
    with contextlib.suppress(OSError):
        mnt.rmdir()

    if args.root_owned:
        n = rewrite_owner_records(target, sorted(set(created)))
        print(f"rewrote {n} record(s) to root:wheel")

    if args.verify:
        dev = run(["hdiutil", "attach", "-imagekey",
                   "diskimage-class=CRawDiskImage", "-nomount",
                   str(target)]).stdout.split()[0]
        try:
            fsck = subprocess.run(["fsck_hfs", "-n", dev],
                                  capture_output=True, text=True)
            print("fsck_hfs:", fsck.stdout.strip().splitlines()[-1]
                  if fsck.stdout.strip() else "(no output)")
        finally:
            subprocess.run(["hdiutil", "detach", dev], capture_output=True)

    print(f"done: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
