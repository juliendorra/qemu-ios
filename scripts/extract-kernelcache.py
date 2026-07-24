#!/usr/bin/env python3
"""Decrypt + decompress an S5L8900 (iPhone 2G / iPod Touch 1G) kernelcache.

The 1.1.x kernelcache ships as an 8900 container:

    [0x800 header] AES-128-CBC(key=GID, iv=0) over sizeOfData bytes
        -> 'complzss' header + Apple LZSS stream
            -> raw ARM Mach-O (MH_MAGIC, kernel + prelinked kexts)

The S5L8900 GID key (188458A6D15034DFE386F23B61D43774) is public and is the
same key already compiled into hw/arm/ipod_touch_8900_engine.h -- iPhone 2G
and iPod Touch 1G share the SoC. This script turns the encrypted cache into a
disassemblable Mach-O so kexts (e.g. AppleReliableSerialLayer, which speaks
H5/BCSP three-wire UART to the baseband -- see IPHONE_2G_BRINGUP_HANDOFF.md)
can be studied.

Usage:
    python3 scripts/extract-kernelcache.py \
        m68ap-artifacts/ipsw/kernelcache.release.s5l8900xrb -o /tmp/kc.raw

Per the repo artifact policy, do NOT commit the decrypted output (it is Apple
firmware); this script only reconstructs it locally from an IPSW the user
already has.
"""
import argparse
import struct
import sys

GID_KEY = bytes.fromhex("188458A6D15034DFE386F23B61D43774")


def aes_cbc_decrypt(data: bytes, key: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except ImportError:
        sys.exit("pip3 install pycryptodome (needed for AES-CBC)")
    return AES.new(key, AES.MODE_CBC, b"\0" * 16).decrypt(data)


def lzss_decompress(src: bytes, out_size: int) -> bytes:
    """Canonical Okumura/Apple LZSS (N=4096, F=18, THRESHOLD=2).

    The ring buffer starts filled with spaces for the first N-F entries and
    r begins at N-F (NOT N-F-1 -- an off-by-one there corrupts every ~4th
    output word).
    """
    N, F, THRESHOLD = 4096, 18, 2
    text = bytearray(b" " * (N - F)) + bytearray(F)
    r = N - F
    out = bytearray()
    flags = 0
    i = 0
    L = len(src)
    while len(out) < out_size and i < L:
        flags >>= 1
        if (flags & 0x100) == 0:
            flags = src[i] | 0xFF00
            i += 1
            if i > L:
                break
        if flags & 1:
            c = src[i]
            i += 1
            out.append(c)
            text[r] = c
            r = (r + 1) & (N - 1)
        else:
            if i + 1 >= L:
                break
            a, b = src[i], src[i + 1]
            i += 2
            pos = a | ((b & 0xF0) << 4)
            cnt = (b & 0x0F) + THRESHOLD
            for k in range(cnt + 1):
                c = text[(pos + k) & (N - 1)]
                out.append(c)
                text[r] = c
                r = (r + 1) & (N - 1)
                if len(out) >= out_size:
                    break
    return bytes(out)


def extract(path: str) -> bytes:
    d = open(path, "rb").read()
    if d[:4] != b"8900":
        # header is "8900" + "1.0" + format byte; some dumps show "89001.0"
        if d[:5] != b"89001":
            print("warning: no 8900 magic; attempting anyway", file=sys.stderr)
    size = struct.unpack("<I", d[0xC:0x10])[0]
    enc = d[0x800 : 0x800 + size]
    dec = aes_cbc_decrypt(enc, GID_KEY)
    if dec[:8] != b"complzss":
        sys.exit("decrypt failed: no 'complzss' magic (wrong key/offset?)")
    declen = struct.unpack(">I", dec[12:16])[0]
    complen = struct.unpack(">I", dec[16:20])[0]
    raw = lzss_decompress(dec[0x180 : 0x180 + complen], declen)
    if raw[:4] != bytes.fromhex("cefaedfe"):
        sys.exit("decompress failed: no MH_MAGIC (feedface) at start")
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kernelcache", help="8900 kernelcache.release.s5l8900xrb")
    ap.add_argument("-o", "--out", required=True, help="raw Mach-O output path")
    args = ap.parse_args()
    raw = extract(args.kernelcache)
    open(args.out, "wb").write(raw)
    print(f"wrote {len(raw)} bytes of raw ARM Mach-O to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
