# Browser/WebAssembly iPod touch Emulator Plan

## Status and decision

This document is the implementation plan for running the existing first-generation
iPod touch emulator entirely in a web browser.

The project will produce two deployment flavors from one codebase:

1. **Classroom**: firmware and a prepacked NAND image are served with the web
   application. This is the primary, fastest path for supervised private use.
2. **Source-loaded**: the application downloads the original firmware/NAND
   artifacts automatically from configured source URLs, verifies them, converts
   them locally when necessary, and caches the prepared result in the browser.

Both flavors execute QEMU and the guest locally. There is no remote emulator and
no server-side VM. A server is needed only to serve static files with the HTTP
headers required for threaded WebAssembly.

The browser port must be based on a modern QEMU tree with Emscripten host support.
The iPod device model should be forward-ported to that tree. Backporting the Wasm
runtime and TCG changes into this QEMU 6.2.50 fork is not the planned approach.

## Goals

- Boot the same N45AP/iPod1,1 software stack as the native emulator.
- Reach a usable SpringBoard with display, touch, Home, Power, sleep, and wake.
- Run all guest code on the student's computer inside the browser sandbox.
- Ship a fast classroom deployment that needs no student interaction before boot.
- Ship a source-loaded deployment that automatically obtains its configured
  artifacts and caches them after the first run.
- Use one frontend, one QEMU/Wasm build, one asset schema, and one test suite for
  both deployment flavors.
- Preserve a working native build as a reference during the forward port.
- Make asset identity, integrity, cache state, and emulator version visible and
  diagnosable.
- Allow the prepared application to work offline after its assets have been
  cached, subject to browser storage retention.

## Non-goals for the first release

- Reimplementing missing Wi-Fi, audio, Bluetooth, USB host integration, or other
  hardware that the native emulator does not currently provide.
- Running QEMU on a server and streaming its display.
- Supporting arbitrary Apple devices, IPSW versions, or NAND layouts.
- Making a single giant HTML file containing the app, Wasm module, and firmware.
- Providing a general QEMU command-line interface to students.
- Mutating the immutable base NAND. Guest writes go to a separate overlay.
- Depending on a public asset URL remaining available forever without a fallback.

## Current repository baseline

The existing machine is a good browser target:

- It is a single-core ARM1176 machine.
- It allocates 128 MiB of guest RAM in `hw/arm/ipod_touch.c`.
- The display is 320 by 480 pixels at 32 bits per pixel.
- The LCD refresh timer currently runs at 10 Hz.
- Touch is already represented as normalized absolute pointer input.
- The iPod-specific implementation is contained in roughly 43 files and 7,400
  lines under `hw/arm`, `hw/i2c`, and `include/hw`.

The present NAND representation is the main browser-specific obstacle:

Before sharing this backend with a browser port, the native cleanup audit in
`SLEEP_WAKE_INVESTIGATION.md` Phase 17 must be resolved or explicitly carried
forward. In particular, the current synchronous page-per-file reads and
`*_new.page` writes are both a native performance issue and an incomplete
persistence contract. Browser work must not preserve that behavior merely for
source compatibility: the packed immutable base plus copy-on-write overlay
below is also the intended clean semantic boundary for a future native
backend.

- Approximately 133,000 individual `.page` files are used.
- Each page contains 2,048 data bytes and 64 spare bytes.
- The unpacked tree consumes roughly 521 MiB because of per-file allocation.
- The actual page payload is roughly 269 MiB.
- The existing compressed archive is roughly 162 MiB.
- The current device opens individual page files synchronously during emulation.

The browser build must not reproduce that directory tree in MEMFS or IndexedDB.

## High-level architecture

```text
Browser page
  |-- iPod canvas and visual shell
  |-- touch, Home, Power, reset, and fullscreen controls
  |-- download/conversion progress and diagnostics
  |-- service worker and application cache
  `-- asset source selection determined at build time
          |
          v
Dedicated worker
  |-- QEMU compiled to WebAssembly
  |-- ARM TCI plus hybrid Wasm JIT when available
  |-- N45AP machine and S5L8900 device models
  |-- QEMU display-to-canvas bridge
  |-- browser-input-to-QEMU bridge
  `-- packed NAND base plus copy-on-write overlay
          |
          v
Browser storage
  |-- verified source artifacts
  |-- prepared/packed NAND cache
  |-- guest write overlay
  `-- cache metadata and schema versions
