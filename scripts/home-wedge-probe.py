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
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--vnc-port", type=int, default=5970)
    ap.add_argument("--gate-timeout", type=int, default=420)
    args = ap.parse_args()

    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    app, icon = btn.BOARDS[args.board]
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

        print("opening an app ...")
        btn.tap(q, *icon)
        time.sleep(8)

        print("pressing HOME (held) ...")
        btn.key(q, "home")

        print(f"PC-sampling {args.samples}x ...")
        for i in range(args.samples):
            time.sleep(0.25)
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
            report["samples"].append({"i": i, "pc": pc, "cpsr": cpsr})
        (args.logs / "report.json").write_text(json.dumps(report, indent=2))

        from collections import Counter
        hist = Counter(s["pc"] for s in report["samples"] if s["pc"])
        print("\nPC histogram after the HOME press:")
        for pc, n in hist.most_common(10):
            where = "kernel" if pc and pc.lower() >= "c0000000" else "user/low"
            print(f"  {pc}  x{n:<3} {where}")
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
