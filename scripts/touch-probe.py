#!/usr/bin/env python3
"""Is the guest UI actually INTERACTIVE? Boot, tap, and prove it from pixels.

Rendering a home screen and *responding to a finger* are different claims. This
boots a board, waits for a stable screen, taps a coordinate over QMP, and
reports whether the framebuffer changed -- with before/after PNGs, so the
verdict is evidence rather than an impression.

Why it exists
-------------
M68AP reached the SpringBoard home screen (2026-07-25). The multitouch
controller model reports its firmware loaded on both boards, but "the driver
uploaded firmware" does not prove "a tap opens an app": the frame has to reach
the guest through the Z1/Z2 SPI path, the ATN GPIO, and SpringBoard's own hit
testing. That whole chain is only observable as a screen change.

Coordinate mapping (see ipod_touch_lcd_mouse_event): the LCD registers a legacy
ABSOLUTE mouse handler and converts with fx = x / 2**15, fy = 1 - y / 2**15,
so a screen pixel (px, py) on the 320x480 panel is
    x_abs = px / 320 * 32768        y_abs = py / 480 * 32768
Note also that the model refuses input until a frame has been visibly stable
(`[LCD] Touch input ready`), which is itself a useful signal in the output.

Examples
--------
  # does tapping Safari in the dock do anything on the iPhone?
  scripts/touch-probe.py --board m68ap --nand /tmp/.../nand \\
      --iboot m68ap-artifacts/builds/4A102/iboot-sb.bin \\
      --nor m68ap-artifacts/builds/4A102/nor.bin \\
      --tap 200,437 --logs /tmp/touchprobe

  # same question on the iPod, which is the known-good control
  scripts/touch-probe.py --board n45ap --tap 160,240 --logs /tmp/touchprobe-ipod
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
PC_BIOS = APP / "Resources" / "pc-bios"
QEMU = REPO / "build-ipod11" / "qemu-system-arm"
M68_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"

FB_BASES = {"iboot_0x0fe00000": 0x0FE00000,
            "kernel_0x0f400000": 0x0F400000,
            "kernel_0x0f496000": 0x0F496000}
FB_W, FB_H, FB_BYTES = 320, 480, 320 * 480 * 4


def _load_classifier():
    """Reuse the lab's screen classifier instead of duplicating thresholds."""
    spec = importlib.util.spec_from_file_location(
        "sblab", REPO / "scripts" / "springboard-lab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.classify_screen


class QMP:
    def __init__(self, path, timeout=15):
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


def tap(q: QMP, px: int, py: int, hold: float = 0.12):
    """One finger down/up at a panel pixel, via the absolute pointer."""
    x = int(px / FB_W * 32768)
    y = int(py / FB_H * 32768)
    move = [{"type": "abs", "data": {"axis": "x", "value": x}},
            {"type": "abs", "data": {"axis": "y", "value": y}}]
    q.cmd("input-send-event", {"events": move})
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": True, "button": "left"}}]})
    time.sleep(hold)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": False, "button": "left"}}]})


def grab(q: QMP, out: Path) -> tuple[bytes, str, dict]:
    """Dump the liveliest framebuffer straight from guest RAM."""
    best, best_label, best_kind = b"", "", {}
    classify = grab.classify
    for label, base in FB_BASES.items():
        raw = out.with_suffix(f".{label}.raw")
        q.cmd("pmemsave", {"val": base, "size": FB_BYTES,
                           "filename": str(raw)})
        d = raw.read_bytes()
        raw.unlink(missing_ok=True)
        kind = classify(d)
        if kind["nonblack_pct"] > best_kind.get("nonblack_pct", -1):
            best, best_label, best_kind = d, label, kind
    return best, best_label, best_kind


def write_png(d: bytes, path: Path):
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.frombytes("RGBA", (FB_W, FB_H), d)
    b, g, r, _ = img.split()                      # guest is BGRA
    Image.merge("RGB", (r, g, b)).save(path)
    return path


def diff_pct(a: bytes, b: bytes) -> float:
    n = changed = 0
    for i in range(0, min(len(a), len(b)), 4 * 7):   # sample every 7th pixel
        n += 1
        if abs(a[i] - b[i]) > 12 or abs(a[i + 1] - b[i + 1]) > 12 \
                or abs(a[i + 2] - b[i + 2]) > 12:
            changed += 1
    return round(100.0 * changed / n, 2) if n else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("m68ap", "n45ap"), required=True)
    ap.add_argument("--nand", type=Path)
    ap.add_argument("--iboot", type=Path)
    ap.add_argument("--nor", type=Path)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--tap", default="200,437",
                    help="panel pixel to tap, 'x,y' (default: dock slot 3)")
    ap.add_argument("--boot-wait", type=float, default=200)
    ap.add_argument("--settle", type=float, default=12,
                    help="seconds to wait after the tap before re-grabbing")
    ap.add_argument("--qemu", type=Path, default=QEMU)
    args = ap.parse_args()

    args.logs.mkdir(parents=True, exist_ok=True)
    grab.classify = _load_classifier()
    qmp_path = Path(f"/tmp/touchprobe-{os.getpid()}.qmp")
    serial = args.logs / "serial.log"
    stderr = args.logs / "stderr.log"

    if args.board == "n45ap":
        machine, iboot = "iPod-Touch", IPOD_FILES / "iboot_204_n45ap.bin"
        nand = args.nand or IPOD_FILES / "nand"
        nor = args.nor or IPOD_FILES / "nor_n45ap.bin"
    else:
        machine, iboot = "iPhone-2G", args.iboot
        nand, nor = args.nand, args.nor
        if not (iboot and nand and nor):
            ap.error("--iboot/--nand/--nor are required for m68ap")

    cmd = [str(args.qemu),
           "-M", f"{machine},bootrom={M68_BOOTROM},iboot={iboot},nand={nand}",
           "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
           "-display", "none", "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_path},server,nowait"]
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)
    result = {"board": args.board, "tap": args.tap}
    try:
        time.sleep(args.boot_wait)
        q = QMP(qmp_path)
        before, label, kind = grab(q, args.logs / "before")
        result["before"] = {"fb": label, **kind}
        write_png(before, args.logs / "before.png")

        px, py = (int(v) for v in args.tap.split(","))
        tap(q, px, py)
        time.sleep(args.settle)

        after, label2, kind2 = grab(q, args.logs / "after")
        result["after"] = {"fb": label2, **kind2}
        write_png(after, args.logs / "after.png")
        result["changed_pct"] = diff_pct(before, after)
        # The model prints this once a frame has been visibly stable; without
        # it, taps are dropped by design and a "no change" verdict is moot.
        blob = stderr.read_text(errors="replace")
        result["input_ready"] = "Touch input ready" in blob
        result["touch_delivered"] = "[TOUCH] mouse DOWN" in blob
        result["verdict"] = (
            "interactive" if result["changed_pct"] >= 1.0 else
            "no-response" if result["touch_delivered"] else
            "input-gated")
        q.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        qmp_path.unlink(missing_ok=True)

    (args.logs / "touch-probe.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"\nPNGs: {args.logs}/before.png {args.logs}/after.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
