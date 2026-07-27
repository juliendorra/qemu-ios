#!/bin/sh
# Decrypt an iPhone1,1 root filesystem from a user-supplied IPSW and extract the
# raw HFS+ partition, for use as the NAND generator's input.
#
# This is the authoritative iPod-style path (a real decrypted root FS), NOT a
# hand-crafted minimal filesystem. Each build's root DMG is vfdecrypt-encrypted
# with a per-FIRMWARE key. Those keys are public, not secrets: for every pre-3.0
# firmware the key sits in the clear inside the restore ramdisk's /usr/sbin/asr.
# The GID key does NOT open these (they are not 8900 containers).
#
# The DMG name and its key both come from scripts/firmware_profiles.py, selected
# by --build. They used to be a positional argument defaulting to 1.1.4's key,
# which meant decrypting any other build without remembering the third argument
# produced garbage and failed four steps later with "no HFS+ volume header" --
# a message that points at the IPSW rather than at the key.
#
# Pipeline:
#   1. compile vfdecrypt (the standard tool; needs openssl/libcrypto)
#   2. vfdecrypt the encrypted DMG -> a UDIF (zlib) compressed DMG
#   3. hdiutil convert UDIF -> raw APM disk (macOS)
#   4. slice out the HFS+ volume (its "HX"/"H+" header sits at partition+0x400)
#
# Output: a raw HFS+ image (a 2048 multiple) that the NAND generator maps into
# the boot partition; by default this build's canonical root.img. Apple-derived
# output is never committed (AGENTS.md); this is tooling only.
#
# Usage:
#   scripts/decrypt-m68ap-rootfs.sh --build 1C28
#   scripts/decrypt-m68ap-rootfs.sh --build 1C28 --dmg <file> --out <file>
#   scripts/decrypt-m68ap-rootfs.sh --build 4A102 --key <hex>   # experiments
#   scripts/decrypt-m68ap-rootfs.sh --build 1C28 --dry-run      # resolve only
#
# With no --dmg, the root DMG is taken from the build's ipsw/ directory, and
# extracted from the .ipsw archive there if it has not been unpacked yet.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD=""; DMG=""; OUT=""; KEY=""; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD="$2"; shift 2 ;;
        --dmg)   DMG="$2"; shift 2 ;;
        --out)   OUT="$2"; shift 2 ;;
        --key)   KEY="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [ -z "$BUILD" ]; then
    echo "--build BUILD is required (1A543a, 1C28, 3A109a, 4A102)" >&2
    echo "the root DMG name and its VFDecrypt key come from the profile" >&2
    exit 2
fi

FW_JSON="$(python3 "$SCRIPT_DIR/m68ap_paths.py" --build "$BUILD" --json)"
fw() { python3 -c "import sys,json;print(json.load(sys.stdin)['$1'])" <<EOF
$FW_JSON
EOF
}
BUILD="$(fw build)"
: "${KEY:=$(fw vfdecrypt_key)}"
: "${OUT:=$(fw root)}"
ROOT_DMG_NAME="$(fw root_dmg)"
IPSW_DIR="$(fw ipsw)"

if [ -z "$KEY" ]; then
    echo "no VFDecrypt key for build $BUILD in firmware_profiles.py" >&2
    exit 1
fi

# Locate the encrypted root DMG: explicit, already unpacked, or still inside
# the .ipsw (which is a plain zip).
if [ -z "$DMG" ]; then
    DMG="$IPSW_DIR/$ROOT_DMG_NAME"
    if [ ! -f "$DMG" ] && [ "$DRY" -eq 1 ]; then
        ARCHIVE="$(ls "$IPSW_DIR"/*.ipsw 2>/dev/null | head -1 || true)"
        echo "build $BUILD (iPhone OS $(fw version))" >&2
        echo "  dmg  $DMG" >&2
        echo "       not unpacked; would extract $ROOT_DMG_NAME from" >&2
        echo "       ${ARCHIVE:-(no .ipsw staged)}" >&2
        echo "  out  $OUT" >&2
        echo "  key  ${KEY%"${KEY#????????}"}… (${#KEY} hex chars, from the profile)" >&2
        exit 0
    fi
    if [ ! -f "$DMG" ]; then
        ARCHIVE="$(ls "$IPSW_DIR"/*.ipsw 2>/dev/null | head -1 || true)"
        if [ -z "$ARCHIVE" ]; then
            echo "no $ROOT_DMG_NAME and no .ipsw in $IPSW_DIR" >&2
            echo "put the retail IPSW there, or pass --dmg" >&2
            exit 1
        fi
        echo "extracting $ROOT_DMG_NAME from $(basename "$ARCHIVE")..." >&2
        unzip -o -j -d "$IPSW_DIR" "$ARCHIVE" "$ROOT_DMG_NAME" >/dev/null
    fi
fi
[ -f "$DMG" ] || { echo "root DMG not found: $DMG" >&2; exit 1; }

echo "build $BUILD (iPhone OS $(fw version))" >&2
echo "  dmg  $DMG" >&2
echo "  out  $OUT" >&2
echo "  key  ${KEY%"${KEY#????????}"}… (${#KEY} hex chars)" >&2
if [ "$DRY" -eq 1 ]; then
    echo "(dry run; stopping before vfdecrypt)" >&2
    exit 0
fi
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
    sys.exit("no HFS+ volume header (H+/HX) found in first 1 MB -- the most "
             "likely cause is the WRONG VFDecrypt key for this build, which "
             "decrypts to garbage rather than failing")
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
