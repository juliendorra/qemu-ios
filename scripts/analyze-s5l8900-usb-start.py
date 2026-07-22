#!/usr/bin/env python3
"""Locate the early S5L8900 IOIpodUSBDevice service lookup path.

The iPod touch 1G and iPhone 2G 1.1.4 kernels contain the same stripped
``IOIpodUSBDevice::start`` implementation at different virtual addresses.
This tool anchors the routine from its ``usb-otg`` and
``function-usb_500_100`` literals, resolves the three loaded call targets,
and emits the service-enumeration observation points used by the optional
QEMU plugin.  It accepts either a decrypted Mach-O or an original ``89001.0``
kernelcache and never writes decrypted firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import struct
import sys
from pathlib import Path


HELPER = Path(__file__).with_name("analyze-m68ap-ftl-open.py")
USB_SERVICE = b"usb-otg\0"
USB_FUNCTION = b"function-usb_500_100\0"


def load_helper():
    spec = importlib.util.spec_from_file_location("s5l8900_kernel_helper", HELPER)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load kernel helper {HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_macho(path: Path, helper, openssl: str, gid_key: str) -> tuple[bytes, dict]:
    source = path.read_bytes()
    provenance = {
        "path": str(path),
        "sha256": hashlib.sha256(source).hexdigest(),
    }
    if source.startswith(struct.pack("<I", helper.MH_MAGIC)):
        provenance["format"] = "Mach-O"
        return source, provenance
    decrypted = helper.decrypt_8900_container(source, openssl, gid_key)
    macho, compression = helper.decompress_complzss(decrypted)
    provenance.update({"format": "89001.0", **compression})
    return macho, provenance


def locate_usb_start(macho: bytes, helper) -> dict:
    (Cs, arch, mode, arm_op_imm, arm_op_mem, arm_op_reg,
     arm_reg_pc) = helper.load_capstone()
    segments = helper.parse_segments(macho)
    prelink = next((item for item in segments if item.name == "__PRELINK"), None)
    if prelink is None:
        raise ValueError("Mach-O has no __PRELINK segment")
    disassembler = Cs(arch, mode)
    disassembler.detail = True

    def string_literal(string: bytes) -> int:
        string_offset = helper.require_one(
            helper.find_all(macho, string), repr(string.rstrip(b"\0")))
        string_va = helper.file_to_va(segments, string_offset)
        literal_offset = helper.require_one(
            helper.find_all(macho, struct.pack("<I", string_va)),
            f"literal for {string!r}")
        return helper.file_to_va(segments, literal_offset)

    wanted_literals = {
        string_literal(USB_SERVICE): "usb_service",
        string_literal(USB_FUNCTION): "usb_function",
    }
    xrefs: dict[str, int] = {}
    prelink_bytes = macho[prelink.fileoff:prelink.fileoff + prelink.filesize]
    for offset in range(0, len(prelink_bytes) - 3, 4):
        decoded = list(disassembler.disasm(
            prelink_bytes[offset:offset + 4], prelink.vmaddr + offset, count=1))
        if not decoded:
            continue
        insn = decoded[0]
        if (not insn.mnemonic.startswith("ldr") or len(insn.operands) < 2 or
                insn.operands[1].type != arm_op_mem or
                insn.operands[1].mem.base != arm_reg_pc):
            continue
        literal = (insn.address + 8 + insn.operands[1].mem.disp) & 0xFFFFFFFF
        name = wanted_literals.get(literal)
        if name:
            if name in xrefs:
                raise ValueError(f"multiple ARM xrefs found for {name}")
            xrefs[name] = insn.address

    if set(xrefs) != {"usb_service", "usb_function"}:
        raise ValueError("could not find both IOIpodUSBDevice string xrefs")
    start = xrefs["usb_service"] - 0xB8
    if xrefs["usb_function"] != start + 0xCC:
        raise ValueError("USB strings do not match the known start-method layout")

    def loaded_target(address: int) -> int:
        offset = helper.va_to_file(segments, address)
        decoded = list(disassembler.disasm(macho[offset:offset + 4], address, count=1))
        if not decoded:
            raise ValueError(f"cannot decode loaded call at {address:#x}")
        insn = decoded[0]
        if (not insn.mnemonic.startswith("ldr") or len(insn.operands) < 2 or
                insn.operands[1].type != arm_op_mem or
                insn.operands[1].mem.base != arm_reg_pc):
            raise ValueError(f"expected PC-relative load at {address:#x}")
        literal = (insn.address + 8 + insn.operands[1].mem.disp) & 0xFFFFFFFF
        literal_offset = helper.va_to_file(segments, literal)
        return struct.unpack_from("<I", macho, literal_offset)[0]

    service_matching = loaded_target(start + 0xB4) & ~1
    wait_for_service = loaded_target(start + 0xC4) & ~1
    arm_function_with_cstring = loaded_target(start + 0xD0) & ~1

    # Both 1.1.4 kernels have the same waitForService instruction layout.
    # The direct Thumb BL at +0x22 calls getExistingServices.
    wait_offset = helper.va_to_file(segments, wait_for_service)
    thumb = Cs(arch, 0x10)  # CS_MODE_THUMB; avoid another optional import.
    thumb.detail = True
    call = list(thumb.disasm(
        macho[wait_offset + 0x22:wait_offset + 0x26],
        wait_for_service + 0x22, count=1))
    if (not call or call[0].mnemonic != "bl" or not call[0].operands or
            call[0].operands[0].type != arm_op_imm):
        raise ValueError("waitForService does not contain the expected enumerator call")
    get_existing = call[0].operands[0].imm & 0xFFFFFFFF

    return {
        "code_modes": {"driver": "ARM", "service_core": "Thumb"},
        "usb_device_start": f"0x{start:08x}",
        "service_matching": f"0x{service_matching:08x}",
        "wait_for_service": f"0x{wait_for_service:08x}",
        "get_existing_services": f"0x{get_existing:08x}",
        "service_observation_points": {
            "candidate": f"0x{get_existing + 0x60:08x}",
            "matched": f"0x{get_existing + 0x88:08x}",
            "iterator_release": f"0x{get_existing + 0xba:08x}",
            "return": f"0x{get_existing + 0xd0:08x}",
        },
        "apple_arm_function": {
            "with_cstring": f"0x{arm_function_with_cstring:08x}",
            "with_function": f"0x{arm_function_with_cstring - 0xe8:08x}",
            "parent_wait_done": f"0x{arm_function_with_cstring - 0x88:08x}",
            "init_failure": f"0x{arm_function_with_cstring - 0x2e:08x}",
        },
        "anchors": {
            "usb_service_xref": f"0x{xrefs['usb_service']:08x}",
            "usb_function_xref": f"0x{xrefs['usb_function']:08x}",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kernel", type=Path,
                        help="decrypted Mach-O or S5L8900 89001.0 kernelcache")
    parser.add_argument("--openssl", default=shutil.which("openssl") or "openssl")
    parser.add_argument("--gid-key", default=None,
                        help="override the S5L8900 GID key used for a container")
    args = parser.parse_args()
    try:
        helper = load_helper()
        macho, source = load_macho(
            args.kernel, helper, args.openssl, args.gid_key or helper.GID_KEY)
        result = {
            "kernel": {
                **source,
                "macho_sha256": hashlib.sha256(macho).hexdigest(),
                "macho_size": len(macho),
            },
            "ioipod_usb_device": locate_usb_start(macho, helper),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
