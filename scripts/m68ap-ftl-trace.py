#!/usr/bin/env python3
"""Run one bounded, machine-readable M68AP AppleNANDFTL trace.

The input NAND must already be a staged, generated full-root tree.  This tool
never touches installed firmware: it rebuilds the current synthetic M68AP NOR
and patches a scratch iBoot inside the log directory, then boots QEMU with the
opt-in ``m68ap-ftl-trace`` plugin.  The plugin records the executed FTL_Open
basic blocks and exits at either clean-open success or the FTLRestore fallback.

Example::

  python3 scripts/m68ap-ftl-trace.py \
    --nand /private/tmp/m68ap-ftl-fresh/nand \
    --logs /private/tmp/m68ap-ftl-result

The JSON report and all logs remain under ``--logs``.  A hard timeout is always
enforced.  Apple-derived scratch outputs are never committed.
"""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_PLUGIN = (REPO / "build-ipod11" / "contrib" / "plugins" /
                  "libm68ap-ftl-trace.dylib")
DEFAULT_BOOTROM = IPOD_FILES / "bootrom_s5l8900"
# 4A102 by name, not by default: this tracer's expected-hash constant below
# was measured against 1.1.4's FTL metadata, so it is genuinely 1.1.4-specific
# rather than merely defaulting to it.
DEFAULT_IBOOT = (REPO / "m68ap-artifacts" / "builds" / "4A102" / "ipsw" /
                 "extracted" / "iboot_204_m68ap.bin")
DEFAULT_TEMPLATE = IPOD_FILES / "nor_n45ap.bin"
DEFAULT_CONTAINERS = (REPO / "m68ap-artifacts" / "builds" / "4A102" / "ipsw" /
                      "extracted" / "nor-containers")

EXPECTED_FTL_META_SHA256 = (
    "4877ba691c75b2134949c3e5a048700dc627d04ba0d65be842382c45519b7e3c")
TRACE_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE block=(0x[0-9a-f]+) insn=(.*)$")
TRACE_VERDICT_RE = re.compile(
    r"^M68AP_FTL_TRACE verdict=(success|restore) address=(0x[0-9a-f]+)$")
PANIC_RE = re.compile(
    r"^M68AP_FTL_TRACE pre_ftl_panic=(0x[0-9a-f]+) "
    r"r0=(0x[0-9a-f]+) sp=(0x[0-9a-f]+) lr=(0x[0-9a-f]+)$")
STACK_RE = re.compile(r"^M68AP_FTL_TRACE stack=([0-9a-f]+)$")
DIAGNOSTIC_POINT_RE = re.compile(
    r"^M68AP_FTL_TRACE diagnostic_point=(0x[0-9a-f]+) (.*)$")
GPIO_PROPERTY_RE = re.compile(
    r"^M68AP_FTL_TRACE gpio_function_parent_property=(\S+) "
    r"service=(0x[0-9a-f]+)$")
SERVICE_MATCH_RE = re.compile(
    r"^M68AP_FTL_TRACE service_match event=(\S+) pc=(0x[0-9a-f]+) "
    r"object=(0x[0-9a-f]+) vtable=(0x[0-9a-f]+) "
    r"references=(0x[0-9a-f]+) state=(0x[0-9a-f]+) "
    r"iterator=(0x[0-9a-f]+) iterator_references=(0x[0-9a-f]+)$")
ROOT_DOMAIN_RELEASE_RE = re.compile(
    r"^M68AP_FTL_TRACE root_domain_release event=(\S+) pc=(0x[0-9a-f]+) "
    r"object=(0x[0-9a-f]+) references=(0x[0-9a-f]+) "
    r"r1=(0x[0-9a-f]+) r2=(0x[0-9a-f]+) "
    r"r4=(0x[0-9a-f]+) r6=(0x[0-9a-f]+) "
    r"sp=(0x[0-9a-f]+) lr=(0x[0-9a-f]+)$")
FTL_CONTEXT_EDGE_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_context_edge pc=(0x[0-9a-f]+) "
    r"r0=(0x[0-9a-f]+) r4=(0x[0-9a-f]+) "
    r"r5=(0x[0-9a-f]+) r8=(0x[0-9a-f]+) "
    r"fp=(0x[0-9a-f]+) buffer=(0x[0-9a-f]+) "
    r"data=(0x[0-9a-f]+) spare=(0x[0-9a-f]+) "
    r"age=(0x[0-9a-f]+) spare_word_8=(0x[0-9a-f]+) "
    r"version=(0x[0-9a-f]+) version_not=(0x[0-9a-f]+)$")
NAND_READ_RE = re.compile(
    r"^itnand_read_page bank=(\d+) page=(\d+) present=(\d+) "
    r"spare_type=(0x[0-9a-f]+)$")
NAND_ID_RE = re.compile(
    r"^itnand_id bank=(-?\d+) value=(0x[0-9a-f]+) active_banks=(\d+)$")
ROOT_PAGE_RE = re.compile(
    r"^itnand_root_page bank=(\d+) page=(\d+) present=(\d+) "
    r"word_0=(0x[0-9a-f]+) word_20=(0x[0-9a-f]+) "
    r"word_400=(0x[0-9a-f]+) "
    r"spare_type=(0x[0-9a-f]+)$")
