#!/usr/bin/env python3
"""Where does iPhone OS 1.0 go when HOME wedges it?

`app-button-probe.py` establishes WHAT happens -- open an app, press HOME, and
the screen never returns to SpringBoard while touch stops being serviced. This
answers WHERE: it repeats that sequence and then PC-samples the guest, so the
wedge can be attributed to a spin, an idle wait, or a userland loop instead of
guessed at.

What the sequence already tells us (IT_LCD_TRACE / IT_MT_TRACE, 2026-07-28):

  * HOME is delivered and acknowledged -- SYSIC group 1 bit 8 (IRQ 0x28), read,
    INTLEVEL-read, ACKed, re-read, twice (press and release).
  * The guest REACTS: `IOCoreSurfaceRootUserClient::attach`, then the app's
    `IOMobileFramebufferUserClient` and `IOCoreSurfaceRootUserClient` both
    detach. So HOME works and the app is being torn down.
  * The LCD window base flips through 0x0f496000 / 0x0fe00000 / 0x0f400000
    continuously until the press, and never again after it.
  * `[MT] frame consumed` stops at the same moment.

So the display pipeline and the touch consumer stop together, right after a
teardown that itself succeeds. That is the shape of a block, not of an ignored
button -- which is why the interrupt-path investigation (correctly) found
nothing wrong.

Usage:
  scripts/home-wedge-probe.py --board m68ap-10 [--samples 40] [--logs DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# App path and the icon coordinate to tap, per board -- taken from
# app-button-probe.py so the two probes open the same app.


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("m68ap-10", "m68ap-114", "n45ap"),
                    required=True)
    ap.add_argument("--logs", type=Path, default=Path("/tmp/home-wedge"))
    ap.add_argument("--app")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--watch", type=float, default=0,
                    help="after HOME, poll the framebuffers for this many "
                         "seconds instead of PC-sampling (latency test)")
    ap.add_argument("--interval", type=float, default=0.02,
                    help="seconds between PC samples")
    ap.add_argument("--settle", type=float, default=40.0,
                    help="seconds to let each tap settle (the guest is slow "
                         "in wall-clock terms under -icount)")
    ap.add_argument("--vnc-port", type=int, default=5970)
    ap.add_argument("--gate-timeout", type=int, default=420)
    args = ap.parse_args()

    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    app, icon = btn.BOARDS[args.board]
    if args.app:
        app = args.app
    args.logs.mkdir(parents=True, exist_ok=True)
    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/home-wedge-{os.getpid()}.sock"

    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               S5L8900_DEBUG="1", IT_LCD_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    log = open(logp, "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    client = None
    report = {"board": args.board, "samples": []}
    try:
        # The display client is mandatory: headless QEMU never calls
        # gfx_update, so the readiness gate never arms and touch is refused.
        client = btn.lock.DisplayClient(args.vnc_port) if hasattr(btn, "lock") else _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py").DisplayClient(args.vnc_port)
        client.start()
        time.sleep(3)
        q = btn.QMP(qmp_path)

        gate = False
        for _ in range(args.gate_timeout):
            time.sleep(1)
            if "Touch input ready" in logp.read_bytes().decode("utf8", "replace"):
                gate = True
                break
        report["gate"] = gate
        print(f"touch gate armed: {gate}")
        if not gate:
            print("FAIL: never reached a home screen -- run is INVALID")
            return 1

        # iPhone OS 1.1.4 shows an educational "Edit Home Screen" modal on
        # first launch -- and every launch is a first launch, the launcher
        # clones a pristine NAND. Until it is dismissed no icon can be tapped.
        dismiss = btn.DISMISS.get(args.board)
        if dismiss:
            print(f"dismissing the first-launch modal at {dismiss} ...")
            btn.tap(q, *dismiss, 0.12)
            time.sleep(args.settle)

        print("opening an app ...")
        btn.tap(q, *icon, 0.12)
        time.sleep(args.settle)

        if args.watch:
            # LATENCY TEST. 1.1.4's HOME needs a ~64 s wait to pass, so
            # "1.0 never returns" may really be "1.0 is much slower". Poll the
            # framebuffers at a LOW rate -- pmemsave is a memory read, not a
            # stop-the-world register query, and 15 s apart it costs nothing --
            # and report the first sample at which the screen leaves the app.
            def lit3():
                out = []
                for nm, a in (("iboot", 0x0fe00000), ("k0", 0x0f400000),
                              ("k1", 0x0f496000)):
                    raw = args.logs / f"watch_{nm}.raw"
                    q.cmd("pmemsave", {"val": a, "size": 320 * 480 * 4,
                                       "filename": str(raw)})
                    d = raw.read_bytes()
                    nz = sum(1 for i in range(0, len(d), 4 * 97)
                             if d[i] or d[i + 1] or d[i + 2])
                    out.append(round(100.0 * nz / (len(d) // (4 * 97)), 1))
                return out

            base = lit3()
            print(f"  t=0s   lit {base}   (app on screen)")
            btn.key(q, "home")
            print("  -> HOME pressed; watching ...")
            t0 = time.time()
            report["watch"] = [{"t": 0, "lit": base}]
            while time.time() - t0 < args.watch:
                time.sleep(15)
                cur = lit3()
                el = int(time.time() - t0)
                report["watch"].append({"t": el, "lit": cur})
                moved = max(abs(c - b) for c, b in zip(cur, base))
                # A drop to near-black is the panel auto-sleeping after idle,
                # NOT a return to SpringBoard. The home screen has a specific
                # signature (~45% lit on 1.0, ~47% on 1.1.4), so require a
                # buffer to land near it before calling this a return.
                home = any(20 < v < 70 for v in cur)
                tag = ""
                if home:
                    tag = "   <== HOME-SCREEN-LIKE"
                elif moved > 8:
                    tag = "   (changed, but not home-like -- panel asleep?)"
                print(f"  t={el:4d}s  lit {cur}{tag}")
            print(f"\nwatched {args.watch}s")
            (args.logs / "report.json").write_text(json.dumps(report, indent=2))
            return 0

        # Sample ACROSS the press, not after it. The after-state is just the
        # idle loop on both builds and says nothing; the interesting window is
        # the handful of milliseconds in which 1.1.4 runs its button path and
        # 1.0 apparently runs nothing.
        print(f"PC-sampling across the press ({args.samples} samples, "
              f"HOME at sample {args.samples // 3}) ...")
        press_at = args.samples // 3
        for i in range(args.samples):
            if i == press_at:
                print("  -> HOME")
                btn.key(q, "home")
            r = q.cmd("human-monitor-command",
                      {"command-line": "info registers"})
            regs = r.get("return", "") or ""
            pc = cpsr = None
            # QEMU's ARM 'info registers' prints "R15=xxxxxxxx" and "PSR=...."
            for part in regs.split():
                if part.startswith("R15="):
                    pc = part[4:]
                if part.startswith("PSR="):
                    cpsr = part[4:]
            report["samples"].append({"i": i, "pc": pc, "cpsr": cpsr,
                                      "phase": "before" if i < press_at
                                      else "after"})
        # Is the home screen actually RENDERED but not scanned out? The LCD
        # scans out whatever w1_framebuffer_base points at; if SpringBoard
        # repainted into one of the other buffers and the base was never
        # re-pointed, a scanout-following probe reads "nothing happened".
        # Dump all three candidate bases and measure them.
        for name, addr in (("iboot", 0x0fe00000), ("kern0", 0x0f400000),
                           ("kern1", 0x0f496000)):
            raw = args.logs / f"fb_{name}.raw"
            q.cmd("pmemsave", {"val": addr, "size": 320 * 480 * 4,
                               "filename": str(raw)})
            d = raw.read_bytes()
            nz = sum(1 for i in range(0, len(d), 4)
                     if d[i] or d[i + 1] or d[i + 2])
            pct = 100.0 * nz / (len(d) // 4)
            report.setdefault("fb_after_home", {})[name] = round(pct, 2)
            print(f"  {name} @{addr:#x}: {pct:6.2f}% non-black")

        (args.logs / "report.json").write_text(json.dumps(report, indent=2))

        from collections import Counter
        for phase in ("before", "after"):
            hist = Counter(s["pc"] for s in report["samples"]
                           if s["pc"] and s["phase"] == phase)
            print(f"\nPC histogram {phase.upper()} the press "
                  f"({sum(hist.values())} samples):")
            for pc, n in hist.most_common(8):
                print(f"  {pc}  x{n}")
        seen_before = {s["pc"] for s in report["samples"]
                       if s["phase"] == "before"}
        only_after = Counter(s["pc"] for s in report["samples"]
                             if s["phase"] == "after"
                             and s["pc"] not in seen_before)
        print("\nPCs seen ONLY after the press (the button path, if any):")
        for pc, n in only_after.most_common(12):
            print(f"  {pc}  x{n}")
        if not only_after:
            print("  (none -- the guest never left the code it was already in)")
        print(f"\nreport: {args.logs / 'report.json'}  (qemu.log alongside)")
    finally:
        if client:
            client.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
