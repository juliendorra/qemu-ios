#!/usr/bin/env python3
"""Does anything the guest writes to the NAND survive a restart?

Until now the answer was never measured, because nothing ever booted from a
NAND the guest had written to. In the default (read-only) mode every write
lands in a ``<page>_new.page`` file the read path never opens, so each boot
starts from the pristine image and the question cannot even be asked. With
``IT_NAND_WRITABLE=1`` it can -- and the answer is no: the second boot wedges
in iBoot with the Apple logo up and *zero* serial lines.

This tool makes that a one-command check instead of a hand-driven A/B:

    boot A   fresh copy of the build's NAND, writable, run until the guest has
             committed pages (or until a deadline)
    boot B   the SAME directory again -- so boot B reads exactly what boot A
             wrote -- and see whether the kernel comes up at all

``--bisect`` then answers "which written pages break it": it rebuilds the NAND
as pristine-plus-a-subset and re-runs boot B, narrowing by delta debugging
until a minimal breaking set is left. That is the one measurement that settles
the cause without any theory about NAND semantics, which matters here because
the obvious theory is already dead -- ``IT_NAND_CMDS`` shows the guest issuing
no erase and no program opcode at all in a 200 s session, so "the model drops
erases" cannot be the explanation on its own.

Success is detected by polling the serial log rather than by waiting out a
fixed deadline, so a healthy boot costs what it costs and only a genuine
failure pays the full timeout.

Examples:
    python3 scripts/nand-persistence-probe.py --board m68ap --build 1A543a \\
        --logs /tmp/np-1.0
    python3 scripts/nand-persistence-probe.py --board m68ap --build 1A543a \\
        --logs /tmp/np-bisect --bisect
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m68ap_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"

# "BSD root" is the first line that proves the kernel mounted storage, which is
# exactly the thing under test. Reaching SpringBoard would prove more but takes
# minutes longer, and every failure seen so far dies long before the kernel.
BOOT_OK_MARKER = "BSD root"


def launch(args, nand: Path, nor: Path, serial: Path, logdir: Path,
           tag: str) -> subprocess.Popen:
    if args.board == "n45ap":
        machine_type, iboot = "iPod-Touch", args.iboot_n45ap
    else:
        machine_type, iboot = "iPhone-2G", args.iboot_m68ap
    machine = (f"{machine_type},bootrom={args.bootrom},"
               f"iboot={iboot},nand={nand}")
    if args.epoch is not None:
        machine += f",epoch={args.epoch}"
    cmd = [str(args.qemu), "-M", machine, "-m", "1G",
           "-pflash", str(nor),
           "-L", str(APP / "Resources" / "pc-bios"),
           "-display", "none", "-serial", f"file:{serial}"]
    (logdir / f"command-{tag}.txt").write_text(" ".join(cmd) + "\n")
    env = dict(os.environ)
    env["IT_NAND_WRITABLE"] = "1"
    # setdefault, not assignment: IT_NAND_WRITE doubles as the trace cap, and a
    # caller chasing a specific page needs to be able to raise it.
    env.setdefault("IT_NAND_WRITE", "1")
    return subprocess.Popen(
        cmd, env=env,
        stdout=(logdir / f"monitor-{tag}.log").open("wb"),
        stderr=(logdir / f"stderr-{tag}.log").open("wb"))


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def run_boot(args, nand: Path, nor: Path, logdir: Path, tag: str,
             timeout: float, settle: float = 0.0,
             want_writes: int = 0, until: str = "") -> dict:
    """Boot once. Return what was reached, stopping as soon as it is known."""
    serial = logdir / f"serial-{tag}.log"
    serial.write_bytes(b"")
    stderr = logdir / f"stderr-{tag}.log"
    proc = launch(args, nand, nor, serial, logdir, tag)
    started = time.time()
    booted_at = None
    result = {"tag": tag, "booted": False, "writes": 0, "multi_writes": 0}
    try:
        while True:
            now = time.time()
            elapsed = now - started
            text = serial.read_text(errors="replace") if serial.exists() else ""
            if booted_at is None and BOOT_OK_MARKER in text:
                booted_at = now
                result["booted"] = True
                result["boot_s"] = round(elapsed, 1)
            errs = stderr.read_text(errors="replace") if stderr.exists() else ""
            result["writes"] = errs.count("[NAND-WRITE]")
            result["multi_writes"] = errs.count("multi 1")
            if proc.poll() is not None:
                result["died"] = True
                break
            if until and until in text:
                result["reached_marker"] = True
            if booted_at is not None:
                enough = (result["writes"] >= want_writes if want_writes
                          else True)
                if until and not result.get("reached_marker"):
                    enough = False
                if enough and now - booted_at >= settle:
                    break
            if elapsed >= timeout:
                result["timed_out"] = True
                break
            time.sleep(2.0)
    finally:
        stop(proc)
    result["serial_lines"] = len(
        serial.read_text(errors="replace").splitlines()) if serial.exists() \
        else 0
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


def changed_pages(pristine: Path, written: Path) -> list[str]:
    """Page files boot A created or rewrote, as 'bankN/PAGE.page'."""
    out = []
    for bank in sorted(p for p in written.iterdir()
                       if p.is_dir() and p.name.startswith("bank")):
        for page in sorted(bank.iterdir()):
            if page.suffix != ".page" or page.name.endswith("_new.page"):
                continue
            rel = f"{bank.name}/{page.name}"
            base = pristine / rel
            if not base.exists() or base.read_bytes() != page.read_bytes():
                out.append(rel)
    return out


def build_nand(pristine: Path, written: Path, subset: list[str],
               dest: Path) -> None:
    """A pristine NAND with exactly `subset` replaced by boot A's version."""
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["cp", "-Rc", str(pristine), str(dest)], check=True)
    for bank in range(8):
        (dest / f"bank{bank}").mkdir(exist_ok=True)
    for rel in subset:
        shutil.copy2(written / rel, dest / rel)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("m68ap", "n45ap"), required=True)
    m68ap_paths.add_build_argument(ap, "--build", required=False)
    ap.add_argument("--epoch", type=int, default=None)
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path, default=m68ap_paths.BOOTROM)
    ap.add_argument("--iboot-n45ap", type=Path,
                    default=IPOD_FILES / "iboot_204_n45ap.bin")
    ap.add_argument("--nor-n45ap", type=Path,
                    default=IPOD_FILES / "nor_n45ap.bin")
    ap.add_argument("--nand-n45ap", type=Path, default=IPOD_FILES / "nand")
    ap.add_argument("--iboot-m68ap", type=Path)
    ap.add_argument("--nor-m68ap", type=Path)
    ap.add_argument("--nand-m68ap", type=Path)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--boot-timeout", type=float, default=240.0,
                    help="how long a boot may take before it counts as failed")
    ap.add_argument("--settle", type=float, default=60.0,
                    help="seconds to keep boot A running after it comes up, so"
                         " the FTL has a chance to commit pages")
    ap.add_argument("--want-writes", type=int, default=1,
                    help="minimum pages boot A must commit before it is"
                         " stopped (0 = do not wait for any)")
    ap.add_argument("--a-until", default="",
                    help="keep boot A running until this string appears in its"
                         " serial log -- 'System Sleep' is the one that"
                         " matters, since the failure this tool was written"
                         " for only shows up once the guest has slept")
    ap.add_argument("--from-written", type=Path,
                    help="skip boot A and treat this directory as its result."
                         " Boot A is the expensive half and its output is just"
                         " a directory, so a bisect can be re-run against one"
                         " that already exists.")
    ap.add_argument("--bisect-timeout", type=float, default=90.0,
                    help="deadline for a bisect probe boot. A healthy boot"
                         " reaches BSD root in ~12 s, so this only has to be"
                         " comfortably above that -- and every FAILING probe"
                         " pays it in full, which is what sets the runtime.")
    ap.add_argument("--keep-nor", action="store_true",
                    help="let boot B inherit boot A's NOR instead of a fresh"
                         " copy -- what a real power cycle does")
    ap.add_argument("--bisect", action="store_true",
                    help="narrow to a minimal set of written pages that breaks"
                         " boot B")
    args = ap.parse_args()

    if args.board == "m68ap":
        if args.build:
            paths = m68ap_paths.get(args.build)
            paths.require("iboot_sb", "nor", "nand")
            args.iboot_m68ap = args.iboot_m68ap or paths.iboot_sb
            args.nor_m68ap = args.nor_m68ap or paths.nor
            args.nand_m68ap = args.nand_m68ap or paths.nand
            if args.epoch is None:
                args.epoch = paths.epoch
            if not args.bootrom.exists():
                args.bootrom = paths.bootrom
            print(f"[np] {m68ap_paths.describe(args.build)}")
        for name in ("iboot_m68ap", "nor_m68ap", "nand_m68ap"):
            if getattr(args, name) is None:
                ap.error(f"--{name.replace('_', '-')} is required for m68ap "
                         "(or pass --build)")
        src_nand, src_nor = args.nand_m68ap, args.nor_m68ap
    else:
        src_nand, src_nor = args.nand_n45ap, args.nor_n45ap

    args.logs.mkdir(parents=True, exist_ok=True)
    nor = args.logs / "nor.bin"
    shutil.copy2(src_nor, nor)
    nand = args.logs / "nand-a"
    if nand.exists():
        shutil.rmtree(nand)
    subprocess.run(["cp", "-Rc", str(src_nand), str(nand)], check=True)
    for bank in range(8):
        (nand / f"bank{bank}").mkdir(exist_ok=True)

    report: dict = {"board": args.board, "build": args.build}

    if args.from_written:
        shutil.rmtree(nand, ignore_errors=True)
        subprocess.run(["cp", "-Rc", str(args.from_written), str(nand)],
                       check=True)
        print(f"[np] boot A skipped; using {args.from_written}")
        a = {"tag": "a", "booted": True, "reused": str(args.from_written),
             "writes": -1, "multi_writes": -1, "elapsed_s": 0.0}
    else:
        print("[np] boot A: fresh NAND, writable")
        a = run_boot(args, nand, nor, args.logs, "a", args.boot_timeout,
                     settle=args.settle, want_writes=args.want_writes,
                     until=args.a_until)
        print(f"[np]   booted={a['booted']} writes={a['writes']} "
              f"multi={a['multi_writes']} "
              f"marker={a.get('reached_marker', '-')} ({a['elapsed_s']}s)")
    report["boot_a"] = a
    if not a["booted"]:
        report["verdict"] = "boot A never came up -- nothing to test"
        print(json.dumps(report, indent=2))
        (args.logs / "report.json").write_text(json.dumps(report, indent=2))
        return 2

    changed = changed_pages(Path(src_nand), nand)
    report["changed_pages"] = len(changed)
    print(f"[np] boot A rewrote {len(changed)} pages")

    # Boot B normally gets a FRESH NOR, so that the only thing carried over
    # from boot A is the NAND -- otherwise a NAND result could be a NOR result
    # in disguise. But the guest writes to NOR as well, and a real device does
    # not reset it between power cycles, so --keep-nor carries boot A's NOR
    # over and tests the pair the guest actually left behind.
    nor_b = args.logs / "nor-b.bin"
    shutil.copy2(nor if args.keep_nor else src_nor, nor_b)

    print("[np] boot B: same NAND again")
    b = run_boot(args, nand, nor_b, args.logs, "b", args.boot_timeout)
    report["boot_b"] = b
    print(f"[np]   booted={b['booted']} serial_lines={b['serial_lines']} "
          f"({b['elapsed_s']}s)")
    report["persists"] = bool(b["booted"])

    if b["booted"]:
        report["verdict"] = "a written NAND boots again"
    else:
        report["verdict"] = "a written NAND does NOT boot again"
        if args.bisect and changed:
            report["bisect"] = bisect(args, Path(src_nand), nand, nor_b,
                                      changed)

    (args.logs / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["persists"] else 1


def bisect(args, pristine: Path, written: Path, nor: Path,
           changed: list[str]) -> dict:
    """Delta-debug down to a minimal set of written pages that breaks boot B.

    Plain binary search is not enough: if the failure needs pages from both
    halves, both halves boot and the search stops with nothing. ddmin handles
    that by raising the granularity instead of giving up.
    """
    log = []
    tested = 0

    def fails(subset: list[str]) -> bool:
        nonlocal tested
        tested += 1
        tag = f"bisect{tested:02d}"
        cand = args.logs / f"nand-{tag}"
        build_nand(pristine, written, subset, cand)
        r = run_boot(args, cand, nor, args.logs, tag, args.bisect_timeout)
        shutil.rmtree(cand, ignore_errors=True)
        log.append({"test": tag, "pages": len(subset), "booted": r["booted"]})
        print(f"[np]   {tag}: {len(subset)} pages -> "
              f"{'boots' if r['booted'] else 'WEDGES'}")
        return not r["booted"]

    if not fails(changed):
        print("[np] the full written set boots in isolation -- the NAND is "
              "not what breaks boot B")
        return {"tests": log, "minimal": None,
                "note": "full set reproduced no failure"}

    current = list(changed)
    n = 2
    while len(current) > 1:
        chunk = max(1, len(current) // n)
        subsets = [current[i:i + chunk] for i in range(0, len(current), chunk)]
        reduced = False
        for sub in subsets:
            if sub and fails(sub):
                current, n, reduced = sub, 2, True
                break
        if reduced:
            continue
        for i in range(len(subsets)):
            complement = [p for j, s in enumerate(subsets) if j != i
                          for p in s]
            if complement and fails(complement):
                current, n = complement, max(2, n - 1)
                reduced = True
                break
        if reduced:
            continue
        if n >= len(current):
            break
        n = min(len(current), n * 2)

    print(f"[np] minimal breaking set: {len(current)} pages")
    return {"tests": log, "minimal": current}


if __name__ == "__main__":
    sys.exit(main())
