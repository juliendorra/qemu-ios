#!/usr/bin/env python3
"""Do Home and Power still work once an APP is open? Boot, open one, and check.

Why this exists
---------------
Every button and sleep/wake result this project has ever recorded was measured
from SpringBoard. `lock-unlock-probe.py` drives power/home/slide on the LOCK
screen; `touch-probe.py` taps the HOME screen. Nothing ever opened an app and
pressed a button -- so when Home and Power turned out to do nothing with an app
frontmost, on ALL THREE bundles, no test failed. It was found by a user, months
into the boards working.

That is the gap this closes: the frontmost-app state is a distinct state, and
buttons have to be re-tested in it.

Two harness rules it inherits the hard way:

  * A DISPLAY CLIENT IS ATTACHED. Under `-display none` QEMU never calls
    gfx_update, the LCD readiness gate never arms, touch is refused -- and,
    measured 2026-07-28, the POWER button does not sleep the device either,
    even from SpringBoard where it demonstrably works in the app. A headless
    run of this probe reports "broken" for a working build. Same lesson as T8
    in M68AP_RENDER_HANDOFF.md.
  * IT DRIVES THE BUNDLE THROUGH ITS LAUNCHER, never `nand=<bundle>/...`.
    Pointing QEMU straight at a bundle's shipped NAND bypasses the per-launch
    clone and leaves guest-written pages inside the app bundle, which then
    dirty every later launch. See TOUCH_INVESTIGATION.md.

What it checks, in order
------------------------
  1. the home screen renders and the touch gate arms
  2. tapping an app icon opens something (touch works on SpringBoard)
  3. touch still works INSIDE the app
  4. HOME returns to SpringBoard          <-- known broken, 2026-07-28
  5. POWER sleeps the panel from inside an app
  6. HOME wakes it again

Each step is a pass/fail with the pixel evidence behind it, and PNGs are
written next to the report.

Examples
--------
  scripts/app-button-probe.py --board n45ap --logs /tmp/appbtn
  scripts/app-button-probe.py --board m68ap-114 --logs /tmp/appbtn-114
  scripts/app-button-probe.py --board m68ap-10 --icon 277,258 --logs /tmp/x
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FB_W, FB_H, FB_BYTES = 320, 480, 320 * 480 * 4
FB_BASES = (0x0FE00000, 0x0F400000, 0x0F496000)

# Per-bundle defaults: the app bundle, and an icon that reliably opens an app.
# Coordinate of the modal's Dismiss button, per board, or None.
#
# iPhone OS 1.1.4 puts up an educational "Edit Home Screen" alert on FIRST
# LAUNCH -- and every launch is a first launch here, because the launcher clones
# a pristine NAND each time (read-only NAND means state resets). It is not a
# touch-and-hold artifact. Until it is dismissed no icon can be tapped, so a
# probe that ignores it never opens an app and every later step measures a
# dialog. 1.0 has no such modal.
DISMISS = {
    "m68ap-114": (160, 324),
    "m68ap-10": None,
    "n45ap": None,
}

BOARDS = {
    "n45ap":    ("/Applications/iPod Touch.app",            (222, 145)),
    "m68ap-114": ("/Applications/iPhone 2G (iOS 1.1.4).app", (277, 258)),
    "m68ap-10":  ("/Applications/iPhone 2G (iOS 1.0).app",   (277, 258)),
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QMP:
    def __init__(self, path: str, timeout: int = 180):
        deadline = time.time() + timeout
        self.sock = None
        while self.sock is None and time.time() < deadline:
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(path)
            except OSError:
                self.sock = None
                time.sleep(0.5)
        if self.sock is None:
            raise SystemExit("QMP never appeared")
        self.f = self.sock.makefile("rw")
        self.f.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, execute, arguments=None):
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.f.write(json.dumps(msg) + "\n")
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise SystemExit("QMP closed")
            reply = json.loads(line)
            if "event" not in reply:
                return reply


def abs_move(q: QMP, px: float, py: float):
    q.cmd("input-send-event", {"events": [
        {"type": "abs", "data": {"axis": "x", "value": int(px / FB_W * 32768)}},
        {"type": "abs", "data": {"axis": "y", "value": int(py / FB_H * 32768)}}]})


def tap(q: QMP, px: int, py: int, hold: float = 0.25):
    abs_move(q, px, py)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": True, "button": "left"}}]})
    time.sleep(hold)
    q.cmd("input-send-event", {"events": [
        {"type": "btn", "data": {"down": False, "button": "left"}}]})


def key(q: QMP, name: str, hold: float = 0.15):
    """Press and release a hardware button, WITH A HOLD.

    Not `send-key`. Measured 2026-07-28: with QMP send-key the power button
    does not sleep the device even from SpringBoard, where a held press
    demonstrably works -- so a send-key-based probe reports every button as
    broken. lock-unlock-probe.py has always used this form; matching it is what
    makes the two agree.
    """
    for down in (True, False):
        q.cmd("input-send-event", {"events": [
            {"type": "key", "data": {"down": down,
                                     "key": {"type": "qcode", "data": name}}}]})
        if down:
            time.sleep(hold)


# Under -icount the guest runs slower in WALL CLOCK than it used to, so the
# original 5/4/8/10 s waits started measuring before the transition finished --
# a run today had every step land one action late, with 4_power_sleeps
# "passing" on a 97% change that was really the app finally opening. Scale them
# all with IT_PROBE_WAIT (default 2x).
_W = float(os.environ.get("IT_PROBE_WAIT", "2"))
WAIT_OPEN, WAIT_TOUCH, WAIT_POWER = (5 * _W, 4 * _W, 10 * _W)
# A working handoff must not consume iOS 1.0's ~33 FinishSurface timeouts.
# IT_HOME_WAIT lets a diagnostic tighten the bound without changing the other
# waits; the normal limit remains comparable to the prompt iOS 1.1.4 path.
WAIT_HOME = float(os.environ.get("IT_HOME_WAIT", str(8 * _W)))


def grab(q: QMP, tmp: Path) -> list:
    """ALL three framebuffer bases, straight from guest RAM.

    This used to return only "the liveliest" base -- the one with the most
    non-black pixels -- and that is unsound whenever the NEW screen is DIMMER
    than stale content left in another buffer. iPhone OS 1.0's HOME step is
    exactly that case (app 99.1% lit -> home screen 45.4%): any buffer still
    holding the app won the brightness contest, so the step reported
    "0.00% changed" while the transition had plainly happened. 1.1.4 hits it
    the other way at step 1, its home screen being 69.5% lit.

    Keeping all three and taking the largest per-base change fixed that -- but
    it introduced the OPPOSITE error, and on 2026-07-30 it produced a FALSE
    PASS: on iPhone OS 1.0 the dismissal repaints the home screen into a buffer
    the display is NOT scanning out, so step 3 scored 94.62% "changed" while the
    visible screen sat unmoved at 99.1% lit. Any buffer changing counted, even
    an invisible one.

    So the verdict now follows the SCANOUT: `scanout_index()` reads the LCD's
    current window base out of the model's own IT_LCD_TRACE output and the steps
    judge that buffer. The other two are still captured, for diagnosis and as a
    fallback when the base has never been programmed.
    """
    out = []
    for base in FB_BASES:
        q.cmd("pmemsave", {"val": base, "size": FB_BYTES,
                           "filename": str(tmp)})
        out.append(tmp.read_bytes())
    return out


LCD_BASE_RE = re.compile(r"\[LCD\] (w[12]) base <- (0x[0-9a-f]+)")


def scanout_index(logp: Path):
    """Which of FB_BASES the LCD is scanning out, from the model's own trace.

    The model's rule is w1, falling back to w2 (`lcd_scanout_base()`), so take
    the most recent w1 program if there is one and the most recent w2 otherwise.
    Returns None when the base has never been programmed -- callers then fall
    back to "any buffer", and say so.
    """
    try:
        txt = logp.read_bytes().decode("utf8", "replace")
    except OSError:
        return None
    w1 = w2 = None
    for win, base in LCD_BASE_RE.findall(txt):
        try:
            b = int(base, 16)
        except ValueError:
            continue
        if b in FB_BASES:
            if win == "w1":
                w1 = FB_BASES.index(b)
            else:
                w2 = FB_BASES.index(b)
    return w1 if w1 is not None else w2


def changed(a, b, idx=None) -> float:
    """Change in the SCANNED-OUT buffer, or the largest change if unknown."""
    if isinstance(a, list):
        if idx is not None and idx < len(a) and idx < len(b):
            return changed(a[idx], b[idx])
        return max((changed(x, y) for x, y in zip(a, b)), default=0.0)
    n = min(len(a), len(b)) // 4
    c = sum(1 for i in range(0, n * 4, 4)
            if abs(a[i] - b[i]) > 12 or abs(a[i+1] - b[i+1]) > 12
            or abs(a[i+2] - b[i+2]) > 12)
    return round(100.0 * c / n, 2) if n else 0.0


def lit(d, idx=None) -> float:
    if isinstance(d, list):
        if idx is not None and idx < len(d):
            return lit(d[idx])
        return max((lit(x) for x in d), default=0.0)
    n = len(d) // 4
    c = sum(1 for i in range(0, n * 4, 4 * 97) if d[i] or d[i+1] or d[i+2])
    return round(100.0 * c / max(1, n // 97), 2)


def slept_verdict(d, before, after, seg) -> bool:
    """Did the PANEL sleep?

    Not a framebuffer question. QEMU never clears guest-owned framebuffer
    memory, so a sleeping panel leaves the last frame sitting in RAM and any
    pixel-based test reads "nothing happened" -- which is how an earlier
    version of this probe scored a WORKING power button as broken. The model
    says so itself; ask it.
    """
    return ("Merlot panel entered sleep" in seg
            or "PMU powered panel off" in seg
            or "Application processor awaiting power loss" in seg)


def png(d, path: Path):
    if isinstance(d, list):
        d = max(d, key=lit)
    import struct, zlib
    rows = b""
    for y in range(FB_H):
        row = d[y * FB_W * 4:(y + 1) * FB_W * 4]
        rows += b"\x00" + bytes(v for i in range(0, FB_W * 4, 4)
                                for v in (row[i+2], row[i+1], row[i]))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", FB_W, FB_H, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=sorted(BOARDS), required=True)
    ap.add_argument("--app", help="override the bundle path")
    ap.add_argument("--icon", help="app icon to tap, 'x,y' in panel pixels")
    ap.add_argument("--pre-tap", help="tap this first, e.g. 180,325 to dismiss "
                                      "1.1.4's Edit Home Screen alert")
    ap.add_argument("--logs", type=Path, default=Path("/tmp/app-button-probe"))
    ap.add_argument("--vnc-port", type=int, default=5998)
    ap.add_argument("--gate-timeout", type=int, default=420)
    ap.add_argument("--ab", action="store_true",
                    help="also exercise POWER on SpringBoard first, where it "
                         "WORKS, so one log holds both the working and the "
                         "failing case for comparison")
    ap.add_argument("--no-display-client", action="store_true",
                    help="DIAGNOSTIC ONLY -- without a client QEMU never "
                         "refreshes and even a working build fails")
    args = ap.parse_args()

    app, icon = BOARDS[args.board]
    if args.app:
        app = args.app
    if args.icon:
        icon = tuple(int(v) for v in args.icon.split(","))

    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    args.logs.mkdir(parents=True, exist_ok=True)
    tmp = args.logs / "fb.raw"
    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/app-button-probe-{os.getpid()}.sock"

    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait"]
    if args.no_display_client:
        cmd += ["-display", "none"]
    else:
        cmd += ["-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]

    env = dict(os.environ, IT_LCD_TRACE="1", S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0")
    log = open(logp, "wb")
    # start_new_session + killpg below: the bundle's entry point is a SHELL that
    # runs QEMU as a CHILD, so terminating `proc` leaves qemu-system-arm alive
    # holding a ~220 MB NAND clone (and the VNC port). Measured: a probe run left
    # a 13-minute-old orphan behind.
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    client = None
    report = {"board": args.board, "app": app, "steps": []}
    rc = 0
    try:
        if not args.no_display_client:
            client = lock.DisplayClient(args.vnc_port)
            client.start()
            time.sleep(3)
            report["display_client"] = client.ok
            print(f"display client attached: {client.ok}")
        q = QMP(qmp_path)

        gate = False
        for _ in range(args.gate_timeout):
            time.sleep(1)
            if "Touch input ready" in logp.read_bytes().decode("utf8", "replace"):
                gate = True
                break
        report["gate"] = gate
        print(f"touch gate armed: {gate}")
        if not gate:
            print("FAIL: never reached a stable home screen -- run is INVALID")
            report["steps"].append({"step": "gate", "pass": False})
            return 2

        def step(name, fn, wait, verdict, note="", attempts=1):
            nonlocal rc
            mark = logp.stat().st_size
            before = grab(q, tmp)
            after = before
            ok = False
            attempt = 0
            for attempt in range(1, attempts + 1):
                fn()
                time.sleep(wait)
                after = grab(q, tmp)
                seg = logp.read_bytes()[mark:].decode("utf8", "replace")
                idx = scanout_index(logp)
                d = changed(before, after, idx)
                ok = verdict(d, before, after, seg)
                if ok:
                    break
            # Judge the buffer the LCD is actually scanning out. Anything else
            # scores repaints the user cannot see -- which is exactly how this
            # step reported PASS 94.62% on 1.0 while the screen sat unmoved.
            idx = scanout_index(logp)
            d = changed(before, after, idx)
            png(after, args.logs / f"{name}.png")
            lb, la = lit(before, idx), lit(after, idx)
            off = max(changed(before, after), 0.0)
            report["steps"].append({"step": name, "diff": d, "pass": bool(ok),
                                    "scanout_index": idx,
                                    "diff_any_buffer": off,
                                    "attempts": attempt,
                                    "lit_before": lb,
                                    "lit_after": la, "note": note})
            extra = ""
            if idx is None:
                extra = "  [scanout UNKNOWN: judged on any buffer]"
            elif off - d > 5:
                extra = f"  [off-screen buffers changed {off:.2f}%]"
            print(f"{'PASS' if ok else 'FAIL'}  {name:24s} screen changed "
                  f"{d:6.2f}%   lit {lb:5.1f}% -> {la:5.1f}%"
                  + (f"   ({note})" if note else "") + extra)
            if not ok:
                rc = 1
            return after

        if args.ab:
            step("A_power_on_springboard", lambda: key(q, "p"), 10,
                 slept_verdict,
                 "POWER from SpringBoard -- the KNOWN-WORKING case")
            step("A_home_wakes", lambda: key(q, "h"), 10,
                 lambda d, b, a, seg: d > 20,
                 "wake back to the lock screen")
            step("A_slide_unlock", lambda: lock.slide(q, steps=18, dwell=0.05),
                 6, lambda d, b, a, seg: d > 20,
                 "slide back to the home screen")

        if args.pre_tap:
            px, py = (int(v) for v in args.pre_tap.split(","))
            step("0_pre_tap", lambda: tap(q, px, py, 0.3), 4,
                 lambda d, b, a, seg: True, "informational")

        dismiss = DISMISS.get(args.board)
        if dismiss:
            print(f"dismissing the first-launch modal at {dismiss} ...")
            tap(q, *dismiss, 0.12)
            time.sleep(WAIT_TOUCH)

        # HOME is only successful if it returns to this actual, icon-filled
        # SpringBoard frame.  The old oracle accepted any >20% repaint after
        # forty seconds, including the iconless partial frame reported by the
        # user; that converted the exact failure under investigation to PASS.
        home_reference = grab(q, tmp)
        png(home_reference, args.logs / "home_reference.png")

        # Require the screen to actually become APP-LIKE, not merely to change.
        # A launch replaces the home grid, so the lit fraction moves a long way
        # (1.0: 45.4 -> 99.2, iPod: 29.9 -> 99.9); a modal sitting over the home
        # screen barely moves it (1.1.4: 69.61 -> 69.55) yet still changed 25.76%
        # of pixels and passed a bare `d > 20`. That false PASS is how this probe
        # once reported 1.1.4 unable to return to SpringBoard when a human could
        # do it by hand.
        step("1_open_app", lambda: tap(q, *icon, 0.12), WAIT_OPEN,
             lambda d, b, a, seg: d > 20 and abs(lit(a, scanout_index(logp))
                                                - lit(b, scanout_index(logp))) > 8,
             "tapping an icon must open something", attempts=3)
        step("2_touch_in_app", lambda: tap(q, 160, 423, 0.12), WAIT_TOUCH,
             lambda d, b, a, seg: d > 2,
             "touch must still work with an app frontmost")
        home_after = step(
            "3_home_returns", lambda: key(q, "h"), WAIT_HOME,
            lambda d, b, a, seg: d > 20 and
            changed(home_reference, a, scanout_index(logp)) < 12,
            "HOME must promptly restore the icon-filled SpringBoard")
        home_ref_diff = changed(home_reference, home_after,
                                scanout_index(logp))
        report["steps"][-1]["home_reference_diff"] = home_ref_diff
        print(f"      SpringBoard reference difference: {home_ref_diff:.2f}%")
        step("4_reopen_for_power", lambda: tap(q, *icon, 0.12), WAIT_OPEN,
             lambda d, b, a, seg: d > 20 and
             abs(lit(a, scanout_index(logp)) -
                 lit(b, scanout_index(logp))) > 8,
             "the app must be frontmost before testing POWER", attempts=3)
        step("5_power_in_app_sleeps", lambda: key(q, "p"), WAIT_POWER,
             slept_verdict, "POWER must sleep with an app frontmost")
        slept = report["steps"][-1]["pass"]
        if slept:
            step("6_home_wakes", lambda: key(q, "h"), 10,
                 lambda d, b, a, seg: d > 20,
                 "HOME must light the panel again")
        else:
            # Waking is meaningless if the panel never slept; asserting it
            # here would PASS vacuously and hide the failure above.
            report["steps"].append({"step": "6_home_wakes", "pass": None,
                                    "note": "skipped: power never slept"})
            print("SKIP  6_home_wakes            (power never slept)")
    finally:
        if client:
            client.stop()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(20)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        tmp.unlink(missing_ok=True)
        (args.logs / "report.json").write_text(json.dumps(report, indent=2))
        print(f"\nreport: {args.logs / 'report.json'}  (PNGs alongside)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
