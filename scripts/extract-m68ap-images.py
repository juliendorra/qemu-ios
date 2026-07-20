#!/usr/bin/env python3
"""Extract raw, decrypted iPhone-2G (M68AP) boot images from an IPSW.

The iPhone 2G and the iPod Touch 1G use the same S5L8900 SoC, so they share the
same GID key ("AES key 0x837"). That key is already hard-coded in
hw/arm/ipod_touch_8900_engine.h (188458A6D15034DFE386F23B61D43774), and it
decrypts the iPhone's 8900-wrapped images exactly as it does the iPod's. No
per-image key lookup is needed.

This tool takes the `all_flash.m68ap.production` directory from a legally
obtained iPhone1,1 IPSW and emits raw artifacts for the QEMU `-M iPhone-2G`
machine:

  iBoot.m68ap.RELEASE.img2  -> iboot_204_m68ap.bin   (raw ARM, for iboot=)
  LLB.m68ap.RELEASE.img2    -> LLB.m68ap.bin          (raw, for a NOR rebuild)
  DeviceTree.m68ap.img2     -> DeviceTree.m68ap.bin   (raw, for a NOR rebuild)

Container shape (S5L8900):
  0x000            8900 header: magic "8900", enc marker @0x07 (0x03=encrypted,
                   0x04=plaintext), sizeOfData @0x0c
  0x800            AES-128-CBC(key=GID, iv=0) payload
  after decrypt:   an IMG2 wrapper ("Img2" stored little-endian as "2gmI",
                   4-char type at +4: tobi=iBoot, llbz=LLB, dtre=DeviceTree),
                   0x400-byte header
  payload+0x400    the raw image (iBoot begins with the ARM reset vector
                   0e 00 00 ea)

NOTE ON POLICY: this script is extraction/conversion tooling only. Do not commit
the IPSW, the decrypted images, keys beyond the one already in the tree, or an
activated NAND. Point it at a user-supplied IPSW at run time.

Usage:
  python3 scripts/extract-m68ap-images.py \
      <path to .../all_flash.m68ap.production> <output dir>
"""
import os
import struct
import subprocess
import sys

GID_KEY = "188458A6D15034DFE386F23B61D43774"  # S5L8900 key 0x837 (public)
IMG2_HEADER_LEN = 0x400
CONTAINER_HEADER_LEN = 0x800

# Source image -> (output name, whether to strip the IMG2 wrapper)
IMAGES = {
    "iBoot.m68ap.RELEASE.img2": ("iboot_204_m68ap.bin", True),
    "LLB.m68ap.RELEASE.img2":   ("LLB.m68ap.bin",        True),
    "DeviceTree.m68ap.img2":    ("DeviceTree.m68ap.bin", True),
}

# Images whose full decrypted IMG2 container (0x400 header + payload) must be
# retained for a synthetic NOR image store. These keep the authentic M68AP
# security epoch in the header, which the M68AP iBoot checks. Emitted as
# <type>.img2c into <out>/nor-containers/ for scripts/build-m68ap-nor.py.
# The order here is the order used in the N45AP NOR image store.
NOR_CONTAINERS = [
    "DeviceTree.m68ap.img2",
    "batterycharging.img2",
    "applelogo.img2",
    "needservice.img2",
    "batterylow0.img2",
    "batterylow1.img2",
    "recoverymode.img2",
]


def decrypt_8900(data):
    if data[:4] != b"8900":
        raise ValueError("not an 8900 container")
    marker = data[7]
    if marker == 0x04:
        return data[CONTAINER_HEADER_LEN:]  # already plaintext
    if marker != 0x03:
        raise ValueError(f"unexpected 8900 enc marker {marker:#x}")
    size = struct.unpack("<I", data[0x0c:0x10])[0]
    payload = data[CONTAINER_HEADER_LEN:CONTAINER_HEADER_LEN + (size - size % 16)]
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-nopad",
         "-K", GID_KEY, "-iv", "0" * 32],
        input=payload, stdout=subprocess.PIPE, check=True)
    return proc.stdout


def main(src_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for src, (out_name, strip_img2) in IMAGES.items():
        src_path = os.path.join(src_dir, src)
        if not os.path.exists(src_path):
            print(f"skip {src}: not found")
            continue
        dec = decrypt_8900(open(src_path, "rb").read())
        if strip_img2:
            if dec[:4] != b"2gmI":
                raise ValueError(f"{src}: decrypted payload is not IMG2 "
                                 f"(got {dec[:4]!r})")
            dec = dec[IMG2_HEADER_LEN:]
        out_path = os.path.join(out_dir, out_name)
        open(out_path, "wb").write(dec)
        print(f"{src} -> {out_path} ({len(dec)} bytes, {len(dec):#x})")

    # Retain full IMG2 containers for NOR construction.
    container_dir = os.path.join(out_dir, "nor-containers")
    os.makedirs(container_dir, exist_ok=True)
    for src in NOR_CONTAINERS:
        src_path = os.path.join(src_dir, src)
        if not os.path.exists(src_path):
            print(f"skip container {src}: not found")
            continue
        dec = decrypt_8900(open(src_path, "rb").read())
        if dec[:4] != b"2gmI":
            raise ValueError(f"{src}: decrypted payload is not IMG2")
        img_type = dec[4:8][::-1].decode("ascii", "replace")
        # data length is stored at header offset 0x10; keep header + that many
        # bytes (trailing bytes past the payload are padding/garbage).
        data_len = struct.unpack("<I", dec[0x10:0x14])[0]
        container = dec[:IMG2_HEADER_LEN + data_len]
        # Name by SOURCE stem, not the IMG2 4CC: two store images differ only
        # by case ('batl' vs 'batL'), which collides on case-insensitive
        # filesystems (macOS default). The stem is unambiguous.
        stem = os.path.splitext(src)[0].replace(".RELEASE", "")
        out_path = os.path.join(container_dir, f"{stem}.img2c")
        open(out_path, "wb").write(container)
        epoch = struct.unpack("<H", dec[0xa:0xc])[0]
        print(f"{src} -> {out_path} (type={img_type} epoch={epoch} "
              f"{len(container):#x} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
