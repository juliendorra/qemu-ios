#!/usr/bin/env python3
"""Fill the M68AP device tree's zero-filled radio properties.

On a real iPhone 2G these fields are populated at boot by iBoot, from the
*baseband's* radio NVRAM (`AT+xdrv=9,1,<block>` over uart1 -- the serial lines
`Read %d bytes from nvram` / `Installing WIFI Calibration`). An emulator has no
source for them: our S-Gold2 stub answers `+XDRV: 9,1,0,0,NULL`, so the
properties keep the all-zero values the firmware ships.

iPhone OS 1.1.x does not care -- its `AppleMRVL868x` bootstraps the card and
reads calibration from the chip's own EEPROM (which `ipod_touch_mv8686.c`
supplies). **iPhone OS 1.0 does care**: it validates the device-tree copy
BEFORE it will bootstrap, and refuses on all-zero:

    AppleMRVL868x: Starting
    AppleMRVL868x: Invalid calibration data in device tree.
    AppleMRVL868x::start(SDIODeviceNub) <2> failed

...and then, once calibration passes, on the zero MAC:

    AppleMRVL868x: MAC Address is all 00's.

so both properties have to be filled. With both, 1.0 attaches
`IO80211Interface` normally and Settings shows Wi-Fi instead of "No Wi-Fi".
See WIFI_SDIO_NOTES.md for the full diagnosis and the measurements.

This is a FIRMWARE-image edit, not a guest-image one, and it is deliberately
NOT a plausible-looking forgery: the calibration is an obvious synthetic ramp
and the MACs are locally administered (the 0x02 bit), so anything that reads
them can tell they did not come from a real device. They exist to satisfy a
sanity check -- nothing in the emulated radio consumes them, and they are not
real RF calibration.

**ON by default for the 1.0 family only** (`build-m68ap-nor.py`, opt out with
`--no-fill-radio-properties`). 1.1.x reads calibration from the card and needs
nothing from the device tree, so it is not filled there.

This once BROKE TOUCH on 1.0, and the reason it no longer does is worth keeping.
A valid MAC got the Wi-Fi driver past its checks and into a main-firmware
download the SDIO model could not satisfy -- a hot retry loop (23 helper-boots
per boot) that starved the multitouch path. The loop was the cause, not the MAC.
With the download fixed (`ipod_touch_mv8686.c` routes 1.0's 32-byte descriptor
straight to `DL_MAIN`; WIFI_SDIO_NOTES.md), measured on the shipped bundle:

    1_open_app PASS 97.11%   2_touch_in_app PASS 35.63%   Firmware loadded.

If the download path ever regresses, expect touch to go with it.

Values are deterministic so that rebuilding a NOR twice gives identical bytes.
"""
from __future__ import annotations

import struct

# Apple device-tree property layout: 32-byte NUL-padded name, uint32 length,
# then the value.
NAME_FIELD = 32
LENGTH_FIELD = 4

CALIBRATION_PROPERTY = b"tx-calibration"
MAC_PROPERTY = b"local-mac-address"

# Locally administered (0x02 in the first octet), matching the OUI-less
# convention ipod_touch_mv8686.c already uses for its BSSID.
MAC_BASE = bytes([0x02, 0x1a, 0x11, 0xe0, 0x86, 0x86])


def _is_all_same(data: bytes) -> bool:
    """The driver's own rule: all 0x00 or all 0xff is 'invalid'."""
    return len(set(data)) <= 1


def _synthetic_calibration(length: int) -> bytes:
    # Any non-uniform pattern passes; a stride-7 ramp is stable, obviously
    # artificial, and never degenerates into a run of equal bytes.
    return bytes((i * 7 + 0x31) & 0xFF for i in range(length))


def _properties(blob: bytes, name: bytes):
    """Yield (value_offset, length) for each occurrence of a property name."""
    i = 0
    while True:
        i = blob.find(name, i)
        if i < 0:
            return
        # Reject substring hits: the name field is NUL-padded, so the byte
        # right after the name must be NUL ("calibration" would otherwise
        # match inside "tx-calibration").
        if blob[i + len(name)] == 0:
            (length,) = struct.unpack_from("<I", blob, i + NAME_FIELD)
            value = i + NAME_FIELD + LENGTH_FIELD
            if 0 < length and value + length <= len(blob):
                yield value, length
        i += 1


def fill(blob: bytearray) -> list[str]:
    """Fill zero-valued radio properties in place. Returns what changed.

    Already-populated properties are left alone, so this is idempotent and
    safe to run on a device tree that some other path has filled.
    """
    changed: list[str] = []

    for value, length in _properties(blob, CALIBRATION_PROPERTY):
        if not _is_all_same(blob[value:value + length]):
            continue
        blob[value:value + length] = _synthetic_calibration(length)
        changed.append(f"{CALIBRATION_PROPERTY.decode()} @{value:#x}: "
                       f"{length} bytes of synthetic calibration")

    # ONLY the Wi-Fi node's MAC. The device tree has three `local-mac-address`
    # properties -- one early (ethernet), one in the Wi-Fi node beside
    # `tx-calibration`, and one just after `uart3`, which is BLUETOOTH.
    #
    # Filling all three wedged iPhone OS 1.1.4's boot: it reached launchd and
    # stopped with `com.apple.BTServer: bluetooth power is now ON` as its last
    # serial line, never rendering a home screen (kernel framebuffer 0.0%
    # non-black across six samples). A zero MAC is what had been keeping the
    # Bluetooth stack from trying to come up over uart3; handing it one starts
    # a bring-up this emulator has no baseband-side answer for.
    #
    # The Wi-Fi node's is the only one anything here needs, so it is the only
    # one filled. It is identified structurally -- the last MAC property before
    # `tx-calibration`, i.e. the one in the same node -- not by a hardcoded
    # offset, because the offsets differ per build.
    cal_sites = [v for v, _ in _properties(blob, CALIBRATION_PROPERTY)]
    if cal_sites:
        candidates = [(v, ln) for v, ln in _properties(blob, MAC_PROPERTY)
                      if v < cal_sites[0] and ln == len(MAC_BASE)]
        if candidates:
            value, length = candidates[-1]
            if _is_all_same(blob[value:value + length]):
                blob[value:value + length] = MAC_BASE
                changed.append(f"{MAC_PROPERTY.decode()} @{value:#x} "
                               f"(Wi-Fi node): "
                               f"{':'.join(f'{b:02x}' for b in MAC_BASE)}")

    return changed
