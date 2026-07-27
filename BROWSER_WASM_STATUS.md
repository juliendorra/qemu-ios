# Browser/WebAssembly port — live status

The dated, working state of the browser port. The design of record is
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md);
this file records what is actually built, what is proven, and what is next.

**Target:** iPhone 2G (M68AP), executed entirely in the viewer's browser, as a
picker across iPhone OS 1.0 / 1.0.2 / 1.1.1 / 1.1.4 — **1.0 first**. Assets are
prepared offline here and self-hosted. iPod touch (N45AP) comes after.

---

## Session — 2026-07-27: delivery decided, assets measured

- **Delivery model settled**: prepared offline, self-hosted, chunked and
  compressed. No in-browser conversion of original artifacts — the M68AP NAND is
  constructed from an IPSW by this repo's pipeline and has no downloadable
  equivalent.
- **Measured the shipping 1.1.4 pack** (300.3 MiB, 148,812 × 2,112 B pages) with
  `scripts/wasm/measure-pack.py`: per-chunk **brotli q11 = 31.4%** → 94.2 MiB
  (lzma 30.9%, zlib 36.5%). No chunk is uniform, but a **full scan found 197 of
  1,201 chunks are duplicates (16.4%)**, one repeating 198 times.

  *Correction to an earlier figure in this session:* a first pass reported ~6%
  duplicates. That sample was taken at raw 256 KiB **byte** offsets, which
  straddle the header/index boundary and are misaligned to the 2,112-byte page
  stride, so almost no two windows could match. Measured at the real chunk
  definition — page-aligned, fixed page count — dedup is 16.4%. Only the
  page-aligned number is meaningful.

- **Measured what a cold boot actually touches — the number the whole delivery
  design turns on.** Added `IT_NAND_TRACE_PAGES` to `hw/arm/ipod_touch_nand.c`
  (records every page fetch as a little-endian u32 VPN, in access order) and
  `scripts/wasm/analyze-nand-trace.py` to reduce a trace against a pack. From a
  boot verified to reach the home screen (kernel framebuffer 73.95% non-black at
  420 s):

  | | |
  | --- | --- |
  | page fetches | 31,384 |
  | distinct pages touched | **25,030 = 16.8% of the pack** |
  | distinct chunks (124 pages) | 351 of 1,201 = 29.2% |
  | **first-boot download, chunked + brotli** | **24.3 MiB** |
  | whole pack + brotli | 94.2 MiB |
  | whole pack raw | 300.3 MiB |

  **Lazy loading wins decisively — ~4× better than a compressed whole-pack
  download.** Chunk size trades read amplification against request count:
  124 pages → 24.3 MiB, 62 → 20.9 MiB, 32 → 18.5 MiB, 16 → 17.0 MiB. **62 pages
  (128 KiB) is now the recommended size**, chosen from our own data rather than
  inherited from Infinite Mac's 256 KiB.

- **Two traps found while taking that measurement**, both worth knowing before
  anyone repeats it:
  - A **read-only NAND never reaches SpringBoard**. Without
    `IT_NAND_WRITABLE=1` writes are silently discarded, and daemons that must
    create state spin forever — the boot stalls after launchd with no error. Use
    a throwaway APFS clone (`cp -Rc`, near-zero space) plus that flag.
  - **SpringBoard does not announce itself on serial.** Grepping the log for
    "SpringBoard" finds nothing even on a fully successful boot. Verify with
    `scripts/fb-snapshot.py`, which reports the kernel framebuffer's non-black
    percentage; scanout itself reads ~0% because the panel sleeps, which is the
    already-documented "black screen is scanout, not SpringBoard" artifact.
- **Researched Infinite Mac's approach** and adopted it: content-addressed
  fixed-size chunks, per-chunk Brotli, service-worker interception keeping the
  emulator's reads synchronous, prefetch of the boot working set, per-chunk
  residency. Its measured bar is boot screen in 1 s, booted in 3 s, cold cache.
