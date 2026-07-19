#!/bin/bash
# Shared S5L8900 iPod Touch 1G / iPhone 2G application launcher.
#
# The filename is retained for compatibility with existing app bundles. Select
# a board with Resources/s5l8900-profile or S5L8900_PROFILE. Explicit artifact
# environment variables override the profile defaults.

set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$DIR/Resources"
FRAMEWORKS="$DIR/Frameworks"
PROFILE_FILE="$RESOURCES/s5l8900-profile"

export DYLD_LIBRARY_PATH="$FRAMEWORKS"

PROFILE="${S5L8900_PROFILE:-}"
if [[ -z "$PROFILE" && -r "$PROFILE_FILE" ]]; then
    IFS= read -r PROFILE < "$PROFILE_FILE" || true
fi
PROFILE="${PROFILE:-ipod-touch}"

case "$PROFILE" in
    ipod-touch)
        DEFAULT_MACHINE="iPod-Touch"
        DEFAULT_DEVICE_NAME="iPod Touch 1G"
        DEFAULT_FIRMWARE_DIR="$RESOURCES/ipod_files"
        DEFAULT_IBOOT="iboot_204_n45ap.bin"
        DEFAULT_NOR="nor_n45ap.bin"
        ;;
    iphone-2g)
        DEFAULT_MACHINE="iPhone-2G"
        DEFAULT_DEVICE_NAME="iPhone 2G"
        DEFAULT_FIRMWARE_DIR="$RESOURCES/iphone_files"
        DEFAULT_IBOOT="iboot_204_m68ap.bin"
        DEFAULT_NOR="nor_m68ap.bin"
        ;;
    *)
        echo "Unsupported S5L8900 profile: $PROFILE" >&2
        echo "Expected ipod-touch or iphone-2g" >&2
        exit 2
        ;;
esac

MACHINE="${S5L8900_MACHINE:-$DEFAULT_MACHINE}"
DEVICE_NAME="${S5L8900_DEVICE_NAME:-$DEFAULT_DEVICE_NAME}"
FIRMWARE_DIR="${S5L8900_FIRMWARE_DIR:-$DEFAULT_FIRMWARE_DIR}"
BOOTROM="${S5L8900_BOOTROM:-$FIRMWARE_DIR/bootrom_s5l8900}"
IBOOT="${S5L8900_IBOOT:-$FIRMWARE_DIR/$DEFAULT_IBOOT}"
NOR="${S5L8900_NOR:-$FIRMWARE_DIR/$DEFAULT_NOR}"
NAND="${S5L8900_NAND:-$FIRMWARE_DIR/nand}"

require_file() {
    local label="$1"
    local path="$2"

    if [[ ! -f "$path" ]]; then
        echo "$DEVICE_NAME $label not found: $path" >&2
        exit 1
    fi
}

require_file "bootrom" "$BOOTROM"
require_file "iBoot" "$IBOOT"
require_file "NOR" "$NOR"
if [[ ! -d "$NAND" ]]; then
    echo "$DEVICE_NAME NAND directory not found: $NAND" >&2
    exit 1
fi

BRIDGE_PID=""
BRIDGE_PORT="${S5L8900_HTTP_BRIDGE_PORT:-8080}"
if [[ "${S5L8900_HTTP_BRIDGE:-1}" != "0" ]] &&
        command -v python3 >/dev/null 2>&1 &&
        [[ -f "$RESOURCES/ipod-http-bridge.py" ]]; then
    python3 "$RESOURCES/ipod-http-bridge.py" \
        --port "$BRIDGE_PORT" --device-name "$DEVICE_NAME" &
    BRIDGE_PID=$!
fi

cleanup() {
    if [[ -n "$BRIDGE_PID" ]]; then
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

QEMU_DIAGNOSTICS=(-serial null)
DEBUG="${S5L8900_DEBUG:-${IPOD_TOUCH_DEBUG:-0}}"
if [[ "$DEBUG" == "1" ]]; then
    QEMU_DIAGNOSTICS=(-serial mon:stdio -d unimp)
fi

"$DIR/MacOS/qemu-system-arm" \
    -M "$MACHINE,bootrom=$BOOTROM,iboot=$IBOOT,nand=$NAND" \
    -m 1G \
    -pflash "$NOR" \
    -L "$RESOURCES/pc-bios" \
    "${QEMU_DIAGNOSTICS[@]}" \
    "$@"
status=$?
exit "$status"
