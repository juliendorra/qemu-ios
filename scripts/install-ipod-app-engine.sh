#!/bin/bash

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 QEMU_BINARY [APP_BUNDLE]" >&2
    exit 2
fi

QEMU_BINARY="$1"
APP_BUNDLE="${2:-/Applications/iPod Touch.app}"
CONTENTS="$APP_BUNDLE/Contents"
TARGET_BINARY="$CONTENTS/MacOS/qemu-system-arm"
TARGET_FRAMEWORKS="$CONTENTS/Frameworks"

if [[ ! -x "$QEMU_BINARY" ]]; then
    echo "QEMU binary is not executable: $QEMU_BINARY" >&2
    exit 1
fi
if [[ ! -d "$TARGET_FRAMEWORKS" || ! -f "$CONTENTS/Info.plist" ]]; then
    echo "Invalid iPod Touch application bundle: $APP_BUNDLE" >&2
    exit 1
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ipod-app-engine.XXXXXX")"
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
codesign --force --deep -s - "$APP_BUNDLE"
codesign --verify --deep --strict "$APP_BUNDLE"

echo "Installed $("$TARGET_BINARY" --version | head -1) into $APP_BUNDLE"
