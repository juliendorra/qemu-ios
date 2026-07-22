#!/usr/bin/env python3
"""Locate and verify the M68AP kernelcache AppleNANDFTL FTL_Open path.

The iPhone1,1 1.1.4 kernelcache is an S5L8900 ``89001.0`` container whose
payload decrypts with the SoC GID key and then uses Apple's ``complzss``
wrapper.  This tool performs that pipeline in memory, validates the Adler-32,
parses the resulting 32-bit Mach-O, and identifies the stripped Whimory
``FTL_Open`` function from its success string and the WMR call site.

No decrypted Apple artifact is written unless the caller explicitly requests
``--macho-out``.  The normal output is machine-readable JSON suitable for a
bring-up log or provenance record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path


GID_KEY = "188458A6D15034DFE386F23B61D43774"
CONTAINER_HEADER_SIZE = 0x800
COMPLZSS_HEADER_SIZE = 0x180
MH_MAGIC = 0xFEEDFACE
LC_SEGMENT = 0x1

FTL_OPEN_OK = b"[FTL:MSG] FTL_Open\t\t\t[OK]\n\0"
LOAD_FTL_CXT_FAILURE = b"[FTL:WRN] Failure running _LoadFTLCxt!\n\0"
FTL_VERSION = 0x46560000
FTL_VERSION_NOT = 0xB9A9FFFF


@dataclass(frozen=True)
class Segment:
    name: str
    vmaddr: int
    vmsize: int
    fileoff: int
    filesize: int

    def file_to_va(self, offset: int) -> int | None:
        if self.fileoff <= offset < self.fileoff + self.filesize:
            return self.vmaddr + offset - self.fileoff
        return None

    def va_to_file(self, address: int) -> int | None:
        if self.vmaddr <= address < self.vmaddr + self.filesize:
            return self.fileoff + address - self.vmaddr
        return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decrypt_8900_container(container: bytes, openssl: str, key: str) -> bytes:
    if len(container) < CONTAINER_HEADER_SIZE or not container.startswith(b"89001.0"):
        raise ValueError("not a recognised 89001.0 kernelcache container")
    encrypted_size = struct.unpack_from("<I", container, 0xC)[0]
    encrypted = container[
        CONTAINER_HEADER_SIZE:CONTAINER_HEADER_SIZE + encrypted_size]
    if len(encrypted) != encrypted_size or encrypted_size % 16:
        raise ValueError("truncated or unaligned 8900 encrypted payload")
    proc = subprocess.run(
        [openssl, "enc", "-d", "-aes-128-cbc", "-K", key,
         "-iv", "0" * 32, "-nopad"],
        input=encrypted, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise ValueError(
            "openssl failed to decrypt the kernelcache: "
            + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def decode_lzss(source: bytes, expected_size: int) -> bytes:
    """Decode the classic 4 KiB-window LZSS stream used by complzss."""
    window_size = 4096
    lookahead = 18
    threshold = 2
    window = bytearray(b" " * (window_size + lookahead - 1))
    write_index = window_size - lookahead
    flags = 0
    source_index = 0
    output = bytearray()

    while source_index < len(source) and len(output) < expected_size:
        flags >>= 1
        if not flags & 0x100:
            flags = source[source_index] | 0xFF00
            source_index += 1
        if flags & 1:
            if source_index >= len(source):
                break
            value = source[source_index]
            source_index += 1
            output.append(value)
            window[write_index] = value
            write_index = (write_index + 1) & (window_size - 1)
            continue

        if source_index + 1 >= len(source):
            break
        read_index = source[source_index]
        count = source[source_index + 1]
        source_index += 2
        read_index |= (count & 0xF0) << 4
        count = (count & 0x0F) + threshold
        for index in range(count + 1):
            value = window[(read_index + index) & (window_size - 1)]
            output.append(value)
            window[write_index] = value
            write_index = (write_index + 1) & (window_size - 1)
            if len(output) == expected_size:
                break

    if len(output) != expected_size:
        raise ValueError(
            f"complzss decoded {len(output):#x} bytes, expected {expected_size:#x}")
    return bytes(output)


def decompress_complzss(payload: bytes) -> tuple[bytes, dict]:
    if len(payload) < COMPLZSS_HEADER_SIZE or payload[:8] != b"complzss":
        raise ValueError("decrypted payload does not start with complzss")
    expected_adler, uncompressed_size, compressed_size = struct.unpack_from(
        ">III", payload, 8)
    compressed = payload[
        COMPLZSS_HEADER_SIZE:COMPLZSS_HEADER_SIZE + compressed_size]
    if len(compressed) != compressed_size:
        raise ValueError("truncated complzss payload")
    macho = decode_lzss(compressed, uncompressed_size)
    actual_adler = zlib.adler32(macho) & 0xFFFFFFFF
    if actual_adler != expected_adler:
        raise ValueError(
            f"complzss Adler-32 mismatch: {actual_adler:#010x} != "
            f"{expected_adler:#010x}")
    return macho, {
        "compressed_size": compressed_size,
        "uncompressed_size": uncompressed_size,
        "adler32": f"0x{actual_adler:08x}",
    }


def parse_segments(macho: bytes) -> list[Segment]:
    if len(macho) < 28 or struct.unpack_from("<I", macho)[0] != MH_MAGIC:
        raise ValueError("decompressed payload is not a 32-bit little-endian Mach-O")
    command_count = struct.unpack_from("<I", macho, 16)[0]
    offset = 28
    segments = []
    for _ in range(command_count):
        if offset + 8 > len(macho):
            raise ValueError("truncated Mach-O load command")
        command, command_size = struct.unpack_from("<II", macho, offset)
        if command_size < 8 or offset + command_size > len(macho):
            raise ValueError("invalid Mach-O load command size")
        if command == LC_SEGMENT:
            if command_size < 56:
                raise ValueError("truncated LC_SEGMENT")
            fields = struct.unpack_from("<II16sIIIIIIII", macho, offset)
            name = fields[2].split(b"\0", 1)[0].decode("ascii", "replace")
            segments.append(Segment(name, fields[3], fields[4], fields[5], fields[6]))
        offset += command_size
    return segments


def file_to_va(segments: list[Segment], offset: int) -> int:
    for segment in segments:
        address = segment.file_to_va(offset)
        if address is not None:
            return address
    raise ValueError(f"file offset {offset:#x} is not backed by a Mach-O segment")


def va_to_file(segments: list[Segment], address: int) -> int:
    for segment in segments:
        offset = segment.va_to_file(address)
        if offset is not None:
            return offset
    raise ValueError(f"VA {address:#x} is not backed by a Mach-O segment")


def find_all(data: bytes, needle: bytes) -> list[int]:
    found = []
    offset = 0
    while True:
        offset = data.find(needle, offset)
        if offset < 0:
            return found
        found.append(offset)
        offset += 1


def require_one(values: list[int], description: str) -> int:
    if len(values) != 1:
        raise ValueError(f"expected one {description}, found {len(values)}")
    return values[0]


def load_capstone():
    try:
        from capstone import CS_ARCH_ARM, CS_MODE_ARM, Cs
        from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG, ARM_REG_PC
    except ImportError as exc:
        raise ValueError(
            "the Python capstone module is required for AppleNANDFTL analysis") from exc
    return Cs, CS_ARCH_ARM, CS_MODE_ARM, ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG, ARM_REG_PC


def locate_ftl_open(macho: bytes, segments: list[Segment]) -> dict:
    (Cs, arch, mode, arm_op_imm, arm_op_mem, arm_op_reg,
     arm_reg_pc) = load_capstone()
    prelink = next((segment for segment in segments if segment.name == "__PRELINK"), None)
    if prelink is None:
        raise ValueError("Mach-O has no __PRELINK segment")

    disassembler = Cs(arch, mode)
    disassembler.detail = True

    def pc_literal(instruction) -> int | None:
        if (not instruction.mnemonic.startswith("ldr") or
                len(instruction.operands) < 2 or
                instruction.operands[1].type != arm_op_mem or
                instruction.operands[1].mem.base != arm_reg_pc):
            return None
        return instruction.address + 8 + instruction.operands[1].mem.disp

    # __PRELINK contains ARM code, Thumb code, strings, and tables.  A single
    # Capstone stream stops at the first word it cannot decode, so decode every
    # aligned ARM word independently and retain the valid instructions.
    prelink_bytes = macho[
        prelink.fileoff:prelink.fileoff + prelink.filesize]
    instructions = []
    for offset in range(0, len(prelink_bytes) - 3, 4):
        decoded = list(disassembler.disasm(
            prelink_bytes[offset:offset + 4], prelink.vmaddr + offset, count=1))
        if decoded:
            instructions.append(decoded[0])

    success_offset = require_one(find_all(macho, FTL_OPEN_OK), "FTL_Open success string")
    success_va = file_to_va(segments, success_offset)
    success_literal_offsets = find_all(macho, struct.pack("<I", success_va))
    success_literal_vas = {file_to_va(segments, item) for item in success_literal_offsets}
    success_xrefs = [
        insn for insn in instructions if pc_literal(insn) in success_literal_vas]
    success_xref = require_one(
        [insn.address for insn in success_xrefs], "ARM xref to FTL_Open success string")

    window_start = success_xref - 0x80
    window_offset = va_to_file(segments, window_start)
    wrapper = list(disassembler.disasm(
        macho[window_offset:window_offset + 0x80], window_start))
    loaded_calls = []
    for first, second in zip(wrapper, wrapper[1:]):
        literal_va = pc_literal(first)
        if (literal_va is None or len(first.operands) < 1 or
                first.operands[0].type != arm_op_reg or
                second.mnemonic != "blx" or not second.operands or
                second.operands[0].type != arm_op_reg or
                first.operands[0].reg != second.operands[0].reg):
            continue
        literal_offset = va_to_file(segments, literal_va)
        target = struct.unpack_from("<I", macho, literal_offset)[0]
        if prelink.vmaddr <= target < prelink.vmaddr + prelink.filesize:
            loaded_calls.append((first.address, second.address, target))
    loaded_calls = [item for item in loaded_calls if item[1] < success_xref]
    if not loaded_calls:
        raise ValueError("could not locate the loaded call preceding FTL_Open success")
    load_call, call_site, ftl_open = loaded_calls[-1]

    failure_offset = require_one(
        find_all(macho, LOAD_FTL_CXT_FAILURE), "_LoadFTLCxt failure string")
    failure_va = file_to_va(segments, failure_offset)
    failure_literal_vas = {
        file_to_va(segments, item)
        for item in find_all(macho, struct.pack("<I", failure_va))}
    failure_xrefs = [
        insn.address for insn in instructions if pc_literal(insn) in failure_literal_vas]
    failure_xref = require_one(failure_xrefs, "ARM xref to _LoadFTLCxt failure string")

    fallback_offset = va_to_file(segments, failure_xref)
    fallback_window = list(disassembler.disasm(
        macho[fallback_offset:fallback_offset + 0x40], failure_xref))
    direct_calls = [
        insn for insn in fallback_window
        if insn.mnemonic == "bl" and insn.operands and
        insn.operands[0].type == arm_op_imm]
    if not direct_calls:
        raise ValueError("could not locate _FTLRestore call after _LoadFTLCxt failure")
    restore_call = direct_calls[0]
    # Capstone exposes high ARM addresses as signed Python integers on some
    # versions; report the architectural 32-bit address.
    ftl_restore = restore_call.operands[0].imm & 0xFFFFFFFF

    version_pair = struct.pack("<II", FTL_VERSION, FTL_VERSION_NOT)
    version_pair_offset = require_one(
        find_all(macho, version_pair), "FTL version/complement literal pair")
    version_literal_va = file_to_va(segments, version_pair_offset)
    version_not_literal_va = version_literal_va + 4
    version_xrefs = [
        insn.address for insn in instructions
        if pc_literal(insn) in (version_literal_va, version_not_literal_va) and
        ftl_open <= insn.address < failure_xref]
    context_loads = {}
    check_start = min(version_xrefs) if version_xrefs else ftl_open
    check_offset = va_to_file(segments, check_start)
    for insn in disassembler.disasm(macho[check_offset:check_offset + 0x40], check_start):
        if (insn.mnemonic.startswith("ldr") and len(insn.operands) >= 2 and
                insn.operands[1].type == arm_op_mem and
                insn.operands[1].mem.disp in (0x7F8, 0x7FC)):
            context_loads[f"0x{insn.operands[1].mem.disp:x}"] = f"0x{insn.address:08x}"

    return {
        "code_mode": "ARM",
        "ftl_open": f"0x{ftl_open:08x}",
        "wmr_loaded_call": {
            "load": f"0x{load_call:08x}",
            "call": f"0x{call_site:08x}",
            "success_log": f"0x{success_xref:08x}",
        },
        "load_ftl_context_failure_log": f"0x{failure_xref:08x}",
        "ftl_restore": {
            "call": f"0x{restore_call.address:08x}",
            "target": f"0x{ftl_restore:08x}",
        },
        "context_version_check": {
            "version": f"0x{FTL_VERSION:08x}",
            "version_not": f"0x{FTL_VERSION_NOT:08x}",
            "loads": context_loads,
        },
    }


def inspect_ftl_meta(path: Path) -> dict:
    page = path.read_bytes()
    if len(page) != 2048 + 64:
        raise ValueError(f"{path}: expected a 2112-byte NAND page")
    return {
        "path": str(path),
        "sha256": sha256(page),
        "dwVersion_at_0x7f8": f"0x{struct.unpack_from('<I', page, 0x7F8)[0]:08x}",
        "dwVersionNot_at_0x7fc": f"0x{struct.unpack_from('<I', page, 0x7FC)[0]:08x}",
        "spare_context_age": struct.unpack_from("<I", page, 0x800)[0],
        "spare_type": f"0x{page[0x809]:02x}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kernelcache", type=Path,
                        help="iPhone1,1 1.1.4 kernelcache.release.s5l8900xrb")
    parser.add_argument("--ftl-meta-page", type=Path,
                        help="optional physical 2112-byte FTL metadata page")
    parser.add_argument("--macho-out", type=Path,
                        help="optional output for the decrypted Mach-O; never commit it")
    parser.add_argument("--openssl", default=shutil.which("openssl") or "openssl")
    parser.add_argument("--gid-key", default=GID_KEY)
    args = parser.parse_args()

    try:
        container = args.kernelcache.read_bytes()
        decrypted = decrypt_8900_container(container, args.openssl, args.gid_key)
        macho, compression = decompress_complzss(decrypted)
        segments = parse_segments(macho)
        analysis = locate_ftl_open(macho, segments)
        result = {
            "kernelcache": {
                "path": str(args.kernelcache),
                "sha256": sha256(container),
                "container": "89001.0",
                **compression,
                "macho_sha256": sha256(macho),
                "macho_magic": "0xfeedface",
            },
            "apple_nand_ftl": analysis,
        }
        if args.ftl_meta_page:
            result["ftl_meta_page"] = inspect_ftl_meta(args.ftl_meta_page)
        if args.macho_out:
            args.macho_out.write_bytes(macho)
            result["kernelcache"]["macho_out"] = str(args.macho_out)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
