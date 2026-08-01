#!/usr/bin/env python3
"""Does TOUCH survive a wake from AUTO-sleep? (User-reported regression, 2026-08-01.)

The report: on the iPhone bundles (1.0 AND 1.1.4), after the device
auto-sleeps from idle, HOME wakes the panel to the lock screen but
slide-to-unlock is dead -- touch never registers. Waking from a MANUAL
power-button sleep is fine. The iPod appears immune -- but the iPod bundle
also never received the engine the iPhone bundles run, so "immune" may just
mean "old engine".

No existing probe covers this path: lock-unlock-probe drives MANUAL sleep
(press P), and every sleep/wake result in the record goes through P. The
auto-sleep path (idle -> dim -> panel off, no button ever pressed) is a
different guest code path and was never regression-tested.

Sequence:
  1. boot to the home screen (touch gate armed), display client attached
  2. do NOTHING until the model logs the panel sleeping (idle auto-lock,
     ~60 s of guest idle + margin)
  3. press H to wake -> lock screen
  4. slide to unlock, judge the scanned-out framebuffer
  5. print every [TOUCH] line after the wake -- "Ignoring input until
     display/driver startup is stable" is the smoking gun for a model-side
     gate that never re-armed

Usage:
  scripts/autosleep-touch-probe.py --board m68ap-114 --logs /tmp/aswake
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

SLEEP_MARKS = ("Merlot panel entered sleep", "PMU powered panel off",
               "panel_off", "backlight off")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", choices=sorted(ab.BOARDS), default="m68ap-114")
    ap.add_argument("--logs", type=Path, default=Path("/tmp/autosleep-touch"))
    ap.add_argument("--vnc-port", type=int, default=5996)
    ap.add_argument("--gate-timeout", type=int, default=420)
    ap.add_argument("--sleep-timeout", type=int, default=300,
                    help="max seconds to wait for the auto-sleep")
    ap.add_argument("--wake-settle", type=float, default=0,
                    help="seconds to WAIT after the panel lights before "
                         "sliding. The wake-detection loop already consumes "
                         "several seconds; extra delay can run past the lock "
                         "screen's own short re-sleep timer and turn a correct "
                         "wake into a false dead-touch result.")
    ap.add_argument("--require-masked-idle", action="store_true",
                    help="accept the run only if the HOME press lands in the "
                         "iOS 1.0 _cpu_idle WFI exit at 0xc005a2e4..0xc005a33c "
                         "with IRQ and FIQ masked. This is the precise old "
                         "lost-GPIO reproduction, not merely an auto-sleep.")
    ap.add_argument("--pre-app", action="store_true",
                    help="USE the device before idling into auto-sleep: open "
                         "an app, press HOME, wait for the return. Every "
                         "passing run so far (five configurations, 2026-08-01) "
                         "was a fresh boot straight to idle, while the "
                         "user-reported failure came after a session of "
                         "'testing the bundled apps' -- and on 1.0 that HOME "
                         "return leaves the documented broken home screen "
                         "(wallpaper + dock, no icons, icons still hittable). "
                         "Guest state carried from that into the sleep is the "
                         "last untested variable reachable from this harness.")
    ap.add_argument("--pre-cycle", action="store_true",
                    help="do one MANUAL P-sleep / H-wake / slide-unlock cycle "
                         "before idling into auto-sleep. Fresh-boot auto-sleep "
                         "wakes PASS shallow AND deep (measured 2026-08-01); "
                         "the user's session had prior sleep cycles, so state "
                         "carried across an earlier wake is the next variable.")
    ap.add_argument("--host-nap", type=int, default=0, metavar="SECS",
                    help="after the guest is asleep, SIGSTOP the emulator for "
                         "SECS seconds and SIGCONT it -- a stand-in for the "
                         "HOST Mac sleeping under an idle emulator. The "
                         "user-visible failure happened after a long unattended "
                         "stretch (2:16 AM on the guest clock), and the two "
                         "affected bundles run -icount while the immune iPod "
                         "does not, so a host wall-clock discontinuity meeting "
                         "icount is the prime remaining suspect.")
    ap.add_argument("--wake-delay", type=int, default=None, metavar="SECS",
                    help="press H exactly SECS after the panel-sleep marker. "
                         "The user's failing wake was ~30 s after the panel "
                         "blanked -- BETWEEN the two delays this probe has "
                         "PASSED at (+5-10 s shallow, +55 s post-deep-commit) "
                         "-- i.e. possibly DURING the OOCSHDWN commit, whose "
                         "'press raced/queued during transition' paths are "
                         "special-cased in the model and never touch-tested. "
                         "The user waited a few seconds after the slider "
                         "appeared before dragging, so a too-early drag is "
                         "ruled out; the wake TIMING is the live variable.")
    ap.add_argument("--poke", action="store_true",
                    help="TAP THE DARK SCREEN once after the auto-sleep, "
                         "before pressing H -- what a human does when the "
                         "panel blanks unexpectedly mid-use. A touch delivered "
                         "while the multitouch is asleep is the remaining "
                         "suspect for the reported wake-touch loss: it fits "
                         "'manual P-sleep is fine' (nobody clicks a screen "
                         "they blanked on purpose) and the iPod's immunity "
                         "(its wake path resets multitouch state, #45c).")
    ap.add_argument("--deep", action="store_true",
                    help="after the panel sleeps, keep waiting for the FULL "
                         "deep-sleep commit (OOCSHDWN / pre-warm park) before "
                         "waking. The shallow wake PASSES on 1.1.4 (measured "
                         "2026-08-01: slide 74.66%% changed); the user-reported "
                         "touch loss must therefore be on this path.")
    args = ap.parse_args()

    app, _icon = ab.BOARDS[args.board]
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    args.logs.mkdir(parents=True, exist_ok=True)
    tmp = args.logs / "fb.raw"
    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/autosleep-{os.getpid()}.sock"

    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    env = dict(os.environ, IT_LCD_TRACE="1", IT_KEY_TRACE="1",
               IT_SYSIC_TRACE="1", IT_GATE_TRACE="1",
               S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0")
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    report = {"board": args.board, "app": app, "options": dict(vars(args)),
              "log": str(logp)}
    # pathlib values are not JSON serializable.
    report["options"]["logs"] = str(args.logs)
    rc = 1
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
        report["touch_gate_armed"] = gate
        if not gate:
            return 2

        # 1.1.4's first-launch modal steals the first tap; dismiss it so the
        # lock screen behind it is the real one later.
        dismiss = ab.DISMISS.get(args.board)
        if dismiss:
            ab.tap(q, *dismiss, 0.12)
            time.sleep(5)

        if args.pre_app:
            print("pre-app: opening an app ...")
            _app, icon = ab.BOARDS[args.board]
            ab.tap(q, *icon, 0.12)
            time.sleep(ab.WAIT_OPEN)
            idx = ab.scanout_index(logp)
            print(f"pre-app: in-app lit={ab.lit(ab.grab(q, tmp), idx):.1f}%")
            print("pre-app: HOME to return promptly ...")
            ab.key(q, "h", 0.15)
            t0 = time.time()
            while time.time() - t0 < 90:
                time.sleep(4)
                idx = ab.scanout_index(logp)
                if ab.lit(ab.grab(q, tmp), idx) < 90:
                    break
            idx = ab.scanout_index(logp)
            print(f"pre-app: home again after {time.time()-t0:.0f}s "
                  f"(lit={ab.lit(ab.grab(q, tmp), idx):.1f}%)")
            time.sleep(5)

        if args.pre_cycle:
            print("pre-cycle: P sleep ...")
            mark = logp.stat().st_size
            ab.key(q, "p", 0.15)
            for _ in range(24):
                time.sleep(5)
                seg = logp.read_bytes()[mark:].decode("utf8", "replace")
                if any(m in seg for m in SLEEP_MARKS):
                    break
            print("pre-cycle: H wake ...")
            ab.key(q, "h", 0.15)
            t0 = time.time()
            while time.time() - t0 < 120:
                time.sleep(4)
                idx = ab.scanout_index(logp)
                if ab.lit(ab.grab(q, tmp), idx) > 20:
                    break
            time.sleep(4)
            before = ab.grab(q, tmp)
            lock.slide(q, steps=18, dwell=0.05)
            time.sleep(6)
            idx = ab.scanout_index(logp)
            d = ab.changed(before, ab.grab(q, tmp), idx)
            print(f"pre-cycle: manual-wake slide changed {d:.2f}% "
                  f"({'ok' if d > 20 else 'ALREADY DEAD'})")
            if d <= 20:
                print("touch already dead after the MANUAL cycle -- the "
                      "auto-sleep framing is wrong; stopping here")
                return 3
            time.sleep(3)

        print(f"idling; waiting up to {args.sleep_timeout}s for auto-sleep ...")
        mark = logp.stat().st_size
        slept = False
        t0 = time.time()
        while time.time() - t0 < args.sleep_timeout:
            time.sleep(5)
            seg = logp.read_bytes()[mark:].decode("utf8", "replace")
            if any(m in seg for m in SLEEP_MARKS):
                slept = True
                break
        print(f"auto-sleep observed: {slept}  (t={time.time()-t0:.0f}s)")
        report["auto_sleep"] = {"observed": slept,
                                "wall_seconds": round(time.time() - t0, 2),
                                "log_offset": mark}
        if not slept:
            print("no auto-sleep within the window -- run says nothing")
            return 2

        if args.deep:
            DEEP_MARKS = ("Application processor awaiting power loss",
                          "Pre-warmed wake parked")
            print("waiting for the DEEP sleep commit ...")
            deep = False
            t0 = time.time()
            while time.time() - t0 < args.sleep_timeout:
                time.sleep(5)
                seg = logp.read_bytes()[mark:].decode("utf8", "replace")
                if any(m in seg for m in DEEP_MARKS):
                    deep = True
                    break
            print(f"deep sleep observed: {deep}  (t={time.time()-t0:.0f}s)")
            if not deep:
                print("no deep sleep within the window -- shallow-only run")

        if args.wake_delay is not None:
            print(f"waiting exactly {args.wake_delay}s from the sleep marker "
                  "before waking ...")
            remaining = args.wake_delay - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)

        if args.poke:
            time.sleep(3)
            print("poking the dark screen (tap at 160,240) ...")
            ab.tap(q, 160, 240, 0.15)
            time.sleep(3)

        if args.host_nap:
            print(f"host nap: SIGSTOP for {args.host_nap}s ...")
            os.killpg(os.getpgid(proc.pid), signal.SIGSTOP)
            time.sleep(args.host_nap)
            os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
            print("host nap: SIGCONT")
            time.sleep(5)

        time.sleep(5)
        mark = logp.stat().st_size
        parks_before = logp.read_bytes().decode("utf8", "replace").count(
            "Pre-warmed wake parked")
        print("pressing H to wake ...")
        ab.key(q, "h", 0.15)
        # A deep wake is a retained-RAM boot and takes tens of seconds; poll
        # until the panel is lit rather than grabbing blind at +8 s.
        woke = None
        t0 = time.time()
        while time.time() - t0 < (120 if args.deep else 8):
            time.sleep(4)
            idx = ab.scanout_index(logp)
            woke = ab.grab(q, tmp)
            if ab.lit(woke, idx) > 20:
                break
        idx = ab.scanout_index(logp)
        print(f"after wake (+{time.time()-t0:.0f}s): lit={ab.lit(woke, idx)}%")
        if woke is not None:
            ab.png(woke[idx] if idx is not None else woke,
                   args.logs / "1_after_wake.png")
        time.sleep(4)
        woke = ab.grab(q, tmp)

        wake_seg = logp.read_bytes()[mark:].decode("utf8", "replace")
        btn_states = []
        for pc, irq, fiq in re.findall(
                r"\[BTN\].*?PC=0x([0-9a-fA-F]+)\s+I=([01]) F=([01])",
                wake_seg):
            btn_states.append({"pc": f"0x{int(pc, 16):08x}",
                               "irq_masked": irq == "1",
                               "fiq_masked": fiq == "1"})
        masked_idle = any(0xc005a2e4 <= int(s["pc"], 16) <= 0xc005a33c
                          and s["irq_masked"] and s["fiq_masked"]
                          for s in btn_states)
        report["wake"] = {"wall_seconds_to_lit": round(time.time() - t0, 2),
                          "lit_percent": ab.lit(woke, idx),
                          "scanout_index": idx,
                          "button_states": btn_states,
                          "masked_idle_reproduced": masked_idle,
                          "log_offset": mark}
        print(f"masked _cpu_idle press reproduced: {masked_idle}")
        if args.require_masked_idle and not masked_idle:
            print("required masked-idle state was not observed -- run is INVALID")
            return 2

        # The definitive signature, independent of what the slide does: did the
        # device PARK AGAIN after we woke it? Counting parks is exact, where a
        # pixel verdict is a judgement call.
        if args.wake_settle:
            print(f"settling {args.wake_settle:.0f}s before the slide "
                  "(a human looks at the screen first) ...")
            time.sleep(args.wake_settle)
        full = logp.read_bytes().decode("utf8", "replace")
        parks_after = full.count("Pre-warmed wake parked")
        reparked = parks_after > parks_before
        idx = ab.scanout_index(logp)
        settled = ab.grab(q, tmp)
        ab.png(settled[idx] if idx is not None else settled,
               args.logs / "2_before_slide.png")
        print(f"after settle: lit={ab.lit(settled, idx):.1f}%  "
              f"parks {parks_before} -> {parks_after}"
              f"{'   *** RE-PARKED: the wake did not stick ***' if reparked else ''}")

        print("sliding to unlock ...")
        before = settled
        lock.slide(q, steps=18, dwell=0.05)
        time.sleep(6)
        idx = ab.scanout_index(logp)
        after = ab.grab(q, tmp)
        ab.png(after[idx] if idx is not None else after,
               args.logs / "3_after_slide.png")
        d = ab.changed(before, after, idx)
        seg = logp.read_bytes()[mark:].decode("utf8", "replace")
        sleep_count = seg.count("Merlot panel entered sleep")
        relit_then_reslept = sleep_count > 0
        touches = [l.strip() for l in seg.splitlines() if "[TOUCH]" in l]
        refused = sum(1 for l in touches if "Ignoring input" in l)
        print(f"slide verdict: screen changed {d:.2f}%  "
              f"({'UNLOCKED -- touch works' if d > 20 else 'DEAD -- touch lost'})")
        print(f"[TOUCH] lines after wake: {len(touches)}  "
              f"({refused} REFUSED BY THE MODEL)")
        for l in touches[:10]:
            print(f"    {l}")
        ok = d > 20 and not reparked and not relit_then_reslept and refused == 0
        report["result"] = {"pass": ok, "screen_changed_percent": d,
                            "lit_after_settle_percent": ab.lit(settled, idx),
                            "parks_before": parks_before,
                            "parks_after": parks_after,
                            "reparked": reparked,
                            "relit_then_reslept": relit_then_reslept,
                            "touch_lines": len(touches),
                            "touches_refused": refused,
                            "log_end_offset": logp.stat().st_size}
        print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}"
              f"{' -- re-parked after the wake' if reparked else ''}"
              f"{f' -- {refused} touches refused' if refused else ''}")
        rc = 0 if ok else 1
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
        tmp.unlink(missing_ok=True)
        (args.logs / "report.json").write_text(json.dumps(report, indent=2))
        print(f"report: {args.logs / 'report.json'}  (PNGs and qemu.log alongside)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
