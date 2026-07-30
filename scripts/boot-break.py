#!/usr/bin/env python3
"""Arm breakpoints BEFORE the guest boots, and record every hit with its args.

The other probes attach gdb only once the home screen is up, because they need
to tap an icon first. That makes them blind to anything that happens during
startup -- and the question left by IN_APP_BUTTON_INVESTIGATION.md is exactly
of that kind: `_IOMobileFramebufferOpen` recorded zero hits after boot, which
does not distinguish "never called" from "called at SpringBoard startup and
failed". Only arming from t=0 separates those, and they point at different fixes.

No input is sent, so nothing here depends on the VM being resumable by QMP.

Usage:
  scripts/boot-break.py --board m68ap-10 \\
      --sym IOMobileFramebuffer:_IOMobileFramebufferOpen --wait 300
  scripts/boot-break.py --board m68ap-114 --sym LayerKit:_LKBackingStoreSwap
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

LIB_DIRS = ["System/Library/Frameworks", "System/Library/PrivateFrameworks",
            "usr/lib"]


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=["m68ap-10", "m68ap-114"], required=True)
    ap.add_argument("--sym", action="append", default=[],
                    help="LIB:SYMBOL to break on, resolved from the guest root")
    ap.add_argument("--kaddr", action="append", default=[],
                    help="raw address to break on")
    ap.add_argument("--wait", type=float, default=300,
                    help="seconds to keep recording after the guest starts")
    ap.add_argument("--max-hits", type=int, default=200)
    ap.add_argument("--gdb-port", type=int, default=1234)
    ap.add_argument("--vnc-port", type=int, default=5997)
    ap.add_argument("--capture-return", action="store_true",
                    help="on each hit, also run to the caller's return address "
                         "and read r0 -- the RETURN VALUE. Entry args alone "
                         "cannot tell 'the open failed' from 'it succeeded for "
                         "a context nobody swaps on'.")
    ap.add_argument("--logs", type=Path, default=None)
    args = ap.parse_args()

    args.logs = args.logs or Path(f"/tmp/bootbreak-{args.board}")
    args.logs.mkdir(parents=True, exist_ok=True)

    brk = _load("sbbreak", REPO / "scripts" / "springboard-button-breakpoint.py")
    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    gsp = _load("gsprobe", REPO / "scripts" / "gsevent-type-probe.py")
    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    groot = _load("guestroot", REPO / "scripts" / "guest_root.py")
    app, _icon = btn.BOARDS[args.board]

    try:
        mnt, mine = groot.attach(brk.ROOTS[args.board], "bootbreak-root")
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 2
    if not mine:
        print(f"  reusing existing mount at {mnt}")
    bps = {}
    try:
        fps = gsp.exec_fingerprints(mnt)
        for spec in args.sym:
            lib, _, sym = spec.partition(":")
            src = None
            for d in LIB_DIRS:
                for c in (Path(mnt) / d).rglob(lib):
                    if c.is_file() and not c.is_symlink():
                        src = c
                        break
                if src:
                    break
            if not src:
                print(f"  !! {lib} not found")
                continue
            syms = {n: v for v, n, d in msym.symbols(src.read_bytes()) if d}
            if sym in syms:
                bps[syms[sym] & ~1] = f"{lib}:{sym}"
                print(f"  will break on {lib}:{sym} at {syms[sym]:#x}")
            else:
                print(f"  !! {sym} not in {lib}")
    finally:
        groot.detach(mnt, mine)
    for a in args.kaddr:
        bps[int(a, 0) & ~1] = a
    if not bps:
        print("FAIL: nothing to break on")
        return 2

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/bootbreak-{os.getpid()}.sock"
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}",
           "-gdb", f"tcp::{args.gdb_port}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client, hits = None, []
    try:
        client = lock.DisplayClient(args.vnc_port)
        client.start()
        # Attach as early as the stub will accept us -- BEFORE the guest has
        # booted, which is the entire point of this script.
        g = None
        for _ in range(60):
            try:
                g = brk.Gdb(args.gdb_port)
                break
            except OSError:
                time.sleep(0.5)
        if g is None:
            print("FAIL: gdbstub never accepted a connection")
            return 1
        g.wait_stop(2)
        for a, n in bps.items():
            print(f"  breakpoint at {a:#x} ({n}): {g.set_break(a)}")
        print(f"armed before boot; recording for {args.wait:.0f}s ...")

        t0 = time.time()
        g.cont()
        while time.time() - t0 < args.wait and len(hits) < args.max_hits:
            if g.wait_stop(2) is None:
                continue
            w = g.regs()
            if not w:
                g.cont()
                continue
            regs, _c = w
            pc = regs[15]
            hdr = g.mem(gsp.EXEC_BASE, 0x40)
            rec = {"t": round(time.time() - t0, 1),
                   "at": bps.get(pc, f"?{pc:#x}"),
                   "proc": fps.get(hdr, "?" if hdr else "??"),
                   "args": [regs[0], regs[1], regs[2], regs[3]],
                   "lr": regs[14]}
            if pc in bps:
                hits.append(rec)
                print(f"  t={rec['t']:<7} {rec['at']:<52} {rec['proc']:<14} "
                      f"r0={regs[0]:#010x} r1={regs[1]:#010x} lr={regs[14]:#010x}")
                # Step off OUR breakpoint, then re-arm it.
                g.del_break(pc)
                g.step()
                g.set_break(pc)
                if args.capture_return:
                    # lr at function entry is the caller's return address, so a
                    # temporary breakpoint there catches the return and r0 is
                    # the value. Bounded and validated: a shared return address
                    # can be reached by somebody else first, and a breakpoint we
                    # did not ask for is how this script grew 156 phantom hits.
                    ret = regs[14] & ~1
                    got = None
                    if ret and ret not in bps:
                        g.set_break(ret)
                        for _try in range(60):
                            g.cont()
                            if g.wait_stop(5) is None:
                                break
                            w2 = g.regs()
                            if not w2:
                                break
                            if w2[0][15] == ret:
                                got = w2[0][0]
                                break
                            # somebody else's stop: step it off and carry on
                            if w2[0][15] in bps:
                                p2 = w2[0][15]
                                g.del_break(p2)
                                g.step()
                                g.set_break(p2)
                            else:
                                g.step()
                        g.del_break(ret)
                        if got is not None:
                            g.step()
                    rec["ret"] = got
                    rec["ret_addr"] = ret
                    print(f"           -> returned r0={got if got is None else hex(got)} "
                          f"to {ret:#010x}")
            else:
                # An unexpected stop. Do NOT set_break(pc) here: that would
                # CREATE a breakpoint at an address we never asked for, and it
                # then fires forever -- one spurious stop produced 156 phantom
                # hits in BlueTool and capped a run before it finished.
                g.step()
            g.cont()

        (args.logs / "hits.json").write_text(json.dumps(hits, indent=1))
        print(f"\n  {len(hits)} hits: "
              f"{dict(Counter(h['at'] + ' in ' + str(h['proc']) for h in hits))}")
        boot = "Touch input ready" in logp.read_bytes().decode("utf8", "replace")
        print(f"  guest reached the touch-ready gate: {boot}")
        if not hits:
            print("  => NOT CALLED at any point from boot to here. That is a "
                  "different\n     fact from 'called and failed', and this run "
                  "can tell them apart.")
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
