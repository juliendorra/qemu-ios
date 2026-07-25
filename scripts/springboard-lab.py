#!/usr/bin/env python3
"""Parallel, self-judging SpringBoard/display bring-up experiments.

Same idea as `scripts/baseband-lab.py`, aimed at a different wall: M68AP
reaches SpringBoard, is [Activated] and registered, and STILL never programs a
framebuffer base -- the guest parks in the kernel wait-for-interrupt idle loop,
i.e. SpringBoard is blocked on an event that never arrives. Finding what it
waits on means comparing many boot configurations, and the only fast way is to
run them concurrently and let the harness judge each one.

What it does
------------
Boots one QEMU per VARIANT (M68AP configurations + an N45AP control that is
known to render), watches each until a decisive verdict, then collects the
evidence that discriminates the hypotheses:

  verdicts
    rendered   an LCD window base was programmed to a kernel framebuffer
               (0x0f400000 / 0x0f496000) or a framebuffer sampled non-black
    wedged     serial stopped growing AND every PC sample sits in the kernel
               idle loop -> a blocked thread, not a crawl
    crawling   serial stopped growing but PCs are spread -> still executing
    panicked   a kernel panic appeared
    timeout    --max-wall reached

  evidence per instance
    pc_histogram        where the guest actually is (idle vs spinning)
    lcd_bases           every window base the guest programmed
    framebuffers        non-black %% of the three candidate FB bases
    phase               last recognised boot marker (how far it got)
    springboard_lines   SpringBoard's own log lines
    driver_tail         the last IOKit attach/registration lines before the wedge
    markers             counted regexes (activation, registration, ...)

Then `--diff A=B` prints the driver/service tokens present in one instance's
post-SpringBoard serial and absent in the other's -- the differential that
points at the missing event (e.g. N45AP-renders vs M68AP-wedges).

Variants are declared in VARIANTS below (name -> board + env + artifact knobs)
so hypotheses are data, not code edits. NAND trees are built once per unique
artifact recipe and cached in the logs dir.

Example
-------
  python3 scripts/springboard-lab.py --logs /tmp/sblab \
      --variants n45ap-control m68ap-full m68ap-nobb m68ap-plain \
      --diff m68ap-full=n45ap-control

Host contention note: simultaneous boots perturb the USB-start window and can
flip the IOIpodUSBDevice::start race (see the baseband lab). Instances are
staggered by --stagger-secs (default 30).
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab_workspace import (NAND_TREE_BYTES, ROOT_HFS_BYTES, DATA_HFS_BYTES,
                           Workspace, attached, human, prune_runs,
                           require_free_bytes)

REPO = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("IPOD_APP", "/Applications/iPod Touch.app/Contents"))
IPOD_FILES = APP / "Resources" / "ipod_files"
PC_BIOS = APP / "Resources" / "pc-bios"
QEMU = REPO / "build-ipod11" / "qemu-system-arm"
M68_BOOTROM = REPO / "m68ap-artifacts" / "appdbg" / "bootrom_s5l8900"
M68_IBOOT = REPO / "m68ap-artifacts" / "stage" / "iboot_204_m68ap_sbpatch.bin"
M68_NOR = REPO / "m68ap-artifacts" / "stage" / "nor_m68ap.bin"
M68_ROOT_HFS = REPO / "m68ap-artifacts" / "stage" / "filesystem-m68ap-readonly.img"
M68_DATA_DMG = REPO / "m68ap-artifacts" / "stage" / "data-m68ap.dmg"

KERNEL_FB_BASES = (0x0F400000, 0x0F496000)
FB_BASES = {"iboot_0x0fe00000": 0x0FE00000,
            "kernel_0x0f400000": 0x0F400000,
            "kernel_0x0f496000": 0x0F496000}
# The kernel idle loop (wait-for-interrupt); PCs here mean "nothing to run".
IDLE_PCS = {"c005a9cc", "c005a9c4", "c005a9c8", "c005a9d0"}

FB_W, FB_H = 320, 480


def classify_screen(d: bytes) -> dict:
    """Tell a HOME screen from a SETUP/activation screen, from raw BGRA pixels.

    Rendering is no longer the question -- WHICH screen renders is. Non-black
    %% cannot answer it (the activation screen with carrier chrome is *more*
    lit than the home screen). Two features separate them cleanly, calibrated
    on four measured frames:

        frame                      colorful%%   dock%%
        N45AP home screen             10.7      81.2
        M68AP setup (no telephony)     1.8       3.8
        M68AP activation (telephony)   1.8      53.4

    App icons are saturated; the setup screens are white/grey artwork on
    black. So `colorful` (max-min channel > 60) is the discriminator, and the
    dock band is a corroborating signal.
    """
    def band(y0, y1):
        nb = col = tot = 0
        for y in range(y0, min(y1, FB_H)):
            row = (y * FB_W) * 4
            for x in range(0, FB_W, 2):
                i = row + x * 4
                b, g, r = d[i], d[i + 1], d[i + 2]
                tot += 1
                if r > 16 or g > 16 or b > 16:
                    nb += 1
                if max(r, g, b) - min(r, g, b) > 60:
                    col += 1
        return (100.0 * nb / tot, 100.0 * col / tot) if tot else (0.0, 0.0)

    all_nb, all_col = band(0, FB_H)
    dock_nb, _ = band(400, 470)
    if all_col >= 5.0 and dock_nb >= 60.0:
        kind = "home"
    elif all_nb >= 2.0:
        kind = "setup"          # activation / connect-to-iTunes / alert
    else:
        kind = "blank"
    return {"kind": kind, "colorful_pct": round(all_col, 1),
            "dock_pct": round(dock_nb, 1), "nonblack_pct": round(all_nb, 1)}

MARKERS = {
    "panic": rb"panic\(cpu",
    "springboard": rb"SpringBoard\[",
    "activated": rb"device is: \[Activated\]",
    "unactivated_flip": rb"activation state to Unactivated",
    "everregistered_missing": rb"didn't have a EverRegistered",
    "registered": rb"previously registered",
    "coresurface": rb"IOCoreSurfaceRootUserClient::attach",
    "mobilefb": rb"IOMobileFramebufferUserClient::attach",
    "multitouch": rb"AppleMultitouch",
    "usb_ready": rb"ready to start usb stack",
    "configuring_sb": rb"Configuring SpringBoard",
}
# Ordered boot phases; the last one seen is the instance's "phase".
PHASES = [
    ("iboot", rb"iBoot version"),
    ("kernel", rb"Darwin Kernel Version|BSD root"),
    ("launchd", rb"launchd\[1\]: BOOT_TIME"),
    ("springboard_start", rb"SpringBoard\["),
    ("activation_checked", rb"device is: \[(Un)?Activated\]"),
    ("registration_checked", rb"previously registered|EverRegistered"),
    ("fb_attached", rb"IOMobileFramebufferUserClient::attach"),
    ("coresurface", rb"IOCoreSurfaceRootUserClient::attach"),
    ("configuring", rb"Configuring SpringBoard"),
]

# --- launch-daemon pruning ------------------------------------------------
# N45AP's devos50 NAND ships only SEVEN LaunchDaemons (AddressBook, CommCenter,
# SpringBoard, configd, mDNSResponder, lockdown, notifyd) and RENDERS, so
# SpringBoard's render path needs at most that set. M68AP's stock 1.1.4 root
# runs all twenty; these are the thirteen extras, removable per-variant to
# test whether one of them wedges SpringBoard (BTServer opens the bluetooth
# UART; iapd/usbptpd/coreaudiod touch hardware the emulator stubs).
PRUNE_SETS = {
    "all13": (
        "com.apple.BTServer.plist", "com.apple.DumpPanic.plist",
        "com.apple.SCHelper-embedded.plist", "com.apple.crashreporterd.plist",
        "com.apple.daily.plist", "com.apple.iapd.plist",
        "com.apple.mDNSResponderHelper.plist", "com.apple.mobile.lockbot.plist",
        "com.apple.securityd.plist", "com.apple.syslogd.plist",
        "com.apple.update.plist", "com.apple.usbptpd.plist",
        "coreaudiod.plist",
    ),
    # bisection halves: hardware-facing daemons vs system-service daemons
    "hw": (
        "com.apple.BTServer.plist", "com.apple.iapd.plist",
        "com.apple.usbptpd.plist", "coreaudiod.plist",
    ),
    "svc": (
        "com.apple.DumpPanic.plist", "com.apple.SCHelper-embedded.plist",
        "com.apple.crashreporterd.plist", "com.apple.daily.plist",
        "com.apple.mDNSResponderHelper.plist", "com.apple.mobile.lockbot.plist",
        "com.apple.securityd.plist", "com.apple.syslogd.plist",
        "com.apple.update.plist",
    ),
    # SpringBoard's first post-lockdown act is an IAP query ("Couldn't get
    # IAP TV out settings" fails fast on N45AP, which has NO iapd); M68AP has
    # iapd + the accessory stack, so the query may block instead of failing
    "iap": ("com.apple.iapd.plist",),
}

# --- hypothesis matrix ----------------------------------------------------
# board: n45ap | m68ap ; dataark/patch: M68AP artifact knobs ; env: extra env.
VARIANTS = {
    # the reference that DOES render
    "n45ap-control": dict(board="n45ap", env={}),
    # M68AP, everything we know how to give it
    "m68ap-full": dict(board="m68ap", dataark=True, patch=True,
                       env={"IT_M68AP_NO_BASEBAND": "1"}),
    # same but with the baseband stub attached
    "m68ap-full-bb": dict(board="m68ap", dataark=True, patch=True,
                          env={"IT_BASEBAND_H5": "1"}),
    # untouched artifacts: the [Unactivated] baseline
    "m68ap-plain": dict(board="m68ap", dataark=False, patch=False,
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    # data ark only (no binary patch)
    "m68ap-ark": dict(board="m68ap", dataark=True, patch=False,
                      env={"IT_M68AP_NO_BASEBAND": "1"}),
    # patch only (no data ark)
    "m68ap-patch": dict(board="m68ap", dataark=False, patch=True,
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- /var skeleton: does a populated data partition let SpringBoard render?
    "m68ap-var": dict(board="m68ap", dataark=True, patch=True,
                      var_skeleton="full",
                      env={"IT_M68AP_NO_BASEBAND": "1"}),
    "m68ap-var-nopatch": dict(board="m68ap", dataark=True, patch=False,
                              var_skeleton="minimal",
                              env={"IT_M68AP_NO_BASEBAND": "1"}),
    # only the strictly-needed dirs, to isolate what the full skeleton breaks
    "m68ap-varmin": dict(board="m68ap", dataark=True, patch=True,
                         var_skeleton="minimal",
                         env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- multitouch protocol: the QEMU model answers in Zephyr2 (iPod)
    # semantics even though the board is M68AP. The guest's Z1 driver will
    # see a broken dialogue; where the [MT] trace then stalls (vs m68ap-full)
    # localises how far SpringBoard's render path depends on the touch stack.
    "m68ap-z2": dict(board="m68ap", dataark=True, patch=True,
                     env={"IT_M68AP_NO_BASEBAND": "1", "IT_FORCE_MT_Z2": "1"}),
    # --- SpringBoard environment: N45AP's devos50 image (which RENDERS) sets
    # LK_ENABLE_MBX2D=0 in com.apple.SpringBoard.plist, forcing LayerKit to
    # software rendering; M68AP's stock plist does not, so its SpringBoard
    # composites via the PowerVR MBX -- which this emulator only stubs (a
    # do-nothing MMIO region). A SpringBoard blocked on a stubbed GPU is
    # exactly the observed quiet wait. This knob replicates the iPod's data
    # fix on the iPhone root.
    "m68ap-mbx": dict(board="m68ap", dataark=True, patch=True, sb_env="mbx2d",
                      env={"IT_M68AP_NO_BASEBAND": "1"}),
    # the render fix (TVOut window) + software compositing, WITH the H5
    # baseband stub attached: does a registered network get SpringBoard past
    # the "Searching..."/emergency activation screen onto the home screen?
    "m68ap-mbx-bb": dict(board="m68ap", dataark=True, patch=True,
                         sb_env="mbx2d", env={"IT_BASEBAND_H5": "1"}),
    # --- TELEPHONY GATE, data-only: drop `telephony` from M68AP's board
    # capability profile so GraphicsServices reports a non-phone device to
    # every consumer (see the `caps` block in build_m68ap_nand).
    "m68ap-notel": dict(board="m68ap", dataark=True, patch=True,
                        sb_env="mbx2d", caps="notel",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    # same, but the key stays present and false (consumer may test presence)
    "m68ap-notel-false": dict(board="m68ap", dataark=True, patch=True,
                              sb_env="mbx2d", caps="notel-false",
                              env={"IT_M68AP_NO_BASEBAND": "1"}),
    # telephony dropped AND a /var that SpringBoard can write its setup state
    # into: with the phone chrome gone the remaining screen is the
    # activation/"connect to iTunes" SETUP screen, and the iPod's device-dump
    # NAND has a populated /var (mobile's home + preferences) where ours is
    # empty. This pairs the two data fixes.
    "m68ap-notel-var": dict(board="m68ap", dataark=True, patch=True,
                            sb_env="mbx2d", caps="notel",
                            var_skeleton="minimal",
                            env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- SETUP gate: SpringBoard reads EverRegistered as a CFString and
    # rejects the integer our ark writes ("wasn't a string: <CFNumber 0>"),
    # so it treats the device as never registered -> connect-to-iTunes.
    # Supply a string; two plausible spellings, measured not guessed.
    "m68ap-everreg-yes": dict(board="m68ap", dataark=True, patch=True,
                              sb_env="mbx2d", ark_profile="everreg-yes",
                              env={"IT_M68AP_NO_BASEBAND": "1"}),
    "m68ap-everreg-1": dict(board="m68ap", dataark=True, patch=True,
                            sb_env="mbx2d", ark_profile="everreg-1",
                            env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- M68AP button idle levels. The device tree puts menu/volup/voldown/
    # ringer/hold on GPIO 0x1600/01/02/03/05 (port 0x16, bits 0/1/2/3/5) with
    # two different flag values (0x100 vs 0x000 = two polarities). The model
    # leaves the whole port at 0, and the guest samples it only twice before
    # switching to interrupts, so a pin that reads "pressed" at boot stays
    # pressed -- the stuck ringer/volume HUD. Masks are tested one bit at a
    # time because forcing the volume pair (0x6) PANICS the kernel early.
    "m68ap-gpio-volup": dict(board="m68ap", dataark=True, patch=True,
                             sb_env="mbx2d", ark_profile="reference-reg",
                             env={"IT_M68AP_NO_BASEBAND": "1",
                                  "IT_M68AP_GPIO_IDLE": "0x2"}),
    "m68ap-gpio-voldown": dict(board="m68ap", dataark=True, patch=True,
                               sb_env="mbx2d", ark_profile="reference-reg",
                               env={"IT_M68AP_NO_BASEBAND": "1",
                                    "IT_M68AP_GPIO_IDLE": "0x4"}),
    "m68ap-gpio-ringer": dict(board="m68ap", dataark=True, patch=True,
                              sb_env="mbx2d", ark_profile="reference-reg",
                              env={"IT_M68AP_NO_BASEBAND": "1",
                                   "IT_M68AP_GPIO_IDLE": "0x8"}),
    # THE REFERENCE ARK: key names/types read off the iPod's own shipped ark
    # (a device that reaches the home screen) -- booleans where we wrote
    # numbers, plus the international/SIM/timezone keys we never wrote at all.
    "m68ap-ref": dict(board="m68ap", dataark=True, patch=True,
                      sb_env="mbx2d", ark_profile="reference",
                      env={"IT_M68AP_NO_BASEBAND": "1"}),
    # same, with the phone chrome off as well
    "m68ap-ref-notel": dict(board="m68ap", dataark=True, patch=True,
                            sb_env="mbx2d", ark_profile="reference",
                            caps="notel",
                            env={"IT_M68AP_NO_BASEBAND": "1"}),
    # reference ark but EverRegistered TRUE: the reference is an iPod that
    # never registered; a phone that HAS registered says so, and this is the
    # value SpringBoard's telephony path is actually looking for.
    "m68ap-refreg": dict(board="m68ap", dataark=True, patch=True,
                         sb_env="mbx2d", ark_profile="reference-reg",
                         env={"IT_M68AP_NO_BASEBAND": "1"}),
    # same, plus drop UserName=mobile so SpringBoard runs as root like N45AP
    "m68ap-mbx-root": dict(board="m68ap", dataark=True, patch=True,
                           sb_env="mbx2d-root",
                           env={"IT_M68AP_NO_BASEBAND": "1"}),
    # isolation: ONLY drop UserName=mobile (no MBX env) — if mbx-root renders
    # and this does too, the whole story is the user, not the GPU
    "m68ap-root": dict(board="m68ap", dataark=True, patch=True, sb_env="root",
                       env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- launch-daemon pruning: reduce M68AP to N45AP's known-rendering set
    "m68ap-prune": dict(board="m68ap", dataark=True, patch=True, prune="all13",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    "m68ap-prune-hw": dict(board="m68ap", dataark=True, patch=True, prune="hw",
                           env={"IT_M68AP_NO_BASEBAND": "1"}),
    "m68ap-prune-svc": dict(board="m68ap", dataark=True, patch=True,
                            prune="svc",
                            env={"IT_M68AP_NO_BASEBAND": "1"}),
    "m68ap-prune-iap": dict(board="m68ap", dataark=True, patch=True,
                            prune="iap",
                            env={"IT_M68AP_NO_BASEBAND": "1"}),
    # --- activation DURABILITY matrix (can the binary patch be dropped?) ---
    # All of these are data-ark-only (patch=False). The question each answers:
    # does [Activated] SURVIVE determine_activation_state's boot re-validation
    # (markers.unactivated_flip == 0), using only lockdownd's data-driven
    # levers? If one holds, hacktivation becomes pure data, like the iPod.
    "ark-minimal": dict(board="m68ap", dataark=True, patch=False,
                        ark_profile="minimal",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-factory": dict(board="m68ap", dataark=True, patch=False,
                        ark_profile="factory",
                        env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-unactsvc": dict(board="m68ap", dataark=True, patch=False,
                         ark_profile="unactsvc",
                         env={"IT_M68AP_NO_BASEBAND": "1"}),
    "ark-all": dict(board="m68ap", dataark=True, patch=False,
                    ark_profile="all",
                    env={"IT_M68AP_NO_BASEBAND": "1"}),
}


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


class QMP:
    def __init__(self, path, timeout=10):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        self.s.connect(str(path))
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("qmp closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute, arguments=None):
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.s.sendall((json.dumps(msg) + "\n").encode())
        while True:
            r = self._read()
            if "return" in r or "error" in r:
                return r

    def hmp(self, line):
        return self.cmd("human-monitor-command",
                        {"command-line": line}).get("return", "")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def build_m68ap_nand(out: Path, dataark: bool, patch: bool, work: Path,
                     ark_profile: str = "minimal",
                     var_skeleton: bool = False,
                     prune: str = None,
                     sb_env: str = None,
                     caps: str = None) -> Path:
    """Build (and cache) an M68AP NAND for a given artifact recipe."""
    if out.exists() and (out / "bank0").exists():
        return out
    work.mkdir(parents=True, exist_ok=True)
    root = M68_ROOT_HFS
    if patch:
        root = work / "root-patched.img"
        if not root.exists():
            run([sys.executable, str(REPO / "scripts" / "hacktivate-m68ap.py"),
                 "patch", "--root-hfs", str(M68_ROOT_HFS), "--out", str(root)])
    if prune:
        pruned = work / f"root-prune-{prune}.img"
        if not pruned.exists():
            # keep the .img suffix: hdiutil types raw images by extension
            tmp = work / f"root-prune-{prune}.tmp.img"
            shutil.copy2(root, tmp)
            with attached(tmp, readonly=False) as mnt:
                for name in PRUNE_SETS[prune]:
                    victim = mnt / "System" / "Library" / "LaunchDaemons" / name
                    if not victim.exists():
                        raise RuntimeError(f"prune target missing: {victim}")
                    victim.unlink()
            tmp.rename(pruned)
        if root != M68_ROOT_HFS:
            root.unlink(missing_ok=True)
        root = pruned
    if caps:
        # The BOARD CAPABILITY PROFILE. GraphicsServices owns the capability
        # table (it exports GSSystemGetCapability and knows the key names
        # telephony/unifiedIPod/camera/...); its data source is
        # SpringBoard.app/<board>.plist. BOTH firmwares ship BOTH profiles --
        # the iPod's own 1.1.4 image contains M68AP.plist with telephony=true
        # -- so this is Apple's board table, selected at runtime, not a
        # per-device build. Editing M68AP's profile is therefore the vendor's
        # own mechanism for "this device is not a phone", applied at ONE
        # authority that every consumer reads, with no binary patched.
        #   notel       -- drop the telephony key (N45AP's profile shape)
        #   notel-false -- keep the key, set it false (in case a consumer
        #                  tests presence rather than truth)
        mutated = work / f"root-caps-{caps}.img"
        if not mutated.exists():
            tmp = work / f"root-caps-{caps}.tmp.img"
            shutil.copy2(root, tmp)
            with attached(tmp, readonly=False) as mnt:
                plist = (mnt / "System" / "Library" / "CoreServices" /
                         "SpringBoard.app" / "M68AP.plist")
                prof = plistlib.loads(plist.read_bytes())
                if caps == "notel":
                    prof["capabilities"].pop("telephony", None)
                elif caps == "notel-false":
                    prof["capabilities"]["telephony"] = False
                else:
                    raise RuntimeError(f"unknown caps profile {caps!r}")
                # the shipped file is a BINARY plist; keep the format (a
                # format change is exactly what broke the data ark once)
                plist.write_bytes(plistlib.dumps(prof,
                                                 fmt=plistlib.FMT_BINARY))
            tmp.rename(mutated)
        if root != M68_ROOT_HFS:
            root.unlink(missing_ok=True)
        root = mutated
    if sb_env:
        mutated = work / f"root-sbenv-{sb_env}.img"
        if not mutated.exists():
            # keep the .img suffix: hdiutil types raw images by extension
            tmp = work / f"root-sbenv-{sb_env}.tmp.img"
            shutil.copy2(root, tmp)
            with attached(tmp, readonly=False) as mnt:
                plist = (mnt / "System" / "Library" / "LaunchDaemons" /
                         "com.apple.SpringBoard.plist")
                job = plistlib.loads(plist.read_bytes())
                if sb_env in ("mbx2d", "mbx2d-root"):
                    job.setdefault("EnvironmentVariables",
                                   {})["LK_ENABLE_MBX2D"] = "0"
                if sb_env in ("mbx2d-root", "root"):
                    job.pop("UserName", None)
                plist.write_bytes(plistlib.dumps(job,
                                                 fmt=plistlib.FMT_BINARY))
            tmp.rename(mutated)
        # the chained-from root is an intermediate; disk is the scarce
        # resource here (a filled volume once killed a whole session)
        if root != M68_ROOT_HFS:
            root.unlink(missing_ok=True)
        root = mutated
    data = M68_DATA_DMG
    if var_skeleton:
        # A populated /var: the generated NAND otherwise ships an EMPTY data
        # partition (no /var/mobile for SpringBoard's `mobile` user, no
        # preferences for configd), which N45AP's device-dump NAND has.
        data = work / "data-var.img"
        if not data.exists():
            ark = work / "data_ark.plist"
            run([sys.executable, str(REPO / "scripts" / "hacktivate-m68ap.py"),
                 "build-dataark", "--out", str(ark), "--profile", ark_profile])
            cmd = [sys.executable, str(REPO / "scripts" / "build-m68ap-var.py"),
                   "--out", str(data), "--data-ark", str(ark)]
            if var_skeleton == "full":
                cmd.append("--full")
            run(cmd)
    elif dataark:
        data = work / "data-ark.img"
        if not data.exists():
            ark = work / "data_ark.plist"
            run([sys.executable, str(REPO / "scripts" / "hacktivate-m68ap.py"),
                 "build-dataark", "--out", str(ark), "--profile", ark_profile])
            size = M68_DATA_DMG.stat().st_size
            dmg = work / "data.dmg"
            if dmg.exists():
                dmg.unlink()
            run(["hdiutil", "create", "-sectors", str(size // 512), "-fs",
                 "Case-sensitive HFS+", "-volname", "var", "-layout", "NONE",
                 "-o", str(dmg)])
            run([sys.executable, str(REPO / "scripts" / "inject-guest-file.py"),
                 "--image", str(dmg), "--src", str(ark),
                 "--dest", "/root/Library/Lockdown/data_ark.plist"])
            cdr = work / "data-raw"
            run(["hdiutil", "convert", str(dmg), "-format", "UDTO",
                 "-o", str(cdr)])
            shutil.move(str(work / "data-raw.cdr"), str(data))
    run([sys.executable, str(REPO / "scripts" / "build-m68ap-nand.py"),
         "--out", str(out), "--signature", "m68ap", "--active-banks", "4",
         "--bbt", "production", "--hfs", str(root), "--data-hfs", str(data),
         "--device", "iPhone1,1", "--ipsw-build", "4A102"])
    # the NAND embeds both filesystems; drop the intermediate images so a
    # multi-recipe matrix peaks at one root image, not one per recipe
    if root != M68_ROOT_HFS:
        root.unlink(missing_ok=True)
    if data != M68_DATA_DMG:
        data.unlink(missing_ok=True)
    return out


class Instance:
    def __init__(self, name, spec, args, cache: Path):
        self.name = name
        self.spec = spec
        self.args = args
        self.cache = cache
        self.dir = args.logs / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.serial = self.dir / "serial.log"
        self.stderr = self.dir / "stderr.log"
        self.qmp_path = Path(f"/tmp/sblab-{os.getpid()}-{name}.qmp")
        self.proc = None
        self._best_fb_label = None
        self.screen = None
        self.result = {"name": name, "spec": {k: v for k, v in spec.items()
                                              if k != "env"}}

    def recipe(self):
        profile = self.spec.get("ark_profile", "minimal")
        return (f"m68ap-ark{int(self.spec.get('dataark', False))}"
                f"-{profile}-patch{int(self.spec.get('patch', False))}"
                f"-var{self.spec.get('var_skeleton', False)}"
                f"-prune{self.spec.get('prune') or 'none'}"
                f"-sbenv{self.spec.get('sb_env') or 'none'}"
                f"-caps{self.spec.get('caps') or 'none'}")

    def prepare(self):
        """Build this variant's NAND recipe into the shared cache.

        Called SERIALLY from main before any instance boots: concurrent NAND
        builds (148k page writes each) plus concurrent hdiutil create/attach
        starve the host badly enough to error some builds and stall the boots
        that do start (observed: two errors + two timeouts at kernel/launchd).
        Preparation is the expensive, I/O-bound part; only the boots need to
        run in parallel.
        """
        if self.spec["board"] != "m68ap":
            return
        shared = self.cache / self.recipe()
        nand = shared / "nand"
        if nand.exists() and not (nand / "bank0").exists():
            shutil.rmtree(nand)          # scrub a half-built cache entry
        if not nand.exists():
            print(f"[{self.name}] building NAND recipe {self.recipe()}",
                  flush=True)
        build_m68ap_nand(nand, self.spec.get("dataark", False),
                         self.spec.get("patch", False), shared,
                         self.spec.get("ark_profile", "minimal"),
                         self.spec.get("var_skeleton", False),
                         self.spec.get("prune"),
                         self.spec.get("sb_env"),
                         self.spec.get("caps"))

    def stage(self):
        board = self.spec["board"]
        stage = self.dir / "stage"
        stage.mkdir(exist_ok=True)
        if board == "n45ap":
            nand = stage / "nand"
            if not nand.exists():
                run(["cp", "-Rc", str(IPOD_FILES / "nand"), str(nand)])
            nor = stage / "nor.bin"
            shutil.copy2(IPOD_FILES / "nor_n45ap.bin", nor)
            return (M68_BOOTROM, IPOD_FILES / "iboot_204_n45ap.bin", nand, nor,
                    "iPod-Touch")
        shared = self.cache / self.recipe()
        self.prepare()                    # no-op if already cached
        nand = stage / "nand"
        if nand.exists():
            shutil.rmtree(nand)
        run(["cp", "-Rc", str(shared / "nand"), str(nand)])
        nor = stage / "nor.bin"
        shutil.copy2(M68_NOR, nor)
        return (M68_BOOTROM, M68_IBOOT, nand, nor, "iPhone-2G")

    def launch(self):
        bootrom, iboot, nand, nor, machine = self.stage()
        cmd = [str(self.args.qemu),
               "-M", f"{machine},bootrom={bootrom},iboot={iboot},nand={nand}",
               "-m", "1G", "-pflash", str(nor), "-L", str(PC_BIOS),
               "-display", "none",
               "-serial", f"file:{self.serial}",
               "-qmp", f"unix:{self.qmp_path},server,nowait"]
        env = dict(os.environ)
        env["IT_LCD_TRACE"] = "1"
        env["IT_FB_TRACE"] = "1"   # full LCD MMIO + panel conversation
        env["IT_MT_TRACE"] = "1"   # multitouch dialogue + firmware state
        env.update(self.spec.get("env", {}))
        (self.dir / "command.txt").write_text(
            " ".join(cmd) + "\n# env: " +
            json.dumps(self.spec.get("env", {})) + "\n")
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=self.stderr.open("wb"),
                                     stderr=subprocess.STDOUT)

    # --- evidence -------------------------------------------------------
    def serial_bytes(self):
        return self.serial.stat().st_size if self.serial.exists() else 0

    def text(self):
        return self.serial.read_bytes() if self.serial.exists() else b""

    def lcd_bases(self):
        if not self.stderr.exists():
            return []
        blob = self.stderr.read_text(errors="replace")
        return sorted(set(re.findall(r"base <- (0x0[0-9a-f]{7})", blob)))

    def trace_summary(self):
        """Condense the [MT]/[FB] stderr traces into comparable evidence:
        which LCD registers the guest wrote (with last value), how the
        multitouch dialogue ended, and whether the touch firmware loaded."""
        if not self.stderr.exists():
            return {}
        fb_writes = {}
        mt_lines = []
        panel_cmds = []
        for line in self.stderr.read_text(errors="replace").splitlines():
            m = re.match(r"\[FB\] wr (0x[0-9a-f]+) = (0x[0-9a-f]+)", line)
            if m:
                fb_writes[m.group(1)] = m.group(2)
                continue
            if line.startswith("[MT] "):
                mt_lines.append(re.sub(r" \(n=\d+\)", "", line[5:]))
                continue
            m = re.match(r"\[FB\] panel (0x[0-9a-f]+)", line)
            if m:
                panel_cmds.append(m.group(1))
        # collapse consecutive repeats but keep order
        def dedupe(seq):
            out = []
            for x in seq:
                if not out or out[-1] != x:
                    out.append(x)
            return out
        mt_lines = dedupe(mt_lines)
        return {
            "fb_regs_written": {k: fb_writes[k] for k in sorted(fb_writes)},
            "mt_firmware_loaded": any("firmware_loaded=1" in l
                                      for l in mt_lines),
            "mt_dialogue": mt_lines[:40] + (["..."] if len(mt_lines) > 80
                                            else []) +
                           (mt_lines[-40:] if len(mt_lines) > 80 else
                            mt_lines[40:]),
            "panel_cmds": dedupe(panel_cmds)[:30],
        }

    def rendered(self):
        for b in self.lcd_bases():
            if int(b, 16) in KERNEL_FB_BASES:
                return True
        return False

    def sample_pcs(self, n=8):
        pcs = []
        try:
            q = QMP(self.qmp_path)
        except Exception:
            return pcs
        try:
            for _ in range(n):
                t = q.hmp("info registers")
                m = re.search(r"R15=([0-9a-fA-F]{8})", t)
                if m:
                    pcs.append(m.group(1).lower())
                time.sleep(0.1)
        except Exception:
            pass
        finally:
            q.close()
        return pcs

    def framebuffers(self):
        out = {}
        try:
            q = QMP(self.qmp_path)
        except Exception:
            return out
        try:
            for label, base in FB_BASES.items():
                raw = self.dir / f"fb_{base:08x}.raw"
                q.cmd("pmemsave", {"val": base, "size": 320 * 480 * 4,
                                   "filename": str(raw)})
                d = raw.read_bytes()
                nb = sum(1 for i in range(0, len(d), 4)
                         if d[i] > 16 or d[i + 1] > 16 or d[i + 2] > 16)
                out[label] = round(100 * nb / (len(d) // 4), 1)
                if out[label] >= (out.get(self._best_fb_label, 0)
                                  if self._best_fb_label else -1):
                    self._best_fb_label = label
                    self.screen = classify_screen(d)
                raw.unlink(missing_ok=True)
        except Exception:
            pass
        finally:
            q.close()
        return out

    def phase(self, blob):
        seen = "none"
        for name, rx in PHASES:
            if re.search(rx, blob):
                seen = name
        return seen

    def collect(self, verdict, wall):
        blob = self.text()
        pcs = self.sample_pcs(self.args.pc_samples)
        lines = blob.decode("latin1", "replace").splitlines()
        sb = [l for l in lines if "SpringBoard[" in l]
        drivers = [l for l in lines
                   if re.search(r"::attach|Registering:|registerFunction", l)]
        self.result.update({
            "verdict": verdict,
            "wall_s": round(wall, 1),
            "serial_lines": len(lines),
            "phase": self.phase(blob),
            "markers": {k: len(re.findall(v, blob)) for k, v in MARKERS.items()},
            "lcd_bases": self.lcd_bases(),
            "rendered": self.rendered(),
            "pc_histogram": Counter(pcs).most_common(6),
            "pcs_all_idle": bool(pcs) and all(p in IDLE_PCS for p in pcs),
            "framebuffers": self.framebuffers(),
            "screen": self.screen,
            "springboard_lines": sb[-6:],
            "driver_tail": drivers[-12:],
            "trace": self.trace_summary(),
        })
        return self.result

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.qmp_path.unlink(missing_ok=True)

    def run(self):
        started = time.monotonic()
        self.launch()
        last_size, last_change = -1, time.monotonic()
        verdict = "timeout"
        try:
            while time.monotonic() - started < self.args.max_wall:
                time.sleep(self.args.poll_secs)
                blob = self.text()
                if re.search(MARKERS["panic"], blob):
                    verdict = "panicked"
                    break
                if self.rendered():
                    time.sleep(self.args.post_success)
                    verdict = "rendered"
                    break
                size = self.serial_bytes()
                if size != last_size:
                    last_size, last_change = size, time.monotonic()
                elif time.monotonic() - last_change > self.args.stall_secs:
                    pcs = self.sample_pcs(self.args.pc_samples)
                    verdict = ("wedged" if pcs and all(p in IDLE_PCS for p in pcs)
                               else "crawling")
                    break
            return self.collect(verdict, time.monotonic() - started)
        finally:
            self.kill()
            # The staged NAND clone + NOR are reproducible from the cache;
            # the evidence (serial.log, stderr.log, command.txt) is not, and
            # is never registered as disposable.
            ws = Workspace(self.dir, keep=self.args.keep_artifacts,
                           label=self.name)
            ws.disposable(self.dir / "stage")
            ws.cleanup(verbose=False)


def _normalise(line: str) -> str:
    """Strip everything that differs between two boots but carries no meaning:
    syslog timestamps, pointers/handles, pids, and the board name itself."""
    s = re.sub(r"^\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+", "", line)   # syslog stamp
    s = re.sub(r"0x[0-9a-fA-F]+", "0xX", s)
    s = re.sub(r"\b[0-9a-f]{6,8}\b", "HEX", s)
    s = re.sub(r"\[\d+\]", "[P]", s)
    s = re.sub(r"\b(N45AP|M68AP|iPod touch|iPhone)\b", "BOARD", s)
    s = re.sub(r"\bc0[0-9a-f]{6}\b", "KADDR", s)
    return s.strip()


def diff_serial(a_dir: Path, b_dir: Path, marker: str = r"SpringBoard\["):
    """Normalised set-diff of the two serial logs from the SpringBoard phase on.

    This is the differential that matters for the render wall: one board paints
    and one does not, so whatever the renderer needs shows up as lines present
    in the working boot and missing from the wedged one. Driver/service lines
    are reported separately because they are the actionable subset.
    """
    def phase_lines(d: Path):
        p = d / "serial.log"
        if not p.exists():
            return []
        lines = p.read_bytes().decode("latin1", "replace").splitlines()
        for i, l in enumerate(lines):
            if re.search(marker, l):
                return [_normalise(x) for x in lines[i:]]
        return [_normalise(x) for x in lines[-200:]]     # never reached it
    a, b = set(phase_lines(a_dir)), set(phase_lines(b_dir))
    def service_like(xs):
        return sorted(x for x in xs
                      if re.search(r"::(attach|start|probe)|Registering:|"
                                   r"config\(|matching|UserClient", x))
    return {
        "only_in_a_services": service_like(a - b)[:40],
        "only_in_b_services": service_like(b - a)[:40],
        "only_in_a_other": sorted(x for x in (a - b) if x not in
                                  set(service_like(a - b)))[:25],
        "only_in_b_other": sorted(x for x in (b - a) if x not in
                                  set(service_like(b - a)))[:25],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--variants", nargs="+", default=["n45ap-control",
                                                      "m68ap-full",
                                                      "m68ap-plain"],
                    help=f"any of: {', '.join(VARIANTS)}")
    ap.add_argument("--diff", action="append", default=[], metavar="A=B",
                    help="print driver-token diff between two instances")
    ap.add_argument("--qemu", type=Path, default=QEMU)
    ap.add_argument("--max-wall", type=float, default=420)
    ap.add_argument("--stall-secs", type=float, default=60)
    ap.add_argument("--post-success", type=float, default=20)
    ap.add_argument("--poll-secs", type=float, default=3)
    ap.add_argument("--pc-samples", type=int, default=8)
    ap.add_argument("--stagger-secs", type=float, default=30)
    ap.add_argument("--keep-artifacts", action="store_true",
                    help="keep staged NANDs and the recipe cache (debugging); "
                         "default deletes them, evidence is always kept")
    ap.add_argument("--keep-runs", type=int, default=3,
                    help="how many previous run dirs to keep under the logs "
                         "parent (0 = keep all)")
    args = ap.parse_args()

    for v in args.variants:
        if v not in VARIANTS:
            ap.error(f"unknown variant {v!r}; known: {', '.join(VARIANTS)}")
    args.logs.mkdir(parents=True, exist_ok=True)
    cache = args.logs / "_cache"
    cache.mkdir(exist_ok=True)

    # Pre-flight: refuse to start rather than fill the disk half-way. Each
    # distinct M68AP recipe costs a NAND tree (+ a patched root / data image
    # when those knobs are set); N45AP stages a CoW clone of the app's NAND.
    recipes = {(v, VARIANTS[v].get("dataark"), VARIANTS[v].get("patch"),
                VARIANTS[v].get("ark_profile", "minimal"),
                VARIANTS[v].get("prune"), VARIANTS[v].get("sb_env"),
                VARIANTS[v].get("caps"))
               for v in args.variants if VARIANTS[v]["board"] == "m68ap"}
    need = len(recipes) * NAND_TREE_BYTES
    # root/data images are intermediates deleted as soon as consumed
    # (prepare runs serially), so the transient peak is two roots + one data
    # image regardless of how many recipes mutate the root
    if any(r[2] or r[4] or r[5] or r[6] for r in recipes):
        need += 2 * ROOT_HFS_BYTES
    if any(r[1] for r in recipes):
        need += DATA_HFS_BYTES
    if need:
        require_free_bytes(args.logs, need,
                           f"{len(recipes)} NAND recipe(s)")
    if args.keep_runs:
        prune_runs(args.logs.parent, args.keep_runs, f"{args.logs.name}*")

    instances = [Instance(v, VARIANTS[v], args, cache) for v in args.variants]
    results = [None] * len(instances)

    # Build every NAND recipe SERIALLY before any boot (see Instance.prepare).
    for inst in instances:
        try:
            inst.prepare()
        except Exception as e:
            print(f"[{inst.name}] PREPARE FAILED: {e}", flush=True)

    def worker(i):
        time.sleep(i * args.stagger_secs)
        print(f"[{instances[i].name}] booting", flush=True)
        try:
            results[i] = instances[i].run()
            r = results[i]
            print(f"[{r['name']}] {r['verdict']} phase={r['phase']} "
                  f"bases={r['lcd_bases']} idle={r['pcs_all_idle']}", flush=True)
        except Exception as e:                      # keep the matrix going
            results[i] = {"name": instances[i].name, "verdict": f"error: {e}"}
            print(f"[{instances[i].name}] ERROR {e}", flush=True)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(len(instances))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for inst in instances:
            inst.kill()
        raise

    print("\n=== MATRIX ===")
    print(f"{'instance':18} {'verdict':10} {'screen':7} {'phase':20} "
          f"{'sb':3} {'bases'}")
    print("-" * 92)
    for r in results:
        if not r:
            continue
        scr = (r.get('screen') or {}).get('kind', '-')
        print(f"{r['name']:18} {r.get('verdict',''):10} {scr:7} "
              f"{r.get('phase',''):20} "
              f"{r.get('markers',{}).get('springboard',0):<3} "
              f"{','.join(r.get('lcd_bases',[])) or '-'}")

    report = {"results": [r for r in results if r], "diffs": {}}
    by_name = {r["name"]: r for r in results if r}
    for spec in args.diff:
        if "=" not in spec:
            continue
        a, b = spec.split("=", 1)
        ta = by_name.get(a, {}).get("trace", {})
        tb = by_name.get(b, {}).get("trace", {})
        if ta or tb:
            ra, rb = ta.get("fb_regs_written", {}), tb.get("fb_regs_written", {})
            print(f"\n=== TRACE DIFF {a} vs {b} ===")
            print(f"  mt_firmware_loaded: {a}={ta.get('mt_firmware_loaded')} "
                  f"{b}={tb.get('mt_firmware_loaded')}")
            only_a = {k: ra[k] for k in ra if k not in rb}
            only_b = {k: rb[k] for k in rb if k not in ra}
            differ = {k: (ra[k], rb[k]) for k in ra
                      if k in rb and ra[k] != rb[k]}
            # the per-board gamma/palette ramp at 0x400..0xffc differs in
            # VALUES on every boot pair; only its presence is signal
            gamma = [k for k in differ if 0x400 <= int(k, 16) <= 0xffc]
            for k in gamma:
                del differ[k]
            print(f"  LCD regs only in {a}: {only_a or '(none)'}")
            print(f"  LCD regs only in {b}: {only_b or '(none)'}")
            print(f"  LCD regs differing: {differ or '(none)'}"
                  + (f" (+{len(gamma)} gamma-ramp regs, values only)"
                     if gamma else ""))
        d = diff_serial(args.logs / a, args.logs / b)
        report["diffs"][spec] = d
        print(f"\n=== DIFF {a} vs {b} (normalised, from SpringBoard on) ===")
        for key, label in (("only_in_b_services", f"services only in {b}"),
                           ("only_in_a_services", f"services only in {a}"),
                           ("only_in_b_other", f"other only in {b}"),
                           ("only_in_a_other", f"other only in {a}")):
            print(f"  -- {label}:")
            for line in d[key][:20]:
                print(f"       {line[:110]}")
            if not d[key]:
                print("       (none)")

    path = args.logs / "matrix.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nreport: {path}")

    # The recipe cache is large and fully reproducible; drop it unless the
    # caller wants to re-run quickly. Evidence stays either way.
    cache_ws = Workspace(args.logs, keep=args.keep_artifacts, label="cache")
    cache_ws.disposable(cache)
    cache_ws.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