MOUNTROOT_RESULT_RE = re.compile(
    r"^M68AP_FTL_TRACE mountroot_result attempt=(\d+) error=(\d+) "
    r"rootdev=(0x[0-9a-f]+) rootvp=(0x[0-9a-f]+) "
    r"rootdevice=(\S*)$")
ADM_ROOT_READ_RE = re.compile(
    r"^itadm_root_read cmd=(0x[0-9a-f]+) count=(\d+) index=(\d+) "
    r"bank=(\d+) page=(\d+)$")
VFS_MOUNT_RE = re.compile(
    r"^M68AP_FTL_TRACE vfs_mount event=(\S+) call=(\d+) "
    r"entry=(0x[0-9a-f]+) name=(\S+) callback=(0x[0-9a-f]+) "
    r"error=(\d+)$")
HFS_MOUNT_RESULT_RE = re.compile(
    r"^M68AP_FTL_TRACE hfs_mount_result error=(\d+)$")
HFS_STAGE_RESULT_RE = re.compile(
    r"^M68AP_FTL_TRACE hfs_stage_result stage=(\S+) error=(\d+)$")
HFS_MOUNTFS_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE hfs_mountfs_block=(0x[0-9a-f]+) insn=(.*)$")
BT_OPEN_STAGE_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_open_stage stage=(\S+) error=(-?\d+) "
    r"fcb=(0x[0-9a-f]+) control=(0x[0-9a-f]+) "
    r"logical_size=(0x[0-9a-f]+)$")
VERIFY_HEADER_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE verify_header_block=(0x[0-9a-f]+) insn=(.*)$")
STORAGE_STRATEGY_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE storage_strategy_block=(0x[0-9a-f]+) insn=(.*)$")
BLOCK_STORAGE_READ_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE block_storage_read_block=(0x[0-9a-f]+) insn=(.*)$")
FTL_CORE_READ_BLOCK_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_core_read_block=(0x[0-9a-f]+) insn=(.*)$")
VERIFY_HEADER_FAILURE_RE = re.compile(
    r"^M68AP_FTL_TRACE verify_header_failure header=(0x[0-9a-f]+) "
    r"r0=(0x[0-9a-f]+) r1=(0x[0-9a-f]+) "
    r"r2=(0x[0-9a-f]+) r3=(0x[0-9a-f]+) "
    r"r4=(0x[0-9a-f]+) r6=(0x[0-9a-f]+) "
    r"fcb=(0x[0-9a-f]+) bytes=([0-9a-f]*)$")
BT_BUFFER_IO_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_buffer_io event=(\S+) pc=(0x[0-9a-f]+) "
    r"buffer=(0x[0-9a-f]+) result=(0x[0-9a-f]+) "
    r"flags=(0x[0-9a-f]+) vnode=(0x[0-9a-f]+) "
    r"data=(0x[0-9a-f]+) lblk_lo=(0x[0-9a-f]+) "
    r"lblk_hi=(0x[0-9a-f]+) blk_lo=(0x[0-9a-f]+) "
    r"blk_hi=(0x[0-9a-f]+)$")
BT_DEVICE_STRATEGY_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_device_strategy target=(0x[0-9a-f]+) "
    r"device=(0x[0-9a-f]+) buffer=(0x[0-9a-f]+) "
    r"blk_lo=(0x[0-9a-f]+) blk_hi=(0x[0-9a-f]+)$")
BT_STORAGE_STRATEGY_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_storage_strategy target=(0x[0-9a-f]+) "
    r"device_number=(0x[0-9a-f]+) buffer=(0x[0-9a-f]+) "
    r"blk_lo=(0x[0-9a-f]+) blk_hi=(0x[0-9a-f]+)$")
BT_PROVIDER_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_provider_read target=(0x[0-9a-f]+) "
    r"provider=(0x[0-9a-f]+) client=(0x[0-9a-f]+) "
    r"memory=(0x[0-9a-f]+) offset_lo=(0x[0-9a-f]+) "
    r"offset_hi=(0x[0-9a-f]+) completion_target=(0x[0-9a-f]+)$")
BT_MEDIA_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_media_read target=(0x[0-9a-f]+) "
    r"provider=(0x[0-9a-f]+) client=(0x[0-9a-f]+) "
    r"memory=(0x[0-9a-f]+) offset_lo=(0x[0-9a-f]+) "
    r"offset_hi=(0x[0-9a-f]+)$")
BT_BLOCK_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_block_read target=(0x[0-9a-f]+) "
    r"provider=(0x[0-9a-f]+) offset_lo=(0x[0-9a-f]+) "
    r"offset_hi=(0x[0-9a-f]+) memory=(0x[0-9a-f]+)$")
BT_BLOCK_SUBMIT_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_block_submit target=(0x[0-9a-f]+) "
    r"driver=(0x[0-9a-f]+) request=(0x[0-9a-f]+) "
    r"offset_lo=(0x[0-9a-f]+) offset_hi=(0x[0-9a-f]+) "
    r"memory=(0x[0-9a-f]+)$")
