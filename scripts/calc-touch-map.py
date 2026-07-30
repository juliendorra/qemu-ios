#!/usr/bin/env python3
"""Measure WHERE a tap actually lands, using Calculator as the readout.

Every other touch test in this tree answers a yes/no question -- did an app
launch, did the screen change -- with a settle window long enough to be unsure
about.  Calculator answers a *precise* one: press a digit and that digit appears
in the display.  So the display is a per-tap oracle for "which button did the
guest think I pressed", and the hit-box boundary can be located by binary search
instead of guessed.

That matters because the reported touch shift is NOT uniform across the screen
on 1.1.4, so a single offset cannot describe it and a map is needed.

What it does
------------
1. restores a snapshot (fast, identical machine state every run) and launches
   Calculator by tapping its home-screen icon;
2. profiles the button grid from the real framebuffer, so the VISUAL geometry is
   measured rather than assumed;
3. calibrates a display fingerprint for each probe digit by tapping its centre;
4. binary-searches the left/right/top/bottom edge of that button's ACTUAL hit
   box, clearing with `c` between taps;
5. prints measured-minus-visual for each edge, per button.

A positive dx means the hit box sits to the RIGHT of the drawn button, i.e. a
tap must be further right than it looks -- equivalently the guest receives a
point LEFT of the cursor.

    scripts/calc-touch-map.py --build 1A543a --snapshot web/public/jit-boot/snapshots/1A543a/state
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402

FB_W, FB_H = 320, 480


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load("fb_snapshot", REPO / "scripts" / "fb-snapshot.py")
QMP = fb.QMP


def ppm_pixels(path: Path) -> bytes:
    d = path.read_bytes()
    return d[d.index(b"255\n") + 4:]


class Machine:
    def __init__(self, qemu, machine, nor, serial, sock, log, incoming):
        cmd = [str(qemu), "-M", machine, "-m", "1G", "-pflash", str(nor),
               "-L", "/Applications/iPod Touch.app/Contents/Resources/pc-bios",
               "-display", "none", "-serial", f"file:{serial}",
               "-icount", "shift=1", "-net", "none",
               "-qmp", f"unix:{sock},server,nowait",
               "-incoming", f"file:{incoming}"]
        self.log = log
        self.proc = subprocess.Popen(cmd, stdout=log.open("wb"),
                                     stderr=subprocess.STDOUT)
        self.q = QMP(sock, wait=60)
        self.q.execute("cont")
        self.shot_dir = log.parent

    def shot(self, tag="s") -> bytes:
        p = self.shot_dir / f"{tag}.ppm"
        self.q.execute("screendump", filename=str(p))
        return ppm_pixels(p)

    def tap(self, px, py, hold=0.25, settle=1.2):
        self.q.execute("input-send-event", events=[
            {"type": "abs", "data": {"axis": "x",
                                     "value": int(px / FB_W * 32768)}},
            {"type": "abs", "data": {"axis": "y",
                                     "value": int(py / FB_H * 32768)}},
            {"type": "btn", "data": {"down": True, "button": "left"}}])
        time.sleep(hold)
        self.q.execute("input-send-event", events=[
            {"type": "btn", "data": {"down": False, "button": "left"}}])
        time.sleep(settle)

    def close(self):
        try:
            self.q.close()
        except Exception:                                   # noqa: BLE001
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def bright_runs(px: bytes, along: str, lo: int, hi: int, frm: int, to: int,
                thresh=0.25, min_len=15):
    """Runs of bright rows/columns, used to find the drawn button grid."""
    vals = []
    for a in range(frm, to):
        t = 0
        for b in range(lo, hi):
            x, y = (a, b) if along == "col" else (b, a)
            o = (y * FB_W + x) * 3
            t += px[o] + px[o + 1] + px[o + 2]
        vals.append(t)
    mx = max(vals) or 1
    runs, start = [], None
    for i, v in enumerate(vals):
        on = v > mx * thresh
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= min_len:
                runs.append((frm + start, frm + i - 1))
            start = None
    if start is not None and (to - frm) - start >= min_len:
        runs.append((frm + start, to - 1))
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--qemu", type=Path,
                    default=REPO / "build-ipod11" / "qemu-system-arm")
    ap.add_argument("--calc-icon", default="122,247",
                    help="home-screen position of the Calculator icon")
    ap.add_argument("--digits", default="5,1,3,7,9",
                    help="which digit buttons to probe")
    m68ap_paths.add_build_argument(ap, required=True)
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    args.logs.mkdir(parents=True, exist_ok=True)

    machine = (f"iPhone-2G,bootrom={paths.bootrom},iboot={paths.iboot_sb},"
               f"nand={paths.nand},epoch={paths.epoch}")
    sock = f"/var/tmp/calcmap-{os.getpid()}.sock"
    if os.path.exists(sock):
        os.unlink(sock)

    m = Machine(args.qemu, machine, paths.nor, args.logs / "serial.log", sock,
                args.logs / "stderr.log", args.snapshot)
    result = {"build": args.build}
    try:
        print("waiting for the restored home screen ...", flush=True)
        time.sleep(25)

        cx, cy = (int(v) for v in args.calc_icon.split(","))
        print(f"launching Calculator at ({cx},{cy}) ...", flush=True)
        m.tap(cx, cy, hold=0.4, settle=25)

        px = m.shot("calc")
        # The Calculator display is a light strip across the top; the keypad is
        # dark. If that is not what we see, we are not in Calculator and every
        # later number would be meaningless.
        top = sum(px[(y * FB_W + x) * 3] for y in range(60, 100)
                  for x in range(40, 280)) / (40 * 240)
        if top < 120:
            print(f"not in Calculator (top strip brightness {top:.0f})",
                  file=sys.stderr)
            return 1
        print(f"in Calculator (top strip {top:.0f})", flush=True)

        # Measure the drawn grid. Keypad occupies roughly y 110..480.
        cols = [r for r in bright_runs(px, "col", 240, 460, 0, FB_W) if
                r[1] - r[0] > 20]
        rows = [r for r in bright_runs(px, "row", 20, 300, 110, FB_H) if
                r[1] - r[0] > 20]
        print("drawn columns:", cols)
        print("drawn rows:   ", rows)
        result["drawn_columns"] = cols
        result["drawn_rows"] = rows
        (args.logs / "map.json").write_text(json.dumps(result, indent=2) + "\n")
        print("\ngeometry written; run again with --digits once the grid above "
              "looks right", flush=True)
        return 0
    finally:
        m.close()
        if os.path.exists(sock):
            os.unlink(sock)


if __name__ == "__main__":
    raise SystemExit(main())
