#!/bin/sh
# Build a minimal case-sensitive HFS+ (HFSX) volume carrying the M68AP
# kernelcache at the path iBoot's fsboot loads, for use as the
# scripts/build-m68ap-nand.py --hfs payload.
#
# iBoot-204.3.14 fsboot loads $boot-path, which defaults to
#   /System/Library/Caches/com.apple.kernelcaches/kernelcache.s5l8900xrb
# from the HFS+ boot partition. Placing the IPSW's kernelcache there is enough
# for iBoot to mount the filesystem, find the file, decrypt/decompress it
# (complzss) and pass its adler32 check -- verified reaching the "done" +
# Mach-O stage on -M iPhone-2G (see IPHONE_2G_BRINGUP_HANDOFF.md).
#
# This is a MINIMAL payload: it does NOT contain a bootable root filesystem, so
# the kernel cannot mount root from it. It exists to exercise the NAND payload
# pipeline (GPT/HFS+/file lookup/kernelcache load) end to end. A full root FS
# requires the decrypted 022-3894-4.dmg (vfdecrypt key not available offline).
#
# Uses macOS hdiutil (native HFSX). Produces a raw image (UDIF read-write,
# -layout NONE => filesystem from byte 0, volume header at +0x400), sized to a
# 2048-byte multiple as build-m68ap-nand.py requires.
#
# Apple-derived firmware is never committed (AGENTS.md); this only builds a
# local artifact from a user-supplied IPSW kernelcache.
#
# Usage:
#   scripts/build-m68ap-hfs-payload.sh <kernelcache.release.s5l8900xrb> <out.dmg> [size_mb]
set -eu

KC="${1:?usage: build-m68ap-hfs-payload.sh <kernelcache> <out.dmg> [size_mb]}"
OUT="${2:?usage: build-m68ap-hfs-payload.sh <kernelcache> <out.dmg> [size_mb]}"
SIZE_MB="${3:-16}"
KPATH="System/Library/Caches/com.apple.kernelcaches"

[ -f "$KC" ] || { echo "kernelcache not found: $KC" >&2; exit 1; }

# hdiutil appends ".dmg" if the output stem lacks it; normalise.
STEM="${OUT%.dmg}"
rm -f "$STEM.dmg"

MNT="$(mktemp -d "${TMPDIR:-/tmp}/m68ap-hfs.XXXXXX")"
trap 'hdiutil detach "$MNT" >/dev/null 2>&1 || true; rmdir "$MNT" 2>/dev/null || true' EXIT

hdiutil create -megabytes "$SIZE_MB" -fs "Case-sensitive HFS+" \
    -volname iPhoneOS -layout NONE -type UDIF -o "$STEM" >/dev/null
hdiutil attach "$STEM.dmg" -mountpoint "$MNT" -nobrowse -owners on >/dev/null
mkdir -p "$MNT/$KPATH"
cp "$KC" "$MNT/$KPATH/kernelcache.s5l8900xrb"
sync
hdiutil detach "$MNT" >/dev/null
trap - EXIT
rmdir "$MNT" 2>/dev/null || true

# UDRW -layout NONE already yields exactly SIZE_MB of raw filesystem (a 2048
# multiple); the volume header sits at offset 0x400.
SZ=$(stat -f%z "$STEM.dmg")
if [ $((SZ % 2048)) -ne 0 ]; then
    echo "warning: image size $SZ is not a 2048 multiple" >&2
fi
printf 'built %s (%s bytes); HFSX volume header:\n' "$STEM.dmg" "$SZ"
dd if="$STEM.dmg" bs=1 skip=1024 count=2 2>/dev/null | xxd
echo "payload: /$KPATH/kernelcache.s5l8900xrb"
