#!/bin/bash

set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "usage: $0 QEMU_BINARY [APP_BUNDLE] [ipod-touch|iphone-2g]" >&2
    exit 2
fi

QEMU_BINARY="$1"
APP_BUNDLE="${2:-/Applications/iPod Touch.app}"
PROFILE="${3:-ipod-touch}"
CONTENTS="$APP_BUNDLE/Contents"
TARGET_BINARY="$CONTENTS/MacOS/qemu-system-arm"
TARGET_FRAMEWORKS="$CONTENTS/Frameworks"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$PROFILE" in
    ipod-touch|iphone-2g) ;;
    *)
        echo "Invalid S5L8900 profile: $PROFILE" >&2
        exit 2
        ;;
esac

if [[ ! -x "$QEMU_BINARY" ]]; then
    echo "QEMU binary is not executable: $QEMU_BINARY" >&2
    exit 1
fi
if [[ ! -d "$TARGET_FRAMEWORKS" || ! -f "$CONTENTS/Info.plist" ]]; then
    echo "Invalid S5L8900 application bundle: $APP_BUNDLE" >&2
    exit 1
fi
for helper in ipod-app-launcher.sh ipod-http-bridge.py; do
    if [[ ! -f "$SCRIPT_DIR/$helper" ]]; then
        echo "Missing installer helper: $SCRIPT_DIR/$helper" >&2
        exit 1
    fi
done

BUNDLE_EXECUTABLE="$(/usr/libexec/PlistBuddy \
    -c 'Print :CFBundleExecutable' "$CONTENTS/Info.plist")"
if [[ -z "$BUNDLE_EXECUTABLE" || "$BUNDLE_EXECUTABLE" == */* ||
        "$BUNDLE_EXECUTABLE" == "qemu-system-arm" ]]; then
    echo "Invalid CFBundleExecutable for launcher installation: $BUNDLE_EXECUTABLE" >&2
    exit 1
fi
TARGET_LAUNCHER="$CONTENTS/MacOS/$BUNDLE_EXECUTABLE"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/s5l8900-app-engine.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Frameworks"
cp -R "$TARGET_FRAMEWORKS/." "$STAGE/Frameworks/"
cp "$QEMU_BINARY" "$STAGE/qemu-system-arm"
chmod 755 "$STAGE/qemu-system-arm"
codesign --remove-signature "$STAGE/qemu-system-arm" 2>/dev/null || true

SEEN="$STAGE/seen-dylibs"
touch "$SEEN"

bundle_dependencies() {
    local binary="$1"
    local dylib name destination first_dependency_line

    first_dependency_line=1
    if [[ "$binary" == *.dylib ]]; then
        # A dylib's first load-command entry is its own install ID.
        first_dependency_line=2
    fi

    while IFS= read -r dylib; do
        [[ -n "$dylib" ]] || continue
        name="$(basename "$dylib")"
        destination="$STAGE/Frameworks/$name"

        if [[ ! -f "$destination" ]]; then
            cp "$dylib" "$destination"
            chmod 644 "$destination"
            codesign --remove-signature "$destination" 2>/dev/null || true
        fi

        install_name_tool -change "$dylib" \
            "@executable_path/../Frameworks/$name" "$binary"

        if ! grep -Fqx "$name" "$SEEN"; then
            echo "$name" >> "$SEEN"
            codesign --remove-signature "$destination" 2>/dev/null || true
            bundle_dependencies "$destination"
        fi
    done < <(otool -L "$binary" | \
        awk -v first="$first_dependency_line" 'NR > first { print $1 }' | \
        grep '^/opt/homebrew/' || true)
}

bundle_dependencies "$STAGE/qemu-system-arm"

if otool -L "$STAGE/qemu-system-arm" | grep -q '/opt/homebrew/'; then
    echo "The staged engine still contains Homebrew load paths" >&2
    exit 1
fi
for dylib in "$STAGE/Frameworks/"*.dylib; do
    if otool -L "$dylib" | awk 'NR > 2 { print $1 }' | \
            grep -q '^/opt/homebrew/'; then
        echo "The staged framework still contains Homebrew load paths: $dylib" >&2
        exit 1
    fi
    codesign --force -s - "$dylib"
done
codesign --force -s - "$STAGE/qemu-system-arm"

ditto "$STAGE/Frameworks" "$TARGET_FRAMEWORKS"
cp "$STAGE/qemu-system-arm" "$TARGET_BINARY"
chmod 755 "$TARGET_BINARY"
cp "$SCRIPT_DIR/ipod-http-bridge.py" "$CONTENTS/Resources/ipod-http-bridge.py"
chmod 644 "$CONTENTS/Resources/ipod-http-bridge.py"
cp "$SCRIPT_DIR/ipod-app-launcher.sh" "$TARGET_LAUNCHER"
chmod 755 "$TARGET_LAUNCHER"
printf '%s\n' "$PROFILE" > "$CONTENTS/Resources/s5l8900-profile"
codesign --force --deep -s - "$APP_BUNDLE"
codesign --verify --deep --strict "$APP_BUNDLE"

echo "Installed $("$TARGET_BINARY" --version | head -1) into $APP_BUNDLE"
