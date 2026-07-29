#!/usr/bin/env python3
"""Poll the GSEvent queue head without perturbing the guest.

The in-app HOME wedge on iPhone OS 1.0 is ASYNCHRONOUS: the first press is
delivered and handled completely -- both SpringBoard handlers hit, the display
stack is unwound, the stack unwinds into the run loop -- and only afterwards does
GSEvent delivery die for every process. Seconds pass. No single-stepper can
reach that: 15000 steps is microseconds of guest time, and anything that stops
the vCPU also stops QMP input from being accepted at all ("VM not running").

So watch memory instead. `_PurpleEventCallback` drains a singly-linked queue
whose HEAD is a global in GraphicsServices' `__bss`:

    ldr r2, [pc, #N] ; add r2, pc, r2      -- the head global
    ldr r0, [r3]                           -- *head
    cmp r0, #0 ; beq <unlock and return>   -- empty
    ldm r0, {r3, r8}                       -- r3 = next, r8 = the GSEvent
    str r3, [r2]                           -- *head = next

so each node is {next, event}, and the event's type sits at event+8 (or, when
that reads 3001, at event+0x38 -- see gsevent-type-probe.py). The address is
derived from the binary here: 1.0 0x38988a0c, 1.1.4 0x38ab2d00.

QMP `memsave` reads GUEST VIRTUAL memory through the current CPU mapping and
does NOT stop the vCPU. GraphicsServices' __bss is per-process at a fixed VA
(no ASLR), so a sample is only meaningful together with the identity of the
process currently mapped -- which is why every sample also reads the Mach-O
header at 0x1000 and fingerprints it. Samples where that is not SpringBoard are
recorded but not counted as SpringBoard's queue.

What the outcomes mean -- and the LIMIT, measured:

  * head goes non-zero and STAYS -> events are enqueued and nobody drains them;
    the run loop / callback side died, and the queued event's type says which
    event is stuck. This the poller CAN see.
  * head always 0 -> only "no PERSISTENT backlog". It does NOT mean nothing is
    enqueued: with `--burst 40` on a WORKING configuration the poller caught a
    queued event 0 times in 240 samples, because the queue drains faster than a
    QMP round-trip. Always run `--burst` before leaning on an empty result.

What this run DID settle, from the same samples: after the first in-app press on
1.0 the QEMU process goes from 0.07 host cores to 0.97 and the only process ever
mapped is SpringBoard -- it SPINS, starving everything else. That is why nothing
else is scheduled, why no further GSEvent is ever sent, and why touch dies with
the button. Reported per phase at the end of every run.

Usage:
  scripts/gsqueue-poll.py --board m68ap-10
  scripts/gsqueue-poll.py --board m68ap-114        # the control that must stay drained
  scripts/gsqueue-poll.py --board m68ap-10 --no-app
"""
from __future__ import annotations

import argparse
import json
import os
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


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def queue_head_addr(gs_bin: Path, entry: int, gsp, dis) -> int | None:
    """The queue-head global, from _PurpleEventCallback's own prologue.

    Structural, not hardcoded: the first `ldr rN,[pc,#imm]` in the function
    whose pc-relative value is completed by an `add rN, pc, rN` gives a __bss
    address, and the head is the one the drain loop dereferences.
    """
    img = dis.Image(gs_bin)
    rows = dis.disasm(img, entry, entry + 0x80)
    lits = {}
    for addr, _raw, text in rows:
        m = dis.LDR_PC.search(text)
        if m:
            reg, imm = m.group(1), int(m.group(2), 0)
            v = img.word(addr + 8 + imm)
            if v is not None:
                lits[reg] = v
        mm = text.split()
        if len(mm) >= 3 and mm[0] == "add" and mm[2] == "pc," :
            reg = mm[1].rstrip(",")
            if reg in lits:
                cand = (addr + 8 + lits[reg]) & 0xFFFFFFFF
                for s in img.secs:
                    if (s.get("name") in ("__bss", "__common", "__data")
                            and s.get("addr", 0) <= cand
                            < s.get("addr", 0) + s.get("size", 0)):
                        return cand
    return None


