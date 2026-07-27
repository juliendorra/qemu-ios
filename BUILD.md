# Building the iPod Touch 1G Emulator

> **Historical provenance:** Carried forward on 2026-07-21 from
> `ipod_touch_1g-qemu6-legacy` at `4221943495`. This guide records the older
> build and application workflow. For the current reproducible QEMU 11 build,
> use [`BUILDING.md`](BUILDING.md).


This guide covers building the QEMU-based iPod Touch 1G (S5L8900) emulator on **macOS (Apple Silicon)**.

The browser/WebAssembly port — iPhone 2G running iPhone OS 1.0/1.0.2/1.1.1/1.1.4
in a browser, from prepared self-hosted assets — is designed in
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md),
with live state in [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) and its
build tooling in [`scripts/wasm/`](scripts/wasm/README.md).

The active QEMU 11 native forward-port, including its commits, successful NAND
DMA fix, current GUI blocker, rejected workarounds, and promotion matrix, is
tracked in [`QEMU_11_PORT.md`](QEMU_11_PORT.md).

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

### Optimized release build

Keep the development build above and configure an independent release tree so
the two binaries can be compared from the same source revision:

```bash
mkdir -p build-release && cd build-release

../configure \
    --enable-sdl \
    --disable-cocoa \
    --target-list=arm-softmmu \
    --disable-capstone \
    --disable-pie \
    --disable-slirp \
    --disable-debug-info \
    --enable-lto \
    --extra-cflags="-I/opt/homebrew/opt/openssl@3/include" \
    --extra-ldflags="-L/opt/homebrew/opt/openssl@3/lib -lcrypto" \
    --with-git-submodules=validate

/usr/bin/python3 -B ../meson/meson.py configure \
    -Doptimization=3 \
    -Db_ndebug=false \
    .

ninja qemu-system-arm
```

This produces an `-O3`, LTO-enabled binary without debug information at
`build-release/qemu-system-arm`. Assertions intentionally remain enabled:
this QEMU 6.2 tree rejects `NDEBUG` in `include/qemu/osdep.h`, so setting
`b_ndebug=true` is not a supported release configuration here.

On the 2026-07-17 M2 benchmark at revision `81d477c5d4`, the release binary
was 13 MB versus 15 MB for the development binary. Three alternating boots
used the same ARM1176 model, quiet serial capture, no display backend, and a
fresh copy-on-write clone of the same NAND for every run. Median time to
SpringBoard improved from 16.194 s to 11.623 s (28.2%); mean time improved
from 14.354 s to 12.908 s (10.1%). The run-to-run spread was large, so use the
mean as the conservative expectation and repeat the benchmark when comparing
machines or further compiler changes. This boot test excludes SDL rendering;
frame-presentation performance must be measured separately.

### Experimental QEMU 11 forward-port build

The isolated `codex/qemu-11-port` branch is based on upstream QEMU 11.0.2.
Revision `f734de901e` is the engine installed in
`/Applications/iPod Touch.app`. Its cold GUI/input, manual and timed
guest-driven sleep, retained wake, repeated Z2 reload, and post-wake drag
matrix passed on 2026-07-17. The 2026-07-18 follow-up also restores the old
fork's non-capturing SDL behavior for the iPod's absolute touchscreen, so the
macOS cursor remains visible and free over the display. The complete
investigation is in `QEMU_11_PORT.md`.

Configure it in a separate worktree and build directory:

```bash
mkdir build-ipod
cd build-ipod

../configure \
    --target-list=arm-softmmu \
    --enable-sdl \
    --disable-cocoa \
    --disable-slirp \
    --disable-docs \
    --disable-debug-info \
    --enable-lto \
    --extra-cflags="-I/opt/homebrew/opt/openssl@3/include" \
    --extra-ldflags="-L/opt/homebrew/opt/openssl@3/lib -lcrypto"

ninja qemu-system-arm
```

The resulting binary is `build-ipod/qemu-system-arm`. The first controlled
headless test used the installed firmware and packed NAND, followed the real
VROM/NOR/LLB/iBoot/kernel chain, and reached the SpringBoard serial marker in
6.087 seconds. The key forward-port compatibility detail is DMAC0 request 2:
the iPod NAND FIFO stub is always ready, while modern PL080 correctly waits for
a peripheral request. The port models that one permanent request explicitly
instead of restoring the old fork's global request-check bypass.

