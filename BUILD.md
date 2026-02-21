# Building the iPod Touch 1G Emulator

This guide covers building the QEMU-based iPod Touch 1G (S5L8900) emulator on **macOS (Apple Silicon)**.

## Prerequisites

### Homebrew packages

```bash
brew install ninja pkg-config glib sdl2 pixman openssl@3 libpng jpeg-turbo \
             gnutls libssh zstd
```

### Clone the repository

```bash
git clone https://github.com/devos50/qemu-ios.git
cd qemu-ios
git checkout ipod_touch_1g
git submodule update --init --recursive
```

### Download firmware files

Download the four firmware files from the
[n45ap_v1 GitHub release](https://github.com/devos50/qemu/releases/tag/n45ap_v1):

- `bootrom_s5l8900`
- `iboot_204_n45ap.bin`
- `nor_n45ap.bin`
- `nand_n45ap.zip`

Place them in a directory that will be accessible at runtime (see below).

---

## Part 1 — Building QEMU

### Configure

```bash
mkdir -p build && cd build

../configure \
    --enable-sdl \
    --disable-cocoa \
    --target-list=arm-softmmu \
    --disable-capstone \
    --disable-pie \
    --disable-slirp \
    --extra-cflags="-I/opt/homebrew/opt/openssl@3/include" \
    --extra-ldflags="-L/opt/homebrew/opt/openssl@3/lib -lcrypto"
```

### Build

```bash
make -j$(sysctl -n hw.ncpu)
```

The binary is at `build/arm-softmmu/qemu-system-arm`.

### Prepare firmware

```bash
mkdir -p build/ipod_files
# Copy the four firmware files into build/ipod_files/
cp bootrom_s5l8900 iboot_204_n45ap.bin nor_n45ap.bin nand_n45ap.zip build/ipod_files/

# Extract the NAND image
cd build/ipod_files
unzip nand_n45ap.zip        # creates a nand/ directory with bank0–bank7
cd ../..
```

### Run

From the `build/` directory:

```bash
./arm-softmmu/qemu-system-arm \
    -M iPod-Touch,bootrom=ipod_files/bootrom_s5l8900,iboot=ipod_files/iboot_204_n45ap.bin,nand=ipod_files/nand \
    -serial mon:stdio \
    -cpu max \
    -m 1G \
    -d unimp \
    -pflash ipod_files/nor_n45ap.bin
```

### Key bindings

| Key | Function     |
|-----|--------------|
| H   | Home button  |
| P   | Power button |

---

## Part 2 — Packaging as a macOS .app bundle

This creates a self-contained `iPod Touch.app` that can be double-clicked from
Finder. All Homebrew dylibs and firmware files are bundled inside.

Run every step from the **repository root** (not `build/`).

### 1. Create the bundle skeleton

```bash
APP="iPod Touch.app"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources/ipod_files"
mkdir -p "$APP/Contents/Resources/pc-bios/keymaps"
mkdir -p "$APP/Contents/Frameworks"
```

### 2. Copy the binary

```bash
cp build/arm-softmmu/qemu-system-arm "$APP/Contents/MacOS/qemu-system-arm"
```

### 3. Copy firmware files and keymaps

```bash
cp build/ipod_files/bootrom_s5l8900   "$APP/Contents/Resources/ipod_files/"
cp build/ipod_files/iboot_204_n45ap.bin "$APP/Contents/Resources/ipod_files/"
cp build/ipod_files/nor_n45ap.bin      "$APP/Contents/Resources/ipod_files/"
cp -R build/ipod_files/nand            "$APP/Contents/Resources/ipod_files/nand"
cp pc-bios/keymaps/*                   "$APP/Contents/Resources/pc-bios/keymaps/"
```

### 4. Copy the app icon

This requires `librsvg` to convert the SVG source to an `.icns` file:

```bash
brew install librsvg

# Render SVG to 1024px PNG with transparency
mkdir -p /tmp/icon_work
rsvg-convert -w 1024 -h 1024 --keep-aspect-ratio -b none \
    icon/IPod_app_iPhone_OS_icon.svg -o /tmp/icon_work/icon.png

# Pad to exact 1024x1024 (the SVG is slightly non-square)
sips -p 1024 1024 /tmp/icon_work/icon.png --out /tmp/icon_work/icon.png

# Generate the .iconset with all required sizes
ICONSET="/tmp/icon_work/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z $size $size /tmp/icon_work/icon.png --out "$ICONSET/icon_${size}x${size}.png"
    double=$((size * 2))
    sips -z $double $double /tmp/icon_work/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png"
done

# Convert to .icns
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf /tmp/icon_work
```

### 5. Bundle Homebrew dylibs

Collect every Homebrew dylib the binary (and its dependencies) reference:

```bash
# Strip the existing code signature so install_name_tool can modify the binary
codesign --remove-signature "$APP/Contents/MacOS/qemu-system-arm"

# Collect and rewrite dylibs
collect_dylibs() {
    local binary="$1"
    otool -L "$binary" | grep /opt/homebrew | awk '{print $1}' | while read dylib; do
        local name=$(basename "$dylib")
        local dest="$APP/Contents/Frameworks/$name"
        if [ ! -f "$dest" ]; then
            cp "$dylib" "$dest"
            chmod 644 "$dest"
            codesign --remove-signature "$dest"
            # Recursively collect dependencies of this dylib
            collect_dylibs "$dest"
        fi
        # Rewrite the reference in the binary
        install_name_tool -change "$dylib" "@executable_path/../Frameworks/$name" "$binary"
    done
}

collect_dylibs "$APP/Contents/MacOS/qemu-system-arm"

# Also rewrite inter-dylib references
for fw in "$APP/Contents/Frameworks/"*.dylib; do
    otool -L "$fw" | grep /opt/homebrew | awk '{print $1}' | while read dylib; do
        local name=$(basename "$dylib")
        install_name_tool -change "$dylib" "@executable_path/../Frameworks/$name" "$fw"
    done
done
```

### 6. Create the launcher script

```bash
cat > "$APP/Contents/MacOS/iPod Touch" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$DIR/Resources"
FRAMEWORKS="$DIR/Frameworks"

export DYLD_LIBRARY_PATH="$FRAMEWORKS"

exec "$DIR/MacOS/qemu-system-arm" \
    -M "iPod-Touch,bootrom=$RESOURCES/ipod_files/bootrom_s5l8900,iboot=$RESOURCES/ipod_files/iboot_204_n45ap.bin,nand=$RESOURCES/ipod_files/nand" \
    -serial mon:stdio \
    -cpu max \
    -m 1G \
    -d unimp \
    -pflash "$RESOURCES/ipod_files/nor_n45ap.bin" \
    -L "$RESOURCES/pc-bios" \
    "$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/iPod Touch"
```

### 7. Create Info.plist

```bash
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>iPod Touch</string>
    <key>CFBundleDisplayName</key>
    <string>iPod Touch 1G Emulator</string>
    <key>CFBundleIdentifier</key>
    <string>com.qemu.ipod-touch-1g</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>iPod Touch</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>NSHumanReadableCopyright</key>
    <string>iPod Touch 1G QEMU Emulator - Based on devos50/qemu fork</string>
</dict>
</plist>
PLIST
```

### 8. Sign the bundle

```bash
codesign --force --deep -s - "$APP"
```

### 9. Run the app

Double-click `iPod Touch.app` in Finder, or from the terminal:

```bash
open "iPod Touch.app"
```

> **Note:** If the app is stored inside `~/Documents/` or `~/Desktop/`, macOS
> will show a permission prompt the first time it runs. Moving the app to
> `/Applications` or another non-protected location avoids this.
