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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m68ap_paths  # noqa: E402

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
    """One finger down/up at a panel pixel, via the absolute pointer.

    `hold` matters. The multitouch model reports motion at
    MT_MOTION_REPORT_HZ (60) in GUEST time, so a press shorter than one report
    interval can be delivered as an instantaneous down/up pair that the guest
    never observes as a finger. 0.12 s is comfortable natively, where guest time
    tracks wall time; under emulation slow enough that it does not (the browser
    port runs at ~2% of real time), it is not.
    """
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
    ap.add_argument("--tap", action="append", default=None,
                    help="panel pixel to tap, 'x,y' (default 200,437 = dock "
                         "slot 3; repeatable, and taps run in "
                         "order in ONE boot, so put the expected no-op first "
                         "-- a tap that works launches an app and there may be "
                         "no way back)")
    ap.add_argument("--hold", type=float, default=0.12,
                    help="seconds to hold each tap")
    m68ap_paths.add_build_argument(ap, required=False)
    ap.add_argument("--boot-wait", type=float, default=200)
    ap.add_argument("--settle", type=float, default=12,
                    help="seconds to wait after the tap before re-grabbing")
    ap.add_argument("--icount", default=None,
                    help="icount shift (e.g. 1). OFF by default -- it appears "
                         "to suppress multitouch frame consumption; see the "
                         "note in the source")
    ap.add_argument("--qemu", type=Path, default=QEMU)
    args = ap.parse_args()

    taps = args.tap or ["200,437"]      # default: dock slot 3

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
        bootrom, epoch = M68_BOOTROM, None
        # --build fills the artifact paths AND the security epoch from the
        # canonical layout. Without the epoch this tool could not boot anything
        # but 1.1.4: a wrong epoch wedges iBoot with an EMPTY serial log, which
        # looks exactly like a hang. Explicit paths still win.
        if args.build:
            paths = m68ap_paths.get(args.build)
            paths.require("iboot_sb", "nor", "nand")
            iboot = iboot or paths.iboot_sb
            nor = nor or paths.nor
            nand = nand or paths.nand
            epoch = paths.epoch
            if paths.bootrom:
                bootrom = paths.bootrom
            print(f"[touch-probe] {m68ap_paths.describe(args.build)}")
        if not (iboot and nand and nor):
            ap.error("--iboot/--nand/--nor are required for m68ap "
                     "(or pass --build)")

    machine_str = f"{machine},bootrom={bootrom if args.board == 'm68ap' else M68_BOOTROM}," \
                  f"iboot={iboot},nand={nand}"
    if args.board == "m68ap" and epoch is not None:
        machine_str += f",epoch={epoch}"

    cmd = [str(args.qemu),
           "-M", machine_str,
           "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
           "-display", "none", "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_path},server,nowait"]
    # An honest clock stops the guest taking timeout paths and panicking
    # (measured natively at 1 panic in 3 boots), so it is the right default for
    # a BOOT. But it is off by default here, because it appears to break the
    # thing this tool measures: with -icount shift=1 a run reached the home
    # screen with `[LCD] Touch input ready` and then consumed ZERO multitouch
    # frames from three taps, including a coordinate that demonstrably launches
    # an app in the browser. Use --icount to reproduce that.
    if args.icount:
        cmd[1:1] = ["-icount", f"shift={args.icount}"]
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)
    result = {"board": args.board, "taps": taps, "hold_s": args.hold,
              "results": []}
    try:
        time.sleep(args.boot_wait)
        q = QMP(qmp_path)
        before, label, kind = grab(q, args.logs / "before")
        result["before"] = {"fb": label, **kind}
        write_png(before, args.logs / "before.png")

        prev = before
        for i, spec in enumerate(taps):
            px, py = (int(v) for v in spec.split(","))
            mark = stderr.stat().st_size          # only read what THIS tap said
            tap(q, px, py, hold=args.hold)
            time.sleep(args.settle)

            after, label2, kind2 = grab(q, args.logs / f"after{i}")
            write_png(after, args.logs / f"after{i}.png")
            with stderr.open("rb") as fh:
                fh.seek(mark)
                fresh = fh.read().decode("utf-8", "replace")

            # `[MT] frame consumed` is THE signal that the guest actually took
            # the touch. Everything else -- ATN raised, bytes clocked, the LCD
            # logging a mouse DOWN -- can be true while the driver drops the
            # frame, which is exactly the case this probe exists to distinguish.
            # Requires IT_MT_TRACE=1 in the environment.
            consumed = [ln.strip() for ln in fresh.splitlines()
                        if "frame consumed" in ln]
            entry = {
                "tap": spec,
                "changed_pct": diff_pct(prev, after),
                "fb": label2, **kind2,
                "touch_delivered": "[TOUCH] mouse DOWN" in fresh,
                "touch_refused": "Ignoring input until" in fresh,
                "mt_frames_consumed": len(consumed),
                "mt_consumed_events": consumed[:8],
                "atn_edges": fresh.count("ATN edge"),
            }
            entry["verdict"] = (
                "interactive" if entry["changed_pct"] >= 1.0 else
                "input-gated" if entry["touch_refused"] else
                "delivered-but-ignored" if entry["mt_frames_consumed"] else
                "frame-never-consumed" if entry["touch_delivered"] else
                "not-delivered")
            result["results"].append(entry)
            print(f"[touch-probe] tap {spec}: {entry['verdict']} "
                  f"changed={entry['changed_pct']:.2f}% "
                  f"consumed={entry['mt_frames_consumed']} "
                  f"atn={entry['atn_edges']}", flush=True)
            prev = after

        blob = stderr.read_text(errors="replace")
        result["input_ready"] = "Touch input ready" in blob
        result["mt_trace_enabled"] = "ATN edge" in blob or "frame consumed" in blob
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
    if not result.get("mt_trace_enabled"):
        print("\nNOTE: no [MT] lines seen -- run with IT_MT_TRACE=1 for the "
              "'frame consumed' verdict, which is the only signal that the "
              "guest actually TOOK a touch.")
    print(f"\nPNGs: {args.logs}/before.png "
          + " ".join(f"{args.logs}/after{i}.png" for i in range(len(taps))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