The GUI blocker was the analogous SPI2 transmit path. The native Z2 driver
uses DMAC1 memory-to-peripheral request 14 for its large firmware transfer.
The synchronous FIFO stub therefore asserts only request 14. With both scoped
requests modeled, the QEMU 11 engine reaches the home screen and survives
manual, timed, and repeated retained-wake cycles.

To promote a tested engine into an existing app bundle, use the staged
installer. It recursively bundles Homebrew dylibs, rejects unresolved runtime
paths before deployment, signs the app, and verifies the final signature:

```bash
scripts/install-ipod-app-engine.sh \
    /private/tmp/qemu-11-port/build-ipod/qemu-system-arm \
    "/Applications/iPod Touch.app" \
    ipod-touch
```

The installer and launcher are shared by both S5L8900 board profiles. The
historical script filenames remain for compatibility. An eventual iPhone
bundle selects its board and default artifact directory with:

```bash
scripts/install-ipod-app-engine.sh \
    /path/to/qemu-system-arm \
    "/Applications/iPhone 2G.app" \
    iphone-2g
```

The selected profile is stored in `Contents/Resources/s5l8900-profile`.
`ipod-touch` uses `iPod-Touch` plus `Resources/ipod_files`; `iphone-2g` uses
`iPhone-2G` plus `Resources/iphone_files`. The launcher also accepts
`S5L8900_PROFILE`, `S5L8900_FIRMWARE_DIR`, `S5L8900_BOOTROM`,
`S5L8900_IBOOT`, `S5L8900_NOR`, and `S5L8900_NAND` overrides.

Do not include `build-ipod/` in commits. It is a local build product and, on
the port branch, is intentionally left untracked.

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

For substantially faster and more repeatable cold boots, build the optional
read-only base pack after extracting the NAND:

```bash
python3 -B scripts/pack-ipod-nand.py build/ipod_files/nand
```

This creates `build/ipod_files/nand/nand.pack` without modifying the source
pages. The emulator uses the pack when present and falls back to the legacy
page files when it is absent. Do not merge or promote historical
`*_new.page` files into the base: they are incomplete write captures, not a
replayable NAND overlay. Writable persistence requires a complete data,
spare, program, and erase model and remains planned work.

### Run

From the `build/` directory:

```bash
./arm-softmmu/qemu-system-arm \
    -M iPod-Touch,bootrom=ipod_files/bootrom_s5l8900,iboot=ipod_files/iboot_204_n45ap.bin,nand=ipod_files/nand \
    -serial mon:stdio \
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
cp scripts/ipod-app-launcher.sh "$APP/Contents/MacOS/iPod Touch"
chmod +x "$APP/Contents/MacOS/iPod Touch"
printf '%s\n' ipod-touch > "$APP/Contents/Resources/s5l8900-profile"
cp scripts/ipod-http-bridge.py "$APP/Contents/Resources/"
```

The launcher starts a loopback-only HTTP compatibility service that can fetch
modern HTTPS pages and rewrite their links for old Safari. This service is
common to both board profiles, but it does **not** itself provide guest
networking: the SDIO Wi-Fi model still needs a working network-backend path
before either guest can reach the host bridge. Disable the service with
`S5L8900_HTTP_BRIDGE=0` or change its host port with
`S5L8900_HTTP_BRIDGE_PORT`.

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

The packaged launcher uses the machine's ARM1176 default and suppresses the
very verbose guest serial and unimplemented-device logs. For an investigation,
launch it from Terminal with diagnostics restored:

```bash
S5L8900_DEBUG=1 "iPod Touch.app/Contents/MacOS/iPod Touch"
```

`IPOD_TOUCH_DEBUG=1` remains accepted as a compatibility alias.

> **Note:** If the app is stored inside `~/Documents/` or `~/Desktop/`, macOS
> will show a permission prompt the first time it runs. Moving the app to
> `/Applications` or another non-protected location avoids this.

## Acceptance testing

Before promoting an engine build into the application bundle, run the
sleep/wake acceptance matrix (see `SLEEP_WAKE_INVESTIGATION.md` for what it
covers and why the lock-phase steps exist):