class Reader:
    """Guest VIRTUAL memory reads over QMP, without stopping the vCPU."""

    def __init__(self, q, tmp: Path):
        self.q = q
        self.tmp = tmp
        self.n = 0
        self.fails = 0

    def read(self, addr, size):
        self.n += 1
        f = self.tmp / f"m{self.n & 1}.bin"
        r = self.q.cmd("memsave", {"val": addr, "size": size,
                                   "filename": str(f)})
        if "error" in r:
            self.fails += 1
            return None
        try:
            return f.read_bytes()
        except OSError:
            return None

    def word(self, addr):
        b = self.read(addr, 4)
        return None if not b or len(b) < 4 else struct.unpack("<I", b)[0]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=["m68ap-10", "m68ap-114"], required=True)
    ap.add_argument("--logs", type=Path, default=None)
    ap.add_argument("--vnc-port", type=int, default=5930)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--baseline", type=float, default=10)
    ap.add_argument("--presses", type=int, default=3)
    ap.add_argument("--press-interval", type=float, default=20)
    ap.add_argument("--poll-hz", type=float, default=5)
    ap.add_argument("--max-walk", type=int, default=64)
    ap.add_argument("--burst", type=int, default=0,
                    help="CONTROL: fire this many taps as fast as possible while "
                         "sampling flat out, to prove the poller can EVER catch "
                         "a non-empty queue. A queue that normally drains in "
                         "microseconds is invisible at 5 Hz, so without this the "
                         "'always empty' result cannot be told apart from an "
                         "instrument that could never see anything.")
    ap.add_argument("--no-app", action="store_true")
    args = ap.parse_args()

    tag = "noapp" if args.no_app else "inapp"
    args.logs = args.logs or Path(f"/tmp/gsq-{args.board}-{tag}")
    args.logs.mkdir(parents=True, exist_ok=True)

    brk = _load("sbbreak", REPO / "scripts" / "springboard-button-breakpoint.py")
    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    gsp = _load("gsprobe", REPO / "scripts" / "gsevent-type-probe.py")
    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    dis = _load("objcdis", REPO / "scripts" / "objc-method-disasm.py")
    app, icon = btn.BOARDS[args.board]

    mnt = Path(f"/tmp/gsq-root-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse",
                    "-mountpoint", str(mnt), str(brk.ROOTS[args.board])],
                   capture_output=True)
    try:
        gs_bin = args.logs / "GraphicsServices"
        gs_bin.write_bytes((mnt / gsp.GS).read_bytes())
        fps = gsp.exec_fingerprints(mnt)
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    syms = {n: v for v, n, d in msym.symbols(gs_bin.read_bytes()) if d}
    cb = syms["_PurpleEventCallback"]
    head_addr = queue_head_addr(gs_bin, cb, gsp, dis)
    if not head_addr:
        print("FAIL: could not derive the queue-head global")
        return 2
    print(f"  _PurpleEventCallback = {cb:#x}")
    print(f"  GSEvent queue head   = {head_addr:#010x}  (GraphicsServices __bss)")
    print(f"  {len(fps)} executables fingerprinted at {gsp.EXEC_BASE:#x}")

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/gsq-{os.getpid()}.sock"
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               IT_KEY_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = None
    samples = []
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
            print("NOT opening an app")
        else:
            print("opening an app ...")
            btn.tap(q, *icon, 0.12)
            time.sleep(args.settle)

        # The QEMU process's own CPU time. Completely non-invasive, and it
        # separates the two readings of "SpringBoard is the only process mapped":
        # a SPINNING guest burns ~1.0 host core, an IDLE one (parked at WFI)
        # burns almost nothing. A healthy M68AP idles at 6-10%.
        qpid = None
        try:
            out = subprocess.run(["pgrep", "-f", qmp_path],
                                 capture_output=True, text=True).stdout.split()
            for cand in out:
                cl = subprocess.run(["ps", "-o", "command=", "-p", cand],
                                    capture_output=True, text=True).stdout
                if "qemu-system-arm" in cl:
                    qpid = int(cand)
        except Exception:
            pass
        print(f"  qemu pid = {qpid}")

        def cputime():
            if not qpid:
                return None
            try:
                t = subprocess.run(["ps", "-o", "cputime=", "-p", str(qpid)],
                                   capture_output=True, text=True).stdout.strip()
            except Exception:
                return None
            if not t:
                return None
            parts = t.replace("-", ":").split(":")
            try:
                parts = [float(x) for x in parts]
            except ValueError:
                return None
            sec = 0.0
            for x in parts:
                sec = sec * 60 + x
            return sec

        cpu = []
        rd = Reader(q, args.logs)
        t0 = time.time()

        def sample(phase):
            hdr = rd.read(gsp.EXEC_BASE, 0x40)
            proc_name = fps.get(hdr, "?" if hdr else "unmapped")
            head = rd.word(head_addr)
            depth, types = 0, []
            node = head
            while node and depth < args.max_walk:
                blk = rd.read(node, 8)
                if not blk or len(blk) < 8:
                    break
                nxt, ev = struct.unpack("<II", blk)
                depth += 1
                if ev:
                    b = rd.read(ev, 0x40)
                    if b and len(b) >= 0x3C:
                        t8, = struct.unpack_from("<I", b, 8)
                        sub, = struct.unpack_from("<I", b, 0x38)
                        types.append(gsp.gs_type(t8, sub))
                node = nxt
            s = {"t": round(time.time() - t0, 2), "phase": phase,
                 "proc": proc_name, "head": head, "depth": depth,
                 "types": types}
            samples.append(s)
            return s

        def press():
            """Both edges, checked. No gdb here, so the VM is always running --
            but check anyway: a silent refusal is how the DOWN/UP count was
            faked once already."""
            for down in (True, False):
                r = q.cmd("input-send-event", {"events": [
                    {"type": "key", "data": {"down": down, "key": {
                        "type": "qcode", "data": "h"}}}]})
                if "error" in r:
                    print(f"  !! key edge refused: {r['error'].get('desc')}")
                if down:
                    time.sleep(0.15)

        def poll(seconds, phase):
            end = time.time() + seconds
            per = 1.0 / args.poll_hz
            next_cpu = 0.0
            while time.time() < end:
                if time.time() >= next_cpu:
                    c = cputime()
                    if c is not None:
                        cpu.append({"t": round(time.time() - t0, 2),
                                    "phase": phase, "cpu": c})
                    next_cpu = time.time() + 1.0
                s = sample(phase)
                if s["head"]:
                    print(f"    t={s['t']:<7} [{phase}] {s['proc']:<14} "
                          f"head={s['head']:#010x} depth={s['depth']} "
                          f"types={s['types']}")
                time.sleep(per)

        print(f"baseline: polling {args.baseline:.0f}s with no input ...")
        poll(args.baseline, "before")

        if args.burst:
            print(f"CONTROL: {args.burst} taps flat out, sampling between "
                  f"every event ...")
            for i in range(args.burst):
                for down in (True, False):
                    q.cmd("input-send-event", {"events": [
                        {"type": "abs", "data": {"axis": "x", "value": 16384}},
                        {"type": "abs", "data": {"axis": "y", "value": 16384}},
                        {"type": "btn", "data": {"down": down,
                                                 "button": "left"}}]})
                    for _ in range(3):
                        sample("burst")
            b = [s for s in samples if s["phase"] == "burst"]
            nz = [s for s in b if s["head"]]
            print(f"  burst: {len(b)} samples, {len(nz)} caught a NON-EMPTY "
                  f"queue -> the poller "
                  f"{'CAN' if nz else 'CANNOT'} observe a queued event")
        for i in range(args.presses):
            print(f"pressing HOME ({i + 1}/{args.presses}) ...")
            press()
            poll(args.press_interval, f"p{i + 1}")

        (args.logs / "queue.json").write_text(json.dumps(
            {"board": args.board, "no_app": args.no_app,
             "head_addr": head_addr, "samples": samples, "cpu": cpu}, indent=1))

        # ---- report --------------------------------------------------------
        print(f"\n=== {args.board} {'--no-app' if args.no_app else 'in-app'}: "
              f"{len(samples)} samples, {rd.fails} failed reads ===")
        sb = [s for s in samples if s["proc"] == "SpringBoard"]
        print(f"\n  samples whose current process is SpringBoard: {len(sb)}"
              f" / {len(samples)}")
        print(f"  processes seen: "
              f"{dict(Counter(s['proc'] for s in samples).most_common(6))}")
        for phase in ["before"] + [f"p{i + 1}" for i in range(args.presses)]:
            ph = [s for s in sb if s["phase"] == phase]
            nz = [s for s in ph if s["head"]]
            mx = max((s["depth"] for s in ph), default=0)
            print(f"    {phase:>4}: {len(ph):>4} SpringBoard samples, "
                  f"{len(nz)} with a NON-EMPTY queue, max depth {mx}")
        allnz = [s for s in samples if s["head"]]
        print(f"\n  non-empty queue seen in ANY process: {len(allnz)} samples")
        if allnz:
            print(f"  event types ever queued: "
                  f"{sorted({t for s in allnz for t in s['types']})}")
            print("  => events ARE enqueued; if they persist, the DRAIN side "
                  "(run loop / callback) is what died")
        else:
            print("  => the queue was ALWAYS empty. This establishes only that "
                  "there is no\n     PERSISTENT BACKLOG -- a stuck drain would "
                  "hold events for tens of seconds\n     and these samples "
                  "would have caught it.")
            print("     It does NOT establish that nothing is enqueued: with "
                  "--burst 40 on a\n     WORKING configuration the poller "
                  "caught a queued event 0 times in 240\n     samples, i.e. the "
                  "queue drains faster than a QMP round-trip. A negative\n"
                  "     from an instrument that cannot produce a positive is not "
                  "evidence.")
        print("\n  which process is mapped, per phase (the CPU's current "
              "context):")
        for phase in (["before"] + (["burst"] if args.burst else [])
                      + [f"p{i + 1}" for i in range(args.presses)]):
            c = Counter(s["proc"] for s in samples if s["phase"] == phase)
            if c:
                print(f"    {phase:>6} ({sum(c.values()):>3}): "
                      f"{dict(c.most_common(5))}")
        print("\n  QEMU host CPU per phase (spinning ~1.0 core, idle ~0.05-0.15):")
        for phase in (["before"] + (["burst"] if args.burst else [])
                      + [f"p{i + 1}" for i in range(args.presses)]):
            pts = [x for x in cpu if x["phase"] == phase]
            if len(pts) >= 2:
                dt = pts[-1]["t"] - pts[0]["t"]
                dc = pts[-1]["cpu"] - pts[0]["cpu"]
                if dt > 0:
                    print(f"    {phase:>6}: {dc / dt:5.2f} cores "
                          f"({dc:.1f}s cpu over {dt:.1f}s)")
        keys = [l for l in logp.read_bytes().decode("utf8", "replace").splitlines()
                if "[KEYTRACE]" in l]
        n_dn = sum(1 for l in keys if "keycode=35 " in l)
        n_up = sum(1 for l in keys if "keycode=163 " in l)
        print(f"\n  [KEYTRACE] the model saw down x{n_dn}, up x{n_up}, for "
              f"{args.presses} presses")
        if n_dn != args.presses or n_up != args.presses:
            print("  !! the model dropped an edge -- run is SUSPECT")
        print(f"\n  raw samples: {args.logs / 'queue.json'}")
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
