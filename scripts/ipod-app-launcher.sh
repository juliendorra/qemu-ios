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

# The M68AP kernel only completes FTL_Open against a *clean* NAND (guest writes
# never persist correctly on the generated tree), and it also writes to NOR. The
# bundle's Resources copy must therefore stay pristine: stage a fresh writable
# clone per launch and throw it away on exit. `cp -Rc` clones on APFS, so this
# is cheap. N45AP keeps the historical in-place behaviour, which its real
# device-dump NAND tolerates.
#
# IMPORTANT: this per-launch clone is why the bundle must ship a PACKED NAND
# (nand.pack + empty bank dirs), which package-iphone-app.sh does. Cloning a
# sparse tree of ~148_000 page files takes minutes on every launch, and since
# no QEMU window appears until it finishes, the app just bounces in the Dock
# and looks hung. With the pack it is a single-file clone (~0 s). The QEMU NAND
# model reads pages from the pack and writes to bank<N>/<page>_new.page, so the
# empty bank dirs must exist and must be writable.
STAGE_DIR=""
if [[ "$PROFILE" == "iphone-2g" && "${S5L8900_STAGE_NAND:-1}" != "0" ]]; then
    STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/s5l8900-nand.XXXXXX")"
    cp -Rc "$NAND" "$STAGE_DIR/nand" 2>/dev/null || cp -R "$NAND" "$STAGE_DIR/nand"
    cp "$NOR" "$STAGE_DIR/nor.bin"
    NAND="$STAGE_DIR/nand"
    NOR="$STAGE_DIR/nor.bin"
fi

BRIDGE_PID=""
HTTPS_PID=""
BRIDGE_PORT="${S5L8900_HTTP_BRIDGE_PORT:-18080}"
if [[ "${S5L8900_HTTP_BRIDGE:-1}" != "0" ]] &&
        command -v python3 >/dev/null 2>&1 &&
        [[ -f "$RESOURCES/ipod-http-bridge.py" ]]; then
    python3 "$RESOURCES/ipod-http-bridge.py" \
        --port "$BRIDGE_PORT" --device-name "$DEVICE_NAME" &
    BRIDGE_PID=$!
fi

HTTPS_PORT="${S5L8900_HTTPS_PROXY_PORT:-18443}"
HTTPS_CONTROL_PORT="${S5L8900_HTTPS_PROXY_CONTROL_PORT:-18442}"
HTTPS_STATE_DIR="${S5L8900_HTTPS_STATE_DIR:-$HOME/Library/Application Support/S5L8900 HTTPS Bridge/$PROFILE}"
HTTPS_PROOF_LOG="${S5L8900_HTTPS_PROOF_LOG:-$HTTPS_STATE_DIR/https-proof.jsonl}"
if [[ "${S5L8900_HTTPS_BRIDGE:-1}" != "0" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        echo "HTTPS bridge requires python3" >&2
        exit 1
    fi
    for helper in ipod-https-proxy.py ipod_tls_common.py; do
        if [[ ! -f "$RESOURCES/$helper" ]]; then
            echo "HTTPS bridge helper missing: $RESOURCES/$helper" >&2
            exit 1
        fi
    done
    mkdir -p "$HTTPS_STATE_DIR"
    chmod 700 "$HTTPS_STATE_DIR"
    echo "Starting local HTTPS compatibility bridge; TLS metadata only is logged." >&2
    echo "Do not enter credentials into sites you do not intend to intercept." >&2
    python3 "$RESOURCES/ipod-https-proxy.py" \
        --port "$HTTPS_PORT" --control-port "$HTTPS_CONTROL_PORT" \
        --state "$HTTPS_STATE_DIR" --proof-log "$HTTPS_PROOF_LOG" &
    HTTPS_PID=$!
    sleep 1
    if ! kill -0 "$HTTPS_PID" 2>/dev/null; then
        wait "$HTTPS_PID" || true
        echo "HTTPS bridge failed to start (ports may already be in use)" >&2
        exit 1
    fi
    export IPOD_HTTPS_PROXY_PORT="$HTTPS_PORT"
    export IPOD_HTTPS_PROXY_CONTROL_PORT="$HTTPS_CONTROL_PORT"
fi

cleanup() {
    if [[ -n "$BRIDGE_PID" ]]; then
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
    if [[ -n "$HTTPS_PID" ]]; then
        kill "$HTTPS_PID" 2>/dev/null || true
        wait "$HTTPS_PID" 2>/dev/null || true
    fi
    if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
        rm -rf "$STAGE_DIR" 2>/dev/null || true
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
