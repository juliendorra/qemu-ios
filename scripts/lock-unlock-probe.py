#!/usr/bin/env python3
"""Does touch still work after a sleep/wake cycle? Repeat it and find out.

Reported symptom (N45AP, 2026-07-25): power, home, slide-to-unlock works; but
after a SECOND power/home the slide no longer responds. One cycle passes, the
next fails -- exactly the shape of a readiness gate that is cleared on sleep
and never re-armed on the second wake.

The relevant model state lives in `hw/arm/ipod_touch_lcd.c`:

  * `panel_off` / `input_ready` / `input_ready_frames` -- the LCD refuses touch
    until a frame has been visibly stable for two seconds (`Touch input ready`);
  * `relight_input_fast` -- set when the panel sleeps while the device was
    already interactive, so a later Sleep Out restores input immediately;
  * `retained_input_wait` -- the retained-RAM wake path, which re-arms input
    only after the Z2 firmware reloads.

Any of those can latch such that the second wake never re-opens input, so this
probe drives the cycle N times and reports the first failing iteration, with
the model's own `[LCD]`/`[TOUCH]` lines for that cycle.

What it does per cycle
----------------------
  power press  ->  screen sleeps        (key P)
  home press   ->  screen wakes to lock (key H)
  slide        ->  drag across the unlock slider
  classify     ->  home screen = unlocked = input worked

Verdicts: `unlocked` (input worked), `stuck-locked` (drag ignored),
`no-wake` (screen never came back), `blank`.

Examples
--------
  scripts/lock-unlock-probe.py --board n45ap --cycles 3 --logs /tmp/lockprobe
  scripts/lock-unlock-probe.py --board m68ap --nand … --iboot … --nor … \\
      --cycles 3 --logs /tmp/lockprobe-iphone
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
PC_BIOS = APP / "Resources" / "pc-bios"
QEMU = REPO / "build-ipod11" / "qemu-system-arm"
M68_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"

FB_BASES = (0x0FE00000, 0x0F400000, 0x0F496000)
FB_W, FB_H, FB_BYTES = 320, 480, 320 * 480 * 4
# The unlock slider sits low on the panel; drag left-to-right across it.
SLIDE_Y = 430
SLIDE_X0, SLIDE_X1 = 45, 280


def _classifier():
    spec = importlib.util.spec_from_file_location(
        "sblab", REPO / "scripts" / "springboard-lab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.classify_screen


class QMP:
    def __init__(self, path, timeout=20):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("qmp closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute, arguments=None):
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.s.sendall((json.dumps(msg) + "\n").encode())
        while True:
            r = self._read()
            if "return" in r or "error" in r:
                return r

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def key(q: QMP, name: str, hold: float = 0.15):
    """Press and release a hardware button (P = power, H = home)."""
    for down in (True, False):
        q.cmd("input-send-event", {"events": [
            {"type": "key", "data": {"down": down,
                                     "key": {"type": "qcode", "data": name}}}]})
        if down:
            time.sleep(hold)


def _abs(q: QMP, px: int, py: int):
    q.cmd("input-send-event", {"events": [
        {"type": "abs", "data": {"axis": "x",
                                 "value": int(px / FB_W * 32768)}},
        {"type": "abs", "data": {"axis": "y",
                                 "value": int(py / FB_H * 32768)}}]})


def slide(q: QMP, steps: int = 12, dwell: float = 0.06):
    """Drag across the unlock slider, with motion the guest can track."""
    _abs(q, SLIDE_X0, SLIDE_Y)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": True, "button": "left"}}]})
    for i in range(1, steps + 1):
        _abs(q, SLIDE_X0 + (SLIDE_X1 - SLIDE_X0) * i // steps, SLIDE_Y)
        time.sleep(dwell)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": False, "button": "left"}}]})


def grab(q: QMP, tmp: Path, classify):
    best = None
    for base in FB_BASES:
        raw = tmp / f"fb_{base:08x}.raw"
        q.cmd("pmemsave", {"val": base, "size": FB_BYTES,
                           "filename": str(raw)})
        d = raw.read_bytes()
        raw.unlink(missing_ok=True)
        kind = classify(d)
        if best is None or kind["nonblack_pct"] > best[1]["nonblack_pct"]:
            best = (d, kind)
    return best


def png(d: bytes, path: Path):
    try:
        from PIL import Image
    except ImportError:
        return
    img = Image.frombytes("RGBA", (FB_W, FB_H), d)
    b, g, r, _ = img.split()
    Image.merge("RGB", (r, g, b)).save(path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("n45ap", "m68ap"), default="n45ap")
    ap.add_argument("--nand", type=Path)
    ap.add_argument("--iboot", type=Path)
    ap.add_argument("--nor", type=Path)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--boot-wait", type=float, default=200)
    ap.add_argument("--settle", type=float, default=8,
                    help="seconds to wait after each input before grabbing")
    ap.add_argument("--qemu", type=Path, default=QEMU)
    ap.add_argument("--warmup", type=int, default=0,
                    help="issue N screendumps before touching. Builds that "
                         "evaluate touch readiness inside gfx_update (i.e. "
                         "anything before the LCD-timer fix) never arm the "
                         "gate under -display none; a screendump forces one "
                         "gfx_update, and the gate needs 2*60 frames. Use "
                         "~200 when testing an OLD binary.")
    args = ap.parse_args()

    args.logs.mkdir(parents=True, exist_ok=True)
    classify = _classifier()
    qmp_path = Path(f"/tmp/lockprobe-{os.getpid()}.qmp")
    serial, stderr = args.logs / "serial.log", args.logs / "stderr.log"

    if args.board == "n45ap":
        machine = "iPod-Touch"
        iboot = args.iboot or IPOD_FILES / "iboot_204_n45ap.bin"
        nand = args.nand or IPOD_FILES / "nand"
        nor = args.nor or IPOD_FILES / "nor_n45ap.bin"
    else:
        machine, iboot, nand, nor = "iPhone-2G", args.iboot, args.nand, args.nor
        if not (iboot and nand and nor):
            ap.error("--iboot/--nand/--nor are required for m68ap")

    cmd = [str(args.qemu),
           "-M", f"{machine},bootrom={M68_BOOTROM},iboot={iboot},nand={nand}",
           "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
           "-display", "none", "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_path},server,nowait"]
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)
    report = {"board": args.board, "cycles": []}
    try:
        time.sleep(args.boot_wait)
        q = QMP(qmp_path)
        if args.warmup:
            shot = args.logs / "warmup.ppm"
            for _ in range(args.warmup):
                q.cmd("screendump", {"filename": str(shot)})
            shot.unlink(missing_ok=True)
            print(f"warmup: {args.warmup} screendumps issued")
        d, kind = grab(q, args.logs, classify)
        png(d, args.logs / "00-booted.png")
        report["booted"] = kind
        print(f"booted: {kind['kind']} ({kind['nonblack_pct']}% non-black)")

        for n in range(1, args.cycles + 1):
            mark = len(stderr.read_bytes())
            key(q, "p")                       # sleep
            time.sleep(args.settle)
            _, slept = grab(q, args.logs, classify)
            key(q, "h")                       # wake
            time.sleep(args.settle)
            d, locked = grab(q, args.logs, classify)
            png(d, args.logs / f"{n:02d}-woken.png")
            slide(q)
            time.sleep(args.settle)
            d, after = grab(q, args.logs, classify)
            png(d, args.logs / f"{n:02d}-after-slide.png")

            if after["kind"] == "home":
                verdict = "unlocked"
            elif locked["nonblack_pct"] < 2.0:
                verdict = "no-wake"
            elif after["nonblack_pct"] < 2.0:
                verdict = "blank"
            else:
                verdict = "stuck-locked"
            tail = stderr.read_bytes()[mark:].decode("latin1", "replace")
            lcd = [l for l in tail.splitlines()
                   if "[LCD]" in l or "[TOUCH]" in l][:8]
            report["cycles"].append({"cycle": n, "verdict": verdict,
                                     "slept": slept, "woken": locked,
                                     "after_slide": after, "model_lines": lcd})
            print(f"cycle {n}: {verdict:12} "
                  f"(slept {slept['nonblack_pct']}%, woken "
                  f"{locked['nonblack_pct']}%, after {after['nonblack_pct']}%)")
            for l in lcd:
                print(f"    {l}")
        q.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        qmp_path.unlink(missing_ok=True)

    bad = [c for c in report["cycles"] if c["verdict"] != "unlocked"]
    report["first_failure"] = bad[0]["cycle"] if bad else None
    (args.logs / "lock-unlock-probe.json").write_text(
        json.dumps(report, indent=2))
    print(f"\nfirst failing cycle: {report['first_failure'] or 'none'}")
    print(f"report: {args.logs}/lock-unlock-probe.json  (PNGs alongside)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
