#!/bin/bash
#
# Full packaging for "iPhone 2G (iOS 1.1.4).app" (M68AP) — one command, end to end.
#
#   scripts/package-iphone-app.sh --firmware BUILD
#                                 [--app PATH] [--qemu PATH] [--build]
#                                 [--nand PATH] [--keep-nand] [--verify-only]
#                                 [--create [--name N] [--bundle-id ID] [--icon F]]
#                                 [--iboot F] [--nor F] [--epoch N]
#
# --firmware names the iPhone OS build (1A543a, 1C28, 3A109a, 4A102) and is
# REQUIRED when firmware is installed: it resolves the iBoot, NOR, NAND and the
# security EPOCH together from m68ap-artifacts/builds/<BUILD>/, so a bundle
# cannot pair one firmware's images with another's epoch. See
# M68AP_BUILD_LAYOUT.md.
#
# (It is spelled --firmware, not --build, only because --build already means
# "rebuild the engine" here. Every python tool in the tree uses --build.)
#
# Steps: (optionally build) -> install engine + launcher + bundled dylibs ->
# GENERATE the home-screen NAND from the staged artifacts -> install the M68AP
# firmware (bootrom, secure-boot-patched iBoot, NOR, NAND) -> sign -> verify.
#
# Why this is a separate script from package-ipod-app.sh: the iPod ships a
# device-dump NAND that is patched in place, while the iPhone's NAND is BUILT
# every time from the IPSW-derived images plus the activation/compositing
# recipe (build-m68ap-homescreen-nand.py). Packaging means different work.
#
# What the guest ends up with, and which parts are shortcuts:
#   * lockdownd activation patch      — required; no data-only ark survives
#                                       boot re-validation without a genuine
#                                       Apple-signed activation record
#   * LK_ENABLE_MBX2D=0               — SHORTCUT for the unmodelled MBX 2D
#                                       (task T2 in M68AP_RENDER_HANDOFF.md);
#                                       devos50's iPod image ships it too
#   * reference-shaped data ark       — key names/types mirrored from a real
#                                       activated device; no Apple material
#
# Idempotent: re-running upgrades the bundle in place.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

APP="/Applications/iPhone 2G (iOS 1.1.4).app"
QEMU="$REPO/build-ipod11/qemu-system-arm"
FIRMWARE=""
NAND_SRC=""
IBOOT_SRC=""
NOR_SRC=""
EPOCH=""
DO_CREATE=0
BUNDLE_NAME=""
BUNDLE_ID=""
ICON_SRC=""
KEEP_NAND=0
DO_BUILD=0
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) APP="$2"; shift 2 ;;
        --qemu) QEMU="$2"; shift 2 ;;
        --nand) NAND_SRC="$2"; shift 2 ;;
        --firmware) FIRMWARE="$2"; shift 2 ;;
        --iboot) IBOOT_SRC="$2"; shift 2 ;;
        --nor) NOR_SRC="$2"; shift 2 ;;
        --epoch) EPOCH="$2"; shift 2 ;;
        --create) DO_CREATE=1; shift ;;
        --name) BUNDLE_NAME="$2"; shift 2 ;;
        --bundle-id) BUNDLE_ID="$2"; shift 2 ;;
        --icon) ICON_SRC="$2"; shift 2 ;;
        --keep-nand) KEEP_NAND=1; shift ;;
        --build) DO_BUILD=1; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
FW="$APP/Contents/Resources/iphone_files"

# Resolve every per-build path from one place. Requiring --firmware here is
# what stops a bundle shipping 1.0's iBoot with 1.1.4's epoch.
if [[ $VERIFY_ONLY -eq 0 ]]; then
    if [[ -z "$FIRMWARE" ]]; then
        echo "--firmware BUILD is required (1A543a, 1C28, 3A109a, 4A102)" >&2
        echo "see M68AP_BUILD_LAYOUT.md" >&2
        exit 2
    fi
    fw_path() {
        python3 -c "import sys,json;print(json.load(sys.stdin)['$1'])" \
            <<< "$FW_JSON"
    }
    FW_JSON="$(python3 "$SCRIPT_DIR/m68ap_paths.py" --build "$FIRMWARE" --json)"
    BUILD_DIR="$(fw_path dir)"
    BOOTROM_SRC="$(fw_path bootrom)"
    : "${IBOOT_SRC:=$(fw_path iboot_sb)}"
    : "${NOR_SRC:=$(fw_path nor)}"
    : "${EPOCH:=$(fw_path epoch)}"
    say "firmware $FIRMWARE (iPhone OS $(fw_path version)), epoch $EPOCH"
fi

