#!/usr/bin/env python3
"""Locate AppleMRVL868x::readEEPROM in a physical RAM dump and disassemble it.
Mapping: kernel virtual 0xC0000000 == dump file offset 0 (phys base 0x08000000)."""
import sys, struct
from capstone import *

DUMP = sys.argv[1] if len(sys.argv) > 1 else "/private/tmp/ram-dump.bin"
VBASE = 0xC0000000
data = open(DUMP, "rb").read()
print(f"dump {len(data)} bytes")

def find_all(needle):
    out=[]; i=0
    while True:
        j = data.find(needle, i)
        if j < 0: break
        out.append(j); i = j+1
    return out

strings = [b"ready status from helper", b"No transmit length",
           b"No Calibration Data Found", b"Reading EEPROM data",
           b"MAC Address is all"]
str_va = {}
for s in strings:
    locs = find_all(s)
    for L in locs:
        str_va[s] = VBASE + L
        print(f"str {s!r} @ file 0x{L:x} va 0x{VBASE+L:x}")
        break

# find 32-bit little-endian words equal to each string VA (literal pools)
def find_refs(va):
    needle = struct.pack("<I", va)
    return find_all(needle)

for s, va in str_va.items():
    refs = find_refs(va)
    print(f"\nrefs to {s!r} (va 0x{va:x}): {[hex(VBASE+r) for r in refs]}")

# Disassemble a window in Thumb around a given file offset
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB); md_t.detail=False
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM); md_a.detail=False

def disasm(off, count=60, thumb=True, back=0):
    md = md_t if thumb else md_a
    start = off - back
    va = VBASE + start
    for insn in md.disasm(data[start:start+count*4], va):
        print(f"  0x{insn.address:x}: {insn.mnemonic} {insn.op_str}")
