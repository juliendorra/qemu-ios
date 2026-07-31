#!/usr/bin/env python3
"""Measure the in-app HOME press swallow, per press, with model-side timing.

The symptom (IN_APP_BUTTON_INVESTIGATION.md, 2026-07-31): with an app
frontmost on iPhone OS 1.0, roughly half of HOME presses do nothing --
SpringBoard receives an UP GSEvent with no DOWN, which dies at the
`_menuButtonTimer == nil` gate. The model always sees both edges.

Hypothesis under test: the iPhone profiles run under `-icount shift=1`, so
QEMU_CLOCK_VIRTUAL advances with executed instructions. With the guest busy
(an app frontmost) a 150 ms wall-clock hold shrinks to a few ms of GUEST
time; the button driver's workloop samples the GPIO pin after the release
edge, reads "not pressed", and synthesizes an UP only. From the idle home
screen the virtual clock warps at wall speed, the hold stays wide, and
delivery is 10/10.

So this probe records, per press:
  * the verdict (did the app dismiss?) -- from the scanned-out framebuffer
  * the press's width in VIRTUAL time -- from the timestamped [BTN] lines
  * the guest's INTSTAT ack times for the menu group -- from [SYSIC] lines

and prints a per-press table. Successful presses are followed by re-opening
the app so every press is an in-app press.

Inherits the app-button-probe harness rules: a display client is attached,
and the bundle is driven through its launcher.

Usage:
  scripts/home-swallow-probe.py --board m68ap-10 --presses 8 --logs /tmp/swallow
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load("appbtn", REPO / "scripts" / "app-button-probe.py")

# [BTN] t=12.345678 keycode=35 ...
BTN_RE = re.compile(r"\[BTN\] t=(\d+)\.(\d+) keycode=(\d+)")
# [SYSIC] t=12.345678 ACK INTSTAT group 1 = 0x00000100 (n=3)
SYSIC_RE = re.compile(
    r"\[SYSIC\] t=(\d+)\.(\d+) (\S+ \S+) group (\d+) = 0x([0-9a-f]+)")


def parse_traces(seg: str):
    """Edge and ack timeline (virtual seconds) from a qemu.log segment."""
    edges = [(int(m[1]) + int(m[2]) / 1e6, int(m[3]))
             for m in BTN_RE.finditer(seg)]
    acks = [(int(m[1]) + int(m[2]) / 1e6, m[3], int(m[4]), int(m[5], 16))
            for m in SYSIC_RE.finditer(seg)
            if m[3] == "ACK INTSTAT"]
    return edges, acks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", choices=sorted(ab.BOARDS), default="m68ap-10")
    ap.add_argument("--presses", type=int, default=8)
    ap.add_argument("--hold", type=float, default=0.15,
                    help="wall-clock hold, matching app-button-probe")
    ap.add_argument("--logs", type=Path, default=Path("/tmp/home-swallow"))
    ap.add_argument("--vnc-port", type=int, default=5997)
    ap.add_argument("--gate-timeout", type=int, default=420)
    args = ap.parse_args()

    app, icon = ab.BOARDS[args.board]
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    args.logs.mkdir(parents=True, exist_ok=True)
    tmp = args.logs / "fb.raw"
    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/home-swallow-{os.getpid()}.sock"

    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    env = dict(os.environ, IT_LCD_TRACE="1", IT_KEY_TRACE="1",
               IT_SYSIC_TRACE="1",
               IT_GPIO_TRACE=str(args.logs / "gpio.log"),
               S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0")
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    presses = []
    rc = 0
    try:
        client = lock.DisplayClient(args.vnc_port)
        client.start()
        time.sleep(3)
        q = ab.QMP(qmp_path)

        gate = False
        for _ in range(args.gate_timeout):
            time.sleep(1)
            if "Touch input ready" in logp.read_bytes().decode("utf8",
                                                               "replace"):
                gate = True
                break
        print(f"touch gate armed: {gate}")
        if not gate:
            return 2
        time.sleep(5)

        def open_app() -> bool:
            idx = ab.scanout_index(logp)
            before = ab.grab(q, tmp)
            ab.tap(q, *icon, 0.12)
            time.sleep(ab.WAIT_OPEN)
            after = ab.grab(q, tmp)
            idx = ab.scanout_index(logp)
            d = ab.changed(before, after, idx)
            la = ab.lit(after, idx)
            ok = d > 20 and la > 90
            print(f"open_app: changed {d:.2f}% lit {la:.1f}% -> "
                  f"{'OPEN' if ok else 'NOT OPEN'}")
            return ok

        if not open_app():
            print("could not open the app; retrying once")
            time.sleep(5)
            if not open_app():
                return 2

        for i in range(args.presses):
            mark = logp.stat().st_size
            before = ab.grab(q, tmp)
            ab.key(q, "h", args.hold)
            time.sleep(ab.WAIT_HOME)
            after = ab.grab(q, tmp)
            seg = logp.read_bytes()[mark:].decode("utf8", "replace")
            idx = ab.scanout_index(logp)
            d = ab.changed(before, after, idx)
            la = ab.lit(after, idx)
            # dismissal = big change AND the dimmer home grid, mirroring
            # app-button-probe's step-3 shape (1.0 app ~99% lit, home ~45%)
            dismissed = d > 20 and la < 90
            edges, acks = parse_traces(seg)
            downs = [t for t, k in edges if k == 35]
            ups = [t for t, k in edges if k == 163]
            width = (ups[0] - downs[0]) if downs and ups else None
            rec = {"press": i + 1, "dismissed": bool(dismissed),
                   "diff": d, "lit_after": la,
                   "virt_width_ms":
                       round(width * 1000, 3) if width is not None else None,
                   "edges": edges, "acks": acks}
            presses.append(rec)
            print(f"press {i+1}: {'DISMISSED' if dismissed else 'SWALLOWED'}"
                  f"  diff {d:6.2f}%  lit {la:5.1f}%  "
                  f"virtual width "
                  f"{rec['virt_width_ms'] if width is not None else '?'} ms  "
                  f"acks {[(round(t, 3), g, hex(v)) for t, _, g, v in acks]}")
            if dismissed and i + 1 < args.presses:
                time.sleep(3)
                if not open_app():
                    time.sleep(5)
                    if not open_app():
                        print("cannot reopen the app; stopping early")
                        break

        n_ok = sum(1 for p in presses if p["dismissed"])
        print(f"\n{n_ok}/{len(presses)} presses dismissed the app")
        (args.logs / "report.json").write_text(json.dumps(
            {"board": args.board, "hold": args.hold, "presses": presses},
            indent=1))
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(2)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
