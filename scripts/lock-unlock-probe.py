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
import threading
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


class DisplayClient(threading.Thread):
    """A minimal VNC/RFB client whose only job is to make QEMU refresh.

    WHY THIS EXISTS. Headless (`-display none`) QEMU never calls gfx_update,
    so the LCD model's touch-readiness logic never runs and every touch is
    refused -- a harness artifact that once "proved" a false regression, and
    worse, tempted a model change to work around it. With a client attached,
    QEMU's VNC server refreshes on its own timer, driving gfx_update at ~30 Hz
    exactly like the packaged app. The pixels are thrown away; only the
    side effect matters.
    """

    daemon = True

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self.ok = False
        self._stop = threading.Event()

    def run(self):
        # QEMU opens the VNC socket well after exec (it loads the NAND pack
        # first), so retry rather than assuming it is listening.
        sock = None
        deadline = time.time() + 60
        while sock is None and time.time() < deadline and not self._stop.is_set():
            try:
                sock = socket.create_connection(("127.0.0.1", self.port),
                                                timeout=5)
            except OSError:
                time.sleep(1)
        if sock is None:
            print("display client: could not connect (is -vnc set?)", flush=True)
            return
        sock.settimeout(10)
        try:
            ver = sock.recv(12)                       # "RFB 003.00x\n"
            if not ver.startswith(b"RFB"):
                return
            sock.sendall(b"RFB 003.008\n")
            n = sock.recv(1)[0]                        # security types
            types = sock.recv(n)
            if 1 not in types:                         # 1 = None
                return
            sock.sendall(bytes([1]))
            if int.from_bytes(sock.recv(4), "big") != 0:
                return                                 # SecurityResult
            sock.sendall(bytes([1]))                   # ClientInit: shared
            hdr = sock.recv(24)                        # ServerInit
            name_len = int.from_bytes(hdr[20:24], "big")
            while name_len > 0:
                name_len -= len(sock.recv(min(name_len, 4096)))
            w = int.from_bytes(hdr[0:2], "big")
            h = int.from_bytes(hdr[2:4], "big")
            self.ok = True
            req = (b"\x03\x01" + (0).to_bytes(2, "big") + (0).to_bytes(2, "big")
                   + w.to_bytes(2, "big") + h.to_bytes(2, "big"))
            while not self._stop.is_set():
                sock.sendall(req)                      # FramebufferUpdateRequest
                try:
                    if not sock.recv(65536):
                        break
                except socket.timeout:
                    pass
                time.sleep(0.05)
        except (OSError, IndexError) as e:
            print(f"display client error: {type(e).__name__}: {e}", flush=True)
        finally:
            sock.close()

    def stop(self):
        self._stop.set()


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


def slide(q: QMP, steps: int = 12, dwell: float = 0.06, hold: float = 0.0):
    """Drag across the unlock slider, with motion the guest can track."""
    _abs(q, SLIDE_X0, SLIDE_Y)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": True, "button": "left"}}]})
    time.sleep(hold)          # a finger rests before it moves
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
    ap.add_argument("--app", type=Path,
                    help="test a PACKAGED bundle instead of the repo build: "
                         "runs the bundle's own launcher (its engine copy, its "
                         "firmware, its bridges, its per-launch NAND staging) "
                         "and appends -qmp/-vnc, which the launcher forwards "
                         "to QEMU. Closes the gap between 'my test passed' and "
                         "'the app works'.")
    ap.add_argument("--no-display-client", action="store_true",
                    help="do NOT attach the VNC refresh client. Headless runs "
                         "skip gfx_update entirely, so the touch-readiness "
                         "logic never executes -- results do not reflect the "
                         "packaged app. Kept only for A/B against old runs.")
    ap.add_argument("--vnc-port", type=int, default=5999)
    ap.add_argument("--icount", type=int, default=None,
                    help="pass -icount N. 1.1.4 boots without icount are "
                         "timing-sensitive and can panic in "
                         "IOIpodUSBDevice::start (the fb-snapshot trap, "
                         "BROWSER_WASM_STATUS.md); use --icount 1 for m68ap")
    # A human slide is slower and produces far more motion frames than the
    # original 12-step/0.7 s default; this bug is timing-sensitive, so the
    # gesture must be sweepable.
    ap.add_argument("--slide-steps", type=int, default=12)
    ap.add_argument("--slide-dwell", type=float, default=0.06)
    ap.add_argument("--slide-hold", type=float, default=0.0)
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

    if args.app:
        launcher = args.app / "Contents" / "MacOS" / "iPod Touch"
        if not launcher.exists():
            ap.error(f"no launcher in bundle: {launcher}")
        # the launcher builds its own -M/-pflash/-L and forwards "$@"
        cmd = [str(launcher),
               "-qmp", f"unix:{qmp_path},server,nowait"]
    else:
        cmd = [str(args.qemu),
               "-M", f"{machine},bootrom={M68_BOOTROM},iboot={iboot},nand={nand}",
               "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
               "-serial", f"file:{serial}",
               "-qmp", f"unix:{qmp_path},server,nowait"]
        if args.icount is not None:
            cmd += ["-icount", str(args.icount)]
    if args.no_display_client:
        if not args.app:
            cmd += ["-display", "none"]
    else:
        # a display BACKEND that a client can attach to, so gfx_update runs
        cmd += ["-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)
    report = {"board": args.board, "cycles": []}
    try:
        client = None
        if not args.no_display_client:
            client = DisplayClient(args.vnc_port)
            client.start()
            time.sleep(2)
        time.sleep(args.boot_wait)
        if client is not None:
            print(f"display client attached: {client.ok}")
            report["display_client"] = client.ok
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
            slide(q, args.slide_steps, args.slide_dwell, args.slide_hold)
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
            # The pixel verdict cannot tell "the slide worked" from "the lock
            # screen went away"; this can. Requires IT_MT_TRACE>=1.
            consumed = tail.count("frame consumed")
            delivered = tail.count("[TOUCH] mouse DOWN")
            report["cycles"].append({"cycle": n, "verdict": verdict,
                                     "slept": slept, "woken": locked,
                                     "after_slide": after, "model_lines": lcd,
                                     "touches_delivered": delivered,
                                     "frames_consumed_by_guest": consumed})
            print(f"cycle {n}: {verdict:12} "
                  f"(slept {slept['nonblack_pct']}%, woken "
                  f"{locked['nonblack_pct']}%, after {after['nonblack_pct']}%"
                  f", frames consumed {consumed})")
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
