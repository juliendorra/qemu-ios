#!/usr/bin/env python3
"""Which GSEvents reach userland when HOME is pressed, and in WHICH process?

RESULT (2026-07-29): the event is NOT mis-routed. On iPhone OS 1.0 with an app
frontmost, the button GSEvent (type1001) IS delivered to SpringBoard,
`-[SpringBoard menuButtonUp:]` really runs, and its early-return ivar is clear --
indistinguishable from 1.1.4, which works. The earlier "event ROUTING" verdict
came from a probe that pressed qcode "home", which the machine's input handler
discards (it takes only P/H), and from breakpoint hits in OTHER PROCESSES.

The obvious probe, "break on `_PurpleEventCallback`", is too blunt on its own:
it fires for EVERY event and its stack cannot say which process it is in. This
one fixes both problems, and asserts its own controls.

  * **The event, not the callback.** `_PurpleEventCallback` sets up, then drains
    a queue. Break at the DEQUEUE -- `ldm r0, {r3, r8}` puts the event in r8 and
    the `ldr r1, [r8, #8]` right after it always runs (1.0 0x3098d028, 1.1.4
    0x30ab642c, derived here, not hardcoded) -- so every hit carries an event.
    Not the loop body at the top of the function: that is reached conditionally,
    after coalescing, and a run that broke there saw ZERO events in 75 s.

  * **The type, decoded the way the OS decodes it.** `_GSEventGetType` is
    `t = [ev+8]; if t != 3001: return t; else map [ev+0x38] (1..6)` -- the same
    literal 0xbb9 on both builds. So the type is readable with two loads, with
    no need to call into the guest.

  * **Which process.** Every main executable in this OS links its __TEXT at
    0x1000 and there is no ASLR, so reading guest virtual memory at 0x1000 --
    which resolves through the CURRENT process's MMU mapping -- gives that
    process's Mach-O header. Fingerprinting it against SpringBoard and every
    /Applications binary names the process outright. (This also settles the
    resolver ambiguity that previously mis-attributed SpringBoard's own frames
    to CommCenter.)

Two controls run on every invocation, because every harness bug in this
investigation has produced SILENCE rather than an error:

  * `IT_KEY_TRACE=1` lines prove the press reached `ipod_touch_key_event`.
  * a QMP `query-status` liveness check per phase proves the guest was actually
    running -- a wedged RSP session leaves the vCPU stopped, which reads exactly
    like "no events".

Usage:
  scripts/gsevent-type-probe.py --board m68ap-10
  scripts/gsevent-type-probe.py --board m68ap-10  --no-app     # the control
  scripts/gsevent-type-probe.py --board m68ap-114             # known-good
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

SB = "System/Library/CoreServices/SpringBoard.app/SpringBoard"
GS = "System/Library/Frameworks/GraphicsServices.framework/GraphicsServices"

# Where to break inside _PurpleEventCallback, derived from the binary.
#
# NOT the loop body at the top of the function (1.0 0x3098ceac): that is reached
# only via `cmp r8,#0 / bne` at the END of the dequeue, AFTER _GSEventTakeLater
# may have coalesced the event away, so events can be processed without ever
# passing through it. A first run broke there and saw ZERO events in 75 s of a
# live SpringBoard.
#
# The unconditional per-event point is the dequeue itself: `ldm r0, {r3, r8}`
# pops the queue entry and puts the event in r8, and the first
# `ldr r1, [r8, #8]` after it -- the type load -- always executes.
#   1.0    0x3098d028      1.1.4  0x30ab642c
DEQUEUE_INSN = bytes.fromhex("080190e8")     # ldm r0, {r3, r8}
TYPELOAD_INSN = bytes.fromhex("081098e5")    # ldr r1, [r8, #8]

EXEC_BASE = 0x1000          # every main executable's __TEXT vmaddr
TYPE_SENTINEL = 3001        # 0xbb9: "look at the subtype at +0x38 instead"
SUBTYPE_MAP = {1: 1, 2: 6, 3: 3, 4: 4, 5: 5, 6: 2}

# Names for the types actually observed, filled in from measurement rather than
# from a header we do not have. Unknown values are printed raw, which is the
# point of the probe.
TYPE_NAMES = {
    1: "LeftMouseDown", 2: "LeftMouseUp", 3: "RightMouseDown",
    4: "RightMouseUp", 5: "MouseMoved", 6: "LeftMouseDragged",
    10: "KeyDown", 11: "KeyUp",
}


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MH_EXECUTE = 2
LC_SEGMENT = 1


def macho_text(data: bytes):
    """(filetype, vmaddr, fileoff, filesize) of __TEXT, or None.

    Parsed here rather than shelled out to `otool`, because fingerprinting every
    executable in the root filesystem is hundreds of files and one subprocess
    each is the difference between a second and a minute.
    """
    if len(data) < 28 or data[:4] not in (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
        return None
    end = "<" if data[:4] == b"\xce\xfa\xed\xfe" else ">"
    filetype, = struct.unpack_from(end + "I", data, 12)
    ncmds, = struct.unpack_from(end + "I", data, 16)
    off = 28
    for _ in range(ncmds):
        if off + 8 > len(data):
            return None
        cmd, cmdsize = struct.unpack_from(end + "II", data, off)
        if not cmdsize:
            return None
        if cmd == LC_SEGMENT and data[off + 8:off + 14] == b"__TEXT":
            vmaddr, _vmsize, fileoff, filesize = struct.unpack_from(
                end + "IIII", data, off + 24)
            return filetype, vmaddr, fileoff, filesize
        off += cmdsize
    return None


def text_seg(binary: Path):
    """(vmaddr, fileoff, filesize) of __TEXT."""
    m = macho_text(Path(binary).read_bytes())
    return None if not m else (m[1], m[2], m[3])


def dequeue_addr(gs: Path, entry: int) -> int | None:
    """Address of the per-event type load inside _PurpleEventCallback.

    Found structurally: the queue pop (`ldm r0, {r3, r8}`) within the function,
    then the first `ldr r1, [r8, #8]` after it. Both builds match.
    """
    vm, fo, _sz = text_seg(gs)
    d = gs.read_bytes()
    start = entry - vm + fo
    pop = d.find(DEQUEUE_INSN, start, start + 0x600)
    if pop < 0:
        return None
    i = d.find(TYPELOAD_INSN, pop, pop + 0x80)
    return None if i < 0 else i - fo + vm


EXEC_DIRS = ["Applications", "System/Library/CoreServices", "usr/sbin",
             "usr/bin", "usr/libexec", "sbin", "bin",
             "System/Library/PrivateFrameworks", "System/Library/Frameworks"]


def exec_fingerprints(mnt: Path) -> dict:
    """{first 0x40 bytes at 0x1000: name} for EVERY main executable.

    Daemons matter as much as apps here: a breakpoint at an executable-range
    address such as 0x6ae0 exists in every process, so a hit in mediaserverd or
    CommCenter is indistinguishable from a hit in SpringBoard until the process
    is named. A first run attributed five such hits to "?" for exactly this
    reason -- only apps and SpringBoard had been fingerprinted.
    """
    out = {}
    for d in EXEC_DIRS:
        base = mnt / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            m = macho_text(data)
            if not m or m[0] != MH_EXECUTE or m[1] != EXEC_BASE:
                continue
            out.setdefault(data[m[2]:m[2] + 0x40], p.name)
    return out


def gs_type(t8: int, sub: int) -> int:
    if t8 != TYPE_SENTINEL:
        return t8
    return SUBTYPE_MAP.get(sub, 0)


def tname(t: int) -> str:
    return TYPE_NAMES.get(t, f"type{t}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=["m68ap-10", "m68ap-114"], required=True)
    ap.add_argument("--logs", type=Path, default=None)
    ap.add_argument("--gdb-port", type=int, default=1234)
    ap.add_argument("--vnc-port", type=int, default=5930)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--baseline", type=float, default=15,
                    help="seconds of event traffic to record BEFORE the press, "
                         "so the press can be read against a steady state")
    ap.add_argument("--wait", type=float, default=60,
                    help="seconds of event traffic to record after the press")
    ap.add_argument("--max-hits", type=int, default=4000)
    ap.add_argument("--no-app", action="store_true",
                    help="press HOME from the home screen -- the control that "
                         "is known to reach SpringBoard's handler on 1.0")
    args = ap.parse_args()

    tag = "noapp" if args.no_app else "inapp"
    args.logs = args.logs or Path(f"/tmp/gsev-{args.board}-{tag}")
    args.logs.mkdir(parents=True, exist_ok=True)

    brk = _load("sbbreak", REPO / "scripts" / "springboard-button-breakpoint.py")
    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    app, icon = btn.BOARDS[args.board]

    mnt = Path(f"/tmp/gsev-root-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                    "-mountpoint", str(mnt), str(brk.ROOTS[args.board])],
                   capture_output=True)
    try:
        sb_bin = args.logs / "SpringBoard"
        sb_bin.write_bytes((mnt / SB).read_bytes())
        gs_bin = args.logs / "GraphicsServices"
        gs_bin.write_bytes((mnt / GS).read_bytes())
        print("building the guest library address map ...")
        libs = brk.library_map(mnt)
        print(f"  {len(libs)} images mapped")
        fps = exec_fingerprints(mnt)
        print(f"  {len(fps)} executables fingerprinted at {EXEC_BASE:#x}")
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    syms = {n: v for v, n, d in msym.symbols(gs_bin.read_bytes()) if d}
    cb = syms.get("_PurpleEventCallback")
    if not cb:
        print("FAIL: _PurpleEventCallback not in the symbol table")
        return 2
    deq = dequeue_addr(gs_bin, cb)
    if not deq:
        print("FAIL: could not derive the dequeue point from _PurpleEventCallback")
        return 2
    print(f"  _PurpleEventCallback = {cb:#x}, per-event dequeue = {deq:#x}")

    bps = {deq: "event"}
    # Expected instruction bytes at each SpringBoard breakpoint, so a hit can be
    # CHECKED rather than assumed: 0x6ae0 is a valid address in every process in
    # this OS, and the code that lives there differs per executable.
    sb_code = {}
    _sbseg = text_seg(sb_bin)
    _sbdata = sb_bin.read_bytes()
    for sel in (b"menuButtonDown:", b"menuButtonUp:"):
        imp = brk.resolve_imp(sb_bin, sel)
        if imp:
            a = imp & ~1
            bps[a] = f"SB.{sel.decode()}"
            o = a - _sbseg[0] + _sbseg[1]
            sb_code[a] = _sbdata[o:o + 16]
            print(f"  -[SpringBoard {sel.decode()}] IMP = {imp:#x}")

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/gsev-{os.getpid()}.sock"
    # IT_KEY_TRACE/IT_SYSIC_TRACE: the run's own control. "No GSEvents after the
    # press" is only interesting if the press actually reached the hardware
    # model, and a probe that cannot tell those two apart is the failure mode
    # this investigation has hit repeatedly.
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               IT_KEY_TRACE="1", IT_SYSIC_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}",
           "-gdb", f"tcp::{args.gdb_port}"]
    # start_new_session: the bundle's entry point is a SHELL that execs QEMU as
    # a CHILD, so terminating `proc` leaves qemu-system-arm alive holding the
    # gdb port and a ~220 MB NAND clone. The next run then dies with "Address
    # already in use" and the disk fills. Kill the whole group instead.
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = None
    records = []
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
            print("dismissing the first-launch modal ...")
            btn.tap(q, *dismiss, 0.12)
            time.sleep(args.settle)
        if args.no_app:
            print("NOT opening an app -- HOME will be pressed from SpringBoard")
        else:
            print("opening an app ...")
            btn.tap(q, *icon, 0.12)
            time.sleep(args.settle)

        print(f"attaching gdbstub on :{args.gdb_port} ...")
        g = brk.Gdb(args.gdb_port)
        for addr, name in bps.items():
            print(f"  breakpoint at {addr:#x} ({name}): {g.set_break(addr)}")

        # QEMU stops the VM when a gdb client attaches, so the guest is stopped
        # here. Track that explicitly: 'c' is only meaningful to a STOPPED
        # target, and sending it to a running one desyncs the stub and leaves
        # the guest halted forever -- which then reads as "no events". A run
        # lost to exactly that is why this is a variable and not an assumption.
        state = {"stopped": True}

        def go():
            if state["stopped"]:
                g.cont()
                state["stopped"] = False

        def drain(seconds, phase):
            """Run the guest, recording every breakpoint hit, for `seconds`."""
            end = time.time() + seconds
            go()
            while time.time() < end and len(records) < args.max_hits:
                stop = g.wait_stop(max(0.2, min(2.0, end - time.time())))
                if stop is None:
                    continue
                state["stopped"] = True
                rc_ = g.regs()
                if not rc_:
                    go()
                    continue
                w, _cpsr = rc_
                pc = w[15]
                which = bps.get(pc, bps.get(pc | 1, f"?{pc:#x}"))
                rec = {"t": round(time.time() - t0, 2), "phase": phase,
                       "at": which, "pc": pc}
                # WHICH PROCESS -- on every hit, not just event hits. The
                # SpringBoard handler breakpoints sit at 0x6ae0/0x6bd8, and
                # EVERY main executable in this OS links its __TEXT at 0x1000,
                # so those addresses exist in every process and a hit is not by
                # itself proof that SpringBoard ran.
                hdr = g.mem(EXEC_BASE, 0x40)
                rec["proc"] = fps.get(hdr, "?" if hdr else "??")
                if which == "event":
                    ev = w[8]
                    blk = g.mem(ev, 0x40)
                    if blk and len(blk) >= 0x3C:
                        t8, = struct.unpack_from("<I", blk, 8)
                        sub, = struct.unpack_from("<I", blk, 0x38)
                        rec.update(ev=ev, raw=t8, sub=sub,
                                   type=gs_type(t8, sub))
                else:
                    rec["lr"] = w[14]
                    rec["lr_in"] = brk.whose(w[14], libs)
                    # Is the code at this address actually SpringBoard's?
                    code = g.mem(pc, 16)
                    rec["real"] = bool(code and code == sb_code.get(pc))
                    # The handler's own early-return gate. Both builds open with
                    #   ldrsb r3, [self, #0x40]   (1.1.4: #0x44)
                    #   cmp r3, #0 ; movne/strbne/popne
                    # so a NON-ZERO byte there means the press is swallowed
                    # before any of the SBSyncController checks are reached.
                    rec["self"] = w[0]
                    blk = g.mem(w[0], 0x48)
                    if blk and len(blk) >= 0x45:
                        rec["ivar40"] = blk[0x40]
                        rec["ivar44"] = blk[0x44]
                records.append(rec)
                # Step off the breakpoint before continuing, or it re-traps.
                g.del_break(pc)
                g.step()
                g.set_break(pc)
                go()
            # Leave the guest running.
            go()
            # LIVENESS. A wedged RSP session leaves the vCPU stopped, and a
            # stopped guest produces no hits -- which reads exactly like "no
            # events", the most dangerous possible false negative here. QMP
            # reports the run state independently of the gdb connection.
            #
            # "paused" right after `c` is NOT proof of a wedge: the guest may
            # simply have hit a breakpoint again and be waiting for us. So
            # sample a few times and only complain if it never runs.
            for _ in range(6):
                if q.cmd("query-status").get("return", {}).get("running"):
                    return True
                time.sleep(0.3)
                if g.wait_stop(0.2) is not None:
                    state["stopped"] = True
                go()                    # it was a pending hit, not a wedge
            print(f"  !! guest NEVER RUNNING across the {phase} phase -- this "
                  f"phase's silence is MEANINGLESS")
            return False

        t0 = time.time()
        print(f"recording baseline for {args.baseline:.0f}s (no input) ...")
        live_before = drain(args.baseline, "before")
        n_before = len(records)
        print(f"  {n_before} hits, guest running: {live_before}")

        print("pressing HOME ...")
        btn.key(q, "h")
        press_t = round(time.time() - t0, 2)
        live_after = drain(args.wait, "after")
        print(f"  {len(records) - n_before} hits after the press, "
              f"guest running: {live_after}")

        (args.logs / "events.json").write_text(json.dumps(
            {"board": args.board, "no_app": args.no_app, "press_t": press_t,
             "live_before": live_before, "live_after": live_after,
             "records": records}, indent=1))

        # ---- report -------------------------------------------------------
        print(f"\n=== {args.board} {'--no-app' if args.no_app else 'in-app'} ===")
        for phase in ("before", "after"):
            c = Counter((r.get("proc", "-"), r.get("type"))
                        for r in records if r["phase"] == phase
                        and r["at"] == "event")
            print(f"\n  {phase} the press: {sum(c.values())} GSEvents")
            for (p, t), n in sorted(c.items(), key=lambda kv: -kv[1]):
                print(f"    {n:5d}  {p:<22} {tname(t) if t is not None else '?'}")
        hits = [r for r in records if r["at"].startswith("SB.")]
        real = [h for h in hits if h.get("real")]
        print(f"\n  SpringBoard handler hits: {len(real)} REAL "
              f"({len(hits) - len(real)} rejected -- same address, other process)")
        for h in hits:
            gate = ""
            if "ivar40" in h:
                gate = (f"  self={h['self']:#x} +0x40={h['ivar40']:#04x} "
                        f"+0x44={h['ivar44']:#04x}")
            print(f"    {'REAL ' if h.get('real') else 'bogus'} t={h['t']:<7} "
                  f"[{h['phase']}] in {h.get('proc')}  {h['at']}  "
                  f"lr={h.get('lr', 0):#x} {h.get('lr_in', '')}{gate}")
        new = {(r.get("proc"), r.get("type")) for r in records
               if r["phase"] == "after" and r["at"] == "event"} - \
              {(r.get("proc"), r.get("type")) for r in records
               if r["phase"] == "before" and r["at"] == "event"}
        print(f"\n  (process, type) pairs seen ONLY after the press: "
              f"{sorted(str(x) for x in new)}")
        # The control: did the press reach the hardware model at all?
        log = logp.read_bytes().decode("utf8", "replace")
        keys = [l for l in log.splitlines() if "[KEYTRACE]" in l]
        print(f"\n  [KEYTRACE] lines (the press reaching the model): {len(keys)}")
        for l in keys[-4:]:
            print(f"    {l.strip()}")
        if not keys:
            print("    !! the HOME press never reached ipod_touch_key_event -- "
                  "this run says NOTHING about routing")
        print(f"\n  raw records: {args.logs / 'events.json'}")
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
