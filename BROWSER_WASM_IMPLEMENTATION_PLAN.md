# Browser/WebAssembly Emulator Plan

> **Historical provenance:** Carried forward on 2026-07-21 from
> `ipod_touch_1g-qemu6-legacy` at `4221943495`.
>
> **Revised 2026-07-26.** The original text assumed a QEMU 6.2.50 tree with no
> Emscripten support and a NAND stored as ~133,000 individual page files. Both
> assumptions are obsolete: the tree is QEMU 11.0.2 with upstream Emscripten
> support, and the packed NAND exists and ships in the packaged apps. The
> sections below have been rewritten accordingly. Live, dated state lives in
> [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md); this file is the design of
> record.
>
> **Revised 2026-07-27.** Delivery is decided: prepared assets, self-hosted, no
> source-loaded first run. The product is a **version picker** — iPhone OS 1.0,
> 1.0.2, 1.1.1 and 1.1.4 side by side — with **1.0 first**. Asset delivery is
> redesigned around chunking and compression following
> [Infinite Mac](https://infinitemac.org)'s measured approach.

## Status and decision

This document is the implementation plan for running the existing S5L8900
emulator entirely in a web browser.

**Primary target: iPhone 2G (M68AP).** The product is a **version picker**: the
visitor chooses an iPhone OS release, boots it, and can compare releases against
each other. All four 1.x builds reach the SpringBoard home screen on the native
emulator as of 2026-07-26 (`IPHONE_OS_1X_VERSIONS.md`):

| Build | Version | iBoot | Epoch | FIL signature | Native status |
| --- | --- | --- | --- | --- | --- |
| `1A543a` | **1.0** | 159 | 0 | `000C` | home screen — **first browser target** |
| `1C28` | 1.0.2 | 159 | 0 | `000C` | home screen |
| `3A109a` | 1.1.1 | 204 | 2 | `200C` | home screen |
| `4A102` | 1.1.4 | 204 | 3 | `300C` | home screen; the most exercised build |

**1.0 ships first.** It is the museum-accurate target, its bootloader
generation (iBoot-159, plaintext 8900 containers, epoch 0) is shared with 1.0.2,
and its kernel has *no* TVOut swap device — the single hardest fix in the 1.1.4
bring-up has nothing to hook in 1.0. Ordering after that follows bootloader
generation: 1.0 → 1.0.2 (same iBoot), then 1.1.1 → 1.1.4 (iBoot-204).

**iPod touch 1G (N45AP) is secondary** — worth having, scheduled after the
iPhone versions ship. The machine, tooling, and frontend stay board-agnostic and
read the board from the asset manifest, so N45AP costs a staging run, not a code
change.

### Delivery: prepared assets, self-hosted

Decided 2026-07-27. Every version is **prepared offline by this repository's
existing pipeline and served from our own origin** as compressed chunks. The
"source-loaded" flavor described later in this document — download the original
artifacts and convert them in the browser on first run — is **not being built**.
Two reasons:

- The M68AP NAND is *constructed* from a retail IPSW (decrypted root filesystem,
  synthesized Whimory/FTL structures, activation patch, LayerKit setting,
  reference data ark). There is nothing at any URL to download and verify.
  Porting that pipeline to JavaScript would be a project larger than the browser
  port itself.
- Preparing offline is also what lets the assets be *optimized*: deterministic
  packing, chunking, and compression happen once at build time instead of on
  every visitor's machine.

Hosting is our own static origin to start. IPFS or another distribution layer is
an option later; nothing in the design depends on the origin beyond HTTP range
support and the isolation headers. [infinitemac.org](https://infinitemac.org) is
the model for this product shape, and a plausible future host — offering them the
emulator is an option once it is good, not a dependency.

QEMU and the guest execute locally in the visitor's browser. There is no remote
emulator and no server-side VM. The server only serves static files with the
HTTP headers threaded WebAssembly requires.

The browser port must be based on a modern QEMU tree with Emscripten host
support. **This has happened.** The tree is QEMU 11.0.2 (`QEMU_11_PORT.md`), the
machines are forward-ported, and 11.0.2 carries Emscripten host support
upstream: `host_os == 'emscripten'`, `--cpu=wasm64`, `util/coroutine-wasm.c`,
`os-wasm.c`, `configs/meson/emscripten.txt`, and a cross-build container recipe
at `tests/docker/dockerfiles/emsdk-wasm64-cross.docker`. No out-of-tree patch
set is required to build for the browser.

## Goals

- Boot the same M68AP/iPhone1,1 iPhone OS 1.1.4 stack as the native emulator,
  and keep N45AP/iPod1,1 bootable from the same build.
- Reach a usable SpringBoard with display, touch, Home, Power, sleep, and wake.
- Run all guest code on the student's computer inside the browser sandbox.
- Ship a fast classroom deployment that needs no student interaction before boot.
- Ship a source-loaded deployment that automatically obtains its configured
  artifacts and caches them after the first run.
- Use one frontend, one QEMU/Wasm build, one asset schema, and one test suite for
  both deployment flavors.
- Keep the native build (`build-ipod11/`) working as the correctness oracle the
  browser build is measured against.
- Make asset identity, integrity, cache state, and emulator version visible and
  diagnosable.
- Allow the prepared application to work offline after its assets have been
  cached, subject to browser storage retention.

## Non-goals for the first release

- Reimplementing missing Wi-Fi, audio, Bluetooth, USB host integration, or other
  hardware that the native emulator does not currently provide.
- Running QEMU on a server and streaming its display.
- Supporting arbitrary Apple devices, IPSW versions, or NAND layouts. The
  catalog is a fixed, curated set: the four iPhone OS 1.x builds, then N45AP.
- Making a single giant HTML file containing the app, Wasm module, and firmware.
- Providing a general QEMU command-line interface to visitors.
- Mutating the immutable base NAND. Guest writes go to a separate overlay.
- Converting original firmware artifacts in the browser. Preparation happens
  offline, in this repository.
- Depending on a public asset URL remaining available forever without a fallback.

## Current repository baseline

The existing machine is a good browser target:

- It is a single-core ARM1176 machine.
- It allocates 128 MiB of guest RAM in `hw/arm/ipod_touch.c`.
- The display is 320 by 480 pixels at 32 bits per pixel.
- The LCD refresh timer currently runs at 10 Hz.
- Touch is already represented as normalized absolute pointer input.
- One machine file serves both boards: `-M iPhone-2G` and `-M iPod-Touch` differ
  by board ID, and per-board facts (PMU I²C bus, NAND bank count, button pin,
  security epoch, TV-out workaround address) branch inside it.
- The device implementation is contained in roughly 43 files under `hw/arm`,
  `hw/i2c`, and `include/hw`.

### The NAND base pack already exists

The original obstacle here — a NAND stored as ~133,000 individual `.page` files,
opened synchronously during emulation — has been solved natively and does not
need solving again for the browser:

- `scripts/pack-ipod-nand.py` builds a deterministic immutable base pack
  (`IPODNAND` v1: 20-byte header, sorted fixed-width index, 2,112-byte payloads).
- `hw/arm/ipod_touch_nand.c` maps it with `g_mapped_file`, binary-searches the
  index, validates magic/version/size/sort order, and keeps the legacy
  per-page directory as a fallback the browser never ships.
- A three-pair M2 cold-boot benchmark reduced median time to SpringBoard from
  9.292 to 5.687 seconds.
- The packaged apps already ship a single `nand.pack` (~300 MiB for M68AP 1.1.4)
  instead of a page tree, so the browser's asset set is a straight copy.

What remains unimplemented is the **writable half**: the copy-on-write overlay
specified below. Testing proved the old `*_new.page` files are incomplete
program captures — giving them read precedence makes iBoot see an HFS signature
of zero and enter recovery — so browser and native overlays must share a newly
specified program/erase and spare-metadata contract rather than importing those
files.

The browser build must not reproduce the per-page directory tree in MEMFS or
IndexedDB; it consumes the pack directly.

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
  |-- ARM TCI (a hybrid Wasm JIT only if measurement demands it)
  |-- M68AP/N45AP machines and S5L8900 device models
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

## One application, one delivery model

Everything is prepared offline and served from our origin. There is no
first-run conversion, no file picker, and no build-time flavor switch: the app
reads a **catalog** of available versions and boots the one the visitor picks.

The "source-loaded flavor" documented below is **retained as design history and
as the path N45AP could take**, since the iPod artifacts do exist as published
files. It is not being built for the iPhone versions. Read that subsection as an
option, not as scope.

### Prepared deployment (what ships)

The output contains all prepared assets on the same origin:

```text
dist/
  index.html
  manifest.webmanifest
  service-worker.js              # intercepts chunk requests
  catalog.json                   # the versions on offer
  emulator/
    qemu-system-arm.js           # Emscripten ES module loader
    qemu-system-arm.wasm
  assets/
    m68ap-10-v1/                 # iPhone OS 1.0, build 1A543a — ships first
      asset-manifest.json
      bootrom_s5l8900
      iboot.bin
      nor.bin
      nand/
        index.bin                # page index + chunk map
        chunks/<hash>.br         # Brotli-compressed, content-addressed
    m68ap-102-v1/                # 1.0.2
    m68ap-111-v1/                # 1.1.1
    m68ap-114-v1/                # 1.1.4
    n45ap-…                      # iPod touch, later
```

Artifact names inside an asset set are normalized (`iboot.bin`, `nor.bin`)
rather than board- or build-specific, because the manifest already names both;
this keeps the frontend from having to know per-board filenames. The set
directory and layout are produced by `scripts/wasm/stage-assets.py`.

Chunks are content-addressed and therefore shareable **across versions**: an
identical chunk in 1.0 and 1.0.2 is stored and downloaded once. The measured
duplicate rate within a single pack is only ~6%, so treat cross-version sharing
as a bonus to measure, not a saving to promise.

### Version catalog

`catalog.json` is what the picker renders, and the only file the app fetches
before the visitor chooses:

```json
{
  "schemaVersion": 1,
  "default": "m68ap-10-v1",
  "versions": [
    {
      "id": "m68ap-10-v1",
      "device": "iPhone 2G",
      "os": "1.0",
      "build": "1A543a",
      "released": "2007-06-29",
      "manifest": "./assets/m68ap-10-v1/asset-manifest.json",
      "downloadSize": 0,
      "notes": "The original release. No TV-out, TSL2561 light sensor."
    }
  ]
}
```

`downloadSize` is the compressed prefetch working set, not the full pack, so the
picker can tell the truth about what choosing a version costs. Each version is
an independent cache namespace keyed by its digests: switching versions never
invalidates another version's cache, and a re-prepared asset set never collides
with the old one.

The firmware is **bundled with the deployment**, but it is not linked into
`emulator.wasm` or encoded into JavaScript. Separate files provide:

- streaming and normal browser caching;
- independent asset replacement;
- content-addressed filenames and immutable cache headers;
- smaller QEMU rebuilds;
- easier integrity verification;
- lower peak memory than embedding large blobs in JavaScript.

The app loads the catalog, the visitor picks a version, and it boots. Nobody
selects files or source URLs. The service worker precaches the application shell
and serves NAND chunks; large firmware data is fetched with explicit progress
reporting rather than silently delaying service-worker install.

### Source-loaded flavor (design history, not scope)

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

The source-loaded flavor is realistic for N45AP, whose artifacts have a named
release in `BUILD.md`. It is not yet realistic for M68AP, whose NAND is
generated from a retail IPSW rather than downloaded (see above). Every URL must
pass a browser deployment preflight. A URL being downloadable
in a desktop browser does not prove that `fetch()` can use it from a
cross-origin-isolated web application.

The source-loaded flavor retains a manual import fallback for unavailable URLs,
CORS failures, rate limits, source removal, or offline recovery. Manual import is
not the primary experience.

## Asset manifest

Every asset set uses the same versioned schema. This is what
`scripts/wasm/stage-assets.py` writes today, extended with the fields the
version picker and chunked NAND need (`build`, `epoch`, chunked `nand`):

```json
{
  "schemaVersion": 1,
  "assetSet": "m68ap-10-v1",
  "board": "M68AP",
  "machine": "iPhone-2G",
  "productType": "iPhone1,1",
  "description": "iPhone 2G (M68AP), S5L8900, iPhone OS 1.0",
  "firmware": "1.0",
  "build": "1A543a",
  "delivery": "bundled",
  "machineOptions": ["epoch=0"],
  "assets": {
    "bootrom": {
      "url": "./bootrom_s5l8900",
      "size": 65536,
      "sha256": "FILLED_AT_STAGING_TIME",
      "format": "raw"
    },
    "iboot": {
      "url": "./iboot.bin",
      "size": 139264,
      "sha256": "FILLED_AT_STAGING_TIME",
      "format": "raw"
    },
    "nor": {
      "url": "./nor.bin",
      "size": 1048576,
      "sha256": "FILLED_AT_STAGING_TIME",
      "format": "raw"
    },
    "nand": {
      "url": "./nand/index.bin",
      "chunkUrl": "./nand/chunks/",
      "size": 314886212,
      "compressedSize": 0,
      "prefetchSize": 0,
      "sha256": "FILLED_AT_STAGING_TIME",
      "format": "ipod-nand-chunks-v1",
      "pagesPerChunk": 62,
      "compression": "br",
      "prefetch": ["FIRST_BOOT_CHUNK_HASHES_IN_ACCESS_ORDER"]
    }
  }
}
```

`machine` and `machineOptions` exist so the frontend builds QEMU's argv from the
manifest rather than carrying board knowledge in JavaScript: an asset set is
self-describing, and adding a version or the N45AP set requires no frontend
change. The security epoch is a per-*firmware* value (0 for 1.0/1.0.2, 2 for
1.1.1, 3 for 1.1.4), which is exactly why it belongs in `machineOptions` and not
in a board table — `-M iPhone-2G,epoch=N` already exists for this.

Asset URLs are relative to the manifest, so a set can be moved or mirrored
whole. `prefetch` is the recorded cold-boot chunk order; `compressedSize` lets
the picker state the real download cost.

The source-loaded manifest uses the same asset keys but may describe source
archives and their conversion:

```json
{
  "schemaVersion": 1,
  "assetSet": "m68ap-114-v1-source",
  "board": "M68AP",
  "machine": "iPhone-2G",
  "productType": "iPhone1,1",
  "firmware": "1.1.4",
  "delivery": "remote-source",
  "machineOptions": [],
  "assets": {
    "bootrom": {
      "url": "https://configured-source.example/bootrom_s5l8900",
      "size": 65536,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "iboot": {
      "url": "https://configured-source.example/iboot_204_m68ap.bin",
      "size": 139264,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "nor": {
      "url": "https://configured-source.example/nor_m68ap.bin",
      "size": 1048576,
      "sha256": "REQUIRED",
      "format": "raw"
    },
    "nand": {
      "url": "https://configured-source.example/nand_m68ap.zip",
      "size": "REQUIRED",
      "sha256": "REQUIRED",
      "format": "page-tree-zip",
      "convertTo": "ipod-nand-pack-v1"
    }
  }
}
```

The M68AP NAND has no public single-file source: it is *constructed* from a
retail IPSW by `scripts/build-m68ap-nand.py` and friends (a decrypted root
filesystem laid into a generated FTL/VFL layout). A source-loaded M68AP flavor
therefore needs either a prepared pack mirror or an in-browser port of that
generator — a materially larger job than unzipping a page tree, and the reason
the classroom flavor is the primary path for this board.

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

### `ipod-nand-pack-v1`, as built

The format is implemented and in production use natively. The description below
is the as-built layout, not a proposal: the writer is
`scripts/pack-ipod-nand.py` and the reader is `nand_open_pack()` /
`nand_read_packed_page()` in `hw/arm/ipod_touch_nand.c`.

All integers are little-endian.

```text
offset  size          field
0       8             magic "IPODNAND"
8       4             version = 1
12      4             page stride = 2112 (2048 data + 64 spare)
16      4             record count N
20      4 * N         index: sorted u32 virtual page numbers
20+4N   2112 * N      payloads, in index order
```

The index is a bare sorted array of keys, not a table of offsets: because every
payload is the same 2,112 bytes, a record's payload address is
`payload_base + slot * 2112`, so the slot found by binary search *is* the
offset. That keeps the index at 4 bytes per page instead of 16.

The key is a single virtual page number, `page * bank_count + bank`, which
orders pages by page-then-bank and lets one `uint32` express the `(bank, page)`
pair. The reader validates magic, version, stride, total length, and strictly
increasing keys, which rejects truncated, malformed, and duplicate-keyed packs
on open. Pages absent from the index read back as erased.

Two known deviations from the original proposal, both deliberate:

- **The bank count is not in the header.** Both sides use a fixed 8
  (`NAND_NUM_BANKS`, and `NUM_BANKS` in the packer) to compute the key, which is
  why the key stays valid on M68AP even though that board's device only
  addresses 4 banks. Writer and reader therefore agree by convention rather than
  by declaration; putting the count in the header is a candidate for v2.
- **No source digest or converter version is embedded.** Provenance lives
  outside the pack, in the asset manifest and in `nand-provenance.json`.

Golden test vectors for the pack are still owed.

## Chunked, compressed asset delivery

A whole-pack download is not the shipping design. With four versions on offer,
naive delivery is 4 × 315 MiB before anyone has picked one, and a visitor who
wants to compare 1.0 against 1.1.4 pays twice.

### What the pack actually looks like (measured 2026-07-27)

The shipping M68AP 1.1.4 pack, via `scripts/wasm/measure-pack.py`:

| | |
| --- | --- |
| total size | 300.3 MiB (314,886,212 B) |
| pages present | 148,812 × 2,112 B |
| index / payload split | 581 KiB / 299.7 MiB |
| chunks at 124 pages | 1,201 |
| **brotli** (q11, per chunk) | **31.4%** → 94.2 MiB |
| lzma | 30.9% → 92.9 MiB |
| zlib -9 | 36.5% → 109.5 MiB |
| uniform (all-one-byte) chunks | 0 of 150 sampled |
| **duplicate chunks (full scan)** | **197 of 1,201 = 16.4%**, one chunk repeating 198× |

Compression is worth roughly a **3× reduction**: a version drops from 300 MiB to
~94 MiB. Content-addressed deduplication is worth another ~16% *within* a single
version, and its cross-version value is still to be measured
(`measure-pack.py --cross`).

### What a cold boot actually touches (measured 2026-07-27)

This is the number the whole design turns on, and it is now measured rather than
assumed. `IT_NAND_TRACE_PAGES` records every page fetch (see
`hw/arm/ipod_touch_nand.c`); `scripts/wasm/analyze-nand-trace.py` reduces the
trace against the pack. The run booted M68AP 1.1.4 to a verified home screen
(kernel framebuffer 73.95% non-black at 420 s):

| | |
| --- | --- |
| page fetches | 31,384 (2,426 of them for pages absent from the pack) |
| **distinct pages touched** | **25,030 = 16.8% of the pack** |
| distinct chunks touched (124 pages) | 351 of 1,201 = 29.2% |
| **first-boot download, chunked + brotli** | **24.3 MiB** |
| first-boot download, whole pack + brotli | 94.2 MiB |
| first-boot download, whole pack raw | 300.3 MiB |

**Lazy loading wins decisively.** A cold boot needs a sixth of the pack's pages,
and chunked delivery turns a 94 MiB download into **24 MiB** — about 4× better
than a compressed whole-pack download, and 12× better than the raw pack. Four
versions of the catalog cost less to try than one version costs today.

Chunk size trades read amplification against per-chunk compression:

| pages/chunk | chunk size | chunks touched | first-boot download |
| --- | --- | --- | --- |
| 124 | 256 KiB | 29.2% | 24.3 MiB |
| 62 | 128 KiB | 24.9% | 20.9 MiB |
| 32 | 66 KiB | 21.8% | 18.5 MiB |
| 16 | 33 KiB | 19.7% | 17.0 MiB |

Smaller chunks fetch less waste (the touched pages are scattered, so a big chunk
drags in neighbours nobody reads) but cost more requests and compress slightly
worse. Going from 124 to 16 pages saves 7.3 MiB while multiplying request count
by 5. **62 pages (128 KiB) is the recommended starting point** — most of the
saving, half the requests of the aggressive option — to be confirmed against
real network latency, since these numbers say nothing about round trips.

### Design, following Infinite Mac

[Infinite Mac](https://infinitemac.org) solved this problem for classic Mac disk
images and its approach transfers almost unchanged. Its measured result — boot
screen in one second, fully booted in three, with a cold HTTP cache — is the bar.

- **Fixed-size content-addressed chunks.** Infinite Mac uses 256 KiB. Our page
  stride is 2,112 bytes, which does not divide evenly into any power of two, so
  a chunk is defined as a **fixed page count** rather than a fixed byte count. A
  page then maps to a chunk by index arithmetic alone, with no lookup table.
  **62 pages (130,944 B ≈ 128 KiB) is the recommended size**, chosen from the
  measured chunk-size table above rather than inherited from Infinite Mac.
- **Each chunk compressed individually.** This is the specific reason to chunk
  manually instead of using HTTP range requests: range requests and
  `Content-Encoding` interact badly in practice, whereas a pre-compressed chunk
  is a plain immutable object. Brotli at maximum quality, computed once at
  build time.
- **Lazy loading with a service worker.** The emulator worker's NAND read path
  stays synchronous; the service worker intercepts the request and serves the
  chunk from cache or network. This is what keeps QEMU's synchronous MMIO path
  intact without threading async through the device model.
- **Prefetch the boot working set.** The chunks touched during a cold boot are
  known — they can be recorded from a native run — and shipped as an ordered
  prefetch list in the manifest, so startup is not serialized on demand-faults.
- **Only touched chunks occupy memory.** Infinite Mac's move from whole-image
  buffering to per-chunk residency dropped its out-of-memory rate from 6.5% to
  0.3%. Ours is the same shape of problem: a 315 MB pack that a boot only
  partially reads.

This is no longer speculative: the measurement above was taken, and it says
first boot moves ~24 MiB rather than ~94 MiB. Repeat it per version — 1.0 in
particular, since it is the first to ship — with:

```sh
IT_NAND_TRACE_PAGES=/tmp/boot.trace IT_NAND_WRITABLE=1 \
    python3 scripts/fb-snapshot.py --board m68ap --boot-wait 420 \
    --nand-m68ap <throwaway clone> ... --logs /tmp/fb
scripts/wasm/analyze-nand-trace.py /tmp/boot.trace <nand.pack> \
    --pages-per-chunk 62 --prefetch prefetch.json
```

The trace must come from a boot that reached a **verified** home screen, not
merely a boot that ran for a while: `fb-snapshot.py` reports the kernel
framebuffer's non-black percentage, and a boot that stalled early would report
a smaller, wrong working set. Note also that the NAND must be a throwaway clone
with `IT_NAND_WRITABLE=1`; a read-only NAND stalls before SpringBoard by design,
because daemons that must create state spin forever.

### What this requires from the device model

`hw/arm/ipod_touch_nand.c` currently maps the pack with `g_mapped_file` and
indexes straight into it. Chunked delivery needs a **seam**: a page-read
function that can resolve a page from a chunk cache instead of from a mapped
range. Native builds keep the mapped-file implementation; the browser build
supplies a chunk-backed one. This is a small, well-scoped change and should land
with tests before any frontend work depends on it.

### Fallback

If chunking proves troublesome, a single Brotli-compressed pack served with
`Content-Encoding: br` still gets the ~3× size reduction, at the cost of
downloading a whole version before it boots. That is the safety net, not the
target.

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

### Base selection: settled

The base is this tree: **QEMU 11.0.2**, branch `ipod_touch_1g`. The forward port
from 6.2.50 is complete and the machines boot natively (`QEMU_11_PORT.md`,
`build-ipod11/`). Nothing needs pinning to a different upstream commit.

The toolchain is pinned in `scripts/wasm/toolchain.env`: Emscripten 4.0.10,
glib 2.84.0, pixman 0.44.2, libffi 3.5.2, zlib 1.3.1 — the same versions QEMU's
own `emsdk-wasm64-cross` container uses. Changing a value there is a toolchain
change that must be re-measured.

### Build configuration

```sh
configure --static --cpu=wasm64 --wasm64-32bit-address-limit \
          --target-list=arm-softmmu --enable-tcg-interpreter \
          --disable-tools --disable-docs
```

- `--cpu=wasm64` is what QEMU 11.0.2 supports for Emscripten; `MEMORY64` is
  required by the port, not chosen by us.
- `--wasm64-32bit-address-limit` keeps the address space at 32 bits, which the
  128 MiB guest never approaches and which is kinder to browser memory limits.
- `--enable-tcg-interpreter` is mandatory: `meson.build` errors out on a
  WebAssembly host without it.

**Docker is not required.** The upstream container exists to pin the SDK and
cross-compile four static dependencies, all of which build natively on macOS.
`scripts/wasm/setup-toolchain.sh` + `build-deps.sh` are the default path;
`build-toolchain.sh` keeps the container for reproducible/CI builds. Two
host-specific corrections the container never needs are recorded in
`BROWSER_WASM_STATUS.md`.

### CPU execution and performance

**QEMU 11.0.2 has no in-tree WebAssembly TCG backend** — `tcg/` contains no
wasm target, and the emscripten host path routes through TCI. So TCI is not a
"correctness fallback" here; it is the only thing this base can do, and the
first measurement.

### What "the JIT" actually is

The JIT is [qemu-wasm](https://github.com/ktock/qemu-wasm), Kohei Tokunaga's
QEMU fork, and specifically its WebAssembly **TCG backend**. Its design:

- Each translation block is compiled into **one WebAssembly module**; a TCG IR
  instruction becomes the corresponding Wasm instruction(s). Generated modules
  are instantiated and run through the browser's own `WebAssembly.Module` /
  `WebAssembly.Instance` APIs — the browser's Wasm engine is the JIT's backend.
- It is **hybrid, not pure JIT**. A forked TCI interprets every block by
  default, and only blocks executed many times (the threshold cited is ~1000)
  are compiled to Wasm. Two reasons: compilation is expensive, and browsers cap
  how many Wasm instances a page may hold. The code generator emits Wasm *and*
  TCI instructions from the same IR.

**Upstreaming is half-done, and we have the half that landed.** The Emscripten
host support plus TCI for 32-bit guests merged in QEMU 10.1 (August 2025), which
is why 11.0.2 builds for the browser at all. The Wasm backend itself is still
out of tree: the v1 series (`tcg/wasm32`, May 2025) was followed by a v2 series
rebased on wasm64 (August 2025), and **QEMU master still has no `tcg/wasm*`
directory** — checked 2026-07-27. So the split is exactly:

| | in our 11.0.2 | out of tree |
| --- | --- | --- |
| Emscripten host, `--cpu=wasm64` | yes | — |
| TCI for 32-bit guests (our ARM1176) | yes | — |
| Wasm TCG backend (the JIT) | **no** | qemu-wasm |

Two things work in our favour if we do adopt it: our guest is 32-bit ARM, the
case that was upstreamed first and is best exercised, and the v2 backend series
is built on wasm64, which is already how we configure.

If TCI cannot reach an acceptable time to a usable SpringBoard, adopting that
patch set becomes a **requirement**, and carrying an out-of-tree TCG backend on
11.0.2 becomes a scoped project of its own — including the risk that it rebases
onto a QEMU we are not on. Measure before deciding; do not adopt speculatively.
Re-check upstream status before starting: if the backend merges, this stops
being a patch-carrying problem and becomes a version bump.

**External evidence that the JIT path is viable.** Infinite Mac benchmarked
qemu-wasm against the two hand-ported PowerPC emulators it already ships: an
MD5 checksum over 100 MB completed in **8 seconds under qemu-wasm, versus 13 for
DingusPPC and 12–18 for PearPC**. A general-purpose emulator compiled to
WebAssembly beating purpose-built C ports is a strong signal that a JIT-equipped
QEMU is fast enough for a browser product. It says nothing about *TCI*, which is
the interpreter — so it raises confidence in the fallback plan, not in the
current build. It is also the reason to keep the JIT decision open rather than
treating TCI's result as final.

The first performance gate compares:

- native QEMU 11.0.2 (the `build-ipod11/` reference);
- browser TCI;
- browser hybrid Wasm JIT, only if TCI fails the gate.

Measure time to iBoot output, Apple logo, first SpringBoard frame, and usable
input. Also record host CPU utilization, Wasm heap high-water mark, total browser
memory, and long-task duration.

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

What exists today (2026-07-26):

```text
scripts/
  pack-ipod-nand.py            # the base-pack writer, shared with native
  wasm/
    README.md
    toolchain.env              # every pinned version
    setup-toolchain.sh         # standalone python (if needed) + emsdk + meson
    build-deps.sh              # wasm64 zlib, libffi, pixman, glib
    build-toolchain.sh         # the container alternative
    build-qemu.sh              # arm-softmmu -> build-wasm/
    stage-assets.py            # asset set + hashed asset-manifest.json
    serve.py                   # COOP/COEP dev server, --check
web/
  index.html
  .gitignore                   # public/assets/ and emulator/ are never committed
  src/
    app/       main.js, shell.css
    emulator/  loader.js       # fetch, verify, cache
    workers/   emulator-worker.js
```

Still planned, not written:

```text
web/
  src/storage/                 # overlay persistence
  tests/
  manifests/{classroom,source}.template.json
docs/browser/{deployment,nand-pack-v1,troubleshooting}.md
scripts/wasm/{build-web.sh,check-source-assets.py,verify-release.py}
```

Working developer entry points:

```sh
scripts/wasm/setup-toolchain.sh
scripts/wasm/build-deps.sh
scripts/wasm/build-qemu.sh
scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G.app" \
    --board m68ap --firmware 1.1.4
scripts/wasm/serve.py
```

There is no Node.js toolchain and no bundler: the frontend is ES modules served
directly. That stays true until something actually requires a build step.

Build scripts must fail clearly when classroom source assets are absent.
Firmware paths are supplied explicitly (`--from-app` or per-artifact paths);
they are never silently discovered from arbitrary directories.

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

- build the native QEMU target (`build-ipod11/`);
- build the Emscripten target from the pinned container (CI uses the container
  path, not the native toolchain, so builds stay reproducible);
- run C/unit tests for the NAND pack and overlay;
- generate and validate synthetic asset packs;
- build the source-loaded frontend;
- run browser tests with synthetic/non-Apple fixtures;
- verify required deployment headers (`scripts/wasm/serve.py --check`);
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

The forward port is finished, so these are no longer a pre-port baseline; they
are the **oracle the browser build is compared against**, and they are still
owed. Capture from native QEMU 11.0.2 (`build-ipod11/`), per board:

- serial output landmarks and timestamps;
- first Apple-logo screenshot;
- first stable SpringBoard screenshot;
- touch on a known icon;
- Home button behavior;
- Power sleep and wake behavior;
- NAND page read/write behavior;
- emulator exit status and diagnostic log.

Existing tooling covers part of this already — `scripts/fb-snapshot.py` for
framebuffer capture, `scripts/iphone-nand-acceptance.py` for the NAND — so the
work is automation and a stored baseline, not new instrumentation. Run the same
sequence after each browser milestone.

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
- the browser reaches SpringBoard within three times the native QEMU 11.0.2 boot
  time, or a documented classroom-acceptable absolute time.

If TCI cannot meet that and a hybrid JIT can, the JIT becomes a release
requirement rather than an optional optimization.

## Milestones

### Phase 0: Baseline and spike — mostly done

- ~~Select and pin modern QEMU, Emscripten, and Wasm TCG revisions.~~ Done:
  QEMU 11.0.2 in-tree, Emscripten 4.0.10 and the dependency set pinned in
  `scripts/wasm/toolchain.env`. No Wasm TCG revision to pin — there is none in
  this base.
- ~~Verify cross-origin isolation.~~ Done: `scripts/wasm/serve.py --check`.
- Prove a minimal `arm-softmmu` browser build. **In progress** — the toolchain
  and three of four dependencies build; see `BROWSER_WASM_STATUS.md`.
- Script the native reference boot and interaction sequence.
- Verify worker threads, display bridge feasibility, and storage quota on
  reference machines.

Exit condition: no architectural blocker to ARM32 execution, static hosting,
asset fetch, or required memory allocation.

### Phase 1: Forward port — done

The QEMU 6.2.50 → 11.0.2 forward port is complete and promoted; both the iPod
touch and iPhone 2G machines boot to their home screens natively. See
`QEMU_11_PORT.md`, `IPHONE_2G_BRINGUP_HANDOFF.md`, and
`M68AP_HOMESCREEN_CASE_STUDY.md`. What this phase still owes the browser port is
**native regression automation** — a scripted boot/serial/screenshot/interaction
baseline to compare browser runs against.

### Phase 2: Browser boot and UI (2-3 weeks)

- Build the machine with Emscripten and get `main()` to run.
- Resolve what the device code does to a browser filesystem: the machine takes
  host paths and uses `fopen`/`g_mapped_file`, and mmap over a ~300 MiB pack in
  MEMFS is the first memory question to measure.
- Add worker lifecycle and QEMU startup glue (skeleton exists, instantiation is
  provisional).
- Add the canvas display bridge — nothing produces frames yet.
- Add the input bridge into QEMU — the frontend emits events, the worker drops
  them.
- Measure TCI, and decide about a JIT on the evidence.

Exit condition: browser SpringBoard is interactive and performance direction is
known.

### Phase 2.5: Package the versions — 1.0 done

- ~~Wire the version axis through the home-screen recipe.~~ Done 2026-07-27:
  one layout (`m68ap-artifacts/builds/<BUILD>/`), one explicit `--build`, no
  default firmware. See [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md).
- ~~Produce and verify a 1.0 asset set end to end.~~ Done: 215.6 MiB pack,
  106,858 pages, signature `000C` and `epoch=0` from the profile, booting to a
  verified home screen (59.04% non-black).
- Repeat for 1.0.2 and 1.1.1: both booted natively during bring-up but neither
  has a staged `root.img` yet, so each needs its IPSW decrypted first.

Exit condition: four asset sets build from one command each.

### Phase 3: NAND delivery and persistence (2-3 weeks)

- ~~Specify `ipod-nand-pack-v1`.~~ Built and in production; documented above.
- ~~Implement the native converter and replace per-page access with pack
  lookup.~~ Done (`scripts/pack-ipod-nand.py`, `hw/arm/ipod_touch_nand.c`).
- **Measure the cold-boot working set** — which chunks a boot to the home screen
  actually touches, per version. Everything below is sized by this number, and
  it is cheap to obtain natively. Do it first.
- Add the page-read seam in `ipod_touch_nand.c` so a chunk cache can back the
  pack.
- Build the chunker: content-addressed, fixed page count, Brotli, deterministic.
- Add the service worker that serves chunks, plus prefetch of the recorded boot
  set.
- Add golden fixtures and round-trip tests for pack and chunks. **Still owed.**
- Implement the copy-on-write overlay and persistence — guest writes currently
  have nowhere to go in the browser.

Exit condition: a warm browser boot uses the cached pack, and guest writes survive
restart without modifying the base.

### Phase 4: The version picker (1-2 weeks)

- Add `catalog.json` and the picker UI: device, OS version, build, release date,
  honest download size.
- Per-version cache namespaces, and a way to see and clear what is stored.
- Offline warm boot for any version already cached.
- Make switching versions cheap enough to actually compare them — that is the
  product, not a convenience.

Exit condition: a visitor can boot 1.0, switch to 1.1.4, and come back to a
warm 1.0 with no network.

### Phase 5: Hardening and classroom release (2-4 weeks)

- Complete the browser and hardware matrix.
- Optimize memory, boot time, and cache behavior.
- Add instructor troubleshooting and reset/export tools.
- Load-test classroom distribution.
- Finish reproducible release tooling and provenance records.

Exit condition: all classroom release gates pass.

The phases overlap where safe. With the forward port and the base pack already
done, the remaining estimate is approximately 4-8 weeks for an engineer
comfortable with QEMU, C, Emscripten, and browser storage. A TCI performance
failure that forces adopting the out-of-tree JIT would extend it substantially.

## Risk register

| Risk | Impact | Mitigation | Go/no-go signal |
| --- | --- | --- | --- |
| TCI is too slow (it is the only engine this base has) | High | Measure first; adopt the out-of-tree hybrid Wasm JIT only if needed | Neither TCI nor JIT reaches acceptable boot/input targets |
| Wasm JIT patch set is out of tree and targets a different QEMU | Medium | Pin a reviewed revision, isolate it from device changes | Patch set cannot be carried on 11.0.2 |
| ~~Forward port changes guest behavior~~ (retired: the port is done and promoted) | — | Native baseline tests still owed for browser comparison | — |
| Device code assumes a real filesystem (`fopen`, `g_mapped_file`) | High | Measure MEMFS/mmap behavior early; add a pack access seam if needed | The 300 MiB pack cannot be mapped within browser memory |
| NAND pack consumes too much memory | High | Blob/chunk access, bounded caches, storage benchmarks | Reference 8 GiB machine repeatedly crashes |
| M68AP NAND has no public single-file source | Resolved by decision | Prepare offline and self-host; no in-browser conversion | — |
| The packaging path is pinned to 1.1.4 (`build-m68ap-homescreen-nand.py` hardcodes `4A102`) | High for a 1.0-first release | Wire the existing `--ipsw-build` axis through the home-screen recipe | 1.0 cannot be packaged reproducibly |
| Per-version fixed constants drift (epoch, FIL signature, PC windows) | High | `firmware_profiles.py` is the single source; manifest carries `epoch` | A version boots with another version's constants |
| Four versions multiply hosting and cache cost | Medium | Chunked + Brotli (~3×), content-addressed sharing, lazy load | Storage or bandwidth exceeds what the origin can serve |
| A cold boot touches most of the pack anyway | Medium | Measure the working set before building the chunk pipeline | Lazy loading saves little over a whole-pack download |
| The build host runs out of disk | Medium | ~5 GiB free needed: deps, build tree, staged assets | Build cannot complete locally |
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

- Forward-port the device model to modern QEMU rather than backporting Wasm.
  *(Done: QEMU 11.0.2.)*
- Target iPhone 2G. Ship **iPhone OS 1.0 first**, then 1.0.2, 1.1.1, 1.1.4 as a
  version picker; N45AP after those, from the same build.
- Prepare every asset set offline in this repository and serve it from our own
  origin. Do not build in-browser conversion of original artifacts.
- Deliver the NAND as content-addressed, individually Brotli-compressed chunks
  loaded on demand through a service worker, following Infinite Mac's measured
  approach. Measured here: ~3× size reduction, 315 MB → ~100 MB per version.
- Keep the firmware version in the asset manifest (`build`, `epoch`), never in
  a board table — epoch and NAND signature are firmware-keyed, not board-keyed.
- Build with the native pinned Emscripten toolchain by default; keep the
  container for reproducible and CI builds. Docker is not a requirement.
- Ship wasm64 + TCI first and let measurement decide whether a JIT is required.
- Keep board knowledge in the asset manifest, not in the frontend.
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

Closed since the plan was written:

- ~~Exact modern QEMU commit and Wasm TCG revision.~~ QEMU 11.0.2 in-tree; no
  Wasm TCG in this base, so TCI until measured otherwise.
- ~~Exact Emscripten and dependency versions.~~ Pinned in
  `scripts/wasm/toolchain.env`.
- ~~Frontend build tool and minimum Node.js version.~~ None: ES modules served
  directly, revisited only if something requires a build step.

Still open:

- Whether TCI is fast enough, and therefore whether the out-of-tree JIT is
  required. **This is the decisive one.**
- How the pack is reached from the wasm heap: MEMFS + mmap, a Blob-backed
  reader, chunk cache, or complete-memory load.
- Whether 62 pages/chunk survives contact with real network latency — the
  measured table optimises bytes, and says nothing about round trips.
- The cold-boot working set for 1.0, 1.0.2 and 1.1.1 (1.1.4 is measured), and
  how much chunk content the four versions share.
- Whether `OffscreenCanvas` is the default or an optimization.
- Exact supported browser versions and hardware baseline.
- The hosting origin, its cache headers, and whether IPFS is added later.

Closed by the 2026-07-27 revision: independent chunk compression **is** the
plan (measured ~3×); the source-loaded first run is **not** being built; the
publishing destination is our own origin.

Each open decision must be closed with a short decision record containing measured
evidence, not preference alone.

## Definition of done

The browser project is complete when:

- one pinned QEMU/Wasm build runs iPhone OS 1.0 in supported browsers, and the
  same build runs 1.0.2, 1.1.1 and 1.1.4 from their own asset sets;
- the N45AP asset set runs on that same build;
- native QEMU 11.0.2 still passes the reference behavior tests;
- the picker boots any version from the catalog, and a cached version boots
  offline;
- every asset set builds reproducibly from one command against its IPSW;
- display, touch, Home, Power, sleep, wake, and persistence pass regression tests;
- asset corruption produces actionable UI errors;
- the packed NAND, chunk, and overlay formats have specifications and golden
  tests;
- performance and memory gates pass on representative hardware;
- deployment headers and concurrent delivery are verified;
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
- [Per-version firmware matrix and status](IPHONE_OS_1X_VERSIONS.md)

### Infinite Mac (the model for asset delivery)

- [infinitemac.org](https://infinitemac.org) — the product shape: many OS
  versions, one browser emulator, instant boot
- [An Instant-Booting Quadra in Your Browser](https://blog.persistent.info/2022/03/blog-post.html)
  — 256 KiB content-addressed chunks, per-chunk Brotli, service-worker
  interception, prefetch; boot screen in 1 s, booted in 3 s cold
- [Disks, CD-ROMs and Custom Instances](https://blog.persistent.info/2023/08/infinite-mac-cd-roms.html)
  — per-chunk residency dropped out-of-memory rates from 6.5% to 0.3%
- [Infinite Mac OS X](https://blog.persistent.info/2025/03/infinite-mac-os-x.html)
  — qemu-wasm benchmarked at 8 s on an MD5 workload against 13 s (DingusPPC)
  and 18 s (PearPC)
- [mihaip/infinite-mac](https://github.com/mihaip/infinite-mac) — source
