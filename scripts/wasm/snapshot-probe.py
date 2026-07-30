#!/usr/bin/env python3
"""Does a post-boot snapshot of the iPhone 2G actually restore?

The browser port wants to skip the cold boot by shipping a machine that is
already at the home screen (BROWSER_WASM_SESSION_A.md, A3).  QEMU 11.0.2 has
``file:`` migration, so mechanically ``migrate file:...`` / ``-incoming
file:...`` is available.  Whether it RESTORES anything usable is a different
question: not one of the 26 ``hw/arm/ipod_touch*.c`` device models defines a
VMStateDescription, so migration saves guest RAM and the CPU but nothing about
the LCD's window bases, the NAND/FTL controller, the PMU, the VIC or the
multitouch device.

This script answers it by framebuffer, which is the only honest verification
here -- SpringBoard never announces itself on serial:

  1. boot natively to the home screen and record the non-black percentage of
     the kernel framebuffer AND the scanout;
  2. stop, migrate to a file, quit;
  3. relaunch with -incoming, cont, and take the same measurements.

A restore that works looks like step 3 matching step 1.  A restore that has
lost device state characteristically shows RAM intact (the kernel framebuffer
still holds the rendered home screen) while the scanout is black, because
``w1_framebuffer_base`` came back as zero.  That distinction is the whole point
of measuring both.

Example:
    python3 scripts/wasm/snapshot-probe.py --build 1A543a \\
        --boot-wait 420 --logs /tmp/snap-1A543a
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import m68ap_paths  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_QEMU = REPO / "build-ipod11" / "qemu-system-arm"
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))

FB_W, FB_H, FB_BPP = 320, 480, 4
FB_SIZE = FB_W * FB_H * FB_BPP
BASES = {"iboot_0x0fe00000": 0x0fe00000,
         "kernel_0x0f400000": 0x0f400000,
         "kernel_0x0f496000": 0x0f496000}

# fb-snapshot.py is not an importable module name (the hyphen), and copying its
# QMP client here would be a second thing to keep in step. Load it by path.
def _load_fb_snapshot():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fb_snapshot", REPO / "scripts" / "fb-snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load_fb_snapshot()
QMP = fb.QMP
measure = fb.measure


def wake(qmp, settle: float = 6.0) -> None:
    """Press Home and let the panel come back.

    Left alone at the home screen the guest auto-locks and the PMU powers the
    panel off ("[LCD] PMU powered panel off"), so a long --boot-wait measures a
    SLEEPING device and reports ~0% non-black -- which looks exactly like a boot
    that never rendered. Pressing Home first is what a user does, and it makes
    the before/after comparison about the snapshot rather than about how long
    the sampler happened to wait.
    """
    qmp.execute("send-key", keys=[{"type": "qcode", "data": "h"}])
    time.sleep(settle)


def tap(qmp, px: int, py: int, hold: float = 0.30) -> None:
    """One finger down/up at a panel pixel, over the absolute pointer.

    The point of tapping AFTER a restore is that it exercises everything a
    snapshot has to bring back but the framebuffer cannot show: the multitouch
    device, the SPI path, the ATN GPIO in sysic, the interrupt controller's
    masks, and the timers that drive all of it. A live panel proves the LCD came
    back; only a tap that changes the screen proves the machine did.
    """
    qmp.execute("input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": int(px / FB_W * 32768)}},
        {"type": "abs", "data": {"axis": "y", "value": int(py / FB_H * 32768)}}])
    qmp.execute("input-send-event", events=[
        {"type": "btn", "data": {"down": True, "button": "left"}}])
    time.sleep(hold)
    qmp.execute("input-send-event", events=[
        {"type": "btn", "data": {"down": False, "button": "left"}}])


def sample(qmp, out: Path, tag: str) -> dict:
    """Scanout plus each candidate framebuffer base, as non-black percentages."""
    out.mkdir(parents=True, exist_ok=True)
    shot = out / f"{tag}_screenout.ppm"
    qmp.execute("screendump", filename=str(shot))
    result = {"screenout_nonzero_pct": round(measure(shot)[0], 3), "bases": {}}
    for name, addr in BASES.items():
        raw = out / f"{tag}_{name}.raw"
        qmp.execute("pmemsave", val=addr, size=FB_SIZE, filename=str(raw))
        result["bases"][name] = round(measure(raw)[0], 3)
    return result


def launch(qemu: Path, machine: str, nor: Path, serial: Path, sock: str,
           logs: Path, tag: str, incoming: str | None) -> subprocess.Popen:
    cmd = [str(qemu), "-M", machine, "-m", "1G",
           "-pflash", str(nor),
           "-L", str(APP / "Resources" / "pc-bios"),
           "-display", "none", "-serial", f"file:{serial}",
           # The standing constraint: without it the guest takes timeout paths
           # and panics, natively as well as in the browser.
           "-icount", "shift=1",
           # No user networking. The native build links slirp and registers a
           # "slirp" savevm section; the WebAssembly build has no slirp at all,
           # so a stream captured with networking on fails to load there with
           #
           #   Unknown section or instance 'slirp' 0
           #
           # A migration stream is only portable between hosts whose DEVICE SETS
           # match, and slirp is the first place the two builds differ. The page
           # passes no -net either, so this makes both sides consistent.
           "-net", "none",
           "-qmp", f"unix:{sock},server,nowait"]
    if incoming:
        cmd += ["-incoming", incoming]
    (logs / f"command-{tag}.txt").write_text(" ".join(cmd) + "\n")
    return subprocess.Popen(
        cmd, stdout=(logs / f"monitor-{tag}.log").open("wb"),
        stderr=(logs / f"stderr-{tag}.log").open("wb"))


def wait_migration(qmp, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = qmp.execute("query-migrate")
        info = r.get("return", {})
        status = info.get("status")
        if status in ("completed", "failed", "cancelled"):
            return info
        time.sleep(0.5)
    return {"status": "timeout"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    ap.add_argument("--boot-wait", type=int, default=420)
    ap.add_argument("--resume-wait", type=int, default=20,
                    help="seconds to let the restored machine run before "
                         "measuring; the panel refreshes at 10 Hz so a few "
                         "seconds is plenty when it works at all")
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--no-wake", action="store_true",
                    help="do not press Home before sampling; the guest "
                         "auto-locks, so a long --boot-wait then measures a "
                         "sleeping panel")
    ap.add_argument("--downtime-ms", type=int, default=600000,
                    help="migration downtime limit; must exceed the transfer "
                         "so it converges in one stop-and-copy pass")
    ap.add_argument("--stop-first", action="store_true",
                    help="stop the guest before migrating. The resulting state "
                         "restores PAUSED and needs a monitor `cont`, so it is "
                         "unusable in the browser; off by default")
    ap.add_argument("--tap-after", default=None,
                    help="after restoring, tap this panel pixel ('x,y') and "
                         "report whether the screen changed -- the only check "
                         "that the restored MACHINE works, not just its panel. "
                         "Use a coordinate known to launch an app; icon row 1 "
                         "(y~67) never registers on 1.0")
    ap.add_argument("--keep-state", action="store_true",
                    help="do not delete the migration file afterwards")
    m68ap_paths.add_build_argument(ap, required=True)
    args = ap.parse_args()

    paths = m68ap_paths.get(args.build)
    paths.require("iboot_sb", "nor", "nand")
    print(f"[snapshot-probe] {m68ap_paths.describe(args.build)}")

    args.logs.mkdir(parents=True, exist_ok=True)
    stage = args.logs / "stage"
    stage.mkdir(exist_ok=True)

    staged_nand = stage / "nand"
    if staged_nand.exists():
        shutil.rmtree(staged_nand)
    subprocess.run(["cp", "-Rc", str(paths.nand), str(staged_nand)], check=True)
    for bank in range(8):
        (staged_nand / f"bank{bank}").mkdir(exist_ok=True)
    staged_nor = stage / "nor.bin"
    shutil.copy2(paths.nor, staged_nor)

    machine = (f"iPhone-2G,bootrom={paths.bootrom},iboot={paths.iboot_sb},"
               f"nand={staged_nand},epoch={paths.epoch}")

    # AF_UNIX sun_path is ~104 bytes on macOS and /tmp gets swept during long
    # runs, both already-paid-for lessons from fb-snapshot.py.
    sock_dir = "/var/tmp" if os.path.isdir("/var/tmp") else "/tmp"
    state = Path(sock_dir) / f"m68ap-snap-{os.getpid()}.state"
    report: dict = {"build": args.build, "state_file": str(state)}

    # ---------------------------------------------------------------- save --
    sock_a = f"{sock_dir}/snapprobe-a-{os.getpid()}.sock"
    for p in (sock_a,):
        if os.path.exists(p):
            os.unlink(p)
    proc = launch(args.qemu, machine, staged_nor, args.logs / "serial-boot.log",
                  sock_a, args.logs, "boot", None)
    try:
        print(f"booting for {args.boot_wait}s ...", flush=True)
        time.sleep(args.boot_wait)
        qmp = QMP(sock_a)
        if not args.no_wake:
            wake(qmp)
        report["before"] = sample(qmp, args.logs / "frames", "before")
        print("before:", json.dumps(report["before"]), flush=True)

        # Deliberately NOT stopping the guest first.
        #
        # QEMU records the SOURCE's runstate in the migration stream's global
        # state section, and on the destination
        # process_incoming_migration_co() only calls vm_start() when that says
        # "running". Snapshot a stopped guest and every restore comes up PAUSED
        # and needs a `cont` over the monitor -- which is fine here but
        # impossible in the browser, where the page has no QMP socket at all.
        #
        # Migrating a live guest converges and stops the source itself, so the
        # state is just as consistent and the destination auto-starts.
        if args.stop_first:
            qmp.execute("stop")
        else:
            # Converge in ONE pass. A live migration of this guest does not
            # converge on its own: under -icount it keeps dirtying pages while
            # the transfer proceeds, and a first attempt timed out mid-stream and
            # left a TRUNCATED file -- which fails on restore with
            # "check_section_footer: Read section footer failed", not with
            # anything that points at the cause.
            #
            # A downtime limit larger than the transfer makes QEMU decide it may
            # stop and copy immediately, so the stream is complete AND the source
            # is recorded as running.
            qmp.execute("migrate-set-parameters",
                        downtime_limit=args.downtime_ms)
        print(f"migrating to {state} ...", flush=True)
        r = qmp.execute("migrate", uri=f"file:{state}")
        if "error" in r:
            report["migrate_error"] = r["error"]
            print("migrate rejected:", r["error"], flush=True)
        else:
            report["migrate"] = wait_migration(qmp)
            print("migrate:", json.dumps(report["migrate"]), flush=True)
        qmp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    if state.exists():
        report["state_bytes"] = state.stat().st_size
        print(f"state file: {report['state_bytes'] / 1048576:.1f} MiB",
              flush=True)
    else:
        report["state_bytes"] = 0
        print("no state file was written", flush=True)
        (args.logs / "report.json").write_text(json.dumps(report, indent=2))
        return 1

    # ------------------------------------------------------------- restore --
    sock_b = f"{sock_dir}/snapprobe-b-{os.getpid()}.sock"
    if os.path.exists(sock_b):
        os.unlink(sock_b)
    proc = launch(args.qemu, machine, staged_nor,
                  args.logs / "serial-resume.log", sock_b, args.logs, "resume",
                  f"file:{state}")
    try:
        qmp = QMP(sock_b, wait=60)
        # -incoming loads the stream and leaves the machine stopped.
        report["resume_status"] = qmp.execute("query-status").get("return")
        qmp.execute("cont")
        print(f"resumed; running {args.resume_wait}s ...", flush=True)
        time.sleep(args.resume_wait)
        # Sample BEFORE waking as well: a restore that lost device state shows
        # guest RAM intact but a black scanout, and pressing Home first would
        # hide exactly that.
        report["after"] = sample(qmp, args.logs / "frames", "after")
        if not args.no_wake:
            wake(qmp)
            report["after_wake"] = sample(qmp, args.logs / "frames",
                                          "after_wake")
            print("after_wake:", json.dumps(report["after_wake"]), flush=True)
        if args.tap_after:
            px, py = (int(v) for v in args.tap_after.split(","))
            before_tap = report.get("after_wake") or report["after"]
            tap(qmp, px, py)
            time.sleep(20)
            report["after_tap"] = sample(qmp, args.logs / "frames", "after_tap")
            a = before_tap["screenout_nonzero_pct"]
            b = report["after_tap"]["screenout_nonzero_pct"]
            report["tap_changed_pct"] = round(abs(b - a), 3)
            report["tap_interactive"] = report["tap_changed_pct"] >= 1.0
            print(f"after_tap: {b}% (was {a}%) "
                  f"interactive={report['tap_interactive']}", flush=True)
        print("after:", json.dumps(report["after"]), flush=True)
        qmp.close()
    except Exception as exc:                       # noqa: BLE001
        report["resume_error"] = repr(exc)
        print("resume failed:", exc, flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not args.keep_state and state.exists():
        state.unlink()

    (args.logs / "report.json").write_text(json.dumps(report, indent=2))

    before = report.get("before", {})
    after = report.get("after", {})
    print("\n=== verdict ===")
    print(f"  scanout   before {before.get('screenout_nonzero_pct')}%"
          f"  after {after.get('screenout_nonzero_pct')}%")
    for name in BASES:
        print(f"  {name:20s} before {before.get('bases', {}).get(name)}%"
              f"  after {after.get('bases', {}).get(name)}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
