#!/usr/bin/env python3
"""Name the loop SpringBoard spins in after the first in-app HOME press.

Established (IN_APP_BUTTON_INVESTIGATION.md, finding 9): on iPhone OS 1.0, after
the first press taken with an app frontmost, QEMU goes from 0.07 to 0.97 host
cores and SpringBoard is the only process ever mapped. It SPINS, starving every
other process -- which is why no further GSEvent is sent and touch dies with the
button.

A spin is the easy case, and it inverts the instrument problem that dogged this
investigation. Nothing needs to make progress any more, so stopping the vCPU is
free; and the guest is in one place, so a handful of samples finds it.

Two passes, both after the spin is established:

  1. **Sample.** Interrupt, read pc, resume, repeat. A histogram over ~40
     samples names the hot function.
  2. **Step.** Single-step a few thousand instructions and report the distinct
     addresses in order, plus the detected period. That is the loop BODY, not
     just a point in it.

Every pc is resolved against real symbols: SpringBoard and UIKit ObjC metadata
plus the full `LC_SYMTAB` of CoreFoundation (2879 symbols), Foundation (5343),
libSystem (4994), libobjc (630) and GraphicsServices. `nm` cannot read these
binaries, which is why they looked stripped; `macho-symbols.py` can.

Ordering matters: the press must be sent BEFORE gdb attaches, because QMP
`input-send-event` is refused outright while the VM is stopped, and attaching
stops it.

Usage:
  scripts/spin-locate.py --board m68ap-10
  scripts/spin-locate.py --board m68ap-114     # control: must NOT spin
"""
from __future__ import annotations

import argparse
import bisect
import json
import struct
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

SB = "System/Library/CoreServices/SpringBoard.app/SpringBoard"

