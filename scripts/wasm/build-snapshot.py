#!/usr/bin/env python3
"""Produce a post-boot snapshot for the browser's instant-boot path.

Boots a firmware natively, waits until the panel is genuinely LIVE, stops the
guest and migrates it to a file.  The browser page loads that file with
``-incoming`` and is at an interactive home screen in ~18 s instead of ~250 s.

Run it as a build step, not by hand: the stream is only valid for the engine
that produced it, and a stale one fails in ways that do not name the cause (see
"Compatibility" below).

    scripts/wasm/build-snapshot.py --build 1A543a

Compatibility -- read before debugging a failed resume
------------------------------------------------------
A migration stream is only loadable by an emulator whose **device set and
vmstate layout match**.  Two ways that has already broken:

* a device gaining or losing a ``VMStateDescription`` -- add one, and every
  older snapshot is stale;
* the two builds differing at all: a stream captured with user networking on
  failed in the WebAssembly build with ``Unknown section or instance 'slirp'``,
  because that build has no slirp.  Hence ``-net none`` here.

So the provenance file records the engine commit, and the page can say
"regenerate the snapshot" instead of showing a black screen.

Delivery
--------
``--brotli`` also writes ``state.br``.  Chunking this the way the NAND is
chunked would buy NOTHING -- the incoming migration reads the whole stream
synchronously at startup, so there is no partial-access pattern to exploit --
but COMPRESSION is worth a great deal, because ~30% of the stream is zero bytes:

    raw        57.23 MiB
    gzip -6    18.57 MiB   32.4%
    brotli q5  14.55 MiB   25.4%    0.7 s
    brotli q11 11.46 MiB   20.0%   98 s

At q11 the snapshot is SMALLER than the chunked cold-boot working set
(18.57 MiB) and reaches an interactive home screen ~14x sooner.  Serve
``state.br`` with ``Content-Encoding: br`` and the browser decompresses it on
the way in, carrying no decoder -- exactly what scripts/wasm/chunk-pack.py's
output relies on.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The producer reuses the verifier's helpers rather than growing a second copy
# of the launch/sample/wake logic that can drift from it.
probe = _load("snapshot_probe", REPO / "scripts" / "wasm" / "snapshot-probe.py")
QMP, measure, launch, wake = probe.QMP, probe.measure, probe.launch, probe.wake


def engine_provenance() -> dict:
    """What produced this stream. The commit is the compatibility key."""
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=REPO, check=True,
                                  capture_output=True, text=True).stdout.strip()
        except Exception:                                  # noqa: BLE001
            return None
    return {
        "engine_commit": git("rev-parse", "HEAD"),
        "engine_dirty": bool(git("status", "--porcelain", "hw", "ui",
                                 "migration", "system")),
        "qemu_version": (REPO / "VERSION").read_text().strip()
        if (REPO / "VERSION").exists() else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path,
                    default=REPO / "web" / "public" / "jit-boot" / "snapshots",
                    help="per-build snapshots are written to <out>/<BUILD>/")
    ap.add_argument("--qemu", type=Path, default=probe.DEFAULT_QEMU)
    ap.add_argument("--boot-wait", type=int, default=300)
    ap.add_argument("--live-pct", type=float, default=40.0,
                    help="non-black percentage that counts as a live panel")
    ap.add_argument("--live-timeout", type=float, default=300.0)
    ap.add_argument("--brotli", action="store_true",
                    help="also write state.br (see Delivery in the docstring)")
    ap.add_argument("--quality", type=int, default=11)
    ap.add_argument("--work", type=Path, default=None,
                    help="scratch directory; defaults to a temp dir")
    # Overrides, for a build whose product artifacts do not exist yet. 4A102 is
    # the live case: `m68ap-artifacts/builds/4A102/nand` has never been
    # generated (W7a), while the shipped 1.1.4 app bundle carries a NAND with
    # full provenance. The staging copy below means the source tree is only
    # ever read, so pointing this at a bundle is safe here -- but nothing else
    # in this repo may do that (see TOUCH_INVESTIGATION.md, "Never point QEMU
    # at a bundle's shipped NAND").
    ap.add_argument("--nand", type=Path, help="override the NAND tree")
    ap.add_argument("--nor", type=Path, help="override the NOR image")
    ap.add_argument("--iboot", type=Path, help="override the iBoot image")
    ap.add_argument("--out-name", default=None,
                    help="write to <out>/<name>/ instead of <out>/<BUILD>/, so "
                         "an override-built snapshot cannot be mistaken for "
                         "one built from the product artifacts")
    m68ap_paths.add_build_argument(ap, required=True)
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    overridden = {k: v for k, v in (("nand", args.nand), ("nor", args.nor),
                                    ("iboot", args.iboot)) if v}
    if overridden:
        missing = [f"{k}: {v}" for k, v in overridden.items() if not v.exists()]
        if missing:
            raise SystemExit("missing override artifact(s):\n  "
                             + "\n  ".join(missing))
        print("[build-snapshot] OVERRIDES: "
              + ", ".join(f"{k}={v}" for k, v in overridden.items()))
    paths.require(*[n for n in ("iboot_sb", "nor", "nand")
                    if n.replace("iboot_sb", "iboot") not in overridden])
    print(f"[build-snapshot] {m68ap_paths.describe(args.build)}")

    work = args.work or Path(
        os.environ.get("TMPDIR", "/tmp")) / f"snapbuild-{os.getpid()}"
    stage = work / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    staged_nand = stage / "nand"
    if staged_nand.exists():
        shutil.rmtree(staged_nand)
    src_nand = args.nand or paths.nand
    src_nor = args.nor or paths.nor
    src_iboot = args.iboot or paths.iboot_sb
    subprocess.run(["cp", "-Rc", str(src_nand), str(staged_nand)], check=True)
    for bank in range(8):
        (staged_nand / f"bank{bank}").mkdir(exist_ok=True)
    staged_nor = stage / "nor.bin"
    shutil.copy2(src_nor, staged_nor)

    machine = (f"iPhone-2G,bootrom={paths.bootrom},iboot={src_iboot},"
               f"nand={staged_nand},epoch={paths.epoch}")
    sock_dir = "/var/tmp" if os.path.isdir("/var/tmp") else "/tmp"
    sock = f"{sock_dir}/snapbuild-{os.getpid()}.sock"
    if os.path.exists(sock):
        os.unlink(sock)
    state = work / "state"

    proc = launch(args.qemu, machine, staged_nor, work / "serial.log", sock,
                  work, "build", None)
    try:
        print(f"booting for {args.boot_wait}s ...", flush=True)
        time.sleep(args.boot_wait)
        qmp = QMP(sock)

        # Wait for a LIVE panel and refuse to ship a dark one. A snapshot of a
        # sleeping device restores to a black screen that cannot be woken in
        # the browser within any sane wall-clock time, because guest time there
        # runs ~50x slower than the wall clock.
        deadline = time.time() + args.live_timeout
        live = False
        while time.time() < deadline:
            shot = work / "probe.ppm"
            qmp.execute("screendump", filename=str(shot))
            pct = measure(shot)[0]
            if pct >= args.live_pct:
                print(f"panel live at {pct:.1f}%", flush=True)
                live = True
                break
            print(f"panel at {pct:.1f}%, waking ...", flush=True)
            wake(qmp, settle=10.0)
        if not live:
            print(f"panel never reached {args.live_pct}% -- refusing to write "
                  "a snapshot of a dark screen", file=sys.stderr)
            return 1

        # Stop first. A LIVE migration of this machine aborts on
        # tlb_reset_dirty_range_all's block assertion, because main RAM is
        # mapped twice (RAM_MEM_BASE and its uncached alias), and the truncated
        # file it leaves fails on restore with a section-footer error that
        # points nowhere near the cause.
        qmp.execute("stop")
        print("migrating ...", flush=True)
        r = qmp.execute("migrate", uri=f"file:{state}")
        if "error" in r:
            print("migrate rejected:", r["error"], file=sys.stderr)
            return 1
        info = probe.wait_migration(qmp)
        if info.get("status") != "completed":
            print("migration did not complete:", info, file=sys.stderr)
            return 1
        qmp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(sock):
            os.unlink(sock)

    raw = state.read_bytes()
    out = args.out / (args.out_name or args.build)
    out.mkdir(parents=True, exist_ok=True)
    (out / "state").write_bytes(raw)
    meta = {
        "build": args.build,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        # Which images this actually came from, so a snapshot built from
        # overrides can never be mistaken for one built from the product tree.
        "artifacts": {"nand": str(src_nand), "nor": str(src_nor),
                      "iboot": str(src_iboot)},
        "overridden": sorted(overridden),
        **engine_provenance(),
    }
    print(f"state: {len(raw) / 1048576:.2f} MiB -> {out / 'state'}")

    if args.brotli:
        try:
            import brotli
        except ImportError:
            print("brotli module not installed; skipping state.br",
                  file=sys.stderr)
        else:
            t0 = time.time()
            comp = brotli.compress(raw, quality=args.quality)
            (out / "state.br").write_bytes(comp)
            meta["brotli_bytes"] = len(comp)
            meta["brotli_quality"] = args.quality
            print(f"state.br: {len(comp) / 1048576:.2f} MiB "
                  f"({100 * len(comp) / len(raw):.1f}%) in "
                  f"{time.time() - t0:.0f}s")

    (out / "state-provenance.json").write_text(json.dumps(meta, indent=2) + "\n")
    if meta.get("engine_dirty"):
        print("WARNING: the engine tree is dirty; this snapshot records a "
              "commit it was not exactly built from", file=sys.stderr)
    if args.work is None:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
