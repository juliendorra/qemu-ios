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

    index = 0
    for value, length in _properties(blob, MAC_PROPERTY):
        if length != len(MAC_BASE) or not _is_all_same(blob[value:value + length]):
            continue
        mac = bytes(MAC_BASE[:-1]) + bytes([(MAC_BASE[-1] + index) & 0xFF])
        blob[value:value + length] = mac
        changed.append(f"{MAC_PROPERTY.decode()} @{value:#x}: "
                       f"{':'.join(f'{b:02x}' for b in mac)}")
        index += 1

    return changed
