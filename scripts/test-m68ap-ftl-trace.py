#!/usr/bin/env python3
"""Fixture-only tests for m68ap-ftl-trace.py log classification."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("m68ap-ftl-trace.py")
SPEC = importlib.util.spec_from_file_location("m68ap_ftl_trace", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TraceParserTests(unittest.TestCase):
    def test_restore_verdict_and_blocks(self) -> None:
        trace = MODULE.parse_trace("""
M68AP_FTL_TRACE diagnostic IOIpodUSBDevice::start skip applied at 0xc04cb198
M68AP_FTL_TRACE diagnostic AppleS5L8900XSDIO start skip applied at 0xc04ba0e8
M68AP_FTL_TRACE diagnostic root-domain stabilization applied object=0xc0918e00 references=0x00010001->0x00020002
M68AP_FTL_TRACE gpio_function_parent_property=IOFunctionParent004040E0 service=0xc08e2800
M68AP_FTL_TRACE service_match event=candidate pc=0xc0134da0 object=0xc0918e00 vtable=0xc018c948 references=0x00020048 state=0x0000ffff iterator=0xc0b73200 iterator_references=0x00000003
M68AP_FTL_TRACE root_domain_release event=entry pc=0xc0122e04 object=0xc0918e00 references=0x00010002 r1=0xc0918e00 r2=0xc0918e04 r4=0xc0ab6400 r6=0xc0918e00 sp=0xc0ffee00 lr=0xc0122e99
M68AP_FTL_TRACE root_domain_release event=updated pc=0xc0122e48 object=0xc0918e00 references=0x00010001 r1=0x00010001 r2=0xc0918e04 r4=0x00010001 r6=0x00010001 sp=0xc0ffee00 lr=0xc0122e99
M68AP_FTL_TRACE ftl_context_edge pc=0xc0473294 r0=0x00000012 r4=0x00000000 r5=0xc0aa0000 r8=0x00000080 fp=0xc0bb0000 buffer=0xc0bb0000 data=0xc0cc0000 spare=0xc0dd0000 age=0x00000001 spare_word_8=0x00004300 version=0x46560000 version_not=0xb9a9ffff
M68AP_FTL_TRACE mountroot_result attempt=1 error=19 rootdev=0x00000e01 rootvp=0x00000000 rootdevice=disk0s1
M68AP_FTL_TRACE vfs_mount event=candidate call=1 entry=0xc01b6700 name=hfs callback=0xc00c1234 error=0
M68AP_FTL_TRACE vfs_mount event=return call=1 entry=0xc01b6700 name=hfs callback=0xc00c1234 error=22
M68AP_FTL_TRACE hfs_mount_result error=22
M68AP_FTL_TRACE hfs_stage_result stage=header_read error=5
M68AP_FTL_TRACE hfs_mountfs_block=0xc00ddd24 insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE bt_open_stage stage=getblock error=-32767 fcb=0xc0aa0000 control=0x00000000 logical_size=0x00247000
M68AP_FTL_TRACE verify_header_block=0xc00e5490 insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE storage_strategy_block=0xc045c4d0 insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE block_storage_read_block=0xc045893c insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE ftl_core_read_block=0xc046f99c insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE verify_header_failure header=0xc0bb000e r0=0x00000200 r1=0x00000000 r2=0x00000200 r3=0x00000001 r4=0x00000200 r6=0x00000247 fcb=0xc0aa0000 bytes=0000000000000000000000000000000000000002000000000000000000000000000000000000
M68AP_FTL_TRACE bt_buffer_io event=strategy_done pc=0xc0071716 buffer=0xc0bc0000 result=0x00000000 flags=0x00020009 vnode=0xc0bd0000 data=0xc0be0000 lblk_lo=0x00000000 lblk_hi=0x00000000 blk_lo=0xfffffff0 blk_hi=0xffffffff
M68AP_FTL_TRACE bt_device_strategy target=0xc008ea2d device=0xc0bf0000 buffer=0xc0bc0000 blk_lo=0x00000008 blk_hi=0x00000000
M68AP_FTL_TRACE bt_storage_strategy target=0xc0401235 device_number=0x0e000001 buffer=0xc0bc0000 blk_lo=0x00000008 blk_hi=0x00000000
M68AP_FTL_TRACE bt_provider_read target=0xc0412345 provider=0xc0bf0000 client=0xc0be0000 memory=0xc0bd0000 offset_lo=0x00004000 offset_hi=0x00000000 completion_target=0xee000000
M68AP_FTL_TRACE bt_media_read target=0xc0423457 provider=0xc0c00000 client=0xc0bf0000 memory=0xc0bd0000 offset_lo=0x00014000 offset_hi=0x00000000
M68AP_FTL_TRACE bt_block_read target=0xc0434567 provider=0xc0c10000 offset_lo=0x00014000 offset_hi=0x00000000 memory=0xc0bd0000
M68AP_FTL_TRACE bt_block_submit target=0xc0445679 driver=0xc0c10000 request=0xc0c20000 offset_lo=0x00014000 offset_hi=0x00000000 memory=0xc0bd0000
M68AP_FTL_TRACE bt_async_submit target=0xc0478901 driver=0xc0c10000 offset_lo=0x00014000 offset_hi=0x00000000 memory=0xc0bd0000 request=0xc0c20000
M68AP_FTL_TRACE bt_execute target=0xc0489011 driver=0xc0c10000 offset_lo=0x00014000 offset_hi=0x00000000 request=0xc0c20000
M68AP_FTL_TRACE bt_device_read target=0xc048abcd device=0xc0c30000 memory=0xc0bd0000 offset_lo=0x00014000 offset_hi=0x00000000
M68AP_FTL_TRACE bt_physical_read target=0xc049bcdf device=0xc0c30000 memory=0xc0bd0000 block=0x0000000b count=0x00000001
M68AP_FTL_TRACE bt_driver_read target=0xc04acdef provider=0xc0c40000 memory=0xc0bd0000 block=0x0000000b count=0x00000001
M68AP_FTL_TRACE ftl_data_read variant=direct event=call target=0xc047a080 block=0x0000000b count=0x00000001 flags=0x00010000
M68AP_FTL_TRACE ftl_data_read variant=direct event=return result=0x00000000
M68AP_FTL_TRACE ftl_map_decision available=0x00000000 requested=0x00000001 processed=0x00000000 page_offset=0x00000000
M68AP_FTL_TRACE ftl_page_read variant=single physical_page=0x00012345 count=0x00000001 descriptor=0xc0bd0000 flags=0x00000000
M68AP_FTL_TRACE ftl_physical_io event=bounds linear_page=0x00012345 total_pages=0x00040000 pages_per_block=0x00000080
M68AP_FTL_TRACE ftl_physical_io event=provider target=0xc0480000 bank=0x00000003 page=0x00001234 data=0xe1000000 spare=0xe1000800
M68AP_FTL_TRACE ftl_physical_io event=provider_return result=0x00000001
M68AP_FTL_TRACE ftl_physical_io event=complete result=0x00000001
M68AP_FTL_TRACE block=0xc047302c insn=push {r4, r5, r6, r7, lr}
M68AP_FTL_TRACE block=0xc04737f4 insn=ldr r8, [pc, #0x90]
M68AP_FTL_TRACE verdict=restore address=0xc0473814
""")
        self.assertEqual(trace["verdict"], "restore")
        self.assertEqual(trace["block_count"], 2)
        self.assertTrue(trace["diagnostic_usb_skip_applied"])
        self.assertTrue(trace["diagnostic_sdio_skip_applied"])
        self.assertTrue(trace["diagnostic_root_stabilization_applied"])
        self.assertEqual(trace["gpio_function_parent"], {
            "property": "IOFunctionParent004040E0",
            "service": "0xc08e2800",
        })
        self.assertEqual(trace["service_matching"][0]["event"], "candidate")
        self.assertEqual(trace["service_matching"][0]["object"], "0xc0918e00")
        self.assertEqual(len(trace["root_domain_releases"]), 2)
        self.assertEqual(trace["root_domain_releases"][1]["references"],
                         "0x00010001")
        self.assertEqual(trace["ftl_context_edges"][0]["pc"], "0xc0473294")
        self.assertEqual(trace["ftl_context_edges"][0]["spare_word_8"],
                         "0x00004300")
        self.assertEqual(trace["mountroot_results"][0]["error"], 19)
        self.assertEqual(trace["mountroot_results"][0]["rootdevice"],
                         "disk0s1")
        self.assertEqual(trace["vfs_mount_events"][0]["name"], "hfs")
        self.assertEqual(trace["vfs_mount_events"][1]["error"], 22)
        self.assertEqual(trace["hfs_mount_results"], [22])
        self.assertEqual(trace["hfs_stage_results"], [
            {"stage": "header_read", "error": 5},
        ])
        self.assertEqual(trace["hfs_mountfs_blocks"][0]["address"],
                         "0xc00ddd24")
        self.assertEqual(trace["bt_open_stages"][0]["error"], -32767)
        self.assertEqual(trace["bt_open_stages"][0]["logical_size"],
                         "0x00247000")
        self.assertEqual(trace["verify_header_blocks"][0]["address"],
                         "0xc00e5490")
        self.assertEqual(trace["storage_strategy_blocks"][0]["address"],
                         "0xc045c4d0")
        self.assertEqual(trace["block_storage_read_blocks"][0]["address"],
                         "0xc045893c")
        self.assertEqual(trace["ftl_core_read_blocks"][0]["address"],
                         "0xc046f99c")
        self.assertEqual(trace["verify_header_failure"]["r6"],
                         "0x00000247")
        self.assertEqual(trace["bt_buffer_io"][0]["event"],
                         "strategy_done")
        self.assertEqual(trace["bt_buffer_io"][0]["blk_lo"],
                         "0xfffffff0")
        self.assertEqual(trace["bt_device_strategy"][0]["target"],
                         "0xc008ea2d")
        self.assertEqual(trace["bt_storage_strategy"][0]["device_number"],
                         "0x0e000001")
        self.assertEqual(trace["bt_provider_reads"][0]["offset_lo"],
                         "0x00004000")
        self.assertEqual(trace["bt_media_reads"][0]["offset_lo"],
                         "0x00014000")
        self.assertEqual(trace["bt_block_reads"][0]["memory"],
                         "0xc0bd0000")
        self.assertEqual(trace["bt_block_submits"][0]["target"],
                         "0xc0445679")
        self.assertEqual(trace["bt_async_submits"][0]["request"],
                         "0xc0c20000")
        self.assertEqual(trace["bt_executes"][0]["target"],
                         "0xc0489011")
        self.assertEqual(trace["bt_device_reads"][0]["device"],
                         "0xc0c30000")
        self.assertEqual(trace["bt_physical_reads"][0]["block"],
                         "0x0000000b")
        self.assertEqual(trace["bt_driver_reads"][0]["target"],
                         "0xc04acdef")
        self.assertEqual(trace["ftl_data_reads"], [
            {"variant": "direct", "event": "call",
             "target": "0xc047a080", "block": "0x0000000b",
             "count": "0x00000001", "flags": "0x00010000"},
            {"variant": "direct", "event": "return",
             "result": "0x00000000"},
        ])
        self.assertEqual(trace["ftl_map_decisions"][0]["available"],
                         "0x00000000")
        self.assertEqual(trace["ftl_page_reads"][0]["physical_page"],
                         "0x00012345")
        self.assertEqual(trace["ftl_physical_io"][1]["bank"],
                         "0x00000003")
        self.assertEqual(trace["ftl_physical_io"][3]["result"],
                         "0x00000001")
        serial = MODULE.parse_serial("Darwin Kernel Version\n")
        self.assertEqual(MODULE.classify(trace, serial, False),
                         "FTL_RESTORE_FALLBACK")

    def test_parse_nand_reads_keeps_structured_tail(self):
        parsed = MODULE.parse_nand_reads(
            "itnand_read_page bank=7 page=25855 present=1 spare_type=0x43\n"
            "unrelated line\n"
            "itnand_read_page bank=3 page=25791 present=0 spare_type=0x00\n")
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["tail"][0]["page"], 25855)
        self.assertFalse(parsed["tail"][1]["present"])

    def test_parse_root_page_read(self):
        parsed = MODULE.parse_nand_reads(
            "itnand_root_page bank=3 page=25856 present=1 "
            "word_0=0x00000000 word_20=0x00000000 "
            "word_400=0x05005848 spare_type=0x40\n"
            "itadm_root_read cmd=0x300 count=2 index=0 bank=3 page=25857\n"
            "itadm_root_read cmd=0x300 count=2 index=1 bank=4 page=25857\n")
        self.assertEqual(parsed["root_pages"], [{
            "bank": 3, "page": 25856, "present": True,
            "word_0": "0x00000000", "word_400": "0x05005848",
            "word_20": "0x00000000",
            "spare_type": "0x40",
        }])
        self.assertEqual(parsed["root_read_requests"][1], {
            "cmd": "0x300", "count": 2, "index": 1,
            "bank": 4, "page": 25857,
        })

    def test_success_verdict(self) -> None:
        trace = MODULE.parse_trace(
            "M68AP_FTL_TRACE verdict=success address=0xc0473830\n")
        serial = MODULE.parse_serial("Darwin Kernel Version\n")
        self.assertEqual(MODULE.classify(trace, serial, False),
                         "FTL_OPEN_SUCCESS")

    def test_pre_ftl_panic(self) -> None:
        stack = bytearray(96)
        stack[0:4] = (0xe0003040).to_bytes(4, "little")
        stack[4:8] = (0xc0130a69).to_bytes(4, "little")
        reason = b"IOPMrootDomain: attached at free()"
        stack[8:8 + len(reason)] = reason
        stack[64:68] = (0xe0003050).to_bytes(4, "little")
        stack[68:72] = (0xc013391f).to_bytes(4, "little")
        stack[80:84] = (0).to_bytes(4, "little")
        stack[84:88] = (0xc0122e77).to_bytes(4, "little")
        trace = MODULE.parse_trace(
            "M68AP_FTL_TRACE pre_ftl_panic=0xc0019790 r0=0x00000000 "
            "sp=0xe0003000 lr=0xc012d963\n"
            f"M68AP_FTL_TRACE stack={stack.hex()}\n")
        serial = MODULE.parse_serial(
            "Darwin Kernel Version\nAppleNANDFTL::start(disk)\npanic(cpu 0")
        self.assertEqual(MODULE.classify(trace, serial, True),
                         "PRE_ROOT_KERNEL_FAILURE")
        self.assertEqual(trace["panic_reason"],
                         "IOPMrootDomain: attached at free()")
        self.assertEqual(trace["panic_registers"]["sp"], "0xe0003000")
        self.assertEqual(trace["return_addresses"],
                         ["0xc012d963", "0xc0130a69", "0xc013391f",
                          "0xc0122e77"])

    def test_timeout_before_kernel(self) -> None:
        self.assertEqual(
            MODULE.classify(MODULE.parse_trace(""), MODULE.parse_serial(""), True),
            "TIMEOUT_BEFORE_KERNEL_FTL")


if __name__ == "__main__":
    unittest.main()