```

The main UI thread must remain responsive. QEMU, asset conversion, decompression,
hashing, and NAND preparation run in workers.

## One application, two deployment flavors

The two flavors are selected by a build-time deployment manifest. Runtime code
must not contain classroom-only branches beyond interpreting that manifest.

### Classroom flavor

The classroom output contains all required prepared assets on the same origin:

```text
dist/classroom/
  index.html
  manifest.webmanifest
  service-worker.js
  emulator.js
  emulator.wasm
  emulator.worker.js
  asset-manifest.json
  assets/
    n45ap-v1/
      bootrom_s5l8900
      iboot_204_n45ap.bin
      nor_n45ap.bin
      nand.pack
```

The firmware is **bundled with the deployment**, but it is not linked into
`emulator.wasm` or encoded into JavaScript. Separate files provide:

- streaming and normal browser caching;
- independent asset replacement;
- content-addressed filenames and immutable cache headers;
- smaller QEMU rebuilds;
- easier integrity verification;
- lower peak memory than embedding large blobs in JavaScript.

The classroom app automatically loads the local manifest and boots. Students do
not select files or source URLs. The deployment should be hosted on a classroom
LAN or a nearby institutional server when possible. A service worker may precache
the application shell, but large firmware data should be fetched and cached with
explicit progress reporting rather than silently delaying service-worker install.

### Source-loaded flavor

The source-loaded output contains the emulator and a manifest of known artifact
URLs. It performs this first-run flow:

1. Fetch the configured source manifest.
2. Check whether a complete prepared asset set with matching hashes already
   exists locally.
3. Download missing original artifacts from the configured URLs.
4. Verify byte length and SHA-256 before accepting any artifact.
5. Convert the source NAND archive to the browser-native pack format locally.
6. Save verified source assets or the prepared pack, according to the storage
   strategy selected after profiling.
7. Start QEMU only after the complete asset set has been committed atomically.
8. On later visits, validate cache metadata and boot without downloading again.

The default source set may point at the N45AP release already named in `BUILD.md`,
but every URL must pass a browser deployment preflight. A URL being downloadable
in a desktop browser does not prove that `fetch()` can use it from a
cross-origin-isolated web application.

The source-loaded flavor retains a manual import fallback for unavailable URLs,
CORS failures, rate limits, source removal, or offline recovery. Manual import is
not the primary experience.

## Asset manifest

Both builds consume the same versioned schema. A representative classroom
manifest is:

```json
{
  "schemaVersion": 1,
  "assetSet": "n45ap-v1",
  "board": "N45AP",
  "productType": "iPod1,1",
  "delivery": "bundled",
  "assets": {
    "bootrom": {
      "url": "./assets/n45ap-v1/bootrom_s5l8900",
      "size": 65536,
      "sha256": "REQUIRED_AT_BUILD_TIME",
      "format": "raw"
    },
    "iboot": {
      "url": "./assets/n45ap-v1/iboot_204_n45ap.bin",
      "size": 139264,
      "sha256": "REQUIRED_AT_BUILD_TIME",
      "format": "raw"
    },
    "nor": {
      "url": "./assets/n45ap-v1/nor_n45ap.bin",
      "size": 1048576,
      "sha256": "REQUIRED_AT_BUILD_TIME",
      "format": "raw"
    },
    "nand": {
      "url": "./assets/n45ap-v1/nand.pack",
      "size": "REQUIRED_AT_BUILD_TIME",
      "sha256": "REQUIRED_AT_BUILD_TIME",
      "format": "ipod-nand-pack-v1"
    }
  }
}
```

The source-loaded manifest uses the same asset keys but may describe source
archives and their conversion:

```json
{
  "schemaVersion": 1,
  "assetSet": "n45ap-v1-source",
  "board": "N45AP",
  "productType": "iPod1,1",
  "delivery": "remote-source",
  "assets": {
    "bootrom": {
      "url": "https://configured-source.example/bootrom_s5l8900",
      "size": 65536,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "iboot": {
      "url": "https://configured-source.example/iboot_204_n45ap.bin",
      "size": 139264,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "nor": {
      "url": "https://configured-source.example/nor_n45ap.bin",
      "size": 1048576,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "nand": {
      "url": "https://configured-source.example/nand_n45ap.zip",
      "size": "REQUIRED",
      "sha256": "REQUIRED",
      "format": "n45ap-page-tree-zip",
      "convertTo": "ipod-nand-pack-v1"
    }
  }
}
```

Manifest rules:

- Placeholder hashes or sizes are fatal in release builds.
- All accepted asset sets are allowlisted by board, product type, and digest.
- Redirects may be followed only over HTTPS and the final origin is recorded.
- Hash verification occurs on bytes, not filenames or HTTP metadata.
- Cache keys include asset-set ID, digest, pack format, converter version, and
  emulator storage ABI.
- Updating any input creates a new cache namespace.
- The UI displays the active asset-set ID and short digests in diagnostics.

## Asset loading state machine

The loader should expose an explicit state machine so failures are actionable:

```text
idle
  -> checking-capabilities
  -> checking-cache
  -> downloading
  -> verifying
  -> converting
  -> committing-cache
  -> loading-qemu
  -> booting
  -> running
```

Every transition reports progress and can fail with a stable error code. Useful
codes include:

- `BROWSER_UNSUPPORTED`
- `CROSS_ORIGIN_ISOLATION_MISSING`
- `REMOTE_CORS_BLOCKED`
- `REMOTE_CORP_BLOCKED`
- `REMOTE_NOT_FOUND`
- `REMOTE_RATE_LIMITED`
- `ASSET_SIZE_MISMATCH`
- `ASSET_HASH_MISMATCH`
- `ARCHIVE_LAYOUT_INVALID`
- `NAND_CONVERSION_FAILED`
- `STORAGE_QUOTA_DENIED`
- `CACHE_COMMIT_FAILED`
- `WASM_INSTANTIATION_FAILED`
- `QEMU_BOOT_TIMEOUT`

Downloads must be abortable and retryable. Partially converted asset sets must
not be considered valid. Commit the cache metadata only after all files and
indexes have been written and verified.

## Packed NAND design

### Requirements

The browser-native NAND representation must:

- preserve all 2,048-byte page payloads and 64-byte spare areas exactly;
- represent missing pages without allocating empty records;
- provide lookup by bank and page number;
- avoid one browser file/database record per NAND page;
- support an immutable base plus a writable overlay;
- be deterministic, so identical input creates an identical pack digest;
- be buildable by a native command-line tool and by a browser conversion worker;
- reject duplicate, truncated, malformed, and out-of-range pages;
- allow future chunking or compression without changing QEMU's NAND semantics.

### Proposed `ipod-nand-pack-v1`

The first format consists of a small header, a fixed-width sorted index, and a
payload region:

- Magic and format version
- Page data size: 2,048
- Spare data size: 64
- Bank count: 8
- Record count
- Index offset and payload offset
- Source asset digest
- Converter version
- One 16-byte index entry per present page
- One 2,112-byte payload per present page

An index entry contains:

- bank number;
- flags/reserved fields;
- page number;
- payload offset.

Entries are sorted by `(bank, page)` and duplicate keys are forbidden. Integer
serialization is explicitly little-endian. The final exact header layout must be
captured in a format specification and golden test vectors before the converter
is considered stable.

The initial implementation should favor a simple uncompressed, memory-mappable
or Blob-backed pack. Compression and HTTP range loading add complexity to a
synchronous MMIO path and should be introduced only after measuring the MVP.

Three loading strategies must be benchmarked:

1. Mount the downloaded pack as a read-only worker Blob without copying it into
   the Wasm heap.
2. Store large pack chunks in browser storage and maintain a bounded page/chunk
   cache in the worker.
3. Load the complete logical pack into Wasm memory.

Strategy 3 is the simplest but has the highest memory cost. The chosen release
strategy must work on representative student hardware without relying on the
maximum theoretical WebAssembly memory size.

### Copy-on-write overlay

Guest writes never modify the base pack. The overlay is keyed by:

```text
(basePackSHA256, bank, page)
```

Each overlay record stores the complete 2,112-byte page, a sequence number, and
an integrity/version field. Reads check the in-memory overlay cache first and
fall back to the base pack.

Overlay requirements:

- batch persistent writes rather than synchronizing each 32-bit NAND FIFO write;
- flush complete pages after QEMU finishes a program operation;
- flush pending changes on pause, reset, page hide, and explicit save;
- tolerate abrupt tab termination without corrupting the last committed state;
- provide `Reset device data`, which deletes only the matching overlay;
- provide optional overlay export/import for classroom support;
- never attach an overlay to a base pack with a different digest.

The existing native behavior that writes `*_new.page` files should not define the
browser persistence contract. The browser overlay gets explicit read-after-write
and restart persistence tests.

## QEMU and WebAssembly strategy

### Base selection

Before implementation begins, pin a specific upstream QEMU commit that:

- builds a 32-bit guest system emulator with Emscripten;
- contains the upstream 32-bit TCI host support introduced in QEMU 10.1 or later;
- can accept the current hybrid WebAssembly TCG/JIT patch set if that patch set
  is not yet upstream;
- has a reproducible Emscripten SDK and dependency container.

Record the QEMU commit, Wasm TCG patch revision, Emscripten version, dependency
digests, and container image digest in the repository. Do not follow moving
branches in release builds.

### Forward-port sequence

1. Preserve a scripted native baseline from the current branch.
2. Create a clean branch based on the selected modern QEMU commit.
3. Port the iPod Kconfig and Meson entries.
4. Port the S5L8900 machine, CPU setup, memory map, and interrupt controllers.
5. Port bootrom, iBoot, NOR, and NAND loading.
6. Port timers, clock, GPIO, I2C, SPI, PMU, LCD, and multitouch.
7. Port AES, SHA, DMA/ADM, USB, SDIO, TV-out, chip ID, and remaining stubs.
8. Resolve modern QEMU reset, input, block, display, and ARM CPU APIs.
9. Build and boot the forward-ported machine natively first.
10. Compare native serial output, first frame, touch, Home, Power, sleep, and wake
    against the preserved baseline.
11. Only then build the same machine for Emscripten.

Small, subsystem-oriented commits are required. Avoid one commit that combines
the QEMU forward port, browser bridge, NAND redesign, and frontend.

### CPU execution and performance

Pure TCI is the correctness fallback. The hybrid Wasm TCG/JIT is the expected
performance path: cold translation blocks are interpreted and hot blocks are
compiled as WebAssembly modules through browser APIs.

The first performance gate compares:

- current native QEMU 6.2.50;
- forward-ported native QEMU;
- browser pure TCI;
- browser hybrid Wasm JIT.

Measure time to iBoot output, Apple logo, first SpringBoard frame, and usable
input. Also record host CPU utilization, Wasm heap high-water mark, total browser
memory, translation-block compilation count, and long-task duration.

## Display bridge

The production frontend should use a small QEMU display listener rather than
depending on QEMU's complete SDL desktop UI.

The bridge will:

- receive display surface creation/resizing events;
- receive dirty rectangle updates;
- expose the current 320 by 480 32-bit surface to the browser worker;
- convert channel order only when required;
- update an `OffscreenCanvas` in the worker when supported;
- fall back to transferring dirty rectangles to the main thread;
- preserve nearest-neighbor scaling and correct portrait aspect ratio;
- render a black frame when the emulated panel is off;
- avoid allocating a new full-frame buffer for every refresh.

At 320 by 480, one complete 32-bit frame is 614,400 bytes. Even a full-frame
10 Hz fallback is manageable, but dirty rectangles and reusable buffers remain
the design target.

The first canvas implementation may use `ImageData`/`putImageData`. WebGL or
WebGPU is not required unless profiling shows that canvas upload is significant.

## Input bridge

The frontend must support:

- pointer down, move, and release;
- mouse and touch/pointer events through the same code path;
- Home button press/release;
- Power button press/release;
- existing H and P keyboard mappings;
- loss-of-focus cleanup so buttons and touch cannot remain stuck;
- fullscreen and responsive scaling without changing guest coordinates.

Browser coordinates are transformed into normalized portrait coordinates before
they enter QEMU. Pointer capture is used during drags so release events are not
lost outside the canvas.

The frontend initially exposes one touch contact because the current emulator
models a single pointer interaction. Multi-contact browser input is a later
device-emulation feature, not a browser-port requirement.

## Worker, threads, and HTTP deployment

The expected Emscripten build uses pthreads and runs QEMU away from the browser
UI thread. Threaded WebAssembly normally requires `SharedArrayBuffer` and a
cross-origin-isolated page.

Every production response must be tested with at least:

```http
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Embedder-Policy: require-corp
```

Same-origin classroom assets should also use deliberate resource and cache
headers, for example:

```http
Cross-Origin-Resource-Policy: same-origin
Content-Type: application/wasm
Cache-Control: public, max-age=31536000, immutable
```

Only content-addressed assets receive immutable caching. `index.html`, deployment
manifests, and update metadata use short-lived or revalidated caching.

Remote source URLs must be preflighted for:

- HTTPS;
- successful redirects;
- CORS access from the public app origin;
- a `Cross-Origin-Resource-Policy` compatible with COEP, or a CORS response that
  satisfies the browser's embedding rules;
- content length behavior;
- optional HTTP range support;
- rate limits and download stability.

If a source fails preflight, the supported alternatives are:

1. use an authorized same-origin mirror;
2. provide a same-origin download proxy;
3. ask the user for the source archive through the manual import fallback.

The classroom app must be served over HTTP(S), including on a local network. A
`file://` launch is not supported. A small documented local server is sufficient
for development, but production classroom hosting must set the required headers.

## Browser storage and offline behavior

Use two logical stores:

- **Asset store**: immutable verified inputs and/or prepared NAND packs.
- **Device state store**: mutable NAND overlays and user preferences.

The implementation should evaluate IndexedDB, OPFS, and worker-backed Blob access
against the supported browser matrix. Storage choice must be hidden behind an
interface so base-pack access and overlay persistence can use different backends.

Required behaviors:

- request persistent storage with `navigator.storage.persist()` when available;
- show estimated required and available quota before large conversion;
- explain that browsers may evict non-persistent storage;
- allow clearing downloaded source assets without clearing device state only when
  the state can still identify its base digest;
- expose storage usage and cache version in diagnostics;
- recover safely from a partially written conversion;
- invalidate prepared data when converter or pack schema versions change.

The service worker caches the application shell. Large firmware and NAND data are
managed by the asset loader so it can report progress, verify hashes, and handle
quota failures explicitly.

## Proposed repository layout

The exact frontend toolchain will be selected during the first spike, but the
target organization is:

```text
web/
  README.md
  package.json
  src/
    app/
    emulator/
    assets/
    storage/
    workers/
  public/
  manifests/
    classroom.template.json
    source.template.json
  tests/
scripts/
  wasm/
    build-toolchain.sh
    build-qemu.sh
    build-web.sh
    check-source-assets.py
    generate-asset-manifest.py
    pack-nand.py
    verify-release.py
containers/
  wasm-builder/
docs/
  browser/
    deployment.md
    nand-pack-v1.md
    troubleshooting.md
```

These files do not exist yet. They are planned outputs, not commands that work in
the current repository.

Planned developer entry points are:

```sh
npm run build:classroom
npm run build:source
npm run test
./scripts/wasm/build-qemu.sh
./scripts/wasm/pack-nand.py --input build/ipod_files/nand --output nand.pack
./scripts/wasm/verify-release.py dist/classroom
```

Build scripts must fail clearly when classroom source assets are absent. Firmware
paths are supplied through ignored local configuration or environment variables;
they are not silently discovered from arbitrary directories.

## Release artifacts

Each release produces:

- versioned application shell;
- pinned QEMU/Wasm artifacts;
- classroom deployment directory when authorized assets are supplied;
- source-loaded deployment directory with no bundled firmware;
- checksums for every published file;
- software bill of materials for toolchain and frontend dependencies;
- source and build instructions required by QEMU's license;
- a compatibility report and performance measurements.

The classroom and source-loaded builds record the same application version and
QEMU commit. Their only intended behavioral difference is asset delivery.

## CI and reproducibility

CI should be split into jobs that do not require private classroom assets and a
controlled release job that does:

### Public CI

- build the modern native QEMU iPod target;
- build the Emscripten target from the pinned container;
- run C/unit tests for the NAND pack and overlay;
- generate and validate synthetic asset packs;
- build the source-loaded frontend;
- run browser tests with synthetic/non-Apple fixtures;
- verify required deployment headers in a local test server;
- ensure no firmware or generated classroom asset appears in tracked files or
  public artifacts.

### Controlled classroom release

- obtain assets from an approved local release input;
- verify the approved digest allowlist;
- prepack the NAND deterministically;
- generate the classroom manifest;
- build the classroom directory;
- run boot smoke tests;
- publish only to the designated private destination;
- record who built it, which approved asset set was used, and all output hashes.

Rebuilding with the same source inputs, QEMU commit, toolchain container, and
manifest must produce identical NAND pack bytes and preferably identical Wasm and
frontend artifacts where the toolchain permits.

## Testing strategy

### Native regression tests

Before the forward port, capture:

- serial output landmarks and timestamps;
- first Apple-logo screenshot;
- first stable SpringBoard screenshot;
- touch on a known icon;
- Home button behavior;
- Power sleep and wake behavior;
- NAND page read/write behavior;
- emulator exit status and diagnostic log.

Run the same sequence after each forward-port milestone.

### NAND tests

- deterministic conversion of a synthetic page tree;
- all bank/page records round-trip byte-for-byte;
- missing pages return the expected erased state;
- duplicate pages are rejected;
- malformed filenames and page lengths are rejected;
- overlay read-after-write works without restart;
- overlay survives browser reload;
- reset deletes the overlay but not the base;
- base digest mismatch prevents overlay attachment;
- abrupt conversion interruption does not create a valid cache entry;
- abrupt write interruption preserves the last committed page state.

### Browser integration tests

- cold classroom load;
- warm classroom load;
- cold source download and conversion;
- warm source-loaded boot with no network;
- missing source URL;
- redirect and CORS failures;
- hash mismatch;
- insufficient storage quota;
- service-worker upgrade;
- pointer drag and release outside canvas;
- Home and Power press/release;
- sleep/wake and black-panel rendering;
- pause/resume after tab visibility changes;
- clear cache and reset device flows.

### Browser matrix

The initial supported target is current desktop Chromium. Firefox and Safari are
release targets only after they pass the same correctness and memory gates.

Test at minimum:

- Chrome/Chromium on macOS, Windows, and Linux;
- Firefox on macOS, Windows, and Linux;
- Safari on a currently supported macOS release;
- representative Intel and Apple Silicon Macs;
- representative institutional Windows laptops;
- a constrained machine with 8 GiB system RAM.

Mobile browsers are exploratory until memory use and background-tab behavior are
proven acceptable.

## Performance and acceptance gates

Record results with browser version, host model, system RAM, power mode, QEMU
commit, and asset digest.

### Proof-of-concept gate

- Emscripten QEMU instantiates successfully.
- iBoot produces recognizable serial output.
- The browser remains responsive during CPU execution.
- Peak memory does not crash the reference 8 GiB test machine.

### MVP gate

- SpringBoard reaches a stable visible frame.
- Touch, Home, and Power behave like the native baseline.
- Display sleep/wake completes repeatedly.
- The classroom build starts without file selection.
- The source-loaded build completes a verified first-run conversion.
- A warm source-loaded run boots with the network disabled.
- Guest writes persist through a full browser restart.

### Classroom release gate

- At least 20 consecutive cold classroom boots complete on reference hardware.
- At least 50 warm boots complete without asset or overlay corruption.
- A class-sized concurrent download test does not saturate the deployment server.
- The local server supplies all required isolation and cache headers.
- Error messages are usable without opening developer tools.
- An instructor can reset one student's state without redeploying the app.
- The authorized artifact set and private distribution destination are recorded.

### Performance targets

Initial targets, subject to measurement during the spike:

- warm application startup begins QEMU within five seconds on reference hardware;
- pointer-to-visible-response latency remains below 100 ms during normal UI use;
- no recurring main-thread task exceeds 50 ms during steady-state emulation;
- browser memory stays safely below practical per-tab limits on an 8 GiB machine;
- hybrid Wasm JIT reaches SpringBoard within three times the forward-ported native
  boot time, or a documented classroom-acceptable absolute time.

If pure TCI is too slow but hybrid JIT meets these gates, hybrid JIT becomes a
release requirement rather than an optional optimization.

## Milestones

### Phase 0: Baseline and spike (1-2 weeks)

- Script the native reference boot and interaction sequence.
- Select and pin modern QEMU, Emscripten, and Wasm TCG revisions.
- Prove a minimal `arm-softmmu` browser build with synthetic firmware.
- Verify worker threads, cross-origin isolation, display bridge feasibility, and
  browser storage quota on reference machines.
- Preflight the intended public source URLs.

Exit condition: no architectural blocker to ARM32 execution, static hosting,
asset fetch, or required memory allocation.

### Phase 1: Forward port (2-4 weeks)

- Port the N45AP machine and devices to modern QEMU.
- Restore native boot parity.
- Keep subsystem commits reviewable.
- Add basic native regression automation.

Exit condition: modern native QEMU reaches SpringBoard and passes touch,
Home/Power, and sleep/wake smoke tests.

### Phase 2: Browser boot and UI (2-3 weeks)

- Build the forward-ported machine with Emscripten.
- Add worker lifecycle and QEMU startup glue.
- Add canvas display and browser input bridges.
- Compare TCI and hybrid JIT performance.

Exit condition: browser SpringBoard is interactive and performance direction is
known.

### Phase 3: NAND and persistence (2-3 weeks)

- Specify `ipod-nand-pack-v1` and add golden fixtures.
- Implement native and browser converters.
- Replace per-page file access with pack lookup.
- Implement the copy-on-write overlay and persistence.
- Select the browser storage/loading strategy from measurements.

Exit condition: a warm browser boot uses the cached pack, and guest writes survive
restart without modifying the base.

### Phase 4: Dual deployments (1-2 weeks)

- Add the deployment manifest schema.
- Build the automatic classroom flavor.
- Build the automatic source-loaded flavor.
- Add download, verification, conversion, caching, and fallback UI.
- Add the service worker and offline warm-boot flow.

Exit condition: both builds pass the same emulator tests and differ only in asset
delivery.

### Phase 5: Hardening and classroom release (2-4 weeks)

- Complete the browser and hardware matrix.
- Optimize memory, boot time, and cache behavior.
- Add instructor troubleshooting and reset/export tools.
- Load-test classroom distribution.
- Finish reproducible release tooling and provenance records.

Exit condition: all classroom release gates pass.

The phases overlap where safe. The total estimate is approximately 6-12 weeks for
an engineer already comfortable with QEMU, C, Emscripten, and browser storage.
Performance or forward-port API changes may extend it.

## Risk register

| Risk | Impact | Mitigation | Go/no-go signal |
| --- | --- | --- | --- |
| Pure TCI is too slow | High | Use hybrid Wasm JIT; profile hot blocks | Hybrid JIT cannot reach acceptable boot/input targets |
| Wasm JIT patch set remains out of tree | Medium | Pin a reviewed revision and isolate it from iPod changes | Patch set cannot be maintained on selected QEMU |
| Forward port changes guest behavior | High | Native baseline, subsystem commits, serial/screenshot tests | Modern native build cannot match current behavior |
| NAND pack consumes too much memory | High | Blob/chunk access, bounded caches, storage benchmarks | Reference 8 GiB machine repeatedly crashes |
| Public source blocks browser fetch | High for source build | Preflight, authorized mirror/proxy, manual fallback | No reliable permitted delivery path exists |
| Browser storage is evicted | Medium | Request persistence, export support, clear UI | Required browsers cannot retain a warm asset set |
| COOP/COEP deployment is misconfigured | High | Header test in CI and deployment verifier | `crossOriginIsolated` is false in production |
| Firmware update reuses wrong state | High | Digest-keyed immutable base and overlay namespace | Overlay can attach across base hashes |
| Classroom network is overloaded | Medium | LAN hosting, pre-cache sessions, load test | Concurrent first loads exceed class setup window |
| Safari/Firefox behavior differs | Medium | Chromium-first MVP, explicit compatibility matrix | Required institutional browser cannot meet gates |
| Private artifacts leak into public CI | High | Separate controlled job, denylist scan, artifact audit | Any firmware digest/file appears in public output |

## Artifact provenance and distribution gate

This plan distinguishes technical delivery from authorization to redistribute
the selected firmware and NAND.

For every asset set, maintain a private provenance record containing:

- source URL or internal source location;
- acquisition date;
- original filename, size, and SHA-256;
- prepared pack digest and converter version;
- intended deployment flavor;
- approved distribution scope;
- person or institutional process authorizing the classroom bundle.

The classroom build pipeline may vendor approved assets into a private deployment.
The source-loaded public build contains URLs and expected hashes but does not
silently publish a second mirror unless that mirror is authorized. Linking to a
known source and downloading from it does not remove the need to check that
source's availability, terms, and browser headers.

This document is an engineering plan, not a determination of rights. A classroom
release is blocked until the project or institution confirms its permitted use
and distribution scope. That check must not be implemented as a vague comment;
it is a recorded release gate.

## Observability and support

The student UI should remain simple, but an instructor diagnostics panel must
show:

- application version;
- QEMU commit and Wasm TCG mode;
- browser and `crossOriginIsolated` status;
- active asset set and short digests;
- asset/cache/overlay sizes;
- storage persistence result;
- current loader state;
- boot elapsed time and last serial lines;
- exportable diagnostic report without firmware bytes.

Logs must avoid dumping guest memory or asset contents. The diagnostics export
contains metadata, timings, stable error codes, and filtered emulator logs.

## Decisions already made

- Forward-port the iPod model to modern QEMU rather than backporting Wasm.
- Produce one application with classroom and source-loaded manifests.
- Make classroom delivery the primary optimized experience.
- Bundle classroom firmware as separate same-origin assets, not inside Wasm.
- Automatically download and cache source assets in the source-loaded build.
- Keep manual import as a fallback.
- Replace the per-page NAND directory with a deterministic packed base.
- Store guest changes in a digest-keyed copy-on-write overlay.
- Run QEMU and conversion work away from the browser main thread.
- Require an HTTP(S) deployment with verified cross-origin-isolation headers.
- Preserve native parity before debugging browser-only failures.

## Open decisions for the spike

- Exact modern QEMU commit and Wasm TCG revision.
- Exact Emscripten and dependency-container versions.
- Frontend build tool and minimum Node.js version.
- Blob, IndexedDB chunk, OPFS, or complete-memory base-pack access.
- Whether independent NAND chunk compression is needed after profiling.
- Whether `OffscreenCanvas` is the default or an optimization.
- Exact supported browser versions and institutional hardware baseline.
- Final public source URLs and their CORS/COEP behavior.
- Authorized private classroom publishing destination.
- Whether classroom devices preload assets before class or fetch on first use.

Each open decision must be closed with a short decision record containing measured
evidence, not preference alone.

## Definition of done

The browser project is complete when:

- one pinned QEMU/Wasm build runs the N45AP emulator in supported browsers;
- modern native QEMU still passes the reference behavior tests;
- the classroom deployment boots automatically from bundled assets;
- the source-loaded deployment downloads, verifies, converts, caches, and boots
  automatically;
- a warm source-loaded deployment boots offline;
- display, touch, Home, Power, sleep, wake, and persistence pass regression tests;
- asset corruption and source failures produce actionable UI errors;
- the packed NAND and overlay formats have specifications and golden tests;
- performance and memory gates pass on representative classroom hardware;
- classroom deployment headers and concurrent delivery are verified;
- public CI and releases contain no unintended private firmware;
- source, licenses, provenance, build instructions, deployment documentation, and
  instructor troubleshooting documentation are complete.

## Technical references

- [QEMU Wasm](https://github.com/ktock/qemu-wasm)
- [QEMU Wasm design and upstreaming status](https://github.com/ktock/qemu-wasm#how-does-it-work)
- [QEMU WebAssembly TCG backend patch series](https://patchew.org/QEMU/cover.1747744132.git.ktokunaga.mail%40gmail.com/)
- [Emscripten pthread support](https://emscripten.org/docs/porting/pthreads.html)
- [Emscripten filesystem API](https://emscripten.org/docs/api_reference/Filesystem-API.html)
- [Emscripten runtime environment](https://emscripten.org/docs/porting/emscripten-runtime-environment.html)
- [Current native build instructions](BUILD.md)