BT_ASYNC_SUBMIT_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_async_submit target=(0x[0-9a-f]+) "
    r"driver=(0x[0-9a-f]+) offset_lo=(0x[0-9a-f]+) "
    r"offset_hi=(0x[0-9a-f]+) memory=(0x[0-9a-f]+) "
    r"request=(0x[0-9a-f]+)$")
BT_EXECUTE_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_execute target=(0x[0-9a-f]+) "
    r"driver=(0x[0-9a-f]+) offset_lo=(0x[0-9a-f]+) "
    r"offset_hi=(0x[0-9a-f]+) request=(0x[0-9a-f]+)$")
BT_DEVICE_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_device_read target=(0x[0-9a-f]+) "
    r"device=(0x[0-9a-f]+) memory=(0x[0-9a-f]+) "
    r"offset_lo=(0x[0-9a-f]+) offset_hi=(0x[0-9a-f]+)$")
BT_PHYSICAL_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_physical_read target=(0x[0-9a-f]+) "
    r"device=(0x[0-9a-f]+) memory=(0x[0-9a-f]+) "
    r"block=(0x[0-9a-f]+) count=(0x[0-9a-f]+)$")
BT_DRIVER_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE bt_driver_read target=(0x[0-9a-f]+) "
    r"provider=(0x[0-9a-f]+) memory=(0x[0-9a-f]+) "
    r"block=(0x[0-9a-f]+) count=(0x[0-9a-f]+)$")
FTL_DATA_READ_CALL_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_data_read variant=(direct|mapped) event=call "
    r"target=(0x[0-9a-f]+) block=(0x[0-9a-f]+) "
    r"count=(0x[0-9a-f]+) flags=(0x[0-9a-f]+)$")
FTL_DATA_READ_RETURN_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_data_read variant=(direct|mapped) event=return "
    r"result=(0x[0-9a-f]+)$")
FTL_MAP_DECISION_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_map_decision available=(0x[0-9a-f]+) "
    r"requested=(0x[0-9a-f]+) processed=(0x[0-9a-f]+) "
    r"page_offset=(0x[0-9a-f]+)$")
FTL_PAGE_READ_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_page_read variant=(single|run) "
    r"physical_page=(0x[0-9a-f]+) count=(0x[0-9a-f]+) "
    r"descriptor=(0x[0-9a-f]+) flags=(0x[0-9a-f]+)$")
