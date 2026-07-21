#!/bin/sh
# Decrypt the iPhone1,1 1.1.4 root filesystem from a user-supplied IPSW and
# extract the raw HFS+ partition, for use as the NAND generator's input.
#
# This is the authoritative iPod-style path (a real decrypted root FS), NOT a
# hand-crafted minimal filesystem. The root DMG (022-3894-4.dmg) is vfdecrypt-
# encrypted; the key is a public firmware key (The iPhone Wiki, "Little Bear
# 4A102 (iPhone1,1)"). The GID key does NOT open it (it is not an 8900 container).
#
#   VFDecrypt key (1.1.4 / 4A102 / iPhone1,1, 022-3894-4.dmg):
#     d0a0c0977bd4b6350b256d6650ec9eca419b6f961f593e74b7e5b93e010b698ca6cca1fe
#
# Pipeline:
#   1. compile vfdecrypt (the standard tool; needs openssl/libcrypto)
#   2. vfdecrypt the encrypted DMG -> a UDIF (zlib) compressed DMG
#   3. hdiutil convert UDIF -> raw APM disk (macOS)
#   4. slice out the HFS+ volume (its "HX"/"H+" header sits at partition+0x400)
#
# Output: filesystem-m68ap-readonly.img (raw HFS+, a 2048 multiple), which the
# NAND generator maps into the boot partition. Apple-derived output is never
# committed (AGENTS.md); this is tooling only.
#
# Usage:
#   scripts/decrypt-m68ap-rootfs.sh <022-3894-4.dmg> <out.img> [vfdecrypt_key]
set -eu

DMG="${1:?usage: decrypt-m68ap-rootfs.sh <encrypted-root.dmg> <out.img> [key]}"
OUT="${2:?usage: decrypt-m68ap-rootfs.sh <encrypted-root.dmg> <out.img> [key]}"
KEY="${3:-d0a0c0977bd4b6350b256d6650ec9eca419b6f961f593e74b7e5b93e010b698ca6cca1fe}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/m68ap-rootfs.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# 1. vfdecrypt: fetch + compile if not already on PATH or in WORK
VFD="$(command -v vfdecrypt || true)"
if [ -z "$VFD" ]; then
    echo "compiling vfdecrypt (standard tool)..." >&2
    curl -fsSL -o "$WORK/vfdecrypt.c" \
      "https://raw.githubusercontent.com/malus-security/iExtractor/master/tools/vfdecrypt/vfdecrypt.c"
    SSL="$(brew --prefix openssl@3 2>/dev/null || brew --prefix openssl 2>/dev/null || echo /usr)"
    cc -DMAC_OSX -Wno-deprecated-declarations "$WORK/vfdecrypt.c" -o "$WORK/vfdecrypt" \
       -I"$SSL/include" -L"$SSL/lib" -lcrypto
    VFD="$WORK/vfdecrypt"
fi

# 2. decrypt -> UDIF compressed dmg
echo "decrypting root filesystem..." >&2
"$VFD" -i "$DMG" -k "$KEY" -o "$WORK/rootfs_udif.dmg"

# 3. UDIF -> raw APM disk (macOS hdiutil)
echo "converting UDIF -> raw..." >&2
rm -f "$WORK/raw.cdr"
hdiutil convert "$WORK/rootfs_udif.dmg" -format UDTO -o "$WORK/raw" >/dev/null

# 4. locate the HFS+ volume header and slice the volume out
python3 - "$WORK/raw.cdr" "$OUT" <<'PY'
import sys, struct
src, out = sys.argv[1], sys.argv[2]
d = open(src, "rb")
raw = d.read(1 << 20)  # scan the first 1 MB for the HFS+ volume header
vh = None
for off in range(0, len(raw) - 2, 512):
    if raw[off:off+2] in (b"H+", b"HX"):
        vh = off
        break
if vh is None:
    sys.exit("no HFS+ volume header (H+/HX) found in first 1 MB")
part_start = vh - 0x400  # volume header sits at +0x400 within the partition
d.seek(vh)
hdr = d.read(0x200)
block_size = struct.unpack(">I", hdr[40:44])[0]
total_blocks = struct.unpack(">I", hdr[44:48])[0]
vol_bytes = block_size * total_blocks
assert vol_bytes % 2048 == 0, f"volume {vol_bytes} not a 2048 multiple"
d.seek(part_start)
with open(out, "wb") as o:
    left = vol_bytes
    while left > 0:
        chunk = d.read(min(1 << 20, left))
        if not chunk:
            break
        o.write(chunk); left -= len(chunk)
print(f"wrote {out}: {vol_bytes} bytes HFS+ ({vol_bytes//1024//1024} MB, "
      f"block {block_size}, {total_blocks} blocks)")
PY
echo "done: $OUT" >&2
