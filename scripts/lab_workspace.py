#!/usr/bin/env python3
"""Shared disk hygiene for the emulator lab tools.

These tools generate big, disposable artifacts: one M68AP NAND tree is
~310 MB (148k pages x 2112 B), a patched root HFS is ~280 MB, a reconstructed
N45AP volume ~272 MB. A handful of experiment runs silently fills a disk --
this actually happened: a 4-variant matrix plus a session of one-off runs
consumed ~4 GB and wedged the machine hard enough that no command could run.

So every lab tool imports this module instead of hand-rolling temp handling:

    from lab_workspace import (require_free_bytes, attached, Workspace,
                               prune_runs, dir_size, human)

    require_free_bytes(logs, 4 * NAND_TREE_BYTES, "4 NAND recipes")
    with attached(image, readonly=True) as mnt:      # guaranteed detach
        ...
    ws = Workspace(run_dir, keep=args.keep_artifacts)
    ws.disposable(stage_dir)                          # removed at the end
    ws.cleanup()

Conventions every tool follows:
  * a `--keep-artifacts` flag preserves everything (debugging); default is to
    delete the bulky, reproducible intermediates and keep the small evidence
    (logs, matrix.json, screenshots);
  * a space check BEFORE the expensive step, failing early with a clear
    message rather than dying half-way and leaving debris;
  * hdiutil attach/detach always via `attached()`, so a crash cannot leak a
    mounted image (leaked mounts also pin their backing files' space).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

# Rough sizes used for pre-flight space checks.
NAND_TREE_BYTES = 320 * 1024 * 1024      # one generated M68AP NAND tree
ROOT_HFS_BYTES = 280 * 1024 * 1024       # one patched root filesystem image
DATA_HFS_BYTES = 26 * 1024 * 1024        # one /var partition image


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def require_free_bytes(path: Path, needed: int, what: str = "this run",
                       headroom: float = 1.25) -> None:
    """Fail early (and legibly) when `path`'s volume cannot hold `needed`.

    `headroom` covers the copies/metadata the estimate does not model. A tool
    that dies mid-way leaves debris that makes the next run worse, so refusing
    up front is strictly better.
    """
    want = int(needed * headroom)
    have = free_bytes(path)
    if have < want:
        raise SystemExit(
            f"not enough free space for {what}: need ~{human(want)}, "
            f"have {human(have)} on {path}.\n"
            f"Free space or pass --keep-artifacts=0 / prune old runs "
            f"(see scripts/lab_workspace.py).")


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            try:
                total += (Path(root) / f).lstat().st_size
            except OSError:
                pass
    return total


@contextlib.contextmanager
def attached(image: Path, mountpoint: Path | None = None,
             readonly: bool = True, nomount: bool = False):
    """hdiutil attach with a guaranteed detach.

    Yields the mountpoint (or the /dev node when `nomount`). Detach is retried
    because a just-written volume can be briefly busy; a leaked mount keeps its
    backing storage pinned, which is exactly how disks fill up unnoticed.
    """
    cmd = ["hdiutil", "attach"]
    if readonly:
        cmd.append("-readonly")
    if nomount:
        cmd.append("-nomount")
    elif mountpoint is not None:
        cmd += ["-mountpoint", str(mountpoint)]
    cmd.append(str(image))
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    dev = None
    for line in out.splitlines():
        tok = line.split()[0] if line.split() else ""
        if tok.startswith("/dev/disk"):
            dev = tok
            break
    target = dev if nomount else (mountpoint or Path(out.split("\t")[-1].strip()))
    try:
        yield target
    finally:
        for _ in range(12):
            r = subprocess.run(["hdiutil", "detach",
                                str(dev if nomount else target)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(0.5)
        else:
            subprocess.run(["hdiutil", "detach", "-force",
                            str(dev if nomount else target)],
                           capture_output=True, text=True)


class Workspace:
    """Tracks disposable artifacts and removes them unless `keep` is set.

    "Disposable" means *reproducible from committed inputs* -- staged NAND
    clones, patched images, raw framebuffer dumps. Evidence (serial logs,
    matrix.json, screenshots) is never registered and always survives.
    """

    def __init__(self, root: Path, keep: bool = False, label: str = ""):
        self.root = Path(root)
        self.keep = keep
        self.label = label
        self._paths: list[Path] = []

    def disposable(self, path: Path) -> Path:
        self._paths.append(Path(path))
        return Path(path)

    def cleanup(self, verbose: bool = True) -> int:
        if self.keep:
            if verbose:
                print(f"[workspace] keeping {len(self._paths)} artifact(s) "
                      f"({self.label})")
            return 0
        freed = 0
        for p in self._paths:
            if not p.exists():
                continue
            size = dir_size(p) if p.is_dir() else p.stat().st_size
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                freed += size
            except OSError:
                pass
        if verbose and freed:
            print(f"[workspace] freed {human(freed)} of disposable artifacts"
                  + (f" ({self.label})" if self.label else ""))
        return freed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False


def prune_runs(parent: Path, keep_last: int = 3, pattern: str = "*") -> int:
    """Keep only the newest `keep_last` run directories under `parent`."""
    if not Path(parent).exists():
        return 0
    runs = sorted((p for p in Path(parent).glob(pattern) if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    freed = 0
    for old in runs[keep_last:]:
        size = dir_size(old)
        try:
            shutil.rmtree(old)
            freed += size
        except OSError:
            pass
    if freed:
        print(f"[workspace] pruned {len(runs) - keep_last} old run(s), "
              f"freed {human(freed)}")
    return freed


if __name__ == "__main__":                      # tiny CLI for manual hygiene
    import argparse
    ap = argparse.ArgumentParser(description="lab disk hygiene")
    ap.add_argument("--free", type=Path, help="report free space for a path")
    ap.add_argument("--size", type=Path, help="report a directory's size")
    ap.add_argument("--prune", type=Path, help="prune old run dirs under here")
    ap.add_argument("--keep-last", type=int, default=3)
    a = ap.parse_args()
    if a.free:
        print(f"free on {a.free}: {human(free_bytes(a.free))}")
    if a.size:
        print(f"{a.size}: {human(dir_size(a.size))}")
    if a.prune:
        prune_runs(a.prune, a.keep_last)
