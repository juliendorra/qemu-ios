#!/usr/bin/env python3
"""Assemble and install the iPhone-2G (M68AP) firmware set into the app bundle.

Establishes parity with the iPod-Touch-1G firmware layout: where the N45AP
machine keeps its firmware in `<App>/Contents/Resources/ipod_files/`, the M68AP
machine keeps its in `<App>/Contents/Resources/iphone_files/`, with the same
file names and the same shared S5L8900 bootrom. This is the layout the existing
`install-ipod-app-engine.sh` `iphone-2g` profile already expects, and the layout
the boot harnesses reference.

    iphone_files/
      bootrom_s5l8900        (shared S5L8900 bootrom, copied from ipod_files)
      iboot_204_m68ap.bin    (from extract-m68ap-images.py)
      nor_m68ap.bin          (from build-m68ap-nor.py)
      nand/                  (from build-m68ap-nand.py; bank0..7/*.page)
      firmware-provenance.json

Apple-derived firmware is never committed to the repo (see AGENTS.md); it lives
only in the installed bundle, exactly as the N45AP firmware does. This installer
is additive -- it never touches ipod_files -- and re-signs the bundle ad-hoc so
it stays launchable, matching install-ipod-app-engine.sh.

Typical pipeline (all inputs user-supplied, none committed):
    python3 scripts/extract-m68ap-images.py <IPSW>/.../all_flash.m68ap.production OUT
    python3 scripts/build-m68ap-nor.py --template <n45ap NOR> \\
        --containers OUT/nor-containers --out OUT/nor_m68ap.bin
    python3 scripts/build-m68ap-nand.py --out OUT/nand-m68ap --signature m68ap
    python3 scripts/install-iphone-firmware.py --from OUT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root: Path) -> str:
    """Order-independent digest of a page tree (path + content)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(sha256(p).encode())
            h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app", type=Path,
                    default=Path("/Applications/iPod Touch.app"),
                    help="application bundle (.app) to install into")
    ap.add_argument("--from", dest="src", type=Path, default=None,
                    help="artifact dir holding extracted/iboot_204_m68ap.bin, "
                         "nor_m68ap.bin and nand-m68ap/ (from the m68ap pipeline)")
    ap.add_argument("--iboot", type=Path, default=None,
                    help="iboot_204_m68ap.bin (overrides --from)")
    ap.add_argument("--nor", type=Path, default=None,
                    help="nor_m68ap.bin (overrides --from)")
    ap.add_argument("--nand", type=Path, default=None,
                    help="generated m68ap NAND dir (overrides --from)")
    ap.add_argument("--bootrom", type=Path, default=None,
                    help="shared S5L8900 bootrom (default: ipod_files/bootrom_s5l8900)")
    ap.add_argument("--no-resign", action="store_true",
                    help="do not re-codesign the bundle after installing")
    ap.add_argument("--keep-existing", action="store_true",
                    help="for any input not given, reuse what the bundle "
                         "already has instead of erroring. Makes a partial "
                         "update possible -- e.g. --nor X --keep-existing "
                         "swaps only the NOR and leaves the activated NAND "
                         "and the iBoot alone.")
    args = ap.parse_args()

    contents = args.app / "Contents"
    resources = contents / "Resources"
    ipod_files = resources / "ipod_files"
    iphone_files = resources / "iphone_files"
    if not resources.is_dir():
        raise SystemExit(f"not an app bundle: {args.app}")

    # Resolve inputs
    src = args.src
    iboot = args.iboot or (src and src / "extracted" / "iboot_204_m68ap.bin")
    nor = args.nor or (src and src / "nor_m68ap.bin")
    nand = args.nand or (src and src / "nand-m68ap")
    bootrom = args.bootrom or (ipod_files / "bootrom_s5l8900")
    if args.keep_existing:
        # Reuse whatever is installed for the inputs not supplied. Staging
        # copies out of iphone_files before the swap below removes it, so
        # sourcing from the destination is safe.
        existing = {"iboot": iphone_files / "iboot_204_m68ap.bin",
                    "nor": iphone_files / "nor_m68ap.bin",
                    "nand": iphone_files / "nand"}
        if iboot is None and existing["iboot"].exists():
            iboot = existing["iboot"]
        if nor is None and existing["nor"].exists():
            nor = existing["nor"]
        if nand is None and existing["nand"].exists():
            nand = existing["nand"]
    for name, p in [("iboot", iboot), ("nor", nor), ("nand", nand),
                    ("bootrom", bootrom)]:
        if p is None:
            raise SystemExit(f"missing input for {name}: pass --from, --{name}"
                             ", or --keep-existing to reuse the installed one")
        if not Path(p).exists():
            raise SystemExit(f"{name} not found: {p}")
    iboot, nor, nand, bootrom = map(Path, (iboot, nor, nand, bootrom))

    # Stage, then swap in atomically (never leave a half-written firmware dir)
    stage = resources / ".iphone_files.stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    shutil.copy2(bootrom, stage / "bootrom_s5l8900")
    shutil.copy2(iboot, stage / "iboot_204_m68ap.bin")
    shutil.copy2(nor, stage / "nor_m68ap.bin")
    subprocess.run(["cp", "-Rc", str(nand), str(stage / "nand")], check=True)

    # Carry over the SYSIC security epoch. The launcher reads <firmware
    # dir>/epoch, and a firmware booted under the wrong epoch wedges in iBoot
    # with an EMPTY serial log -- indistinguishable from a hang. Since this
    # installer REPLACES iphone_files wholesale, forgetting the file silently
    # converts a working 1.0/1.1.1 bundle (epoch 0/2) into one that boots to a
    # black screen and says nothing. Copy it before the swap.
    epoch_src = iphone_files / "epoch"
    if epoch_src.exists():
        shutil.copy2(epoch_src, stage / "epoch")

    manifest = {
        "profile": "iphone-2g",
        "board": "M68AP",
        "soc": "S5L8900",
        "files": {
            "bootrom_s5l8900": {"sha256": sha256(stage / "bootrom_s5l8900"),
                                "source": "shared S5L8900 bootrom"},
            "iboot_204_m68ap.bin": {"sha256": sha256(stage / "iboot_204_m68ap.bin")},
            "nor_m68ap.bin": {"sha256": sha256(stage / "nor_m68ap.bin")},
            "nand": {"tree_sha256": sha256_tree(stage / "nand")},
        },
        "note": "Apple-derived firmware, not committed to the repository "
                "(AGENTS.md). Parity layout with ipod_files/.",
    }
    # carry over the NAND constructor's provenance if present
    nand_prov = nand / "nand-provenance.json"
    if nand_prov.exists():
        manifest["nand_provenance"] = json.loads(nand_prov.read_text())
    (stage / "firmware-provenance.json").write_text(
        json.dumps(manifest, indent=2) + "\n")

    if iphone_files.exists():
        shutil.rmtree(iphone_files)
    stage.rename(iphone_files)

    if not args.no_resign:
        rc = subprocess.run(
            ["codesign", "--force", "--deep", "-s", "-", str(args.app)],
            capture_output=True, text=True)
        if rc.returncode != 0:
            print("WARNING: ad-hoc re-sign failed (bundle files are installed, "
                  "but the .app signature is now stale):\n" + rc.stderr,
                  file=sys.stderr)

    print(f"installed iPhone-2G firmware into {iphone_files}")
    for f in sorted(iphone_files.iterdir()):
        kind = "dir " if f.is_dir() else "file"
        print(f"  {kind} {f.name}")
    print("\nfile hashes:")
    for name, meta in manifest["files"].items():
        print(f"  {name}: {meta.get('sha256') or meta.get('tree_sha256')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
