#!/usr/bin/env python3
"""Hacktivate the M68AP (iPhone 2G) so SpringBoard gets past the activation gate.

Why this exists
---------------
A fresh-from-IPSW M68AP boots `[Unactivated]`; iPhone OS 1.x then shows an
activation screen (never the home screen) and SpringBoard never programs its
framebuffer base, so the panel stays black. N45AP (iPod Touch) ships a
pre-activated NAND (devos50: "I copied activation records from an actual
device ... modified the NAND filesystem to bypass various checks").

RECOMMENDED (authentic, single source of truth): the `build-dataark` command
here emits a `data_ark.plist`; inject it into the guest /var with
`scripts/inject-guest-file.py`, regenerate the NAND, boot. lockdownd then
serves `Activated` (and EverRegistered, etc.) to EVERY consumer -- SpringBoard,
CommCenter, Preferences, iTunes-sync -- exactly the way N45AP's pre-activated
NAND does. This is how the iPod does it and avoids a rabbit hole of per-binary
patches. Verified: with a clean, UNpatched root FS, this alone takes M68AP to
`SpringBoard[15]: lockdown says the device is: [Activated], state is 2` and
clears the EverRegistered gate too.

Two facts that cost cycles to learn:
  * The data ark MUST be a **binary** plist. An earlier XML injection made
    lockdownd log "Could not load" -- that was a format (parse) failure, NOT
    the HFS-write-visibility problem first suspected. The guest reads
    macOS-written HFS files fine (the DNS restore and this both prove it).
  * root:wheel ownership is NOT required for the data ark (lockdownd runs as
    root and reads a uid-501 file); it IS required for launchd plists. See
    `inject-guest-file.py --root-owned`.

FALLBACK / reference (a single in-binary patch, if you cannot rebuild the data
partition): the `analyse`/`disasm`/`patch` commands byte-rename lockdownd's
"Unactivated" CFString constant to "Activated" in the root-HFS image. It also
reaches [Activated], but it only fixes lockdownd's own report -- other
consumers (SpringBoard's EverRegistered, etc.) still read the empty data ark,
so it leads to the per-binary rabbit hole. Prefer the data ark.

Usage
-----
  scripts/hacktivate-m68ap.py build-dataark --out /tmp/data_ark.plist
  scripts/inject-guest-file.py --image <data.hfs> --src /tmp/data_ark.plist \
      --dest /root/Library/Lockdown/data_ark.plist
  scripts/build-m68ap-nand.py --hfs <root> --data-hfs <data.hfs> ...

  # fallback binary patch:
  scripts/hacktivate-m68ap.py patch --root-hfs <root.img> --out <patched.img>
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path


# --- Mach-O (32-bit ARM) --------------------------------------------------

class MachO:
    def __init__(self, data: bytes):
        self.d = data
        if data[:4] != bytes.fromhex("cefaedfe"):
            raise ValueError("not a little-endian 32-bit Mach-O (MH_MAGIC)")
        self.sects = []  # (segname, sectname, vmaddr, size, fileoff)
        ncmds = struct.unpack("<I", data[16:20])[0]
        off = 28
        for _ in range(ncmds):
            cmd, cs = struct.unpack("<II", data[off:off + 8])
            if cmd == 1:  # LC_SEGMENT
                nsects = struct.unpack("<I", data[off + 48:off + 52])[0]
                so = off + 56
                for _k in range(nsects):
                    sn = data[so:so + 16].rstrip(b"\0").decode("latin1")
                    seg = data[so + 16:so + 32].rstrip(b"\0").decode("latin1")
                    addr, size, foff = struct.unpack("<III", data[so + 32:so + 44])
                    self.sects.append((seg, sn, addr, size, foff))
                    so += 68
            off += cs

    def vm_to_fo(self, vm):
        for _seg, _sn, addr, size, foff in self.sects:
            if size and addr <= vm < addr + size:
                return foff + (vm - addr)
        return None

    def fo_to_vm(self, fo):
        for _seg, _sn, addr, size, foff in self.sects:
            if size and foff <= fo < foff + size:
                return addr + (fo - foff)
        return None

    def cstr(self, vm):
        fo = self.vm_to_fo(vm)
        if fo is None:
            return None
        end = self.d.index(b"\0", fo)
        s = self.d[fo:end]
        return s.decode("latin1") if s.isascii() else None

    def find_cstring_vm(self, s: str):
        i = self.d.find(s.encode() + b"\0")
        return self.fo_to_vm(i) if i >= 0 else None

    def pool_xrefs(self, target_vm: int):
        """File offsets whose 4 LE bytes equal target_vm (literal-pool refs)."""
        needle = struct.pack("<I", target_vm)
        out = []
        start = 0
        while True:
            i = self.d.find(needle, start)
            if i < 0:
                break
            out.append(i)
            start = i + 1
        return out


# --- the patch ------------------------------------------------------------
#
# Analysis result (see `analyse`/`disasm`): lockdownd's activation-state
# determination is a large, path-dependent function. An empty-record device
# resolves to the CFString constant "Unactivated"; the plain-constant ref
# (0x109cf8) is only reached on the IMEI-mismatch path, which an empty device
# skips ("No IMEI in the activation record"), so a pool redirect there does not
# help. The robust, code-path-independent fix is to RENAME the "Unactivated"
# CFString to "Activated": wherever lockdownd reports the state, SpringBoard
# then reads "Activated" and renders the home screen.
#
# Two same-length raw-byte edits, applied by unique context so they land in the
# right HFS data block regardless of fragmentation (never via a macOS HFS
# mount, which is the write path that failed for data-ark injection):
#   1. the C string  "Unactivated\0"  ->  "Activated\0\0\0"   (12 bytes)
#   2. the CFString constant's length word  11 -> 9, located by the
#      <str-ptr=0x0009d8a0><len=0x0000000b> pair unique to that constant.

PATCH_DESCRIPTION = 'rename lockdownd "Unactivated" activation string -> "Activated"'

# The edits are DERIVED from the image, not hardcoded, so the same code works on
# every iPhone OS 1.x lockdownd. Hardcoding them to 1.1.4's literal pool values
# (str-ptr 0x0009d8a0, isa 0x384ff3b8) meant a different build silently found
# zero occurrences; 1.1.1's are 0x0007def0 / 0x384c73b8 with the identical
# structure. What IS stable is the shape:
#
#   * lockdownd's __cstring holds "...state\0<padding>Unactivated\0" -- the only
#     place in the whole root filesystem where "Unactivated" follows "state"
#     across NUL padding (measured: exactly 1 match in both 1.1.1 and 1.1.4,
#     out of 4 bare "Unactivated" occurrences filesystem-wide).
#   * TWO __cfstring constants point at that string with length 11. Both must
#     become 9, or SpringBoard's exact compare sees "Activated\0\0".
#
# lockdownd is stored contiguously in the HFS image (verified: the Mach-O header
# sits exactly at str_offset - str_file_offset), so the binary can be parsed in
# place and every address derived from its own load commands.
UNACTIVATED_RE = re.compile(rb"state\x00+Unactivated\x00")
NEW_STATE = b"Activated"
OLD_LEN, NEW_LEN = 11, 9


def derive_patches(image: bytes) -> list:
    """Return [(name, offset, original, replacement)] derived from the image."""
    matches = UNACTIVATED_RE.findall(image)
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly 1 lockdownd 'state\\0+Unactivated' site, found "
            f"{len(matches)}; refusing to patch")
    m = UNACTIVATED_RE.search(image)
    str_off = image.index(b"Unactivated\x00", m.start())

    # Walk back to lockdownd's Mach-O header and parse it in place.
    base = image.rfind(bytes.fromhex("cefaedfe"), 0, str_off)
    if base < 0:
        raise SystemExit("no Mach-O header precedes the activation string")
    macho = MachO(image[base:str_off + 0x200000])
    str_va = macho.fo_to_vm(str_off - base)
    if str_va is None or macho.cstr(str_va) != "Unactivated":
        raise SystemExit(
            f"derived VA {str_va and hex(str_va)} does not resolve back to "
            f"'Unactivated'; lockdownd may be fragmented in this image")

    patches = [(
        "activation-string", str_off,
        b"Unactivated\x00",
        NEW_STATE + b"\x00" * (len(b"Unactivated\x00") - len(NEW_STATE)),
    )]

    # __cfstring constants: <isa><flags><str ptr><length>. Find them by the
    # pointer we just derived, and patch the length word that follows it.
    needle = struct.pack("<I", str_va) + struct.pack("<I", OLD_LEN)
    found = 0
    start = base
    while True:
        i = image.find(needle, start, base + len(macho.d))
        if i < 0:
            break
        patches.append(("cfstring-length", i + 4,
                        struct.pack("<I", OLD_LEN),
                        struct.pack("<I", NEW_LEN)))
        found += 1
        start = i + 1
    if found != 2:
        raise SystemExit(
            f"expected 2 CFString constants of length {OLD_LEN} pointing at "
            f"{str_va:#x}, found {found}; refusing to patch")
    print(f"  derived: 'Unactivated' at image {str_off:#x} (VA {str_va:#x}), "
          f"{found} CFString length words")
    return patches


def apply_patches(image: bytes) -> bytes:
    buf = bytearray(image)
    for name, off, orig, repl in derive_patches(image):
        assert len(orig) == len(repl), name
        if bytes(buf[off:off + len(orig)]) != orig:
            raise SystemExit(
                f"patch {name!r} at {off:#x}: expected {orig!r}, found "
                f"{bytes(buf[off:off + len(orig)])!r}")
        buf[off:off + len(repl)] = repl
        print(f"  patched {name}: image offset {off:#x}")
    return bytes(buf)


def analyse(lockdownd: bytes):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
    m = MachO(lockdownd)
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    for name in ("Activated", "Unactivated", "The device was factory activated",
                 "Setting the activation state to %s",
                 "determine_activation_state"):
        vm = m.find_cstring_vm(name)
        print(f"string {name!r:45} vm={hex(vm) if vm else None}")
        if vm:
            xr = m.pool_xrefs(vm)
            print("   pool xrefs (file offs):",
                  [hex(x) for x in xr[:8]], "->",
                  [hex(m.fo_to_vm(x)) for x in xr[:8] if m.fo_to_vm(x)])
    return m


def disasm(lockdownd: bytes, start_vm: int, length: int):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
    import re
    m = MachO(lockdownd)
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    fo = m.vm_to_fo(start_vm)
    code = m.d[fo:fo + length]
    for ins in md.disasm(code, start_vm):
        ann = ""
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:
            mt = re.search(r"\[pc,?\s*#(-?0x[0-9a-f]+|-?\d+)\]", ins.op_str)
            if mt:
                pa = (ins.address + 8 + int(mt.group(1), 0)) & 0xffffffff
                pf = m.vm_to_fo(pa)
                if pf is not None:
                    val = struct.unpack("<I", m.d[pf:pf + 4])[0]
                    s = m.cstr(val)
                    ann = f"  ; =0x{val:08x}"
                    if s and s.isprintable() and len(s) >= 2:
                        ann += f' "{s[:38]}"'
        print(f"  {ins.address:08x} {ins.mnemonic:7} {ins.op_str}{ann}")


# Minimal lockdown data ark that clears the activation + registration gates.
# Keys mirror the ones an activated device carries (observed on N45AP's real
# data ark), but contain NO Apple certificates/tokens -- the *cached*
# ActivationState string is what lockdownd's _load_cached_activation_state
# serves, and it is sufficient for SpringBoard to proceed past [Activated] and
# the EverRegistered check. Domain-qualified key form is "<domain>-<key>";
# bare "-<key>" is the default domain.
DATA_ARK = {
    "com.apple.mobile.lockdown_cache-ActivationState": "Activated",
    "-ActivationStateAcknowledged": 1,
    "-SBLockdownEverRegisteredKey": 0,
    "-BrickState": 0,
    "-iTunesHasConnected": 1,
    "-PasswordProtected": 0,
    "-ProtocolVersion": "2",
}

# Profiles for the activation-DURABILITY question: the `minimal` ark above sets
# only the *cached* state, which `determine_activation_state` re-validates at
# boot and overrides back to Unactivated (no Apple-signed activation record
# exists). lockdownd carries two data-driven levers that may satisfy that
# re-validation WITHOUT any binary patch -- if either holds, hacktivation
# becomes pure data, like the iPod's populated ark:
#   * FactoryActivated        -> lockdownd logs "The device was factory
#                                activated" on a path that skips ticket checks
#   * AllowUnactivatedService -> lets the device serve while unactivated
# `all` sets both plus a non-cache ActivationState in the default domain.
ARK_PROFILES = {
    "minimal": {},
    "factory": {"-FactoryActivated": True, "-ActivationState": "FactoryActivated"},
    "unactsvc": {"-AllowUnactivatedService": True},
    "all": {"-FactoryActivated": True, "-ActivationState": "Activated",
            "-AllowUnactivatedService": True,
            "com.apple.mobile.lockdown-ActivationState": "Activated"},
    # TYPE, not value: SpringBoard reads EverRegistered as a CFString and
    # logs "lockdown had a value for EverRegistered but it wasn't a string:
    # <CFNumber 0>" for the integer the minimal ark writes -- so it discards
    # the value and treats the device as never registered, which is a
    # "set me up with iTunes" state. These profiles supply a STRING instead.
    # ("YES"/"1" are the two plausible spellings; measure, don't guess.)
    "everreg-yes": {"-SBLockdownEverRegisteredKey": "YES"},
    "everreg-1": {"-SBLockdownEverRegisteredKey": "1"},
    # THE REFERENCE SHAPE. Read off the iPod's own shipped data ark -- a real
    # device that reaches the home screen -- via
    #   extract-hfs-from-nand.py <ipod nand> root.img   (single-partition NAND,
    #   so /private/var/root/Library/Lockdown/data_ark.plist is in the ROOT)
    # Only key NAMES, TYPES and generic values are mirrored; no Apple-signed
    # material is copied (the reference's activation record and its
    # StoreIdentityCookie stay on the reference).
    #
    # Two classes of difference from the ark we had been writing:
    #  1. TYPES: the reference stores CFBooleans where we wrote CFNumbers
    #     (0/1). A binary plist keeps that distinction, and SpringBoard's
    #     "wasn't a string" complaint fires for our CFNumber *and* for a
    #     CFString -- consistent with it wanting the boolean the real device
    #     has, with a misleading message.
    #  2. MISSING KEYS: the reference carries international settings, SIM
    #     status, timezone and iTunes/registration flags. M68AP logs
    #     "lockdown: _load_international_settings: Could not load languages
    #     list / Could not load the locale" -- a device with no language has
    #     never been set up, which is exactly the screen we are stuck on.
    "reference": {
        "-ActivationStateAcknowledged": True,
        "-BrickState": False,
        "-DeviceName": "iPhone",
        "-FirmwareVersion": "iBoot-204.3.14",   # M68AP's own iBoot BUILD_TAG
        "-PasswordProtected": False,
        "-ProtocolVersion": "2",
        "-SBLockdownEverRegisteredKey": False,
        "-SIMStatus": "kCTSIMSupportSIMStatusReady",
        "-SomebodySetTimeZone": True,
        "-TimeZone": "Europe/Paris",
        "-Uses24HourClock": False,
        "-iTunesHasConnected": True,
        "com.apple.international-HostKeyboard": "en_US",
        "com.apple.international-Keyboard": "en_US",
        "com.apple.international-Language": "en",
        "com.apple.international-Locale": "en_US",
        "com.apple.mobile.restriction-ProhibitAppInstall": False,
        "com.apple.mobile.lockdown_cache-ActivationState": "Activated",
    },
}
# The reference is an iPod: it has never registered on a network, so its
# EverRegistered is False and the iPod's SpringBoard build never asks. On the
# iPhone build the key IS consulted -- with the reference ark it is finally
# ACCEPTED ("lockdown says we've previously registered: [0], state is 0"),
# which also settles the type question: the consumer wants a CFBoolean, and
# the "wasn't a string" complaint is a misleading message. A phone that has
# registered would say True.
ARK_PROFILES["reference-reg"] = dict(ARK_PROFILES["reference"])
ARK_PROFILES["reference-reg"]["-SBLockdownEverRegisteredKey"] = True


def build_dataark(out: Path, profile: str = "minimal"):
    import plistlib
    if profile not in ARK_PROFILES:
        raise SystemExit(f"unknown profile {profile!r}; "
                         f"known: {', '.join(ARK_PROFILES)}")
    ark = dict(DATA_ARK)
    ark.update(ARK_PROFILES[profile])
    out.write_bytes(plistlib.dumps(ark, fmt=plistlib.FMT_BINARY))
    print(f"wrote binary data_ark.plist [{profile}] "
          f"({out.stat().st_size} bytes) -> {out}")
    print("keys:", ", ".join(sorted(ark)))
    print("next: inject-guest-file.py --image <data.hfs> --src", out,
          "--dest /root/Library/Lockdown/data_ark.plist")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    da = sub.add_parser("build-dataark",
                        help="emit a minimal binary lockdown data_ark.plist")
    da.add_argument("--out", type=Path, required=True)
    da.add_argument("--profile", default="minimal",
                    choices=sorted(ARK_PROFILES),
                    help="extra activation levers to include (default: "
                         "minimal = cached ActivationState only)")
    a = sub.add_parser("analyse", help="locate the activation patch site (fallback)")
    a.add_argument("--lockdownd", type=Path, required=True)
    dd = sub.add_parser("disasm", help="disassemble a vm range")
    dd.add_argument("--lockdownd", type=Path, required=True)
    dd.add_argument("--vm", type=lambda x: int(x, 0), required=True)
    dd.add_argument("--len", type=lambda x: int(x, 0), default=0x120)
    p = sub.add_parser("patch", help="patch lockdownd in a raw root-HFS image")
    p.add_argument("--root-hfs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if args.cmd == "build-dataark":
        build_dataark(args.out, args.profile)
        return 0
    if args.cmd == "analyse":
        analyse(args.lockdownd.read_bytes())
        return 0
    if args.cmd == "disasm":
        disasm(args.lockdownd.read_bytes(), args.vm, args.len)
        return 0
    # patch mode: rename the Unactivated activation string in the root HFS
    print(f"hacktivate: {PATCH_DESCRIPTION}")
    image = args.root_hfs.read_bytes()
    patched = apply_patches(image)
    args.out.write_bytes(patched)
    print(f"wrote patched root HFS -> {args.out} ({len(patched)} bytes)")
    print("next: build-m68ap-nand.py --hfs", args.out,
          "--data-hfs <data> --signature m68ap --active-banks 4 --bbt production")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