```bash
python3 scripts/ipod-acceptance-test.py --timed
IPOD_QEMU=/path/to/build/qemu-system-arm python3 scripts/ipod-acceptance-test.py
```

---

## Part 3b — A bundle for a DIFFERENT iPhone OS version

`package-iphone-app.sh` can build a bundle from scratch and pin it to any 1.x
firmware. Two things vary per version and both must be passed: the iBoot build
(1.0/1.0.x ship iBoot-159, 1.1.x iBoot-204) and the **security epoch** — M68AP
defaults to 1.1.4's epoch 3, and booting 1.0's epoch-0 images under it wedges in
iBoot with an *empty* serial log, which looks exactly like a hang.

```bash
scripts/package-iphone-app.sh --create \
    --app "/Applications/iPhone 2G (iOS 1.0).app" \
    --name "iPhone 2G (iOS 1.0)" \
    --nand  m68ap-artifacts/stage-1.0/nand \
    --iboot m68ap-artifacts/stage-1.0/iboot_159_m68ap_sbpatch.bin \
    --nor   m68ap-artifacts/stage-1.0/nor_m68ap.bin \
    --epoch 0
```

`--create` builds the skeleton from `packaging/iphone-2g/Info.plist.in` (plus
`--bundle-id` and an optional `--icon`), so no existing bundle is needed. The
epoch is written to `Contents/Resources/iphone_files/epoch` and the launcher
appends `,epoch=N` to the machine options when that file is present.

Pass `--stage DIR` to take the iBoot/NOR defaults from a different staging
directory instead of naming them individually.

## Part 3 — Building the iPhone 2G (M68AP) app bundle

The same engine/launcher serve both boards; the bundle differs only in its
**profile**, its **firmware directory**, and one M68AP-specific runtime rule
(a fresh NAND per launch). Verified working: the resulting app boots M68AP to
SpringBoard through its own launcher.

### 1. Create the bundle

Either install the engine into a new bundle with the sanctioned script (it
copies the launcher + helpers, bundles every Homebrew dylib, rewrites their
paths to `@executable_path/../Frameworks`, and signs):

```bash
scripts/install-ipod-app-engine.sh \
    build-ipod11/qemu-system-arm \
    "/Applications/iPhone 2G.app" \
    iphone-2g
```

…or clone a working `iPod Touch.app` and switch its profile — the launcher is
shared, so only the profile file decides the board:

```bash
cp -R "/Applications/iPod Touch.app" "/Applications/iPhone 2G.app"
printf 'iphone-2g\n' > "/Applications/iPhone 2G.app/Contents/Resources/s5l8900-profile"
```

If you clone, and you replace the QEMU binary with a fresh local build, you
**must** rewrite its dylib references — a repo build links Homebrew absolute
paths (`/opt/homebrew/...`) and will not run on another machine:

```bash
APP="/Applications/iPhone 2G.app"; BIN="$APP/Contents/MacOS/qemu-system-arm"
cp build-ipod11/qemu-system-arm "$BIN"
for dep in $(otool -L "$BIN" | awk '/\/opt\/homebrew/{print $1}'); do
    install_name_tool -change "$dep" \
        "@executable_path/../Frameworks/$(basename "$dep")" "$BIN"
done
codesign --remove-signature "$APP" 2>/dev/null; codesign --force --deep --sign - "$APP"
```

Check with `otool -L "$BIN" | grep -c /opt/homebrew` → must be **0**. (Each
bundled dylib keeps its own Homebrew *install-ID* on line 2 of `otool -L`; that
is cosmetic and also true of the shipped iPod bundle. What matters is that
nothing *depends* on a Homebrew path.)

### 2. Install the M68AP firmware

```bash
python3 scripts/install-iphone-firmware.py --app "/Applications/iPhone 2G.app"
```

This fills `Contents/Resources/iphone_files/` with the names the launcher
expects: `bootrom_s5l8900`, `iboot_204_m68ap.bin`, `nor_m68ap.bin`, `nand/`.

**Important:** the iBoot the launcher loads must be the **secure-boot-patched**
build, installed under the plain name:

```bash
cp m68ap-artifacts/stage/iboot_204_m68ap_sbpatch.bin \
   "/Applications/iPhone 2G.app/Contents/Resources/iphone_files/iboot_204_m68ap.bin"
cp m68ap-artifacts/appdbg/bootrom_s5l8900 \
   "/Applications/iPhone 2G.app/Contents/Resources/iphone_files/bootrom_s5l8900"
```