if [[ $VERIFY_ONLY -eq 0 ]]; then
    if [[ $DO_BUILD -eq 1 ]]; then
        say "building the engine"
        ninja -C "$REPO/build-ipod11" qemu-system-arm
    fi
    if [[ $DO_CREATE -eq 1 && ! -d "$APP" ]]; then
        # Build the skeleton from the committed template instead of requiring
        # an existing bundle to clone. Everything else in a .app is built
        # (engine, dylibs, signature) or Apple-derived (firmware), so the
        # template plus the launcher IS the whole non-derivable part.
        say "creating bundle skeleton at $APP"
        # install-ipod-app-engine.sh validates the bundle by the presence of
        # Contents/Frameworks and Contents/Info.plist, so create both.
        # install-ipod-app-engine.sh validates the bundle by Contents/
        # Frameworks + Info.plist, and refuses to run without a firmware NAND
        # directory, so create the placeholders it wants. The real firmware is
        # installed by the step after it.
        mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" \
                 "$APP/Contents/Frameworks" \
                 "$APP/Contents/Resources/iphone_files/nand"
        name="${BUNDLE_NAME:-$(basename "$APP" .app)}"
        sed -e "s|@NAME@|$name|g" \
            -e "s|@BUNDLE_ID@|${BUNDLE_ID:-com.qemu.iphone-2g}|g" \
            "$REPO/packaging/iphone-2g/Info.plist.in" > "$APP/Contents/Info.plist"
        if [[ -n "$ICON_SRC" && -f "$ICON_SRC" ]]; then
            cp "$ICON_SRC" "$APP/Contents/Resources/AppIcon.icns"
        fi
    fi
    [[ -x "$QEMU" ]] || { echo "no engine at $QEMU (pass --build)" >&2; exit 1; }
    [[ -d "$APP" ]] || { echo "no app bundle at $APP (see BUILD.md Part 3)" >&2; exit 1; }

    say "installing engine, launcher and dylibs into $APP"
    "$SCRIPT_DIR/install-ipod-app-engine.sh" "$QEMU" "$APP" iphone-2g

    TMP_NAND=""
    if [[ -z "$NAND_SRC" ]]; then
        TMP_NAND="$(mktemp -d "${TMPDIR:-/tmp}/m68ap-nand.XXXXXX")/nand"
        say "generating the home-screen NAND (this is the slow part)"
        python3 "$SCRIPT_DIR/build-m68ap-homescreen-nand.py" \
            --build "$FIRMWARE" --out "$TMP_NAND"
        NAND_SRC="$TMP_NAND"
    else
        say "using the NAND you supplied: $NAND_SRC"
    fi
    [[ -d "$NAND_SRC/bank0" ]] || { echo "not a NAND tree: $NAND_SRC" >&2; exit 1; }

    # PACK the NAND before installing it. This is not an optimisation, it is
    # what makes the app launchable: the launcher clones the NAND per launch
    # (the M68AP kernel needs a clean one), and a sparse tree is ~148_000
    # files -- cloning that on every launch takes minutes, during which no
    # window appears and the Dock icon just bounces. The QEMU NAND model reads
    # pages from nand.pack (immutable base) and writes to bank<N>/<page>_new
    # .page, so shipping the pack plus EMPTY bank directories is functionally
    # identical and stages in ~0 s. The bank dirs must exist: the write path
    # fopen()s into them and hw_error()s if they are missing.
    if [[ ! -f "$NAND_SRC/nand.pack" ]]; then
        say "packing the NAND (one file instead of ~148k)"
        python3 "$SCRIPT_DIR/pack-ipod-nand.py" "$NAND_SRC"
    fi

    say "installing M68AP firmware"
    mkdir -p "$FW"
    # The launcher loads the iBoot under its plain name; it MUST be the
    # secure-boot-patched build or the kernel never starts.
    # --iboot/--nor/--epoch let one recipe ship a DIFFERENT firmware version.
    # The launcher loads the iBoot under its plain name whatever its build, so
    # a 1.0 bundle carries iBoot-159 here; --epoch is what stops it wedging
    # (M68AP defaults to 1.1.4's epoch 3, and 1.0's images are epoch 0).
    cp "$IBOOT_SRC"   "$FW/iboot_204_m68ap.bin"
    cp "$BOOTROM_SRC" "$FW/bootrom_s5l8900"
    cp "$NOR_SRC"     "$FW/nor_m68ap.bin"
    if [[ -n "$EPOCH" ]]; then
        printf '%s\n' "$EPOCH" > "$FW/epoch"
        say "security epoch pinned to $EPOCH"
    else
        rm -f "$FW/epoch"
    fi
    rm -rf "$FW/nand.new"
    mkdir -p "$FW/nand.new"
    cp "$NAND_SRC/nand.pack" "$FW/nand.new/nand.pack"
    for bank in "$NAND_SRC"/bank*; do
        mkdir -p "$FW/nand.new/$(basename "$bank")"   # writable, empty
    done
    # Carry the constructor's sidecar. Shipping pack+empty-banks discards the
    # page tree deliberately, but the sidecar is the ONLY record of which
    # firmware's filesystem and which guest modifications are in that pack --
    # and without it a bundle cannot be told apart from stock firmware after
    # the fact. It is ~2 KB.
    if [[ -f "$NAND_SRC/nand-provenance.json" ]]; then
        cp "$NAND_SRC/nand-provenance.json" "$FW/nand.new/nand-provenance.json"
    else
        say "WARNING: $NAND_SRC has no nand-provenance.json; the bundle will" \
            "record its NAND provenance as MISSING"
    fi
    rm -rf "$FW/nand"
    mv "$FW/nand.new" "$FW/nand"
    if [[ -n "$TMP_NAND" && $KEEP_NAND -eq 0 ]]; then
        rm -rf "$(dirname "$TMP_NAND")"
    elif [[ -n "$TMP_NAND" ]]; then
        echo "  kept generated NAND at $TMP_NAND"
    fi

    printf 'iphone-2g\n' > "$APP/Contents/Resources/s5l8900-profile"

    # This script installs the firmware itself rather than going through
    # install-iphone-firmware.py, so nothing here would otherwise touch
    # firmware-provenance.json -- and it silently did not, for long enough
    # that a shipped bundle carried a manifest describing a NAND that had been
    # replaced several packagings earlier. Derive it from what is now on disk.
    # (This also signs, so no separate codesign call is needed.)
    say "refreshing the firmware manifest"
    python3 "$SCRIPT_DIR/install-iphone-firmware.py" \
        --app "$APP" --refresh-manifest
