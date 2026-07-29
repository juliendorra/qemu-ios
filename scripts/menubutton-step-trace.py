#!/usr/bin/env python3
"""Single-step -[SpringBoard menuButtonDown:/Up:] and record the branches taken.

Established 2026-07-29 (NEXT_SESSION_HANDOFF.md): on iPhone OS 1.0 with an app
frontmost the button GSEvent IS delivered to SpringBoard and `menuButtonUp:`
REALLY RUNS, with its early-return ivar clear -- the same as 1.1.4, which works.
So the divergence is inside the handler. This traces it.

Both builds have the same shape:

    ldrsb r3, [self, #0x40]        ; 1.1.4: #0x44   -- swallow-once flag
    [[SBSyncController sharedInstance] isRestoring]        -> return
    [[SBSyncController sharedInstance] isResetting]        -> return
    [[SBSyncController sharedInstance] isSoftwareUpdating] -> return
    [self shouldRunFieldTestScript] -> field-test path
    ldr r3, [self, #0x10] ; cmp r3, #0 ; beq RETURN       <-- the interesting gate
    [self _setMenuButtonTimer:] ; [[self+0xc] clickedMenuButton]

RESULT: everything above the `_menuButtonTimer` test is identical on both builds
and takes the identical decision; that one gate is the whole difference, and it
is NOT stable between runs. When it passes on 1.0 the click IS dispatched to
`-[SBUIController clickedMenuButton]` and the app still does not close, so that
method is the next layer (`--trace-sel clickedMenuButton`). See
NEXT_SESSION_HANDOFF.md.

Two things make this safe to do over the gdbstub:

  * **Step OVER calls.** A `bl` into objc_msgSend would cost thousands of steps
    and wander through other libraries. On a call, put a temporary breakpoint at
    pc+4 and continue instead.
  * **Validate every stop.** Every main executable links __TEXT at 0x1000, so
    these addresses exist in EVERY process and a breakpoint there fires in all
    of them (measured: 440 bogus hits to 1 real one on 1.1.4). Each stop is
    accepted only if the Mach-O header at 0x1000 fingerprints as SpringBoard AND
    the code at pc matches SpringBoard's own bytes.

Usage:
  scripts/menubutton-step-trace.py --board m68ap-10
  scripts/menubutton-step-trace.py --board m68ap-114
  scripts/menubutton-step-trace.py --board m68ap-10 --no-app
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

SB = "System/Library/CoreServices/SpringBoard.app/SpringBoard"
FUNC_SPAN = 0x400          # generous upper bound on either handler's size


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CALL = re.compile(r"^blx?\s")
# The function ends at its first UNCONDITIONAL `pop {..., pc}`. Conditional
# forms (popne/popeq) are the early returns and must not terminate the scan.
EPILOGUE = re.compile(r"^pop\s+\{[^}]*\bpc\b")


def annotated(binary: Path, start: int, span: int, next_imp: int | None = None):
    """({addr: 'instruction ; [receiver selector]'}, end_addr) for a function.

    `next_imp` -- the address of the following method, from the ObjC metadata --
    is the RELIABLE end. Scanning for the first unconditional `pop {..,pc}` is
    only a fallback: 1.1.4's `menuButtonUp:` has a shared epilogue in the MIDDLE
    of the function (every early return branches to it), so that heuristic cuts
    it at 0x7bd4 when it really runs past 0x7c14.
    """
    dis = _load("objcdis", REPO / "scripts" / "objc-method-disasm.py")
    img = dis.Image(binary)
    limit = min(start + span, next_imp) if next_imp else start + span
    rows = dis.disasm(img, start, limit)
    named, out, end = {}, {}, start
    for addr, _raw, text in rows:
        note = ""
        m = dis.LDR_PC.search(text)
        if m:
            reg, imm = m.group(1), int(m.group(2), 0)
            lit = img.word(addr + 8 + imm)
            nm = (img.deref_name(lit) or img.cstr(lit)) if lit else None
            if nm:
                named[reg] = nm
        mm = re.match(r"ldr\s+(\w+), \[(\w+)\]$", text)
        if mm and mm.group(2) in named:
            named[mm.group(1)] = named[mm.group(2)]
        if re.match(r"mov\s+r0, r4$", text):
            named["r0"] = "self"
        if "_objc_msgSend" in text:
            note = f"   ; [{named.get('r0', '?')} {named.get('r1', '?')}]"
            text = "bl _objc_msgSend"
        out[addr] = text + note
        end = addr + 4
        if next_imp is None and EPILOGUE.match(text):
            break
    return out, end


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=["m68ap-10", "m68ap-114"], required=True)
    ap.add_argument("--logs", type=Path, default=None)
    ap.add_argument("--gdb-port", type=int, default=1234)
    ap.add_argument("--vnc-port", type=int, default=5930)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--wait", type=float, default=90)
    ap.add_argument("--max-steps", type=int, default=400)
    ap.add_argument("--presettle", type=float, default=10,
                    help="seconds to let the guest run after the gdb attach, "
                         "before the first press")
    ap.add_argument("--presses", type=int, default=3)
    ap.add_argument("--repress", type=float, default=25,
                    help="seconds to wait for a hit before pressing again")
    ap.add_argument("--window", type=float, default=3.0,
                    help="seconds the handler breakpoints stay armed after a "
                         "button GSEvent is seen in SpringBoard")
    ap.add_argument("--trace-sel", action="append", default=[],
                    help="additional method to trace, by selector (e.g. "
                         "clickedMenuButton). Armed in the same window as "
                         "menuButtonUp:, so a call FROM it is caught.")
    ap.add_argument("--only-up", action="store_true",
                    help="trace ONLY menuButtonUp:. Tracing menuButtonDown: "
                         "single-steps the guest between the key-down and the "
                         "key-up, which inflates the guest-visible hold and can "
                         "fire the _menuButtonTimer that menuButtonUp: gates "
                         "on -- i.e. the instrument changes the branch it is "
                         "measuring. Use this for any down/up comparison.")
    ap.add_argument("--no-app", action="store_true")
    args = ap.parse_args()

    tag = "noapp" if args.no_app else "inapp"
    args.logs = args.logs or Path(f"/tmp/mbtrace-{args.board}-{tag}")
    args.logs.mkdir(parents=True, exist_ok=True)

    brk = _load("sbbreak", REPO / "scripts" / "springboard-button-breakpoint.py")
    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    gsp = _load("gsprobe", REPO / "scripts" / "gsevent-type-probe.py")
    app, icon = btn.BOARDS[args.board]

    mnt = Path(f"/tmp/mbtrace-root-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                    "-mountpoint", str(mnt), str(brk.ROOTS[args.board])],
                   capture_output=True)
    try:
        sb_bin = args.logs / "SpringBoard"
        sb_bin.write_bytes((mnt / SB).read_bytes())
        gs_bin = args.logs / "GraphicsServices"
        gs_bin.write_bytes((mnt / gsp.GS).read_bytes())
        fps = gsp.exec_fingerprints(mnt)
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    # The trigger. Leaving the handler breakpoints armed continuously does not
    # work: they sit at executable-range addresses that OTHER processes also
    # execute, and one run collected 628104 stops -- all of them rejected, and
    # collectively enough to starve the guest so the real press never ran.
    # So arm them only inside a short window, opened by the button GSEvent
    # itself, which is observed at a GraphicsServices address instead.
    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    gsyms = {n: v for v, n, d in msym.symbols(gs_bin.read_bytes()) if d}
    deq = gsp.dequeue_addr(gs_bin, gsyms["_PurpleEventCallback"])
    print(f"  GSEvent dequeue = {deq:#x}  (arms the handler breakpoints)")
    BUTTON_TYPES = {1000: "menuButtonDown:", 1001: "menuButtonUp:"}

    seg = gsp.text_seg(sb_bin)
    data = sb_bin.read_bytes()

    def code_at(va, n=16):
        return data[va - seg[0] + seg[1]: va - seg[0] + seg[1] + n]

    xref = _load("objcxref", REPO / "scripts" / "objc-xref.py")
    dis = _load("objcdis2", REPO / "scripts" / "objc-method-disasm.py")
    all_imps = sorted({i for i, _n in xref.methods(dis.Image(sb_bin))})

    def next_imp(a):
        after = [i for i in all_imps if i > a]
        return after[0] if after else None

    named_methods = xref.methods(dis.Image(sb_bin))

    def add(a, label):
        text, end = annotated(sb_bin, a, FUNC_SPAN, next_imp(a))
        funcs[a] = {"name": label, "text": text, "end": end}
        print(f"  {label} = {a:#x}..{end:#x} ({len(text)} instructions)")

    funcs = {}
    for sel in (b"menuButtonDown:", b"menuButtonUp:"):
        imp = brk.resolve_imp(sb_bin, sel)
        if imp:
            add(imp & ~1, sel.decode())
    # Extra selectors come from the ObjC METHOD TABLE, not resolve_imp: the
    # latter scans for a pointer to the selector cstring and takes the next
    # word as the IMP, which mis-resolved `clickedMenuButton` to 0x732f0
    # (inside the string section) instead of 0xd794.
    for want in args.trace_sel:
        hits = [(i, n) for i, n in named_methods
                if n.endswith(f" {want}]") or n.endswith(f"{want}]")]
        if not hits:
            print(f"  !! selector {want} not found in the method table")
        for i, n in hits:
            add(i, n)
    if not funcs:
        print("FAIL: could not resolve the handlers")
        return 2

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/mbtrace-{os.getpid()}.sock"
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               IT_KEY_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}",
           "-gdb", f"tcp::{args.gdb_port}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = None
    traces = []
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
        if not args.no_app:
            print("opening an app ...")
            btn.tap(q, *icon, 0.12)
            time.sleep(args.settle)

        print(f"attaching gdbstub on :{args.gdb_port} ...")
        g = brk.Gdb(args.gdb_port)
        g.set_break(deq)                 # the trigger; handlers stay disarmed
        bps_set = {deq}
        armed = set()

        def arm(addr):
            if addr not in armed:
                g.set_break(addr)
                armed.add(addr)
                bps_set.add(addr)

        def disarm_all():
            for a in list(armed):
                g.del_break(a)
                armed.discard(a)
                bps_set.discard(a)

        def resume(pc):
            """Continue, STEPPING OFF the breakpoint first.

            QEMU re-traps immediately if you `c` while still sitting on a
            breakpoint address, so a plain cont() here is an infinite loop that
            makes no guest progress -- two runs were lost to it, reporting
            430068 and 628104 "stops" that were all the same one.
            """
            if pc in bps_set:
                g.del_break(pc)
                g.step()
                g.set_break(pc)
            g.cont()

        def is_springboard():
            hdr = g.mem(gsp.EXEC_BASE, 0x40)
            return fps.get(hdr) == "SpringBoard"

        def trace(entry):
            """Single-step one handler, stepping OVER calls. -> [(pc, text)]"""
            fn = funcs[entry]
            lo, hi = entry, fn["end"]
            seq, pc = [], entry
            for _ in range(args.max_steps):
                text = fn["text"].get(pc, "?")
                seq.append((pc, text))
                if CALL.match(text):
                    # Step over the call: temporary breakpoint at the return.
                    ret = pc + 4
                    g.set_break(ret)
                    bps_set.add(ret)
                    g.cont()
                    ok = False
                    for _try in range(200):
                        if g.wait_stop(5) is None:
                            break
                        w = g.regs()
                        if w and w[0][15] == ret and is_springboard():
                            ok = True
                            break
                        # somebody else's copy of this address, or the trigger
                        # breakpoint: step off it and keep going.
                        resume(w[0][15] if w else ret)
                    g.del_break(ret)
                    bps_set.discard(ret)
                    if not ok:
                        seq.append((ret, "!! lost the thread stepping over"))
                        return seq
                    pc = ret
                else:
                    g.step()
                    w = g.regs()
                    if not w:
                        seq.append((0, "!! no registers"))
                        return seq
                    pc = w[0][15]
                if not (lo <= pc < hi):
                    seq.append((pc, f"<- RETURNED (pc left {lo:#x}..{hi:#x})"))
                    return seq
            seq.append((pc, "!! step budget exhausted"))
            return seq

        # Attaching gdb STOPS the VM, and under -icount the guest needs a moment
        # of real running before it will service input again: a first version
        # pressed 1 s after `c` and got the key-down only, with no handler ever
        # entered. Let it run first, and re-press if nothing arrives.
        g.cont()
        print(f"letting the guest run for {args.presettle:.0f}s after attach ...")
        time.sleep(args.presettle)

        stops = {"total": 0, "events": 0, "armed": 0, "rejected": 0}
        press = 0
        window_until = 0.0
        deadline = time.time() + args.wait
        next_press = time.time()
        done = set()
        while time.time() < deadline and len(traces) < 2 + len(args.trace_sel):
            if time.time() >= next_press and press < args.presses:
                press += 1
                print(f"pressing HOME ({press}/{args.presses}) ...")
                btn.key(q, "h")
                next_press = time.time() + args.repress
            if armed and time.time() > window_until:
                disarm_all()            # window closed; stop paying for rejects
            if g.wait_stop(min(3, max(0.2, deadline - time.time()))) is None:
                continue
            stops["total"] += 1
            w = g.regs()
            if not w:
                g.cont()
                continue
            pc = w[0][15]

            if pc == deq:
                # A GSEvent was dequeued. Arm the matching handler only if this
                # is SpringBoard receiving a BUTTON event (type 1000/1001).
                stops["events"] += 1
                blk = g.mem(w[0][8], 0x40)
                ty = None
                if blk and len(blk) >= 0x3C:
                    t8 = int.from_bytes(blk[8:12], "little")
                    sub = int.from_bytes(blk[0x38:0x3C], "little")
                    ty = gsp.gs_type(t8, sub)
                if ty in BUTTON_TYPES and is_springboard():
                    want = BUTTON_TYPES[ty]
                    if args.only_up and want != "menuButtonUp:":
                        resume(pc)
                        continue
                    for a, f in funcs.items():
                        if (f["name"] == want
                                or any(f["name"].endswith(f" {x}]")
                                       for x in args.trace_sel)) \
                                and a not in done:
                            arm(a)
                            stops["armed"] += 1
                            window_until = time.time() + args.window
                            print(f"  GSEvent type{ty} in SpringBoard -> armed "
                                  f"{want}")
                resume(pc)
                continue

            if pc not in funcs:
                resume(pc)
                continue
            # Validate: right process AND right code, or it is another
            # executable that merely has an address here.
            if not is_springboard() or g.mem(pc, 16) != code_at(pc):
                stops["rejected"] += 1
                if stops["rejected"] % 2000 == 0:
                    disarm_all()        # this window is a lost cause
                resume(pc)
                continue
            self_ = w[0][0]
            iv = g.mem(self_, 0x50) or b""
            def _w(off):
                return (int.from_bytes(iv[off:off + 4], "little")
                        if len(iv) >= off + 4 else None)
            print(f"\n### REAL hit: -[SpringBoard {funcs[pc]['name']}] "
                  f"self={self_:#x}")
            print(f"    _uiController(+0xc)={_w(0xc):#x} "
                  f"_menuButtonTimer(+0x10)={_w(0x10):#x} "
                  f"_screenShooting(+0x{0x44 if args.board == 'm68ap-114' else 0x40:x})"
                  f"={iv[0x44 if args.board == 'm68ap-114' else 0x40] if len(iv) > 0x44 else -1}")
            seq = trace(pc)
            traces.append({"handler": funcs[pc]["name"], "self": self_,
                           "uiController": _w(0xc),
                           "menuButtonTimer": _w(0x10),
                           "seq": [[a, t] for a, t in seq]})
            for a, t in seq:
                print(f"    {a:08x}  {t}")
            done.add(pc)
            disarm_all()          # one trace per handler is enough
            g.cont()              # trace() already left the pc past the entry

        (args.logs / "trace.json").write_text(json.dumps(
            {"board": args.board, "no_app": args.no_app, "traces": traces},
            indent=1))
        keys = [l for l in logp.read_bytes().decode("utf8", "replace").splitlines()
                if "[KEYTRACE]" in l]
        print(f"\n  [KEYTRACE] lines: {len(keys)}  (the press reaching the model)")
        print(f"  traces captured: {len(traces)}  -> {args.logs / 'trace.json'}")
        print(f"  gdb stops: {stops['total']} total, {stops['events']} GSEvent "
              f"dequeues, {stops['armed']} arming windows, {stops['rejected']} "
              f"handler-address hits in OTHER processes")
        if not keys:
            print("  !! the press never reached the model -- run is INVALID")
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
