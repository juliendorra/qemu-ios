#!/usr/bin/env python3
"""Symbolize a `-d exec` trace against MBX2D/MBXConnect/AppleMBX anchors.

Reads a QEMU exec log (produced e.g. by scripts/mbx-freeze-driver.py
--exec-trace), extracts the guest PC (the SECOND field of the bracket
triple `[flags/PC/...]` -- the first is CPU flags, an easy mis-parse),
and names each TB from:
  * the userland symbol tables of MBX2D.framework / MBXConnect.framework
    (dump them once with scripts/macho-symbols.py <binary> --defined-only)
  * the built-in AppleMBX kext anchor table below (LIVE 1A543a addresses,
    derived 2026-08-02; restore-kernelcache file VA = live - 0x2000).

Usage:
  scripts/mbx-trace-symbolize.py <exec.log> [--syms mbx2d_syms.txt] [--tail N]
where the optional syms file holds lines of "<hex-addr> <name>".
"""
import argparse, bisect, collections, re, sys

# AppleMBX kext, iPhone OS 1.0 (1A543a), LIVE addresses.  Derived in the
# 2026-08-02 session (MBX_SDO_MMU_HANDOFF.md section 0.2); keep in sync.
KEXT_ANCHORS = {
    0xc032b340: 'taWatchdog_loop',
    0xc032b44c: 'waitForHWContext',
    0xc032bb24: 'engine_register_init',
    0xc032bc0c: 'soft_event_post',
    0xc032e044: 'finish_sleep_path',
    0xc032e4fc: 'kick_sync_descriptor',
    0xc032e77c: 'method7_record_parser',
    0xc032e914: 'cmd4_blitcolor_case',
    0xc032e974: 'cmd5_blitcopy_case',
    0xc032ebf0: 'cmd3d_shared_case',
    0xc032ed00: 'cmd10_case',
    0xc032ee48: 'mbx_isr',
    0xc032f1ec: 'retire_wrapper',
    0xc032f274: 'finish_entry_retire0x4000',
    0xc032f6f0: 'submit_and_wait_1s',
    0xc0335638: 'reg_read_helper',
    0xc0335640: 'reg_write_helper',
    0xc0336988: 'op_retirement',
    0xc0337c64: 'bit10_surface_handler',
    0xc0337ce8: 'queue_retire',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--syms', action='append', default=[])
    ap.add_argument('--tail', type=int, default=40)
    args = ap.parse_args()

    anchors = list(KEXT_ANCHORS.items())
    for path in args.syms:
        for line in open(path):
            p = line.split()
            if len(p) >= 2 and p[0].startswith('0x'):
                anchors.append((int(p[0], 16), p[-1]))
    anchors.sort()
    addrs = [a for a, _ in anchors]

    def sym(pc):
        i = bisect.bisect_right(addrs, pc) - 1
        if i < 0 or pc - anchors[i][0] >= 0x4000:
            return hex(pc)
        a, n = anchors[i]
        return f'{n}+{pc - a:#x}'

    pcs = []
    for line in open(args.log):
        m = re.search(r'\[[0-9a-f]+/([0-9a-f]+)/', line)
        if m:
            pcs.append(int(m.group(1), 16))
    print('TBs:', len(pcs))
    seq = []
    for pc in pcs:
        n = sym(pc).split('+')[0]
        if not seq or seq[-1] != n:
            seq.append(n)
    print('function sequence tail:', seq[-args.tail:])
    c = collections.Counter(sym(pc).split('+')[0] for pc in pcs)
    print('top functions:', c.most_common(15))
    print('last raw:', [sym(p) for p in pcs[-12:]])


if __name__ == '__main__':
    main()
