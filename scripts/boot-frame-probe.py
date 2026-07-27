#!/usr/bin/env python3
"""Boot one S5L8900 board and record when (if ever) it draws a visible frame.

The acceptance harness takes a single end-of-run screenshot, which cannot tell
"never draws" apart from "not drawn yet at the cutoff".  This probe boots the
machine in real time (the kernel needs a wall clock; icount parks it), fires a
monitor ``screendump`` every ``--interval`` seconds, and measures the fraction
of non-black pixels in each snapshot.  It records, per sample, the elapsed wall
time, the framebuffer darkness, the serial line count, and the last IOKit
matching event seen so far.  It stops at the first visibly-drawn frame (or the
timeout) and writes a JSON timeline.

This makes "does this board reach a visible SpringBoard frame, and when" a
single repeatable command for both the iPod (N45AP) oracle and the iPhone
(M68AP) target, instead of an LLM-driven sequence of greps.

Example
-------
    python3 scripts/boot-frame-probe.py --board n45ap \
        --timeout 600 --interval 15 --logs /tmp/probe-n45ap

    python3 scripts/boot-frame-probe.py --board m68ap \
        --build 4A102 \
        --stabilize-root-domain \
        --timeout 600 --interval 15 --logs /tmp/probe-m68ap
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
DEFAULT_PLUGIN = REPO / "build-ipod11" / "contrib" / "plugins" / \
    "libm68ap-ftl-trace.dylib"
DEFAULT_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"

# S5L8900 panel geometry (both boards: 320x480, 32bpp).
FB_W, FB_H = 320, 480


def raw_nonzero_pct(path: Path) -> float:
    """Percent of non-zero bytes in a raw guest-RAM dump (-1 if unreadable)."""
    try:
        data = path.read_bytes()
    except OSError:
        return -1.0
    if not data:
        return -1.0
    return sum(1 for b in data if b) / len(data) * 100.0

# IOKit matching events, in the serial log, that mark boot progress.
IOKIT_EVENT_RE = re.compile(
    rb"(config\([^)]*\): (?:starting|stalling) on [^,\r\n]+"
    rb"|[A-Za-z0-9_]+::start\([^)]*\)(?: <\d+>)?"
    rb"|Registering:\s*\S+"
    rb"|BSD root:[^\r\n]+"
    rb"|launchd\[1\][^\r\n]*"
    rb"|[A-Za-z0-9_]*[Ss]pringBoard[^\r\n]*)")


def measure_ppm_nonzero(path: Path) -> tuple[int, int, float]:
    """Return (width, height, percent-of-bytes-nonzero) for a P6 PPM."""
    try:
        raw = path.read_bytes()
    except OSError:
        return (0, 0, -1.0)
    if not raw.startswith(b"P6"):
        return (0, 0, -1.0)
    # Parse the P6 header: magic, width, height, maxval, then binary data.
    fields: list[bytes] = []
    i = 2
    while len(fields) < 3 and i < len(raw):
        while i < len(raw) and raw[i] in b" \t\n\r":
            i += 1
        if i < len(raw) and raw[i:i + 1] == b"#":
            while i < len(raw) and raw[i] not in b"\n":
                i += 1
            continue
        start = i
        while i < len(raw) and raw[i] not in b" \t\n\r":
            i += 1
        fields.append(raw[start:i])
    if len(fields) < 3:
        return (0, 0, -1.0)
    i += 1  # single whitespace after maxval
    try:
        width, height = int(fields[0]), int(fields[1])
    except ValueError:
        return (0, 0, -1.0)
    data = raw[i:]
    if not data:
        return (width, height, 0.0)
    nonzero = sum(1 for b in data if b)
    return (width, height, nonzero / len(data) * 100.0)


def last_iokit_event(serial: Path) -> tuple[int, str]:
    """Return (serial_line_count, last IOKit matching event text)."""
    try:
        raw = serial.read_bytes()
    except OSError:
        return (0, "")
    lines = raw.count(b"\n")
    matches = IOKIT_EVENT_RE.findall(raw)
    last = matches[-1].decode("latin-1").strip() if matches else ""
    return (lines, last)


def build_command(args, serial: Path, trace_log: Path, observer_log: Path,
                  monitor_fifo: Path) -> list[str]:
    if args.board == "n45ap":
        machine = (f"iPod-Touch,bootrom={args.bootrom},"
                   f"iboot={args.iboot_n45ap},nand={args.staged_nand}")
        profile = "n45ap"
    else:
        machine = (f"iPhone-2G,bootrom={args.bootrom},"
                   f"iboot={args.iboot_m68ap},nand={args.staged_nand}")
        profile = "m68ap"
    serial_arg = ["-serial", "null"] if args.serial_null \
        else ["-serial", f"file:{serial}"]
    cmd = [str(args.qemu), "-M", machine, "-m", "1G",
           "-pflash", str(args.staged_nor),
           "-L", str(APP / "Resources" / "pc-bios"),
           "-display", "none", *serial_arg,
           "-monitor", "stdio"]
    # Real time: the kernel needs a wall clock, so we deliberately omit
    # -icount (any icount shift parks the kernel in iBoot's UART loop).
    if args.observer_plugin and args.observer_plugin.is_file():
        spec = (f"{args.observer_plugin},profile={profile},"
                f"service-observer-only=true,trace-details=false,"
                f"log={observer_log}")
        if args.stabilize_root_domain:
            spec += ",stabilize-root-domain=true"
        cmd.extend(["-plugin", spec, "-d", "plugin", "-D", str(trace_log)])
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=("m68ap", "n45ap"), required=True)
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--bootrom", type=Path, default=DEFAULT_BOOTROM)
    ap.add_argument("--iboot-n45ap", type=Path,
                    default=IPOD_FILES / "iboot_204_n45ap.bin")
    ap.add_argument("--nor-n45ap", type=Path,
                    default=IPOD_FILES / "nor_n45ap.bin")
    ap.add_argument("--nand-n45ap", type=Path, default=IPOD_FILES / "nand")
    ap.add_argument("--iboot-m68ap", type=Path)
    ap.add_argument("--nor-m68ap", type=Path)
    ap.add_argument("--nand-m68ap", type=Path)
    ap.add_argument("--stabilize-root-domain", action="store_true",
                    help="M68AP only: apply the diagnostic root-domain retain.")
    ap.add_argument("--observer-plugin", type=Path, default=DEFAULT_PLUGIN)
    ap.add_argument("--no-observer", action="store_true")
    ap.add_argument("--serial-null", action="store_true",
                    help="boot like the shipping app (-serial null): no serial "
                    "capture, fastest boot for a pure display-present test.")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--visible-threshold", type=float, default=1.0,
                    help="percent of non-black bytes that counts as 'drawn'.")
    ap.add_argument("--stop-on-frame", action="store_true",
                    help="stop as soon as a visible frame is detected.")
    ap.add_argument("--logs", type=Path, required=True)
    args = ap.parse_args()

    if args.no_observer:
        args.observer_plugin = None
    if args.board == "m68ap":
        for name in ("iboot_m68ap", "nor_m68ap", "nand_m68ap"):
            if getattr(args, name) is None:
                ap.error(f"--{name.replace('_', '-')} is required for m68ap")

    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    stage.mkdir(exist_ok=True)
    frames = args.logs / "frames"
    frames.mkdir(exist_ok=True)

    # Stage writable NAND (APFS clone) + NOR.
    if args.board == "n45ap":
        src_nand, src_nor = args.nand_n45ap, args.nor_n45ap
    else:
        src_nand, src_nor = args.nand_m68ap, args.nor_m68ap
    args.staged_nand = stage / "nand"
    if args.staged_nand.exists():
        shutil.rmtree(args.staged_nand)
    subprocess.run(["cp", "-Rc", str(src_nand), str(args.staged_nand)],
                   check=True)
    for bank in range(8):
        (args.staged_nand / f"bank{bank}").mkdir(exist_ok=True)
    args.staged_nor = stage / "nor.bin"
    shutil.copy2(src_nor, args.staged_nor)

    serial = args.logs / "serial.log"
    serial.write_bytes(b"")
    trace_log = args.logs / "trace.log"
    observer_log = args.logs / "observer.log"
    cmd = build_command(args, serial, trace_log, observer_log,
                        args.logs / "monitor.fifo")

    (args.logs / "command.txt").write_text(" ".join(cmd) + "\n")
    mon_out = (args.logs / "monitor.log").open("wb")
    err_out = (args.logs / "stderr.log").open("wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=mon_out,
                            stderr=err_out)

    timeline: list[dict] = []
    first_frame: dict | None = None
    start = time.time()
    deadline = start + args.timeout
    try:
        while time.time() < deadline and proc.poll() is None:
            time.sleep(args.interval)
            elapsed = round(time.time() - start, 1)
            shot = frames / f"t{int(elapsed):04d}.ppm"
            try:
                assert proc.stdin is not None
                proc.stdin.write(f"screendump {shot}\n".encode())
                # Also dump the two kernel framebuffer bases directly from
                # guest RAM.  SpringBoard/CoreSurface render there; the LCD
                # only shows them if register w1_framebuffer_base points at
                # one of them.  Dumping RAM separates "did the OS render" from
                # "is the scanout base wired to the rendered buffer".
                k1 = frames / f"t{int(elapsed):04d}.k1.raw"
                k2 = frames / f"t{int(elapsed):04d}.k2.raw"
                fb_bytes = FB_W * FB_H * 4
                proc.stdin.write(
                    f"pmemsave 0x0f400000 {fb_bytes} {k1}\n".encode())
                proc.stdin.write(
                    f"pmemsave 0x0f496000 {fb_bytes} {k2}\n".encode())
                proc.stdin.flush()
            except (OSError, BrokenPipeError):
                break
            time.sleep(0.6)  # let QEMU write the files
            w, h, nz = measure_ppm_nonzero(shot)
            k1_nz = raw_nonzero_pct(k1)
            k2_nz = raw_nonzero_pct(k2)
            lines, last = last_iokit_event(serial)
            sample = {"elapsed_s": elapsed, "fb_nonzero_pct": round(nz, 3),
                      "kfb_0f400000_nonzero_pct": round(k1_nz, 3),
                      "kfb_0f496000_nonzero_pct": round(k2_nz, 3),
                      "width": w, "height": h, "serial_lines": lines,
                      "last_iokit_event": last}
            for scratch in (k1, k2):  # 600 KB each; keep the timeline, not RAM
                try:
                    scratch.unlink()
                except OSError:
                    pass
            timeline.append(sample)
            print(json.dumps(sample), flush=True)
            if nz >= args.visible_threshold and first_frame is None:
                first_frame = sample
                if args.stop_on_frame:
                    break
    finally:
        if proc.poll() is None:
            try:
                assert proc.stdin is not None
                final = args.logs / "final.ppm"
                proc.stdin.write(f"stop\nscreendump {final}\n".encode())
                proc.stdin.flush()
                time.sleep(0.4)
            except (OSError, BrokenPipeError):
                pass
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        mon_out.close()
        err_out.close()

    lines, last = last_iokit_event(serial)
    final_nz = measure_ppm_nonzero(args.logs / "final.ppm")[2]
    summary = {
        "board": args.board,
        "timeout_s": args.timeout,
        "interval_s": args.interval,
        "reached_visible_frame": first_frame is not None,
        "first_frame": first_frame,
        "final_fb_nonzero_pct": round(final_nz, 3),
        "final_serial_lines": lines,
        "final_iokit_event": last,
        "serial": str(serial),
        "timeline": timeline,
    }
    (args.logs / "probe.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n=== SUMMARY ===")
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "timeline"}, indent=2))
    print(f"\nreport: {args.logs / 'probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
