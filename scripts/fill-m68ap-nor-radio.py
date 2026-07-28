#!/usr/bin/env python3
"""Fill the radio properties in an ALREADY-BUILT M68AP NOR.

`build-m68ap-nor.py` now does this while assembling a NOR, which is the right
place for it. This exists for the NORs that were built before that, because
rebuilding one needs a real N45AP NOR as the SysCfg/geometry template
(`--template`) and that file is not always around -- while the fix is a
few hundred bytes inside a container that is stored in plaintext.

The device tree lives in the NOR's `dtre` IMG2 container uncompressed and
unencrypted, and nothing verifies the container payload (only the IMG2 header
carries a CRC, over bytes [0:0x64]), so the properties can be filled in place.
That is not an assumption: a NOR patched exactly this way boots iPhone OS 1.0
with `IO80211Interface` attached and the home screen rendering normally.

Idempotent -- already-filled properties are left alone.

  scripts/fill-m68ap-nor-radio.py m68ap-artifacts/builds/1A543a/nor.bin
  scripts/fill-m68ap-nor-radio.py --check <nor>        # report, change nothing
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m68ap_dt_radio


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("nor", type=Path, nargs="+", help="nor_m68ap.bin to patch")
    parser.add_argument("--check", action="store_true",
                        help="report what would change; write nothing")
    parser.add_argument("--backup", action="store_true",
                        help="keep <nor>.orig before writing")
    args = parser.parse_args()

    status = 0
    for path in args.nor:
        if not path.is_file():
            print(f"{path}: missing", file=sys.stderr)
            status = 1
            continue
        blob = bytearray(path.read_bytes())
        if b"tx-calibration" not in blob:
            print(f"{path}: no device tree found in this NOR (encrypted "
                  f"container?) -- nothing done", file=sys.stderr)
            status = 1
            continue
        changed = m68ap_dt_radio.fill(blob)
        if not changed:
            print(f"{path}: already filled")
            continue
        for note in changed:
            print(f"  {note}")
        if args.check:
            print(f"{path}: {len(changed)} property(ies) WOULD be filled")
            continue
        if args.backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".orig"))
        path.write_bytes(bytes(blob))
        print(f"{path}: filled {len(changed)} property(ies)")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