fi

say "verifying"
fail=0
check() { if [[ -n "$2" ]]; then printf '  ok   %s\n' "$1"; else printf '  FAIL %s\n' "$1"; fail=1; fi; }

check "profile = iphone-2g" "$(grep -Fx iphone-2g "$APP/Contents/Resources/s5l8900-profile" 2>/dev/null || true)"
check "engine present" "$([[ -x "$APP/Contents/MacOS/qemu-system-arm" ]] && echo y)"
homebrew_deps="$(otool -L "$APP/Contents/MacOS/qemu-system-arm" 2>/dev/null | grep -c '/opt/homebrew' || true)"
check "no Homebrew load paths (found $homebrew_deps)" "$([[ "$homebrew_deps" == 0 ]] && echo y)"
check "engine supports -M iPhone-2G" \
      "$("$APP/Contents/MacOS/qemu-system-arm" -M help 2>/dev/null | grep -i iPhone-2G || true)"
check "firmware: bootrom" "$([[ -f "$FW/bootrom_s5l8900" ]] && echo y)"
# Verify the PROPERTY (is it patched?), not identity with one staged file:
# a versioned bundle may legitimately carry a different build -- a 1.0 bundle
# ships iBoot-159, whose patch site is 0x5350, not 204's 0x5990. The patch tool
# locates it by pattern in any 1.x image.
check "firmware: iBoot (secure-boot-patched)" \
      "$(python3 "$SCRIPT_DIR/patch-m68ap-iboot.py" --verify \
             "$FW/iboot_204_m68ap.bin" 2>/dev/null | grep -c ': patched')"
check "firmware: NOR" "$([[ -f "$FW/nor_m68ap.bin" ]] && echo y)"
check "firmware: NAND pack" "$([[ -f "$FW/nand/nand.pack" ]] && echo y)"
check "firmware: writable bank dirs" "$([[ -d "$FW/nand/bank3" ]] && echo y)"
# A sparse tree here means every launch clones ~148k files before any window
# appears -- the "app bounces forever in the Dock" failure.
nand_files="$(find "$FW/nand" -type f 2>/dev/null | wc -l | tr -d ' ')"
check "NAND stages fast (files: $nand_files, want 1)" "$([[ "$nand_files" -le 2 ]] && echo y)"
check "launcher stages NAND per launch" \
      "$(grep -l 'S5L8900_STAGE_NAND' "$APP/Contents/MacOS/"* 2>/dev/null | head -1)"
check "signature valid" "$(codesign --verify --deep --strict "$APP" 2>&1 >/dev/null && echo y)"

if [[ $fail -ne 0 ]]; then
    echo; echo "packaging INCOMPLETE" >&2; exit 1
fi
cat <<EOF

iPhone 2G (iOS 1.1.4).app is ready: open it, or \`open -a "$APP"\`
  expect: ~2-3 min to the home screen (Phone/Mail/Safari/iPod dock).
  the status bar reads "No Service" — telephony registration is still open work.
EOF
