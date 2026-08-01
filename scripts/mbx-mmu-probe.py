#!/usr/bin/env python3
"""Walk the MBX MMU page table and find where the 2D command stream lives.

T2, step 1 (MBX_HANDOFF.md, 2026-08-01). The MBX has an MMU: the driver
writes EIGHT guest-PHYSICAL page addresses into registers 0x1000..0x101c
(loop at kernel 0xc03b7334, VA->PA per entry, bound literal 0x1020) and
enables it with 0x1020 = 0x00010001. That is an 8-entry page DIRECTORY, so
the "aperture offsets" the driver hands the engine -- 0x8000, 0x1b000,
0x1d000, 0x21000 and the 2D command blocks at 0xa00000 -- are MBX VIRTUAL
addresses over 8 x 4 MiB = 32 MiB of mapped space, not offsets into our
MMIO window.

This settles the question the whole 2D model rests on, WITHOUT writing any
device code: are those addresses backed by real guest DRAM we can read and
write, and does the command block the guest wrote actually land there?

  * If the translated page for 0xa00000 already holds the command block,
    the CPU has its own mapping onto the same pages: the model only has to
    READ guest memory (and write completions back) -- and the long-standing
    "answering 0 in the aperture is load-bearing" anomaly is explained,
    because the aperture was never where the data lived.
  * If it is empty, the aperture IS the write path and the model must
    forward writes through this translation instead of dropping them.

Method: boot, reach the home screen, harvest the directory from the model's
own IT_MBX_TRACE output, then pmemsave the eight pages over QMP and decode
them offline. Nothing is polled around user input (see dismiss-latency's
polling trap).

Example:
    python3 scripts/mbx-mmu-probe.py --build 4A102 --logs /tmp/mbx-mmu
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
sys.path.insert(0, str(REPO / "scripts"))
import m68ap_paths  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lockprobe = _load("lockprobe", REPO / "scripts" / "lock-unlock-probe.py")

PDE_RE = re.compile(r"\[MBX\].*WR 0x0(10[0-9a-f]{2}) = 0x([0-9a-f]{8})")

# MBX virtual addresses the driver hands the engine, from the measured init
# and the 2D command traffic (IN_APP_BUTTON_INVESTIGATION.md).
TARGETS = {
    0x8000: "engine base (reg 0x608)",
    0x1b000: "engine base (reg 0x60c)",
    0x1d000: "command range (reg 0x824)",
    0x21000: "command range (reg 0x83c)",
    0xa00000: "2D command blocks",
}

PAGE = 0x1000
RAM_BASE = 0x08000000
RAM_END = 0x48000000


def walk(pmem, directory, va):
    """MBX VA -> guest physical, via directory[va>>22] -> PTE[va>>12 & 0x3ff].

    Flag bits are unknown, so the low 12 bits of every entry are masked off
    and reported separately rather than guessed at.
    """
    di, ti, off = (va >> 22) & 0x3FF, (va >> 12) & 0x3FF, va & 0xFFF
    if di >= len(directory):
        return None, f"directory index {di} beyond the {len(directory)} entries"
    pde = directory[di]
    table = pmem(pde & ~0xFFF, PAGE)
    if table is None:
        return None, f"page table at {pde:#010x} unreadable"
    pte = int.from_bytes(table[ti * 4:ti * 4 + 4], "little")
    if not pte:
        return None, (f"PTE[{ti}] in table {pde & ~0xFFF:#010x} is ZERO "
                      f"(nothing mapped at this VA)")
    pa = (pte & ~0xFFF) | off
    return pa, f"pde[{di}]={pde:#010x} pte[{ti}]={pte:#010x} flags={pte & 0xFFF:#05x}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    m68ap_paths.add_build_argument(ap)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--qemu", type=Path,
                    default=REPO / "build-ipod11" / "qemu-system-arm")
    ap.add_argument("--deadline", type=float, default=600)
    ap.add_argument("--exercise", action="store_true",
                    help="after the home screen: tap an app icon, wait, press "
                         "HOME, and wait out the dismissal before walking the "
                         "table. On 1.0 the dismissal renders ~33 blocks of "
                         "2D commands into MBX VA 0xa00000 (aperture writes "
                         "the model drops), so this is the run that can "
                         "answer whether those words ALSO land in the DRAM "
                         "the page table maps -- the trace records every "
                         "aperture write, and the report diffs them against "
                         "the translated page.")
    ap.add_argument("--icon", type=int, nargs=2, default=(277, 258),
                    metavar=("X", "Y"),
                    help="icon to open for --exercise (default: the "
                         "app-button-probe m68ap-10 icon)")
    ap.add_argument("--keep-stage", action="store_true",
                    help="keep the staged NAND clone (~300 MB) after the run; "
                         "by default it is deleted, per the repo's "
                         "disk-hygiene rule")
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    nand = stage / "nand"
    if nand.exists():
        import shutil
        shutil.rmtree(nand)
    stage.mkdir(exist_ok=True)
    subprocess.run(["cp", "-Rc", str(paths.nand), str(nand)], check=True)
    for b in range(8):
        (nand / f"bank{b}").mkdir(exist_ok=True)
    nor = stage / "nor.bin"
    subprocess.run(["cp", str(paths.nor), str(nor)], check=True)

    classify = lockprobe._classifier()
    qmp_path = Path(f"/tmp/mbxmmu-{os.getpid()}.qmp")
    serial, stderr = args.logs / "serial.log", args.logs / "stderr.log"
    vnc_port = 5996

    cmd = [str(args.qemu),
           "-M", (f"iPhone-2G,bootrom={m68ap_paths.BOOTROM},"
                  f"iboot={paths.iboot_sb},nand={nand}"),
           "-m", "1G", "-pflash", str(nor), "-icount", "1",
           "-L", str(lockprobe.PC_BIOS),
           "-serial", f"file:{serial}",
           "-qmp", f"unix:{qmp_path},server,nowait",
           "-vnc", f"127.0.0.1:{vnc_port - 5900}"]
    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    env = dict(os.environ)
    env.setdefault("IT_M68AP_NO_BASEBAND", "1")
    env["IT_MBX_TRACE"] = "all"
    proc = subprocess.Popen(cmd, env=env, stdout=stderr.open("wb"),
                            stderr=subprocess.STDOUT)

    report = {"build": args.build, "targets": {}}
    try:
        client = lockprobe.DisplayClient(vnc_port)
        client.start()
        print(f"booting {args.build} (IT_MBX_TRACE=all) ...", flush=True)
        time.sleep(60)
        q = lockprobe.QMP(qmp_path)
        deadline = time.time() + args.deadline
        kind = {"kind": "blank"}
        while time.time() < deadline:
            d, kind = lockprobe.grab(q, args.logs, classify)
            if kind["kind"] == "home":
                break
            if kind["nonblack_pct"] > 5.0:
                lockprobe.slide(q)
                time.sleep(8)
                d, kind = lockprobe.grab(q, args.logs, classify)
                if kind["kind"] == "home":
                    break
            time.sleep(10)
        print(f"screen: {kind}")
        report["screen"] = kind

        if args.exercise and kind["kind"] == "home":
            # Deliberately generous, fixed waits: under -icount guest time
            # runs slower than wall clock, and the 1.0 dismissal alone is
            # ~34 s of guest time. No polling between input and verdict
            # (the dismiss-latency pmemsave trap).
            x, y = args.icon
            print(f"exercise: tapping icon ({x},{y}), waiting, HOME, "
                  f"waiting out the dismissal ...", flush=True)
            lockprobe._abs(q, x, y)
            q.cmd("input-send-event", {"events": [
                {"type": "btn", "data": {"down": True, "button": "left"}}]})
            time.sleep(0.12)
            q.cmd("input-send-event", {"events": [
                {"type": "btn", "data": {"down": False, "button": "left"}}]})
            time.sleep(60)
            d, kind = lockprobe.grab(q, args.logs, classify)
            lockprobe.png(d, args.logs / "in-app.png")
            print(f"after tap: {kind}")
            report["in_app"] = kind
            lockprobe.key(q, "h")
            # Guest time under -icount runs several times slower than wall
            # clock (measured ~6x on this boot), so a fixed wall-clock wait
            # undershoots the ~34 s guest-time dismissal -- the first run of
            # this mode stopped at the 0x12C soft event with zero command
            # writes captured. Instead, watch the model's own trace: wait
            # until aperture writes (offset >= 0x2000) appear and stop
            # growing, with a hard cap.
            print("waiting for the 2D command stream in the trace ...",
                  flush=True)
            wr_re2 = re.compile(rb"WR 0x([0-9a-f]{4,7}) =")
            last, stable, waited = -1, 0, 0
            while waited < 600 and stable < 3:
                time.sleep(30)
                waited += 30
                n = sum(1 for m in wr_re2.finditer(stderr.read_bytes())
                        if int(m.group(1), 16) >= 0x2000)
                stable = stable + 1 if (n == last and n > 0) else 0
                last = n
                print(f"  t+{waited}s: {n} aperture-write trace lines "
                      f"(stable x{stable})", flush=True)
            d, kind = lockprobe.grab(q, args.logs, classify)
            lockprobe.png(d, args.logs / "after-home.png")
            print(f"after HOME: {kind}")
            report["after_home"] = kind

        # ---- harvest the page directory from the model's own trace --------
        text = stderr.read_bytes().decode("latin1", "replace")
        entries = {}
        for m in PDE_RE.finditer(text):
            entries[int(m.group(1), 16)] = int(m.group(2), 16)
        directory = [entries.get(0x1000 + 4 * i, 0) for i in range(8)]
        report["page_directory"] = [f"{e:08x}" for e in directory]
        print("\npage directory (regs 0x1000..0x101c):")
        for i, e in enumerate(directory):
            ok = RAM_BASE <= e < RAM_END
            print(f"  [{i}] 0x{e:08x}  {'DRAM' if ok else 'NOT DRAM'}")
        if not any(directory):
            print("no directory writes seen -- did the guest program the MMU?")
            return 1

        def pmem(pa, size):
            if not (RAM_BASE <= pa < RAM_END):
                return None
            raw = args.logs / "_pm.bin"
            q.cmd("pmemsave", {"val": pa, "size": size,
                               "filename": str(raw)})
            d = raw.read_bytes()
            raw.unlink(missing_ok=True)
            return d

        # ---- translate every address the driver hands the engine ----------
        print("\ntranslations:")
        for va, what in sorted(TARGETS.items()):
            pa, how = walk(pmem, directory, va)
            rec = {"what": what, "how": how}
            if pa is None:
                print(f"  MBX 0x{va:08x} ({what}): UNMAPPED -- {how}")
            else:
                page = pmem(pa & ~0xFFF, PAGE) or b""
                nonzero = sum(1 for b in page if b)
                head = page[(pa & 0xFFF):(pa & 0xFFF) + 32].hex()
                rec.update({"pa": f"{pa:08x}",
                            "page_nonzero_bytes": nonzero,
                            "first32": head})
                print(f"  MBX 0x{va:08x} ({what}) -> PA 0x{pa:08x}")
                print(f"      {how}")
                print(f"      page has {nonzero}/{PAGE} nonzero bytes; "
                      f"first 32 at target: {head}")
                (args.logs / f"page_{va:08x}.bin").write_bytes(page)
            report["targets"][f"{va:08x}"] = rec

        # ---- diff every traced aperture WRITE against the mapped DRAM -----
        # The model DROPS these writes (IT_MBX_RAM off), so if the words are
        # nevertheless present at the translated addresses, the guest CPU
        # has its own mapping onto those pages and the aperture was never
        # the real data path. If they are absent, the aperture IS the write
        # path and the model must forward it through this table.
        wr_re = re.compile(r"\[MBX\].*WR 0x([0-9a-f]{4,7}) = 0x([0-9a-f]{8})")
        text = stderr.read_bytes().decode("latin1", "replace")
        writes = {}
        for m in wr_re.finditer(text):
            a = int(m.group(1), 16)
            if a >= 0x2000:
                writes[a] = int(m.group(2), 16)
        report["aperture_writes"] = len(writes)
        if writes:
            match = miss = untrans = 0
            page_cache = {}
            for a, v in sorted(writes.items()):
                pa, how = walk(pmem, directory, a)
                if pa is None:
                    untrans += 1
                    continue
                pg = pa & ~0xFFF
                if pg not in page_cache:
                    page_cache[pg] = pmem(pg, PAGE) or b""
                data = page_cache[pg]
                got = int.from_bytes(data[(pa & 0xFFF):(pa & 0xFFF) + 4],
                                     "little") if data else None
                if got == v:
                    match += 1
                else:
                    miss += 1
                    if miss <= 8:
                        print(f"  MISS MBX 0x{a:07x} -> PA 0x{pa:08x}: "
                              f"wrote 0x{v:08x}, DRAM has "
                              f"0x{got:08x}" if got is not None else
                              f"  MISS MBX 0x{a:07x}: page unreadable")
            report["aperture_diff"] = {"match": match, "miss": miss,
                                       "untranslatable": untrans}
            print(f"\naperture writes vs mapped DRAM: {match} match, "
                  f"{miss} differ, {untrans} untranslatable "
                  f"(of {len(writes)} distinct addresses)")
        # ---- locate the op-state structures the completion path polls ----
        # The recovery routine sleeps on [[obj+0x1a4]+0x60] and re-posts soft
        # events from [1a4]+0x24/+0x4c and [1a8]+0x2c; nothing in the kext
        # ever WRITES those words, so the engine owns them -- and the engine
        # can only reach memory through this page table. The driver object
        # announces itself on the console ("AppleMBXDevice(0xc0a36800): Init"),
        # which is all we need to read the two pointers and ask whether they
        # are mapped. If they are, the model can write completions there;
        # if not, the completion travels by some other route and that is
        # worth knowing before any more device code is written.
        m = re.search(rb"AppleMBXDevice\(0x([0-9a-f]{8})\)",
                      serial.read_bytes())
        if m:
            obj_va = int(m.group(1), 16)
            obj_pa = (obj_va - 0xC0000000) + RAM_BASE
            print(f"\nAppleMBXDevice at VA 0x{obj_va:08x} (PA 0x{obj_pa:08x})")
            report["mbx_device"] = {"va": f"{obj_va:08x}"}
            # reverse map: every PTE in the eight tables, page -> MBX VA
            rev = {}
            for di, pde in enumerate(directory):
                tbl = pmem(pde & ~0xFFF, PAGE)
                if not tbl:
                    continue
                for ti in range(1024):
                    pte = int.from_bytes(tbl[ti * 4:ti * 4 + 4], "little")
                    if pte & ~0xFFF:
                        rev.setdefault(pte & ~0xFFF,
                                       (di << 22) | (ti << 12))
            print(f"page table maps {len(rev)} pages")
            report["mapped_pages"] = len(rev)
            for off, name in ((0x1a4, "[obj+0x1a4]"), (0x1a8, "[obj+0x1a8]")):
                raw = pmem(obj_pa + off, 4)
                if not raw:
                    continue
                ptr = int.from_bytes(raw, "little")
                pa = (ptr - 0xC0000000) + RAM_BASE if ptr >= 0xC0000000 else ptr
                mapped = rev.get(pa & ~0xFFF)
                where = (f"MBX VA 0x{mapped | (pa & 0xFFF):06x}"
                         if mapped is not None else "NOT MMU-mapped")
                print(f"  {name} = 0x{ptr:08x} -> PA 0x{pa:08x}: {where}")
                report.setdefault("op_state", {})[name] = {
                    "ptr": f"{ptr:08x}", "pa": f"{pa:08x}",
                    "mbx_va": f"{mapped:06x}" if mapped is not None else None}
                if mapped is not None:
                    w = pmem(pa + 0x60, 4)
                    if w:
                        print(f"      +0x60 (the word the recovery sleep "
                              f"waits on) = 0x{int.from_bytes(w, 'little'):08x}")
        q.close()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        qmp_path.unlink(missing_ok=True)
        # Disk hygiene, the repo convention (M68AP_RENDER_HANDOFF "Traps"):
        # a NAND tree is ~300 MB and these probes stage one per run. Leaving
        # them behind filled this machine's data volume to 100% on
        # 2026-08-01 and broke an app install mid-flight. Inside the finally
        # so an early return or a crash still cleans up. --keep-stage keeps
        # it when a run needs the guest's own writes for post-mortem.
        if not args.keep_stage:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


    (args.logs / "mbx-mmu-probe.json").write_text(
        json.dumps(report, indent=2) + "\n")
    print(f"\nreport: {args.logs / 'mbx-mmu-probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