and the NAND must be a **generated** tree (`build-m68ap-nand.py`, e.g.
`m68ap-artifacts/stage/nand-m68ap-fresh`), not an N45AP dump.

### 2b. One-command packaging (recommended)

Both bundles have an end-to-end packaging script. They are the supported path;
the manual steps below remain for understanding and for repair work.

```bash
scripts/package-ipod-app.sh   --build          # /Applications/iPod Touch.app
scripts/package-iphone-app.sh --build          # /Applications/iPhone 2G.app
scripts/package-iphone-app.sh --verify-only    # audit an installed bundle
```

`package-iphone-app.sh` also GENERATES the guest image it installs, via
`scripts/build-m68ap-homescreen-nand.py` — the lockdownd activation patch,
`LK_ENABLE_MBX2D=0`, and the reference-shaped data ark, i.e. the exact
combination measured to reach the home screen. Packaging can therefore not
drift from the verified configuration.

**Ship a PACKED NAND — this is a correctness requirement, not an optimisation.**
The launcher clones the M68AP NAND on every launch (the kernel needs a clean
one). A sparse tree is ~148 000 page files, and cloning it takes minutes during
which no QEMU window appears — the app just bounces in the Dock and looks hung.
The QEMU NAND model reads pages from `nand.pack` and writes to
`bank<N>/<page>_new.page`, so the bundle ships **`nand.pack` plus empty bank
directories** (the write path `fopen()`s into them and `hw_error()`s if they are
missing). Staging then takes ~0 s. The packaging script does this and its
`--verify-only` audit fails if a bundle ever regains a sparse tree.

**`codesign --remove-signature` must NOT be run before `install_name_tool`.**
A freshly linked binary carries a *linker-signed* ad-hoc signature; stripping it
leaves a hole in `__LINKEDIT` and `install_name_tool` then refuses the file
("link edit information does not fill the __LINKEDIT segment"), which broke
engine installs outright. Rewriting the load commands invalidates the signature
anyway and everything is re-signed at the end.

### 3. Fresh NAND per launch (M68AP only)

The M68AP kernel completes `FTL_Open` only against a **clean** NAND, and the
guest writes to NOR as well, so the bundle's `Resources` copies must stay
pristine. `scripts/ipod-app-launcher.sh` therefore clones the NAND and NOR into
a temp dir for every launch of the `iphone-2g` profile and deletes them on
exit (`cp -Rc` clones on APFS, so this is cheap). Set `S5L8900_STAGE_NAND=0` to
opt out. N45AP keeps the historical in-place behaviour.

If you cloned an older bundle, reinstall the launcher so it carries this rule:

```bash
cp scripts/ipod-app-launcher.sh "/Applications/iPhone 2G.app/Contents/MacOS/iPod Touch"
codesign --force --deep --sign - "/Applications/iPhone 2G.app"
```

(The launcher filename stays `iPod Touch` — it is the bundle's
`CFBundleExecutable`. Optionally set the display identity:
`CFBundleName`/`CFBundleDisplayName`/`CFBundleIdentifier` via PlistBuddy, then
re-sign.)

### 4. Run and verify

```bash
open "/Applications/iPhone 2G.app"
# or, with serial output for debugging:
S5L8900_DEBUG=1 "/Applications/iPhone 2G.app/Contents/MacOS/iPod Touch" -display none \
    -serial "file:/tmp/i2g.log"
grep -c "SpringBoard\[" /tmp/i2g.log     # >0 == reached SpringBoard
```

### Known limitations

* **The screen is currently black.** M68AP reaches SpringBoard and reports
  `[Activated]`, but never programs a kernel framebuffer base — the open
  render blocker (see `M68AP_RENDER_HANDOFF.md`). The app runs; it does not
  yet display a home screen.
* Activation requires the patched root filesystem + lockdown data ark; a plain
  IPSW-derived NAND boots `[Unactivated]`.
* The bundle is **arm64-only** and **ad-hoc signed**: on another Mac Gatekeeper
  will report an unidentified developer (right-click → Open). It is otherwise
  self-contained — no Homebrew required on the target machine.