# Binaries worth naming addresses in, at their guest link addresses.
SYM_LIBS = [
    ("CoreFoundation", "System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"),
    ("Foundation", "System/Library/Frameworks/Foundation.framework/Foundation"),
    ("UIKit", "System/Library/Frameworks/UIKit.framework/UIKit"),
    ("GraphicsServices", "System/Library/Frameworks/GraphicsServices.framework/GraphicsServices"),
    ("libobjc", "usr/lib/libobjc.A.dylib"),
    ("libSystem", "usr/lib/libSystem.B.dylib"),
]


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
    ap.add_argument("--logs", type=Path, default=None)
    ap.add_argument("--gdb-port", type=int, default=1234)
    ap.add_argument("--vnc-port", type=int, default=5930)
    ap.add_argument("--settle", type=float, default=45)
    ap.add_argument("--spin-wait", type=float, default=25,
                    help="seconds after the press before sampling, so the spin "
                         "is established and measurable")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--sample-gap", type=float, default=0.1)
    ap.add_argument("--steps", type=int, default=6000)
    ap.add_argument("--kernelcache", type=Path, default=None,
                    help="resolve KERNEL addresses through this extracted "
                         "kernelcache's kmod_info list, and walk the kernel "
                         "stack at the spin to name the CALLER -- i.e. which "
                         "driver asked the MBX to do work")
    ap.add_argument("--break-kaddr", type=lambda v: int(v, 0), default=None,
                    help="instead of hunting a spin, break at this KERNEL "
                         "address and report who called it (lr + r7 frames + a "
                         "stack scan, all named through --kernelcache). Kernel "
                         "addresses are global, so unlike an executable's IMPs "
                         "a hit needs no process disambiguation.")
    ap.add_argument("--hits", type=int, default=3)
    ap.add_argument("--no-app", action="store_true")
    args = ap.parse_args()

    tag = "noapp" if args.no_app else "inapp"
    args.logs = args.logs or Path(f"/tmp/spin-{args.board}-{tag}")
    args.logs.mkdir(parents=True, exist_ok=True)

    brk = _load("sbbreak", REPO / "scripts" / "springboard-button-breakpoint.py")
    btn = _load("appbuttonprobe", REPO / "scripts" / "app-button-probe.py")
    lock = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")
    gsp = _load("gsprobe", REPO / "scripts" / "gsevent-type-probe.py")
    msym = _load("machosym", REPO / "scripts" / "macho-symbols.py")
    xref = _load("objcxref", REPO / "scripts" / "objc-xref.py")
    dis = _load("objcdis", REPO / "scripts" / "objc-method-disasm.py")
    app, icon = btn.BOARDS[args.board]

    groot = _load("guestroot", REPO / "scripts" / "guest_root.py")
    try:
        mnt, mounted_here = groot.attach(brk.ROOTS[args.board], "spin-root")
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 2
    if not mounted_here:
        print(f"  reusing existing mount at {mnt}")

    symmap = []
    try:
        sb_bin = args.logs / "SpringBoard"
        sb_bin.write_bytes((mnt / SB).read_bytes())
        fps = gsp.exec_fingerprints(mnt)
        libs = brk.library_map(mnt)
        for i, n in xref.methods(dis.Image(sb_bin)):
            symmap.append((i, n))
        for nm, rel in SYM_LIBS:
            src = mnt / rel
            if not src.is_file():
                continue
            dst = args.logs / nm
            dst.write_bytes(src.read_bytes())
            data = dst.read_bytes()
            try:
                for v, s, d in msym.symbols(data):
                    if d and v:
                        symmap.append((v, f"{nm}:{s}"))
            except Exception:
                pass
            if nm in ("UIKit", "Foundation"):
                try:
                    for i, n in xref.methods(dis.Image(dst)):
                        symmap.append((i, n))
                except Exception:
                    pass
    finally:
        groot.detach(mnt, mounted_here)

    symmap.sort()
    addrs = [a for a, _ in symmap]
    print(f"  {len(symmap)} symbols, {len(libs)} images, "
          f"{len(fps)} executables fingerprinted")

    def name_of(addr):
        if addr >= 0xC0000000:
            return "KERNEL"
        i = bisect.bisect_right(addrs, addr) - 1
        if i >= 0 and addr - symmap[i][0] < 0x8000:
            return f"{symmap[i][1]}+{addr - symmap[i][0]:#x}"
        return brk.whose(addr, libs)

    # ---- kernel naming, from the kernelcache -------------------------------
    kexts, ksyms, kskew = [], [], None
    if args.kernelcache:
        xr = _load("kaddr", REPO / "scripts" / "kernel-addr-symbolize.py")
        kdata = args.kernelcache.read_bytes()
        ksegs = xr.segments(args.kernelcache)
        pre = next((x for x in ksegs if x["name"] == "__PRELINK"), None)
        kskew = pre["vmaddr"] - pre["fileoff"]
        kexts = xr.kmods(kdata, (pre["vmaddr"], pre["vmaddr"] + pre["vmsize"]))
        ktext = next((x for x in ksegs if x["name"] == "__TEXT"), None)
        kranges = [(ktext["vmaddr"], ktext["vmaddr"] + ktext["vmsize"], "kernel")]
        kranges += [(a, a + sz, n) for n, a, sz in kexts]
        print(f"  {len(kexts)} kexts from the kernelcache")

        def kname(addr):
            for lo, hi, n in kranges:
                if lo <= addr < hi:
                    return f"{n.replace('com.apple.driver.', '')}+{addr - lo:#x}"
            return None
    else:
        def kname(addr):
            return None

    def func_of(addr):
        n = name_of(addr)
        return n.rsplit("+", 1)[0] if "+" in n else n

    logp = args.logs / "qemu.log"
    qmp_path = f"/tmp/spin-{os.getpid()}.sock"
    env = dict(os.environ, S5L8900_HTTP_BRIDGE="0", S5L8900_HTTPS_BRIDGE="0",
               IT_KEY_TRACE="1")
    cmd = [f"{app}/Contents/MacOS/iPod Touch",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{args.vnc_port - 5900}",
           "-gdb", f"tcp::{args.gdb_port}"]
    proc = subprocess.Popen(cmd, env=env, stdout=open(logp, "wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = None
    out = {}
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
            # PRECONDITION, verified: the spin only happens on a press taken
            # with an app FRONTMOST. A run whose icon tap missed presses HOME
            # from the home screen, does not spin, and looks exactly like "the
            # bug did not reproduce" -- which cost one run before this check
            # existed. The home screen sits near 45% lit, an app near 99%.
            tmpfb = args.logs / "fb.raw"
            for attempt in range(3):
                print(f"opening an app (attempt {attempt + 1}) ...")
                btn.tap(q, *icon, 0.12)
                time.sleep(args.settle)
                litv = max(btn.lit(b) for b in btn.grab(q, tmpfb))
                print(f"  screen lit fraction: {litv:.1f}%")
                if litv > 80:
                    break
            else:
                print("FAIL: no app frontmost after 3 taps -- run is INVALID "
                      "(a press from the home screen does not spin)")
                out["invalid"] = "no app frontmost"
                return 1

        # The QEMU pid, for the CPU control.
        qpid = None
        for cand in subprocess.run(["pgrep", "-f", qmp_path],
                                   capture_output=True, text=True).stdout.split():
            cl = subprocess.run(["ps", "-o", "command=", "-p", cand],
                                capture_output=True, text=True).stdout
            if "qemu-system-arm" in cl:
                qpid = int(cand)

        def cputime():
            if not qpid:
                return None
            t = subprocess.run(["ps", "-o", "cputime=", "-p", str(qpid)],
                               capture_output=True, text=True).stdout.strip()
            if not t:
                return None
            sec = 0.0
            for x in t.replace("-", ":").split(":"):
                try:
                    sec = sec * 60 + float(x)
                except ValueError:
                    return None
            return sec

        # PRESS FIRST, gdb second: QMP input is refused while the VM is stopped,
        # and attaching a gdb client stops it.
        print("pressing HOME (before attaching gdb) ...")
        for down in (True, False):
            r = q.cmd("input-send-event", {"events": [
                {"type": "key", "data": {"down": down,
                                         "key": {"type": "qcode",
                                                 "data": "h"}}}]})
            if "error" in r:
                print(f"  !! key edge refused: {r['error'].get('desc')}")
            if down:
                time.sleep(0.15)

        c0 = cputime()
        t0 = time.time()
        print(f"waiting {args.spin_wait:.0f}s for the spin to establish ...")
        time.sleep(args.spin_wait)
        c1 = cputime()
        cores = None
        if c0 is not None and c1 is not None:
            cores = (c1 - c0) / (time.time() - t0)
            print(f"  QEMU host CPU since the press: {cores:.2f} cores")
        out["cores"] = cores
        # THE CONTROL. Everything below only means something if it is spinning.
        if cores is not None and cores < 0.5:
            print("  NOT SPINNING -- there is no loop to name in this run. "
                  "(Expected on 1.1.4.)")

        print(f"attaching gdbstub on :{args.gdb_port} ...")
        g = brk.Gdb(args.gdb_port)
        g.wait_stop(2)                      # attaching stops the VM

        if args.break_kaddr is not None:
            a = args.break_kaddr
            print(f"  breakpoint at {a:#x} ({kname(a) or '?'}): "
                  f"{g.set_break(a)}")
            callers = []
            for h in range(args.hits):
                g.cont()
                if g.wait_stop(60) is None:
                    print(f"  no hit {h + 1} within 60 s")
                    break
                w = g.regs()
                if not w:
                    break
                regs, _c = w
                sp, fp, lr, pc = regs[13], regs[7], regs[14], regs[15]
                print(f"\n  === hit {h + 1}: pc={pc:#x} {kname(pc) or ''} ===")
                print(f"    lr      {lr:#010x}  {kname(lr) or name_of(lr)}")
                rec = {"pc": pc, "lr": lr, "lr_name": kname(lr), "frames": []}
                cur = fp
                for _ in range(10):
                    blk = g.mem(cur, 8)
                    if not blk or len(blk) < 8:
                        break
                    prev, ret = struct.unpack("<II", blk)
                    if not ret or ret == 0xFFFFFFFF:
                        break
                    nm = kname(ret) or name_of(ret)
                    print(f"    frame   {ret:#010x}  {nm}")
                    rec["frames"].append([ret, nm])
                    if not prev or prev <= cur:
                        break
                    cur = prev
                blk = g.mem(sp, 0x200)
                seen, scan = set(), []
                if blk:
                    for i in range(0, len(blk) - 4, 4):
                        v, = struct.unpack_from("<I", blk, i)
                        nm = kname(v)
                        if nm and v not in seen and not nm.startswith("kernel+"):
                            seen.add(v)
                            scan.append([v, nm])
                    print("    stack scan (kext addresses):")
                    for v, nm in scan[:12]:
                        print(f"      {v:#010x}  {nm}")
                rec["stack_scan"] = scan
                callers.append(rec)
                # step off before continuing, or QEMU re-traps at the same pc
                g.del_break(a)
                g.step()
                g.set_break(a)
            out["callers"] = callers
            (args.logs / "spin.json").write_text(json.dumps(out, indent=1))
            print(f"\n  {len(callers)} hits recorded -> "
                  f"{args.logs / 'spin.json'}")
            return 0

        # ---- pass 1: sample ------------------------------------------------
        hist, procs, sampled = Counter(), Counter(), []
        for i in range(args.samples):
            w = g.regs()
            if w:
                pc = w[0][15]
                hdr = g.mem(gsp.EXEC_BASE, 0x40)
                pr = fps.get(hdr, "?" if hdr else "unmapped")
                hist[func_of(pc)] += 1
                procs[pr] += 1
                sampled.append({"pc": pc, "name": name_of(pc), "proc": pr,
                                "cpsr": w[1]})
            g.cont()
            time.sleep(args.sample_gap)
            g.interrupt()
        # ---- who CALLED into here? ------------------------------------------
        # The button path is healthy; what fails is the display transition, so
        # the question is which driver asked the MBX to do work. Walk the r7
        # frame chain (XNU/ARM uses r7 as the frame pointer) and, because a leaf
        # poll loop may have no frame, ALSO scan the stack for words that land
        # in a kext -- belt and braces, since a wrong caller here would send the
        # next person to model the wrong device.
        w = g.regs()
        if w and args.kernelcache:
            regs, _cpsr = w
            sp, fp, lr, pc = regs[13], regs[7], regs[14], regs[15]
            print(f"\n  --- who called into the MBX? "
                  f"pc={pc:#x} lr={lr:#x} sp={sp:#x} r7={fp:#x} ---")
            print(f"    lr        {lr:#010x}  {kname(lr) or name_of(lr)}")
            out["caller_lr"] = [lr, kname(lr)]
            frames = []
            cur = fp
            for _ in range(12):
                blk = g.mem(cur, 8)
                if not blk or len(blk) < 8:
                    break
                prev, ret = struct.unpack("<II", blk)
                if not ret or ret == 0xFFFFFFFF:
                    break
                nm = kname(ret) or name_of(ret)
                print(f"    frame     {ret:#010x}  {nm}")
                frames.append([ret, nm])
                if not prev or prev <= cur:
                    break
                cur = prev
            out["frames"] = frames
            print("    stack scan (distinct kext addresses, sp..sp+0x400):")
            seen, scanned = set(), []
            blk = g.mem(sp, 0x400)
            if blk:
                for i in range(0, len(blk) - 4, 4):
                    v, = struct.unpack_from("<I", blk, i)
                    nm = kname(v)
                    if nm and nm.split("+")[0] not in ("kernel",) and v not in seen:
                        seen.add(v)
                        scanned.append([v, nm])
                for v, nm in scanned[:24]:
                    print(f"      {v:#010x}  {nm}")
            out["stack_scan"] = scanned
        print(f"\n  --- {args.samples} samples during the spin ---")
        print(f"  process: {dict(procs.most_common(5))}")
        for k, v in hist.most_common(12):
            print(f"    x{v:<4} {k}")
        out["samples"] = sampled

        # ---- pass 2: step, and find the period -----------------------------
        print(f"\n  --- single-stepping {args.steps} instructions ---")
        seq = []
        for _ in range(args.steps):
            g.step()
            w = g.regs()
            if not w:
                break
            seq.append(w[0][15])
        out["seq"] = seq
        prof = Counter(func_of(p) for p in seq)
        print(f"  instruction profile over {len(seq)} steps:")
        for k, v in prof.most_common(12):
            print(f"    {v:6d}  {100 * v / max(1, len(seq)):5.1f}%  {k}")

        # period: distance between repeats of the first pc, verified
        period = None
        if seq:
            first = seq[0]
            for j in range(1, len(seq)):
                if seq[j] == first:
                    p = j
                    if all(seq[k] == seq[k % p] for k in range(min(len(seq), 4 * p))):
                        period = p
                        break
        out["period"] = period
        if period:
            print(f"\n  LOOP DETECTED: period {period} instructions, "
                  f"repeating {len(seq) // period} times in the sample")
            body = seq[:period]
            print("  loop body (in execution order):")
            last = None
            for k, pc in enumerate(body):
                nm = name_of(pc)
                f = func_of(pc)
                if f != last:
                    print(f"    [{k:>4}] {pc:#010x}  {nm}")
                    last = f
        else:
            uniq = sorted(set(seq))
            print(f"\n  no exact period; {len(uniq)} distinct addresses. "
                  f"Function transitions:")
            last = None
            shown = 0
            for pc in seq:
                f = func_of(pc)
                if f != last:
                    print(f"    {pc:#010x}  {name_of(pc)}")
                    last = f
                    shown += 1
                    if shown > 40:
                        print("    ...")
                        break

        (args.logs / "spin.json").write_text(json.dumps(out, indent=1))
        keys = [l for l in logp.read_bytes().decode("utf8", "replace").splitlines()
                if "[KEYTRACE]" in l]
        print(f"\n  [KEYTRACE] edges the model saw: {len(keys)} (want 2)")
        print(f"  raw: {args.logs / 'spin.json'}")
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
