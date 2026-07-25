#!/bin/bash
#
# Full packaging for "iPod Touch.app" (N45AP) — one command, end to end.
#
#   scripts/package-ipod-app.sh [--app PATH] [--qemu PATH] [--build] [--verify-only]
#
# Steps: (optionally build) -> install engine + launcher + bundled dylibs ->
# restore DNS + inject the HTTPS-bridge CA into the NAND -> pack -> sign ->
# verify. Everything except the guest firmware, which the bundle already ships.
#
# The companion script for the other board is package-iphone-app.sh. They are
# deliberately two scripts, not one with a flag: the boards differ in what
# packaging *means* (the iPod ships a device-dump NAND that gets patched in
# place; the iPhone generates its NAND from staged artifacts every time).
#
# Idempotent: re-running upgrades the bundle in place.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

APP="/Applications/iPod Touch.app"
QEMU="$REPO/build-ipod11/qemu-system-arm"
DO_BUILD=0
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) APP="$2"; shift 2 ;;
        --qemu) QEMU="$2"; shift 2 ;;
        --build) DO_BUILD=1; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }

if [[ $VERIFY_ONLY -eq 0 ]]; then
    if [[ $DO_BUILD -eq 1 ]]; then
        say "building the engine"
        ninja -C "$REPO/build-ipod11" qemu-system-arm
    fi
    [[ -x "$QEMU" ]] || { echo "no engine at $QEMU (pass --build)" >&2; exit 1; }
    [[ -d "$APP" ]] || { echo "no app bundle at $APP" >&2; exit 1; }

    say "installing engine, launcher and dylibs into $APP"
    # This also restores DNS, injects the HTTPS-bridge CA, packs the NAND and
    # signs the bundle (N45AP profile).
    "$SCRIPT_DIR/install-ipod-app-engine.sh" "$QEMU" "$APP" ipod-touch
fi

say "verifying"
FW="$APP/Contents/Resources/ipod_files"
fail=0
check() { if [[ -n "$2" ]]; then printf '  ok   %s\n' "$1"; else printf '  FAIL %s\n' "$1"; fail=1; fi; }

check "profile = ipod-touch" "$(grep -Fx ipod-touch "$APP/Contents/Resources/s5l8900-profile" 2>/dev/null || true)"
check "engine present" "$([[ -x "$APP/Contents/MacOS/qemu-system-arm" ]] && echo y)"
homebrew_deps="$(otool -L "$APP/Contents/MacOS/qemu-system-arm" 2>/dev/null | grep -c '/opt/homebrew' || true)"
check "no Homebrew load paths (found $homebrew_deps)" "$([[ "$homebrew_deps" == 0 ]] && echo y)"
check "firmware: bootrom" "$([[ -f "$FW/bootrom_s5l8900" ]] && echo y)"
check "firmware: iboot" "$([[ -f "$FW/iboot_204_n45ap.bin" ]] && echo y)"
check "firmware: NOR" "$([[ -f "$FW/nor_n45ap.bin" ]] && echo y)"
check "firmware: NAND (banks)" "$([[ -d "$FW/nand/bank0" ]] && echo y)"
check "signature valid" "$(codesign --verify --deep --strict "$APP" 2>&1 >/dev/null && echo y)"

if [[ $fail -ne 0 ]]; then
    echo; echo "packaging INCOMPLETE" >&2; exit 1
fi
printf '\niPod Touch.app is ready: open it, or `open -a "%s"`\n' "$APP"
