#!/usr/bin/env python3
"""Per-firmware-build constants for S5L8900 boards (M68AP iPhone / N45AP iPod).

Until now the toolchain had exactly one variation axis, `board_id`, and "M68AP"
was used as a synonym for "iPhone OS 1.1.4 / 4A102 / iBoot-204.3.14". That is
not true of the other 1.x builds: the 8900 container format, the security epoch,
the bootloader build and the NAND (FIL/WMR) signature all change per firmware,
not per board. See IPHONE_OS_1X_VERSIONS.md for how each value was measured.

The measured matrix (read out of the real IPSWs, not from a wiki):

    build    format  epoch  iBoot      FIL signature
    1A543a   4 plain   0    iBoot-159  0x43303030 "000C"
    1C28     4 plain   0    iBoot-159  0x43303030 "000C"
    3A109a   3 enc     2    iBoot-204  0x43303032 "200C"
    4A102    3 enc     3    iBoot-204  0x43303033 "300C"
    N45AP    3 enc     2    iBoot-204  0x43303032 "200C"   (iPod reference)

Note that 3A109a matches the working N45AP baseline on all three of epoch,
bootloader build and NAND signature -- it is the closest sibling of the machine
we already boot.

This module holds constants only: no firmware, no keys beyond the two that are
already public and already in the tree (the S5L8900 GID key, and the published
per-build VFDecrypt keys, which are recoverable from the restore ramdisk anyway).
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

# 8900 container "format" byte at header offset 0x07.
FORMAT_ENCRYPTED = 0x03  # AES-128-CBC(key=GID, iv=0) over the payload
FORMAT_PLAINTEXT = 0x04  # payload stored in the clear (1.0 / 1.0.x only)

# S5L8900 GID key ("AES key 0x837"). Public, SoC-wide, shared by iPhone 2G and
# iPod Touch 1G, and already compiled into hw/arm/ipod_touch_8900_engine.h.
GID_KEY = "188458A6D15034DFE386F23B61D43774"


@dataclass(frozen=True)
class FirmwareProfile:
    """Everything that varies between iPhone OS 1.x builds on one board."""

    build: str                  # Apple build number, e.g. "4A102"
    version: str                # marketing version, e.g. "1.1.4"
    board: str                  # "m68ap" or "n45ap"
    device: str                 # e.g. "iPhone1,1"

    img_format: int             # FORMAT_ENCRYPTED or FORMAT_PLAINTEXT
    epoch: int                  # security epoch in the 8900/IMG2 header
    iboot: str                  # bootloader build string, e.g. "iBoot-204"
    fil_signature: int          # FIL/WMR signature word at bank0/page0 word0

    root_dmg: Optional[str] = None       # root filesystem DMG in the IPSW
    vfdecrypt_key: Optional[str] = None  # published key for that DMG
    ipsw_sha1: Optional[str] = None      # for the acceptance gate

    # (Kernelcache shape does NOT vary: 1.0, 1.1.1 and 1.1.4 all store
    # 8900 -> complzss with no IMG2 wrapper. Only the 8900 layer differs, and
    # img_format already covers that.)

    # Kexts whose absence changes what the machine must model. Informational,
    # used by the acceptance report.
    notes: tuple = field(default=())

    @property
    def encrypted(self) -> bool:
        return self.img_format == FORMAT_ENCRYPTED

    @property
    def fil_signature_bytes(self) -> bytes:
        return self.fil_signature.to_bytes(4, "little")

    @property
    def iboot_out_name(self) -> str:
        """Conventional raw-iBoot filename for this build."""
        return f"iboot_{self.iboot.split('-')[1]}_{self.board}.bin"


PROFILES: Dict[str, FirmwareProfile] = {
    "1A543a": FirmwareProfile(
        build="1A543a", version="1.0", board="m68ap", device="iPhone1,1",
        img_format=FORMAT_PLAINTEXT, epoch=0, iboot="iBoot-159",
        fil_signature=0x43303030,
        root_dmg="694-5262-39.dmg",
        vfdecrypt_key=("28c909fc6d322fa18940f03279d70880e59a4507"
                       "998347c70d5b8ca7ef090ecccc15e82d"),
        ipsw_sha1="fb8bb3ee2e9a997affbb97868599f2995c78209c",
        notes=("no AppleH1TVOut device -- the TVOut swap-device workaround has "
               "nothing to hook",
               "ambient light sensor is AppleTSL2561, NOT ISL29003",
               "iBoot-159 has no security-epoch check; it gates on 'trust "
               "information' instead"),
    ),
    "1C28": FirmwareProfile(
        build="1C28", version="1.0.2", board="m68ap", device="iPhone1,1",
        img_format=FORMAT_PLAINTEXT, epoch=0, iboot="iBoot-159",
        fil_signature=0x43303030,
        root_dmg="694-5298-5.dmg",
        # Not from a wiki: recovered from the IPSW itself. For every pre-3.0
        # firmware the VFDecrypt key is stored in the clear inside the restore
        # ramdisk's /usr/sbin/asr, so it can be found by unwrapping the ramdisk
        # (itself an 8900 container) and scanning asr for a 72-hex-char string.
        vfdecrypt_key=("7d5962d0b582ec2557c2cade50de90f4353a1c1d"
                       "e07b74212513fef9cc71fb890574bfe5"),
        ipsw_sha1="7f5c0ff1f84a0202b75a55c3fcb362e415334d1e",
        notes=("same bootloader generation as 1.0",),
    ),
    "3A109a": FirmwareProfile(
        build="3A109a", version="1.1.1", board="m68ap", device="iPhone1,1",
        img_format=FORMAT_ENCRYPTED, epoch=2, iboot="iBoot-204",
        fil_signature=0x43303032,
        root_dmg="022-3602-17.dmg",
        vfdecrypt_key=("f45de7637a62b200950e550f4144696d7ff3dc5f"
                       "0b19c8efdf194c88f3bc2fa808fea3b3"),
        ipsw_sha1="d441dd1c71ce18f25d8fc4faa71c1e6eaa02d02c",
        notes=("matches the N45AP baseline on epoch, iBoot build and NAND "
               "signature -- the cheapest second build",),
    ),
    "4A102": FirmwareProfile(
        build="4A102", version="1.1.4", board="m68ap", device="iPhone1,1",
        img_format=FORMAT_ENCRYPTED, epoch=3, iboot="iBoot-204",
        fil_signature=0x43303033,
        root_dmg="022-3894-4.dmg",
        vfdecrypt_key=("d0a0c0977bd4b6350b256d6650ec9eca419b6f96"
                       "1f593e74b7e5b93e010b698ca6cca1fe"),
        ipsw_sha1="000811bac096011b50ebf6ec1ec2285b62fda4cb",
        notes=("the currently supported build",),
    ),
    # The iPod reference the M68AP work is diffed against.
    "n45ap": FirmwareProfile(
        build="n45ap", version="1.1", board="n45ap", device="iPod1,1",
        img_format=FORMAT_ENCRYPTED, epoch=2, iboot="iBoot-204",
        fil_signature=0x43303032,
        notes=("reference oracle, not an IPSW target",),
    ),
}

DEFAULT_BUILD = "4A102"


def get(build: str) -> FirmwareProfile:
    """Look up a profile by build number, case-insensitively."""
    for key, profile in PROFILES.items():
        if key.lower() == build.lower():
            return profile
    known = ", ".join(k for k in PROFILES if k != "n45ap")
    raise KeyError(f"unknown firmware build {build!r} (known: {known})")


def add_build_argument(parser, flag="--build", default=DEFAULT_BUILD):
    """Add a standard --build option to an argparse parser."""
    parser.add_argument(
        flag, default=default, metavar="BUILD",
        help=f"firmware build number (default {default}; "
             f"one of {', '.join(k for k in PROFILES if k != 'n45ap')})")
    return parser


if __name__ == "__main__":
    hdr = f"{'build':8} {'version':8} {'fmt':>4} {'epoch':>5}  {'iBoot':10} signature"
    print(hdr)
    print("-" * len(hdr))
    for p in PROFILES.values():
        fmt = "plain" if not p.encrypted else "enc"
        sig = f"{p.fil_signature:#010x} {p.fil_signature_bytes.decode('ascii')}"
        print(f"{p.build:8} {p.version:8} {fmt:>4} {p.epoch:5}  {p.iboot:10} {sig}")
