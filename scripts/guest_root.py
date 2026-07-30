#!/usr/bin/env python3
"""Attach a guest root image, reusing an existing attachment if there is one.

A disk image can only be attached ONCE. Two sessions share this tree and the
other one keeps the root images mounted for its own work, so a plain
`hdiutil attach` fails with "Resource busy" -- and the failure is silent in the
worst way: the script carries on with a mountpoint directory it created but
nobody populated, and dies later on a FileNotFoundError naming a path inside it,
which reads like a missing firmware file rather than a busy image.

Every probe here had its own copy of the attach/detach lines, so the bug had to
be found and fixed four times. It lives here now.

    mnt, mine = guest_root.attach(img)
    ...
    guest_root.detach(mnt, mine)        # only detaches what we attached
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def existing_mount(img: Path) -> Path | None:
    """Where `img` is already attached, or None.

    `hdiutil info` prints an "image-path : <path>" line per image followed by
    its device/mountpoint lines. The mountpoint line is tab-separated and may
    have only TWO fields (`/dev/disk5\t\t/private/tmp/m68_10`), which is what a
    `len(parts) >= 3` check got wrong the first time.
    """
    img = Path(img).resolve()
    out = subprocess.run(["hdiutil", "info"], capture_output=True,
                         text=True).stdout
    cur, found = None, None
    for line in out.splitlines():
        if line.startswith("image-path"):
            cur = line.split(":", 1)[1].strip()
        elif line.startswith("/dev/disk") and cur:
            parts = line.split()
            if len(parts) >= 2 and parts[-1].startswith("/"):
                try:
                    if Path(cur).resolve() == img:
                        found = Path(parts[-1])
                except OSError:
                    pass
    return found


def attach(img: Path, tag: str = "guest-root") -> tuple[Path | None, bool]:
    """(mountpoint, attached_by_us). Raises RuntimeError if it cannot be read."""
    img = Path(img)
    have = existing_mount(img)
    if have is not None:
        return have, False

    import os
    mnt = Path(f"/tmp/{tag}-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                        "-mountpoint", str(mnt), str(img)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Lost a race, or busy for another reason: look again before failing.
        have = existing_mount(img)
        if have is not None:
            return have, False
        raise RuntimeError(f"cannot attach {img}: {r.stderr.strip()}")
    return mnt, True


def detach(mnt: Path | None, mine: bool) -> None:
    if mnt is not None and mine:
        subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)