FTL_PHYSICAL_IO_RE = re.compile(
    r"^M68AP_FTL_TRACE ftl_physical_io event=(\S+)(?: (.*))?$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_log(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_bytes().decode("latin1", "replace").replace("\r", "")


def decode_panic_stack(registers: dict | None, stack_hex: str | None) -> dict:
    """Decode the bounded stack snapshot emitted at the pre-FTL panic.

    The M68 kernel uses a simple ARM frame chain here.  Keeping this decoder in
    the harness turns a raw diagnostic capture into a stable reason and return-
    address list without an interactive disassembly pass after every boot.
    """
    if not registers or not stack_hex:
        return {"stack_ascii": [], "panic_reason": None,
                "return_addresses": []}
    try:
        stack = bytes.fromhex(stack_hex)
    except ValueError:
        return {"stack_ascii": [], "panic_reason": None,
                "return_addresses": []}

    ascii_runs = [match.decode("ascii") for match in
                  re.findall(rb"[ -~]{8,}", stack)]
    panic_reason = next(
        (text for text in ascii_runs if "attached at free()" in text), None)

    stack_base = int(registers["sp"], 16)

    def word(address: int) -> int | None:
        offset = address - stack_base
        if offset < 0 or offset + 4 > len(stack):
            return None
        return int.from_bytes(stack[offset:offset + 4], "little")

    return_addresses = [registers["lr"]]
    frame = word(stack_base)
    saved_lr = word(stack_base + 4)
    if saved_lr:
        return_addresses.append(f"0x{saved_lr:08x}")
    visited = set()
    while frame is not None and frame not in visited:
        visited.add(frame)
        next_frame = word(frame)
        saved_lr = word(frame + 4)
        if next_frame is None or saved_lr is None:
            break
        if saved_lr:
            return_addresses.append(f"0x{saved_lr:08x}")
        if next_frame <= frame:
            break
        frame = next_frame
    return {"stack_ascii": ascii_runs, "panic_reason": panic_reason,
            "return_addresses": return_addresses}


def parse_trace(plugin_text: str) -> dict:
    blocks = []
    verdict = None
    verdict_address = None
    diagnostic_usb_skip = False
    diagnostic_sdio_skip = False
    diagnostic_root_stabilization = False
    pre_ftl_panic = False
    panic_registers = None
    stack_hex = None
    diagnostic_points = []
    gpio_function_parent = None
    service_matching = []
    root_domain_releases = []
    ftl_context_edges = []
    mountroot_results = []
    vfs_mount_events = []
    hfs_mount_results = []
    hfs_stage_results = []
    hfs_mountfs_blocks = []
    bt_open_stages = []
    verify_header_blocks = []
    storage_strategy_blocks = []
    block_storage_read_blocks = []
    ftl_core_read_blocks = []
    verify_header_failure = None
    bt_buffer_io = []
    bt_device_strategy = []
    bt_storage_strategy = []
    bt_provider_reads = []
    bt_media_reads = []
    bt_block_reads = []
    bt_block_submits = []
    bt_async_submits = []
    bt_executes = []
    bt_device_reads = []
    bt_physical_reads = []
    bt_driver_reads = []
    ftl_data_reads = []
    ftl_map_decisions = []
    ftl_page_reads = []
    ftl_physical_io = []
    diagnostics = []
    for raw_line in plugin_text.splitlines():
        line = raw_line.strip()
        match = TRACE_BLOCK_RE.match(line)
        if match:
            blocks.append({"address": match.group(1),
                           "first_instruction": match.group(2)})
            continue
        match = TRACE_VERDICT_RE.match(line)
        if match:
            verdict, verdict_address = match.groups()
            continue
        if "diagnostic IOIpodUSBDevice" in line and "skip applied" in line:
            diagnostic_usb_skip = True
        if "diagnostic AppleS5L8900XSDIO start skip applied" in line:
            diagnostic_sdio_skip = True
        if "diagnostic root-domain stabilization applied" in line:
            diagnostic_root_stabilization = True
        match = PANIC_RE.match(line)
        if match:
            pre_ftl_panic = True
            panic, r0, sp, lr = match.groups()
            panic_registers = {"panic": panic, "r0": r0, "sp": sp,
                               "lr": lr}
        match = STACK_RE.match(line)
        if match:
            stack_hex = match.group(1)
        match = DIAGNOSTIC_POINT_RE.match(line)
        if match:
            registers = dict(re.findall(
                r"(r[0-6]|sp|lr)=(0x[0-9a-f]+)", match.group(2)))
            diagnostic_points.append({"address": match.group(1),
                                      "registers": registers})
        match = GPIO_PROPERTY_RE.match(line)
        if match:
            gpio_function_parent = {"property": match.group(1),
                                    "service": match.group(2)}
        match = SERVICE_MATCH_RE.match(line)
        if match:
            (event, pc, obj, vtable, references, state, iterator,
             iterator_references) = match.groups()
            service_matching.append({
                "event": event, "pc": pc, "object": obj,
                "vtable": vtable, "references": references,
                "state": state, "iterator": iterator,
                "iterator_references": iterator_references,
            })
        match = ROOT_DOMAIN_RELEASE_RE.match(line)
        if match:
            (event, pc, obj, references, r1, r2, r4, r6, sp,
             lr) = match.groups()
            root_domain_releases.append({
                "event": event, "pc": pc, "object": obj,
                "references": references, "r1": r1, "r2": r2,
                "r4": r4, "r6": r6, "sp": sp, "lr": lr,
            })
        match = FTL_CONTEXT_EDGE_RE.match(line)
        if match:
            keys = ("pc", "r0", "r4", "r5", "r8", "fp", "buffer",
                    "data", "spare", "age", "spare_word_8", "version",
                    "version_not")
            ftl_context_edges.append(dict(zip(keys, match.groups())))
        match = MOUNTROOT_RESULT_RE.match(line)
        if match:
            attempt, error, rootdev, rootvp, rootdevice = match.groups()
            mountroot_results.append({
                "attempt": int(attempt), "error": int(error),
                "rootdev": rootdev, "rootvp": rootvp,
                "rootdevice": rootdevice,
            })
        match = VFS_MOUNT_RE.match(line)
        if match:
            event, call, entry, name, callback, error = match.groups()
            vfs_mount_events.append({
                "event": event, "call": int(call), "entry": entry,
                "name": name, "callback": callback,
                "error": int(error),
            })
        match = HFS_MOUNT_RESULT_RE.match(line)
        if match:
            hfs_mount_results.append(int(match.group(1)))
        match = HFS_STAGE_RESULT_RE.match(line)
        if match:
            stage, error = match.groups()
            hfs_stage_results.append({"stage": stage, "error": int(error)})
        match = HFS_MOUNTFS_BLOCK_RE.match(line)
        if match:
            address, instruction = match.groups()
            hfs_mountfs_blocks.append({
                "address": address, "first_instruction": instruction,
            })
        match = BT_OPEN_STAGE_RE.match(line)
        if match:
            stage, error, fcb, control, logical_size = match.groups()
            bt_open_stages.append({
                "stage": stage, "error": int(error), "fcb": fcb,
                "control": control, "logical_size": logical_size,
            })
        match = VERIFY_HEADER_BLOCK_RE.match(line)
        if match:
            address, instruction = match.groups()
            verify_header_blocks.append({
                "address": address, "first_instruction": instruction,
            })
        match = STORAGE_STRATEGY_BLOCK_RE.match(line)
        if match:
            address, instruction = match.groups()
            storage_strategy_blocks.append({
                "address": address, "first_instruction": instruction,
            })
        match = BLOCK_STORAGE_READ_BLOCK_RE.match(line)
        if match:
            address, instruction = match.groups()
            block_storage_read_blocks.append({
                "address": address, "first_instruction": instruction,
            })
        match = FTL_CORE_READ_BLOCK_RE.match(line)
        if match:
            address, instruction = match.groups()
            ftl_core_read_blocks.append({
                "address": address, "first_instruction": instruction,
            })
        match = VERIFY_HEADER_FAILURE_RE.match(line)
        if match and verify_header_failure is None:
            keys = ("header", "r0", "r1", "r2", "r3", "r4", "r6",
                    "fcb", "bytes")
            verify_header_failure = dict(zip(keys, match.groups()))
        match = BT_BUFFER_IO_RE.match(line)
        if match:
            keys = ("event", "pc", "buffer", "result", "flags", "vnode",
                    "data", "lblk_lo", "lblk_hi", "blk_lo", "blk_hi")
            bt_buffer_io.append(dict(zip(keys, match.groups())))
        match = BT_DEVICE_STRATEGY_RE.match(line)
        if match:
            keys = ("target", "device", "buffer", "blk_lo", "blk_hi")
            bt_device_strategy.append(dict(zip(keys, match.groups())))
        match = BT_STORAGE_STRATEGY_RE.match(line)
        if match:
            keys = ("target", "device_number", "buffer", "blk_lo", "blk_hi")
            bt_storage_strategy.append(dict(zip(keys, match.groups())))
        match = BT_PROVIDER_READ_RE.match(line)
        if match:
            keys = ("target", "provider", "client", "memory", "offset_lo",
                    "offset_hi", "completion_target")
            bt_provider_reads.append(dict(zip(keys, match.groups())))
        match = BT_MEDIA_READ_RE.match(line)
        if match:
            keys = ("target", "provider", "client", "memory", "offset_lo",
                    "offset_hi")
            bt_media_reads.append(dict(zip(keys, match.groups())))
        match = BT_BLOCK_READ_RE.match(line)
        if match:
            keys = ("target", "provider", "offset_lo", "offset_hi", "memory")
            bt_block_reads.append(dict(zip(keys, match.groups())))
        match = BT_BLOCK_SUBMIT_RE.match(line)
        if match:
            keys = ("target", "driver", "request", "offset_lo", "offset_hi",
                    "memory")
            bt_block_submits.append(dict(zip(keys, match.groups())))
        match = BT_ASYNC_SUBMIT_RE.match(line)
        if match:
            keys = ("target", "driver", "offset_lo", "offset_hi", "memory",
                    "request")
            bt_async_submits.append(dict(zip(keys, match.groups())))
        match = BT_EXECUTE_RE.match(line)
        if match:
            keys = ("target", "driver", "offset_lo", "offset_hi", "request")
            bt_executes.append(dict(zip(keys, match.groups())))
        match = BT_DEVICE_READ_RE.match(line)
        if match:
            keys = ("target", "device", "memory", "offset_lo", "offset_hi")
            bt_device_reads.append(dict(zip(keys, match.groups())))
        match = BT_PHYSICAL_READ_RE.match(line)
        if match:
            keys = ("target", "device", "memory", "block", "count")
            bt_physical_reads.append(dict(zip(keys, match.groups())))
        match = BT_DRIVER_READ_RE.match(line)
        if match:
            keys = ("target", "provider", "memory", "block", "count")
            bt_driver_reads.append(dict(zip(keys, match.groups())))
        match = FTL_DATA_READ_CALL_RE.match(line)
        if match:
            variant, target, block, count, flags = match.groups()
            ftl_data_reads.append({
                "variant": variant, "event": "call", "target": target,
                "block": block, "count": count, "flags": flags,
            })
        match = FTL_DATA_READ_RETURN_RE.match(line)
        if match:
            variant, result = match.groups()
            ftl_data_reads.append({
                "variant": variant, "event": "return", "result": result,
            })
        match = FTL_MAP_DECISION_RE.match(line)
        if match:
            keys = ("available", "requested", "processed", "page_offset")
            ftl_map_decisions.append(dict(zip(keys, match.groups())))
        match = FTL_PAGE_READ_RE.match(line)
        if match:
            keys = ("variant", "physical_page", "count", "descriptor", "flags")
            ftl_page_reads.append(dict(zip(keys, match.groups())))
        match = FTL_PHYSICAL_IO_RE.match(line)
        if match:
            event, fields = match.groups()
            item = {"event": event}
            if fields:
                item.update(dict(re.findall(
                    r"(\w+)=(0x[0-9a-f]+)", fields)))
            ftl_physical_io.append(item)
        if line.startswith("M68AP_FTL_TRACE"):
            diagnostics.append(line)
    panic_details = decode_panic_stack(panic_registers, stack_hex)
    return {
        "blocks": blocks,
        "block_count": len(blocks),
        "verdict": verdict,
        "verdict_address": verdict_address,
        "diagnostic_usb_skip_applied": diagnostic_usb_skip,
        "diagnostic_sdio_skip_applied": diagnostic_sdio_skip,
        "diagnostic_root_stabilization_applied": diagnostic_root_stabilization,
        "pre_ftl_panic": pre_ftl_panic,
        "panic_registers": panic_registers,
        "panic_stack_hex": stack_hex,
        "diagnostic_points": diagnostic_points,
        "gpio_function_parent": gpio_function_parent,
        "service_matching": service_matching,
        "root_domain_releases": root_domain_releases,
        "ftl_context_edges": ftl_context_edges,
        "mountroot_results": mountroot_results,
        "vfs_mount_events": vfs_mount_events,
        "hfs_mount_results": hfs_mount_results,
        "hfs_stage_results": hfs_stage_results,
        "hfs_mountfs_blocks": hfs_mountfs_blocks,
        "bt_open_stages": bt_open_stages,
        "verify_header_blocks": verify_header_blocks,
        "storage_strategy_blocks": storage_strategy_blocks,
        "block_storage_read_blocks": block_storage_read_blocks,
        "ftl_core_read_blocks": ftl_core_read_blocks,
        "verify_header_failure": verify_header_failure,
        "bt_buffer_io": bt_buffer_io,
        "bt_device_strategy": bt_device_strategy,
        "bt_storage_strategy": bt_storage_strategy,
        "bt_provider_reads": bt_provider_reads,
        "bt_media_reads": bt_media_reads,
        "bt_block_reads": bt_block_reads,
        "bt_block_submits": bt_block_submits,
        "bt_async_submits": bt_async_submits,
        "bt_executes": bt_executes,
        "bt_device_reads": bt_device_reads,
        "bt_physical_reads": bt_physical_reads,
        "bt_driver_reads": bt_driver_reads,
        "ftl_data_reads": ftl_data_reads,
        "ftl_map_decisions": ftl_map_decisions,
        "ftl_page_reads": ftl_page_reads,
        "ftl_physical_io": ftl_physical_io,
        **panic_details,
        "diagnostics": diagnostics,
    }


def parse_serial(serial_text: str) -> dict:
    markers = {
        "iboot_ftl_open": "[FTL:MSG] FTL_Open" in serial_text,
        "kernel_banner": "Darwin Kernel Version" in serial_text,
        "kernel_ftl_start": "AppleNANDFTL::start(disk)" in serial_text,
        "kernel_ftl_init": "[FTL:MSG] FTL_Init" in serial_text,
        "kernel_ftl_open_failed": "FTL_Open failed" in serial_text,
        "ftl_restore": "FTLRestore" in serial_text,
        "still_waiting_root": "Still waiting for root device" in serial_text,
        "root_mount_failed": "root filesystem mount failed" in serial_text,
        "bsd_root": "BSD root" in serial_text,
        "panic": "panic(cpu" in serial_text,
        "fatal_exception": "Debugger message: Fatal Exception" in serial_text,
        "launchd": "launchd" in serial_text,
        "springboard": "SpringBoard" in serial_text,
    }
    lines = serial_text.splitlines()
    interesting = [line for line in lines if any(needle in line for needle in (
        "AppleNANDFTL", "[FTL:", "FTLRestore", "ScanForFree",
        "Still waiting for root device", "panic(cpu", "Fatal Exception"))]
    return {"markers": markers, "interesting_tail": interesting[-80:]}


def parse_nand_reads(trace_text: str) -> dict:
    reads = []
    identifications = []
    root_pages = []
    root_read_requests = []
    for raw_line in trace_text.splitlines():
        match = NAND_READ_RE.match(raw_line.strip())
        if match:
            bank, page, present, spare_type = match.groups()
            reads.append({"bank": int(bank), "page": int(page),
                          "present": present == "1",
                          "spare_type": spare_type})
        match = NAND_ID_RE.match(raw_line.strip())
        if match:
            bank, value, active_banks = match.groups()
            identifications.append({
                "bank": int(bank), "value": value,
                "active_banks": int(active_banks),
            })
        match = ROOT_PAGE_RE.match(raw_line.strip())
        if match:
            (bank, page, present, word_0, word_20, word_400,
             spare_type) = match.groups()
            root_pages.append({
                "bank": int(bank), "page": int(page),
                "present": present == "1", "word_0": word_0,
                "word_20": word_20, "word_400": word_400,
                "spare_type": spare_type,
            })
        match = ADM_ROOT_READ_RE.match(raw_line.strip())
        if match:
            cmd, count, index, bank, page = match.groups()
            root_read_requests.append({
                "cmd": cmd, "count": int(count), "index": int(index),
                "bank": int(bank), "page": int(page),
            })
    return {"count": len(reads), "tail": reads[-64:],
            "identifications": identifications,
            "root_pages": root_pages,
            "root_read_requests": root_read_requests}


def classify(trace: dict, serial: dict, timed_out: bool) -> str:
    markers = serial["markers"]
    if markers["springboard"]:
        return "SPRINGBOARD_STARTED"
    if markers["launchd"]:
        return "LAUNCHD_STARTED"
    if markers["kernel_banner"] and (markers["panic"] or
                                     markers["fatal_exception"]):
        return ("POST_ROOT_KERNEL_FAILURE" if markers["bsd_root"]
                else "PRE_ROOT_KERNEL_FAILURE")
    if markers["bsd_root"]:
        return "ROOT_MOUNTED"
    if trace["verdict"] == "success":
        return "FTL_OPEN_SUCCESS"
    if trace["verdict"] == "restore":
        return "FTL_RESTORE_FALLBACK"
    if trace["pre_ftl_panic"]:
        return "PRE_FTL_KERNEL_FAILURE"
    if markers["kernel_ftl_start"]:
        return "PRE_FTL_STALL"
    if timed_out:
        return "TIMEOUT_BEFORE_KERNEL_FTL"
    return "BOOT_FAILED_BEFORE_KERNEL_FTL"


def validate_nand(nand: Path) -> dict:
    if str(nand.resolve()).startswith("/Applications/"):
        raise SystemExit("refusing installed NAND; pass a staged generated copy")
    manifest_path = nand / "nand-provenance.json"
    if not manifest_path.is_file():
        raise SystemExit(f"staged NAND lacks provenance: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    page_count = manifest.get("page_count", 0)
    filesystem = manifest.get("populated_pages", {}).get("filesystem")
    if page_count < 100_000 or not filesystem:
        raise SystemExit(
            f"NAND is not a full-root seed (page_count={page_count}, "
            f"filesystem={bool(filesystem)})")
    meta_info = manifest.get("populated_pages", {}).get("ftl_meta", {})
    meta_bank = meta_info.get("bank")
    meta_page = meta_info.get("page")
    if not isinstance(meta_bank, int) or not 0 <= meta_bank < 8 or \
            not isinstance(meta_page, int) or meta_page < 0:
        raise SystemExit(f"invalid FTL metadata provenance: {meta_info}")
    meta = nand / f"bank{meta_bank}" / f"{meta_page}.page"
    if not meta.is_file():
        raise SystemExit(f"missing FTL metadata page: {meta}")
    meta_hash = sha256_file(meta)
    if meta_hash != EXPECTED_FTL_META_SHA256:
        raise SystemExit(
            f"unexpected FTL metadata hash {meta_hash}; expected "
            f"{EXPECTED_FTL_META_SHA256}")
    return {"manifest": str(manifest_path), "page_count": page_count,
            "filesystem": filesystem, "ftl_meta_sha256": meta_hash,
            "ftl_meta": {"bank": meta_bank, "page": meta_page}}


def require_paths(paths: list[Path]) -> None:
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit("missing required inputs:\n  " + "\n  ".join(missing))


def run_checked(command: list[str]) -> str:
    completed = subprocess.run(
        command, cwd=REPO, check=True, capture_output=True, text=True)
    return completed.stdout + completed.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nand", type=Path, required=True,
                        help="staged full-root NAND generated by build-m68ap-nand.py")
    parser.add_argument("--logs", type=Path,
                        default=Path(f"/private/tmp/m68ap-ftl-trace-{int(time.time())}"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    parser.add_argument("--plugin", type=Path, default=DEFAULT_PLUGIN)
    parser.add_argument("--bootrom", type=Path, default=DEFAULT_BOOTROM)
    parser.add_argument("--iboot", type=Path, default=DEFAULT_IBOOT)
    parser.add_argument("--nor-template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--containers", type=Path, default=DEFAULT_CONTAINERS)
    parser.add_argument("--icount-shift", type=int, default=-1,
                        help="negative for real time (default), otherwise QEMU shift")
    parser.add_argument("--no-skip-usb-start", action="store_true",
                        help="disable the plugin's diagnostic USB-start skip")
    parser.add_argument("--skip-sdio-start", action="store_true",
                        help="diagnostically decline the SDIO driver's start")
    parser.add_argument("--skip-platform-functions", action="store_true",
                        help="diagnostically make platform functions unavailable")
    parser.add_argument("--stabilize-root-domain", action="store_true",
                        help="diagnostically retain the root domain at the known startup race")
    parser.add_argument("--minimal-plugin", action="store_true",
                        help="apply requested diagnostic startup adjustments "
                             "without detailed storage tracing")
    parser.add_argument("--continue-after-verdict", action="store_true",
                        help="continue toward root mount after recording FTL_Open")
    parser.add_argument("--stop-at-verify-failure", action="store_true",
                        help="stop after the first invalid HFS B-tree header")
    args = parser.parse_args()

    require_paths([args.nand, args.qemu, args.plugin, args.bootrom, args.iboot,
                   args.nor_template, args.containers])
    nand_info = validate_nand(args.nand)
    logs = args.logs.resolve()
    if logs.exists() and any(logs.iterdir()):
        raise SystemExit(f"refusing non-empty log directory: {logs}")
    logs.mkdir(parents=True, exist_ok=True)

    staged_nor = logs / "nor_m68ap.bin"
    staged_bootrom = logs / "bootrom_s5l8900"
    staged_iboot = logs / "iboot_204_m68ap_sbpatch.bin"
    shutil.copy2(args.bootrom, staged_bootrom)
    preparation_log = run_checked([
        sys.executable, str(REPO / "scripts" / "build-m68ap-nor.py"),
        "--template", str(args.nor_template),
        "--containers", str(args.containers), "--out", str(staged_nor),
    ])
    preparation_log += run_checked([
        sys.executable, str(REPO / "scripts" / "patch-m68ap-iboot.py"),
        str(args.iboot), str(staged_iboot),
    ])
    (logs / "preparation.log").write_text(preparation_log)

    serial_path = logs / "serial.log"
    plugin_path = logs / "ftl-plugin.log"
    stderr_path = logs / "qemu-stderr.log"
    monitor_path = logs / "monitor.log"
    plugin_opts = str(args.plugin)
    if args.no_skip_usb_start:
        plugin_opts += ",skip-usb-start=false"
    if args.skip_sdio_start:
        plugin_opts += ",skip-sdio-start=true"
    if args.skip_platform_functions:
        plugin_opts += ",skip-platform-functions=true"
    if args.stabilize_root_domain:
        plugin_opts += ",stabilize-root-domain=true"
    if args.minimal_plugin:
        plugin_opts += ",trace-details=false"
    if args.continue_after_verdict:
        plugin_opts += ",stop-at-verdict=false"
    if args.stop_at_verify_failure:
        plugin_opts += ",stop-at-verify-failure=true"
    command = [
        str(args.qemu), "-M",
        (f"iPhone-2G,bootrom={staged_bootrom},iboot={staged_iboot},"
         f"nand={args.nand.resolve()}"),
        "-m", "1G", "-pflash", str(staged_nor),
        "-L", str(APP / "Resources" / "pc-bios"),
        "-display", "none", "-serial", f"file:{serial_path}",
        "-monitor", "stdio",
        "-plugin", plugin_opts,
        "-d", "plugin", "-D", str(plugin_path),
        "-trace", "enable=itnand_read_page",
        "-trace", "enable=itnand_id",
        "-trace", "enable=itnand_root_page",
        "-trace", "enable=itadm_root_read",
    ]
    if args.icount_shift >= 0:
        command.extend(["-icount", f"shift={args.icount_shift}"])

    started = time.monotonic()
    timed_out = False
    with stderr_path.open("wb") as stderr, monitor_path.open("wb") as monitor:
        process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.PIPE,
                                   stdout=monitor, stderr=stderr)
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                assert process.stdin is not None
                process.stdin.write(b"stop\ninfo registers\n")
                process.stdin.flush()
                time.sleep(0.25)
            except (OSError, BrokenPipeError):
                pass
            process.send_signal(signal.SIGKILL)
            process.wait()
    elapsed = time.monotonic() - started

    trace = parse_trace(decode_log(plugin_path))
    serial = parse_serial(decode_log(serial_path))
    nand_reads = parse_nand_reads(decode_log(plugin_path))
    status = classify(trace, serial, timed_out)
    report = {
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
        "timed_out": timed_out,
        "qemu_exit_code": process.returncode,
        "inputs": {
            "nand": str(args.nand.resolve()), **nand_info,
            "nor_sha256": sha256_file(staged_nor),
            "iboot_input_sha256": sha256_file(args.iboot),
            "iboot_patched_sha256": sha256_file(staged_iboot),
            "qemu": str(args.qemu.resolve()),
            "plugin": str(args.plugin.resolve()),
            "icount_shift": args.icount_shift,
        },
        "trace": trace,
        "serial": serial,
        "nand_reads": nand_reads,
        "logs": {
            "directory": str(logs), "serial": str(serial_path),
            "plugin": str(plugin_path), "stderr": str(stderr_path),
            "nand_reads": str(plugin_path),
            "monitor": str(monitor_path),
        },
    }
    report_path = logs / "result.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    summary = {
        "status": status,
        "elapsed_seconds": report["elapsed_seconds"],
        "timed_out": timed_out,
        "verdict": trace["verdict"],
        "panic_reason": trace["panic_reason"],
        "diagnostic_root_stabilization_applied":
            trace["diagnostic_root_stabilization_applied"],
        "ftl_context_edges": trace["ftl_context_edges"],
        "mountroot_results": trace["mountroot_results"],
        "vfs_mount_events": trace["vfs_mount_events"],
        "hfs_mount_results": trace["hfs_mount_results"],
        "hfs_stage_results": trace["hfs_stage_results"],
        "hfs_mountfs_block_tail": trace["hfs_mountfs_blocks"][-32:],
        "bt_open_stages": trace["bt_open_stages"],
        "verify_header_blocks": trace["verify_header_blocks"],
        "storage_strategy_blocks": trace["storage_strategy_blocks"],
        "block_storage_read_blocks": trace["block_storage_read_blocks"],
        "verify_header_failure": trace["verify_header_failure"],
        "bt_buffer_io": trace["bt_buffer_io"],
        "bt_device_strategy": trace["bt_device_strategy"],
        "bt_storage_strategy": trace["bt_storage_strategy"],
        "bt_provider_reads": trace["bt_provider_reads"],
        "bt_media_reads": trace["bt_media_reads"],
        "bt_block_reads": trace["bt_block_reads"],
        "bt_block_submits": trace["bt_block_submits"],
        "bt_async_submits": trace["bt_async_submits"],
        "bt_executes": trace["bt_executes"],
        "bt_device_reads": trace["bt_device_reads"],
        "root_pages": nand_reads["root_pages"],
        "nand_identifications": nand_reads["identifications"],
        "root_read_requests": nand_reads["root_read_requests"],
        "nand_read_tail": nand_reads["tail"][-16:],
        "serial_markers": serial["markers"],
    }
    print(json.dumps(summary, indent=2))
    print(f"report: {report_path}")
    return 0 if status in (
        "FTL_OPEN_SUCCESS", "FTL_RESTORE_FALLBACK", "ROOT_MOUNTED",
        "LAUNCHD_STARTED", "SPRINGBOARD_STARTED") else 2


if __name__ == "__main__":
    sys.exit(main())
