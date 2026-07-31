#!/usr/bin/env python3
"""Measure the HOME-press -> home-screen latency, gdb-free, by screen sampling.

The dismissal on 1.0 takes seconds even now that nothing wedges and nothing is
watchdog-killed (no crash log for the dismissed app; the MBX burst at
dismissal spans <1 ms). This probe answers WHERE the wall-clock goes with the
only instrument that cannot perturb it: polling the framebuffers over QMP
(pmemsave, the same capture app-button-probe uses) every ~300 ms and printing
a timeline of scanout change and lit fraction, so "frozen for N s then snaps"
and "animates slowly" look different on sight.

If the first press produces no visible change within --first-wait, a second
press is sent and the clock notes it: the FIRST in-app press loses its DOWN
about half the time (documented, separate bug), and a latency number that
silently includes that loss would be garbage.

  scripts/dismiss-latency.py --board m68ap-10
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", default="m68ap-10")
    ap.add_argument("--vnc-port", type=int, default=5931)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--first-wait", type=float, default=6)
    ap.add_argument("--watch", type=float, default=45)
    ap.add_argument("--tap-first", action="store_true")
    ap.add_argument("--press-delay", type=float, default=5,
                    help="seconds between CONFIRMED app-open and the press; "
                         "the delivery seems to die with in-app idle time")
    ap.add_argument("--hold", type=float, default=0.15,
                    help="key hold in WALL seconds; under -icount the guest "
                         "sees far less, and a too-short hold may sit below "
                         "the button debounce")
    ap.add_argument("--poll", type=float, default=0.3,
                    help="seconds between framebuffer polls. 0.3 catches "
                         "animation frames but the pmemsave BQL traffic "
                         "perturbs delivery; 1.0 is the gentle setting for "
                         "latency numbers (the 2026-07-31 dl-10 runs at 0.3 "
                         "measured the AUTO-LOCK DIM, not the press)")
    ap.add_argument("--logs", type=Path, default=Path("/tmp/dismiss-latency"))
    args = ap.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)

    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    app, icon = btn.BOARDS[args.board]

    logp = args.logs / "qemu.log"
    tmp = args.logs / "fb.raw"
    qmp_path = f"/tmp/dismiss-latency-{os.getpid()}.sock"
    # IT_LCD_TRACE so scanout_index() can name the visible buffer.
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               IT_LCD_TRACE="1", IT_KEY_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = None
    try:
        client = lock.DisplayClient(args.vnc_port)
        client.start()
        time.sleep(3)
        q = btn.QMP(qmp_path)
        for _ in range(420):
            time.sleep(1)
            if "Touch input ready" in logp.read_bytes().decode("utf8", "replace"):
                break
        else:
            print("FAIL: no home screen; run is INVALID")
            return 1
        print("home screen up")
        dismiss = btn.DISMISS.get(args.board)
        if dismiss:
            btn.tap(q, *dismiss, 0.12)
            time.sleep(args.settle)
        idx = btn.scanout_index(logp)
        home = btn.grab(q, tmp)
        print(f"scanout index: {idx}; home-screen lit={btn.lit(home, idx)}%")
        print("opening an app ...")
        btn.tap(q, *icon, 0.12)
        # Poll until the app is CONFIRMED open (scanout goes bright), so the
        # press delay below is measured from a known state -- a fixed sleep
        # once let the launch land inside the press window and produced a run
        # that said nothing (dl-10f).
        t_tap = time.time()
        inapp = None
        while time.time() - t_tap < 120:
            time.sleep(1)
            idx = btn.scanout_index(logp)
            cur = btn.grab(q, tmp)
            if btn.lit(cur, idx) > 90:
                inapp = cur
                break
        if inapp is None:
            print("FAIL: app never opened; run is INVALID")
            return 1
        t_open = time.time()
        print(f"app open {t_open - t_tap:.1f}s after the tap; "
              f"in-app lit={btn.lit(inapp, idx)}%")
        if args.press_delay:
            print(f"waiting {args.press_delay:.0f}s before pressing ...")
            time.sleep(args.press_delay)

        if args.tap_first:
            # app-button-probe's sequence taps the app before pressing HOME
            # and its step 3 passes; without the tap both presses are
            # swallowed (measured, dl-10b/dl-10c). Reproduce its shape to get
            # a clean render-latency number; the swallow is its own bug.
            print("tapping the app first ...")
            btn.tap(q, 160, 240, 0.12)
            time.sleep(3)
        print("pressing HOME (ladder: again every "
              f"{args.first_wait:.0f}s until the screen goes home-like) ...")
        t0 = time.time()
        btn.key(q, "h", args.hold)
        presses = [0.0]
        prev = inapp
        rows = []
        while time.time() - t0 < args.watch:
            time.sleep(args.poll)
            idx = btn.scanout_index(logp)
            cur = btn.grab(q, tmp)
            t = time.time() - t0
            ch = btn.changed(prev, cur, idx)
            ch_any = btn.changed(prev, cur)      # ANY buffer, incl. invisible
            vs_home = btn.changed(home, cur, idx)
            lit_now = btn.lit(cur, idx)
            rows.append((t, ch, lit_now, vs_home))
            if ch > 0.5 or ch_any > 0.5:
                print(f"  t={t:6.2f}s  scanout[{idx}] changed {ch:6.2f}%  "
                      f"any-buffer {ch_any:6.2f}%  "
                      f"lit {lit_now:5.1f}%  vs-home {vs_home:6.2f}%")
            prev = cur
            if vs_home < 20.0:
                print(f"  t={t:6.2f}s  HOME SCREEN reached "
                      f"({t - presses[-1]:.2f}s after press #{len(presses)})")
                break
            if t - presses[-1] > args.first_wait and len(presses) < 8:
                presses.append(t)
                print(f"  t={t:6.2f}s  still in-app -- press #{len(presses)}")
                btn.key(q, "h", args.hold)
        pressed_again = presses[-1] if len(presses) > 1 else None
        keys = [l for l in logp.read_bytes().decode("utf8", "replace").splitlines()
                if "[KEYTRACE]" in l]
        print(f"\n  [KEYTRACE] lines: {len(keys)} (expect 2 per press)")
        for l in keys[-6:]:
            print(f"    {l.strip()}")
        react = next((r for r in rows if r[1] > 0.5), None)
        back = next((r for r in rows if r[3] < 20.0), None)
        base = pressed_again or 0.0
        print("\nsummary:")
        print(f"  second press sent at: {pressed_again}")
        if react:
            print(f"  first visible reaction: t={react[0]:.2f}s "
                  f"({react[0] - base:.2f}s after the effective press)")
        else:
            print("  NO visible reaction in the whole window")
        if back:
            print(f"  scanout within 20% of the home screen: t={back[0]:.2f}s "
                  f"({back[0] - base:.2f}s after the effective press)")
        else:
            print("  scanout NEVER settled near the home-screen reference")
    finally:
        if client:
            client.stop()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
