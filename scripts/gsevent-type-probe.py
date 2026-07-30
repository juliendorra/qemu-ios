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

`--presses N` presses repeatedly and counts the menu DOWN (type1000) and UP
(type1001) events delivered per press. Measured 2026-07-29, 10 presses each:
1.0 home screen 10/10, 1.1.4 in-app 10/10, but **1.0 IN-APP 1/1** -- press 1 is
delivered and handled end to end, and then NO process receives another GSEvent
at all. See NEXT_SESSION_HANDOFF.md.

Usage:
  scripts/gsevent-type-probe.py --board m68ap-10 --presses 10
  scripts/gsevent-type-probe.py --board m68ap-10 --no-app --presses 10
  scripts/gsevent-type-probe.py --board m68ap-10 --no-gdb --presses 10  # control
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
    1000: "MENU-BUTTON-DOWN", 1001: "MENU-BUTTON-UP",
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
    ap.add_argument("--presses", type=int, default=1,
                    help="how many HOME presses to make, each with its own "
                         "recording window -- the menu DOWN/UP delivery count")
    ap.add_argument("--press-interval", type=float, default=6,
                    help="seconds of recording after each press")
    ap.add_argument("--break-sym", action="append", default=[],
                    help="break on LIB:SYMBOL, e.g. LayerKit:_LKBackingStoreSwap. "
                         "Repeatable. Library addresses are unambiguous across "
                         "processes, unlike an executable's IMPs.")
    ap.add_argument("--break-addr", action="append", default=[],
                    help="break on a raw address, as NAME=0xADDR. For return "
                         "sites and mid-function points that have no symbol -- "
                         "e.g. SwapWait's return-from-kernel instruction, so an "
                         "entry with no matching return names a thread wedged "
                         "in the kernel.")
    ap.add_argument("--deref", action="append", default=[],
                    help="on hits at a --break-sym breakpoint, also read guest "
                         "memory: 'SYMSUFFIX=r0+8,r0+0xa0'. The suffix is "
                         "matched against the breakpoint name, each rN+OFF is "
                         "a 4-byte read at that register plus offset. This is "
                         "what turns 'SwapBegin was entered' into 'SwapBegin "
                         "was entered on the display whose fb field is X'.")
    ap.add_argument("--watch-port", action="store_true",
                    help="also break on the GraphicsServices event-PORT calls "
                         "(_GSGetPurpleSystemEventPort, _ResetEventPortSet, "
                         "_GSRegisterApplicationPort, _GSSendSystemEvent, "
                         "_GSSendEvent)")
    ap.add_argument("--taps", action="store_true",
                    help="tap the screen after every press and record that too "
                         "-- the control that separates 'the button path died' "
                         "from 'all event delivery died'")
    ap.add_argument("--tap-at", type=int, nargs=2, default=(160, 240),
                    metavar=("X", "Y"))
    ap.add_argument("--no-gdb", action="store_true",
                    help="CONTROL: skip the gdbstub entirely and only count "
                         "[KEYTRACE] edges. Stopping the vCPU at a breakpoint "
                         "can itself lose a key edge (virtual time does not "
                         "advance while the guest is stopped, and this session "
                         "runs under -icount), so a dropped release must be "
                         "reproduced WITHOUT gdb before it means anything.")
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

    groot = _load("guestroot", REPO / "scripts" / "guest_root.py")
    try:
        mnt, mnt_mine = groot.attach(brk.ROOTS[args.board], "gsev-root")
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 2
    if not mnt_mine:
        print(f"  reusing existing mount at {mnt}")
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
        mnt_saved = str(mnt) if args.break_sym else None
    finally:
        if not args.break_sym:
            groot.detach(mnt, mnt_mine)

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

    # `event` is the per-event dequeue; `callback` is the CFMachPort callback
    # ENTRY. Both matter: if the callback still fires after the wedge but no
    # event is dequeued, the mach message is arriving and the QUEUE is empty; if
    # the callback stops firing too, nothing is being posted to the port at all.
    bps = {deq: "event", cb: "callback"}
    # The event PORT itself. After the first in-app press on 1.0, the kernel
    # still ACKs the button IRQ but _PurpleEventCallback never fires again in
    # ANY process -- so the mach message stops arriving. These are the calls
    # that could make that happen, and they are ordinary GraphicsServices
    # addresses (>= 0x30000000), so unlike the SpringBoard IMPs they are not
    # ambiguous across processes.
    # Arbitrary library symbols, e.g. LayerKit's compositing entry points. These
    # are shared-library addresses (>= 0x30000000), so unlike an executable's
    # IMPs they are unambiguous across processes.
    for spec in args.break_sym:
        lib, _, sym = spec.partition(":")
        src = None
        for root in ("System/Library/Frameworks", "System/Library/PrivateFrameworks",
                     "usr/lib"):
            cand = list((Path(mnt_saved) / root).rglob(lib)) if mnt_saved else []
            for c in cand:
                if c.is_file() and not c.is_symlink():
                    src = c
                    break
            if src:
                break
        if not src:
            print(f"  !! {lib} not found for {spec}")
            continue
        libsyms = {n: v for v, n, d in msym.symbols(src.read_bytes()) if d}
        if sym in libsyms:
            bps[libsyms[sym] & ~1] = f"{lib}:{sym}"
            print(f"  watching {lib}:{sym} at {libsyms[sym]:#x}")
        else:
            print(f"  !! {sym} not in {lib}")
    if mnt_saved:
        groot.detach(Path(mnt_saved), mnt_mine)

    for spec in args.break_addr:
        name, _, addr = spec.partition("=")
        bps[int(addr, 0) & ~1] = name
        print(f"  watching {name} at {addr}")

    # --deref parsing: breakpoint-name suffix -> [(reg#, offset, label)]
    derefs = []
    for spec in args.deref:
        suffix, _, exprs = spec.partition("=")
        lst = []
        for e in exprs.split(","):
            e = e.strip()
            reg, _, off = e.partition("+")
            lst.append((int(reg.lstrip("r")), int(off, 0) if off else 0, e))
        derefs.append((suffix, lst))

    if args.watch_port:
        for n in ("_GSGetPurpleSystemEventPort", "_ResetEventPortSet",
                  "_GSRegisterApplicationPort", "_GSSendSystemEvent",
                  "_GSSendEvent"):
            if n in syms:
                bps[syms[n]] = n
                print(f"  watching {n} at {syms[n]:#x}")
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

        if args.no_gdb:
            print(f"CONTROL run: no gdbstub. {args.presses} presses ...")
            for i in range(args.presses):
                btn.key(q, "h")
                time.sleep(args.press_interval)
            log = logp.read_bytes().decode("utf8", "replace")
            keys = [l for l in log.splitlines() if "[KEYTRACE]" in l]
            n_dn = sum(1 for l in keys if "keycode=35 " in l)
            n_upk = sum(1 for l in keys if "keycode=163 " in l)
            print(f"\n=== {args.board} "
                  f"{'--no-app' if args.no_app else 'in-app'}, NO GDB ===")
            print(f"  the MODEL saw: keycode=35 (down) x{n_dn}, "
                  f"keycode=163 (up) x{n_upk}, for {args.presses} presses")
            print("  => edges are dropped WITHOUT gdb too" if
                  (n_dn != args.presses or n_upk != args.presses) else
                  "  => every edge arrives; the losses seen under gdb are the "
                  "INSTRUMENT")
            return 0

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

        def service(phase):
            """Record the stop we are sitting on, then step off and resume."""
            rc_ = g.regs()
            if not rc_:
                go()
                return
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
                for suffix, lst in derefs:
                    if not which.endswith(suffix):
                        continue
                    mem = {}
                    for regn, off, label in lst:
                        blk = g.mem((w[regn] + off) & 0xffffffff, 4)
                        mem[label] = (struct.unpack("<I", blk)[0]
                                      if blk and len(blk) == 4 else None)
                    rec["mem"] = mem
                    rec["r0"] = w[0]
                    rec["r1"] = w[1]
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

        def drain(seconds, phase):
            """Run the guest, recording every breakpoint hit, for `seconds`."""
            end = time.time() + seconds
            go()
            while time.time() < end and len(records) < args.max_hits:
                if g.wait_stop(max(0.2, min(2.0, end - time.time()))) is None:
                    continue
                state["stopped"] = True
                service(phase)
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

        def send_ev(events, phase, tries=40):
            """Send QMP input events, and make sure they are ACCEPTED.

            `qmp_input_send_event` REFUSES everything with "VM not running"
            while the VM is stopped (ui/input.c), and QMP.cmd does not look at
            the reply -- so a breakpoint landing mid-gesture silently swallows
            it. Measured: 10 key-downs but only 5 key-ups reached the model
            under gdb, against 10/10 with --no-gdb, which manufactured a fake
            "the DOWN event is rarely delivered" result. Retry, servicing (and
            RECORDING) whatever breakpoint is in the way.
            """
            for n in range(tries):
                go()
                r = q.cmd("input-send-event", {"events": events})
                if "error" not in r:
                    return n
                if g.wait_stop(0.5) is not None:
                    state["stopped"] = True
                    service(phase)
                else:
                    state["stopped"] = True
                    go()
                time.sleep(0.02)
            print(f"  !! input event NEVER accepted -- run is INVALID")
            return -1

        def hold(seconds, phase):
            """Let the guest run for `seconds`, still recording every stop."""
            end = time.time() + seconds
            while time.time() < end:
                if g.wait_stop(end - time.time()) is not None:
                    state["stopped"] = True
                    service(phase)

        def key_edge(down, phase):
            return send_ev([{"type": "key", "data": {"down": down, "key": {
                "type": "qcode", "data": "h"}}}], phase)

        def press(phase, hold_s=0.15):
            a = key_edge(True, phase)
            hold(hold_s, phase)
            b = key_edge(False, phase)
            return a, b

        def tap(px, py, phase, hold_s=0.15):
            """A screen tap, sent with the same acceptance checking.

            This is the control that separates "the BUTTON path died" from "ALL
            event delivery died": after the wedge, a tap should still produce
            GSEvents for the foreground app if only the button path is broken.
            """
            send_ev([{"type": "abs", "data": {"axis": "x",
                                              "value": int(px / 320 * 32768)}},
                     {"type": "abs", "data": {"axis": "y",
                                              "value": int(py / 480 * 32768)}}],
                    phase)
            a = send_ev([{"type": "btn", "data": {"down": True,
                                                  "button": "left"}}], phase)
            hold(hold_s, phase)
            b = send_ev([{"type": "btn", "data": {"down": False,
                                                  "button": "left"}}], phase)
            return a, b

        press_times = []
        live_after = True
        retries = []
        for i in range(args.presses):
            print(f"pressing HOME ({i + 1}/{args.presses}) ...")
            retries.append(press(f"p{i + 1}"))
            press_times.append(round(time.time() - t0, 2))
            live_after = drain(args.press_interval, f"p{i + 1}") and live_after
            if args.taps:
                print(f"  tapping the screen (t{i + 1}) ...")
                tap(args.tap_at[0], args.tap_at[1], f"t{i + 1}")
                live_after = drain(args.press_interval, f"t{i + 1}") and live_after
        n_pressed = len(records) - n_before
        if args.wait:
            live_after = drain(args.wait, "after") and live_after
        print(f"  {len(records) - n_before} hits after the presses "
              f"({n_pressed} within the press windows), "
              f"guest running: {live_after}")

        (args.logs / "events.json").write_text(json.dumps(
            {"board": args.board, "no_app": args.no_app,
             "press_times": press_times,
             "live_before": live_before, "live_after": live_after,
             "records": records}, indent=1))

        # ---- the menu-button delivery count -------------------------------
        # THE question this mode exists for: SpringBoard must receive a DOWN
        # (type1000) and an UP (type1001) for each press. `menuButtonUp:` gates
        # on `_menuButtonTimer`, which only `menuButtonDown:` sets -- so a
        # missing DOWN silently swallows the press.
        press_phases = []
        for i in range(args.presses):
            press_phases.append(f"p{i + 1}")
            if args.taps:
                press_phases.append(f"t{i + 1}")
        sb_ev = [r for r in records if r["at"] == "event"
                 and r.get("proc") == "SpringBoard"]
        print(f"\n=== {args.board} {'--no-app' if args.no_app else 'in-app'}: "
              f"{args.presses} presses ===")
        print("\n  phase | DOWN(1000) | UP(1001) | other types (any process)")
        n_down = n_up = 0
        all_ev = [r for r in records if r["at"] == "event"]
        for ph in press_phases:
            evs = [r for r in sb_ev if r["phase"] == ph]
            d = sum(1 for r in evs if r.get("type") == 1000)
            u = sum(1 for r in evs if r.get("type") == 1001)
            other = sorted({(r.get("proc"), r.get("type")) for r in all_ev
                            if r["phase"] == ph} - {("SpringBoard", 1000),
                                                    ("SpringBoard", 1001)})
            n_down += d
            n_up += u
            print(f"   {ph:>4} |     {d:^6} |   {u:^4} | {other}")
        bad = [r for r in retries if -1 in r]
        print(f"\n  key edges: retries per press (down, up) = {retries}")
        if bad:
            print(f"  !! {len(bad)} press(es) had an edge that was never "
                  f"accepted -- those rows are INVALID")
        print(f"\n  TOTAL over {args.presses} presses:  "
              f"DOWN(type1000) = {n_down}   UP(type1001) = {n_up}")
        if n_up and not n_down:
            print("  => the DOWN event is NEVER delivered: _menuButtonTimer can "
                  "never be set,\n     so every UP is swallowed at the "
                  "`_menuButtonTimer == nil` gate.")

        for phase in ("before",) + tuple(press_phases) + ("after",):
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
        libhits = [r for r in records if ":" in r["at"]]
        if libhits:
            print(f"\n  library breakpoint hits: {len(libhits)} "
                  f"(full list in events.json)")
            c = Counter((r["phase"], r["at"], r.get("proc")) for r in libhits)
            for (ph, at, p), n in sorted(c.items()):
                print(f"    {n:5d}  [{ph}] {at} in {p}")
            withmem = [r for r in libhits if "mem" in r]
            for r in withmem[:120]:
                ms = " ".join(f"{k}={v:#x}" if v is not None else f"{k}=?"
                              for k, v in r["mem"].items())
                print(f"      t={r['t']:<8} [{r['phase']}] {r['at']} "
                      f"in {r.get('proc')} r0={r.get('r0', 0):#x} "
                      f"r1={r.get('r1', 0):#x} lr={r.get('lr', 0):#x} {ms}")
            if len(withmem) > 120:
                print(f"      ... {len(withmem) - 120} more in events.json")
        new = {(r.get("proc"), r.get("type")) for r in records
               if r["phase"] != "before" and r["at"] == "event"} - \
              {(r.get("proc"), r.get("type")) for r in records
               if r["phase"] == "before" and r["at"] == "event"}
        print(f"\n  (process, type) pairs seen ONLY after a press: "
              f"{sorted(str(x) for x in new)}")
        # The control: did the press reach the hardware model at all?
        log = logp.read_bytes().decode("utf8", "replace")
        keys = [l for l in log.splitlines() if "[KEYTRACE]" in l]
        n_dn = sum(1 for l in keys if "keycode=35 " in l)
        n_upk = sum(1 for l in keys if "keycode=163 " in l)
        print(f"\n  [KEYTRACE] the MODEL saw: keycode=35 (down) x{n_dn}, "
              f"keycode=163 (up) x{n_upk}, for {args.presses} presses")
        if n_dn != args.presses or n_upk != args.presses:
            print("  !! the model itself dropped an edge -- the guest cannot be "
                  "blamed for what\n     ipod_touch_key_event never saw")
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