- **Established what "the JIT" is, and what our engine already has.** The JIT is
  [qemu-wasm](https://github.com/ktock/qemu-wasm)'s WebAssembly TCG backend:
  each translation block becomes one Wasm module executed via the browser's
  `WebAssembly.Module`/`Instance` APIs, and it is *hybrid* — a forked TCI
  interprets everything and only blocks run ~1000 times get compiled, because
  compilation is costly and browsers cap live Wasm instances.

  Upstreaming is half done and **we have the half that landed**: Emscripten host
  support plus TCI for 32-bit guests merged in QEMU 10.1, which is why 11.0.2
  builds for the browser at all. The backend is still out of tree — verified
  2026-07-27 that **QEMU master has no `tcg/wasm*` directory**, and our own
  `tcg/` has none either. Our guest is 32-bit ARM (the upstreamed case) and the
  v2 backend series targets wasm64 (how we configure), so adoption would be
  well-aligned if it becomes necessary.

  *Correction:* our build links libffi and passes `ASYNCIFY_IMPORTS=ffi_call_js`,
  which looks like JIT plumbing already present. It is not — that is TCI's own
  helper-call path (`ffi_call`, `tcg/tci.c:366`). No part of the JIT is in tree.

- **Found supporting evidence for the JIT fallback**: Infinite Mac benchmarked
  qemu-wasm at 8 s on an MD5 workload against DingusPPC's 13 s and PearPC's 18 s
  — a JIT-equipped QEMU beats hand-ported emulators. Says nothing about TCI,
  which is what we currently have.
- **Corrected a stale assumption**: all four 1.x builds now reach the home
  screen natively (`IPHONE_OS_1X_VERSIONS.md`, 2026-07-26), including 1.0. The
  1.0-first plan is therefore feasible; earlier notes saying 1.0 was blocked on
  the ADM are out of date.

**The packaging gap for a 1.0-first release is closed** (2026-07-27). Every
build now uses one layout and an explicit `--build` — see
[`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md). The **iPhone OS 1.0 pack
exists and boots to the home screen**:

| | 1.0 (`1A543a`) | 1.1.4 (`4A102`) |
| --- | --- | --- |
| pack | **215.6 MiB, 106,858 pages** | 300.3 MiB, 148,812 pages |
| brotli, whole pack | **63.8 MiB (29.6%)** | 94.2 MiB (31.4%) |
| cold boot touches | **20,397 pages = 19.1%** | 25,030 pages = 16.8% |
| **first boot, chunked (62 pages) + brotli** | **18.6 MiB** | 20.9 MiB |
| home screen verified | 59.04% non-black | 73.95% non-black |

1.0 is both the first target and the **cheapest**: 18.6 MiB to first boot
against a 215.6 MiB raw pack — a 12× reduction, and a third of what a
compressed whole-pack download would cost.

**Measurements still owed:** the cold-boot working set for 1.0, 1.0.2 and 1.1.1
(1.1.4 is done), and how much chunk content the four versions share
(`measure-pack.py --cross`, which needs the other packs built first).

### Reproducing the measurements

```sh
# whole-pack compression and dedup
scripts/wasm/measure-pack.py <nand.pack>            # sampled, fast
scripts/wasm/measure-pack.py <nand.pack> --full     # exact, slow
scripts/wasm/measure-pack.py a.pack b.pack --cross  # sharing between versions

# cold-boot working set: trace a VERIFIED home-screen boot, then reduce it
cp -Rc <bundle nand> /tmp/nand-clone                # APFS clone, ~0 bytes
IT_NAND_WRITABLE=1 IT_NAND_TRACE_PAGES=/tmp/boot.trace \
    python3 scripts/fb-snapshot.py --board m68ap --boot-wait 420 \
    --bootrom … --iboot-m68ap … --nor-m68ap … --nand-m68ap /tmp/nand-clone \
    --logs /tmp/fb
# require kernel_0x0f400000 nonzero_pct to be high before trusting the trace
scripts/wasm/analyze-nand-trace.py /tmp/boot.trace <nand.pack> \
    --pages-per-chunk 62 --prefetch prefetch.json
```

---

## Session — 2026-07-26: toolchain and tools

### What changed since the plan was written

The plan's two largest prerequisites are already done, which removes most of its
Phase 1:

- **The forward port happened.** This tree is QEMU **11.0.2**, not 6.2.50
  (`QEMU_11_PORT.md`).
- **Emscripten support is in-tree and upstream.** QEMU 11.0.2 already carries
  `host_os == 'emscripten'`, `--cpu=wasm64`, `util/coroutine-wasm.c`,
  `os-wasm.c`, `configs/meson/emscripten.txt`, and a container recipe at
  `tests/docker/dockerfiles/emsdk-wasm64-cross.docker`. No out-of-tree patch set
  is needed to *build*.
- **The packed NAND exists.** `scripts/pack-ipod-nand.py` and the reader in
  `hw/arm/ipod_touch_nand.c` already implement `IPODNAND` v1 (mmap'd, sorted
  index, binary search), and the packaged apps ship one. The plan's "specify and
  implement the pack" work is done for the read path. The copy-on-write overlay
  is still unimplemented.

### Proven this session

- **Docker is not required.** Emscripten **4.0.10** installs and runs natively on
  this arm64 macOS host (`emcc --version` verified). The container is retained
  only as the reproducible/CI path.
- **The host's Python is too old for emsdk** (Command Line Tools ships 3.9.6;
  emsdk requires ≥ 3.10). Rather than changing the host's Python — the native
  QEMU build depends on it — `setup-toolchain.sh` unpacks a pinned standalone
  CPython 3.12.13 into `.wasm-toolchain/`.
- **meson comes from this tree.** QEMU vendors `python/wheels/meson-1.10.0.whl`;
  the toolchain venv installs it offline, so no system meson is needed.
- **zlib 1.3.1, libffi 3.5.2 and pixman 0.44.2 cross-compile to wasm64**
  natively, same versions and flags as the upstream container.
  - **macOS-only correction:** zlib's `configure` detects a Darwin *build* host
    and swaps the archiver for Apple's `libtool`, which rejects emcc's wasm
    objects (`adler32.o is not an object file`). `--uname=Linux` forces the
    generic branch and keeps `AR=emar`. The container never hits this because
    its build host is Linux.

### Blocked: the host disk is full

The volume is at **100% (≈300 MiB free of 228 GiB)**. Two steps died on
`ENOSPC` and are unfinished, not broken:

- **glib 2.84.0** did not install (`target/lib/pkgconfig/glib-2.0.pc` absent);
  its build tree plus the pcre2 subproject needs roughly 1–2 GiB.
- **Asset staging** failed mid-copy of the 300 MiB `nand.pack`; the partial
  `web/public/assets/` was deleted.

Rough space needed to finish: **~1–2 GiB** for glib, **~2–3 GiB** for the QEMU
wasm build tree, **~300 MiB** for the staged asset set. `.wasm-toolchain/` is
currently 1.8 GiB (1.6 GiB of that is the emsdk itself).

### Tools added (`scripts/wasm/`)

| tool | purpose |
| --- | --- |
| `toolchain.env` | every pinned version in one place |
| `setup-toolchain.sh` | standalone Python (if needed) + emsdk 4.0.10 + meson venv |
| `build-deps.sh` | wasm64 zlib/libffi/pixman/glib into `.wasm-toolchain/target` |
| `build-toolchain.sh` | the container alternative (reproducible/CI) |
| `build-qemu.sh` | `arm-softmmu` for wasm64/TCI into `build-wasm/` |
| `stage-assets.py` | asset set + hashed `asset-manifest.json` |
| `serve.py` | COOP/COEP static server with byte ranges and `--check` |

Plus a `web/` shell: capability gate, asset loader (download → verify →
Cache Storage), worker lifecycle, canvas display target, and the input bridge
(pointer with capture, Home/Power, the native `H`/`P` bindings, blur cleanup).

### Decisions taken

- **Native toolchain by default, container for releases.** Nothing about the
  build requires Linux, and routine work should not need a running daemon.
- **wasm64 + TCI.** QEMU 11.0.2's meson refuses a WebAssembly host without
  `--enable-tcg-interpreter`; there is no in-tree WebAssembly TCG backend at
  this version. TCI is the correctness path and the first measurement.
  `--wasm64-32bit-address-limit` keeps the address space at 32 bits, which the
  128 MiB guest never exceeds.
- **Assets stay out of git.** `web/public/assets/` and `web/emulator/` are
  ignored; staging needs explicit paths or one `--from-app` bundle.

### Not yet done / open

- **The wasm QEMU build has never been run to completion.** Expect real work in
  the iPod device code: it uses `fopen`/`g_mapped_file` per artifact, and mmap
  over a ~300 MiB pack in MEMFS is the first memory question to measure.
- **No display bridge.** QEMU is built `-display none`; the plan's display
  listener (surface + dirty rects → `OffscreenCanvas`) is unwritten, so the
  worker's `frame` message has no producer yet.
- **No input path into QEMU.** The frontend emits input events; the worker
  drops them until the bridge exists.
- **The worker's module instantiation is provisional** — written against
  Emscripten's documented shape, to be re-checked against the first real
  `qemu-system-arm.js`.
- **No copy-on-write overlay.** Guest writes have nowhere to go in the browser;
  the base pack must stay immutable.
- **Performance is unmeasured.** If TCI cannot reach an acceptable time to
  SpringBoard, the out-of-tree [qemu-wasm](https://github.com/ktock/qemu-wasm)
  JIT becomes a requirement rather than an optimization. That is the next
  go/no-go signal.
- **`crypto.subtle` has no streaming digest**, so the loader hashes the whole
  300 MiB pack in memory. Acceptable on desktop; revisit for constrained hosts.

### Reproduction

```bash
scripts/wasm/setup-toolchain.sh
scripts/wasm/build-deps.sh
scripts/wasm/build-qemu.sh
scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G.app" \
    --board m68ap --firmware 1.1.4
scripts/wasm/serve.py            # then open http://localhost:8010
```
