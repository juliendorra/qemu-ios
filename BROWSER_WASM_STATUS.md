# Browser/WebAssembly port — live status

The dated, working state of the browser port. The design of record is
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md);
this file records what is actually built and proven.

> **Starting a session? Read [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md)**
> — the ordered next steps, their acceptance criteria, and the traps.

**Target:** iPhone 2G (M68AP), executed entirely in the viewer's browser, as a
picker across iPhone OS 1.0 / 1.0.2 / 1.1.1 / 1.1.4 — **1.0 first**. Assets are
prepared offline here and self-hosted. iPod touch (N45AP) comes after.

---

## Session — 2026-07-28: QEMU RUNS IN WEBASSEMBLY

**`build-wasm/qemu-system-arm.wasm` exists (53 MB) and boots iPhone OS 1.1.4's
iBoot and Darwin kernel.** It then panics in a driver-matching race, and TCI is
~13× slower than native. Both are quantified below.

### What it took

QEMU 11.0.2's Emscripten support accepted our configure line unchanged — no
patches. Four things had to be fixed:

1. **Wrong ninja target.** Emscripten names it `qemu-system-arm.js`; asking for
   `qemu-system-arm` fails.
2. **OpenSSL — the real blocker.** Four device files included `<openssl/aes.h>`
   or `<openssl/sha.h>`, and OpenSSL does not cross-compile to wasm64. Three
   uses were dead code; the two live ones now use what QEMU already ships —
   glib's `GChecksum` for SHA1, `crypto/aes.h` plus a new
   `include/hw/arm/ipod_touch_aes_cbc.h` for AES-CBC (QEMU has the cipher but
   no CBC wrapper). The helper mirrors OpenSSL's `CRYPTO_cbc128_*` exactly,
   including the trailing partial block and the in-place IV update. **This is a
   portability fix, not a browser hack: the native build no longer needs
   `-lcrypto` for these devices either.**
3. **The 8900 engine's AES code lives in its header**, so grepping the `.c`
   showed nothing and the first edit removed a declaration it needed. The native
   build caught it immediately.
4. **`-lnodefs.js` never reaches the link** — QEMU's `configs/meson/emscripten.txt`
   overrides `LDFLAGS`, and even via `--extra-ldflags` the emitted module still
   stubs NODEFS out. Unresolved; MEMFS is used instead, which is what the
   browser needs anyway. Staging all 301.5 MiB into MEMFS takes **0.7 s**.

### Measured: TCI is ~13× slower than native

Same firmware (4A102), same host, same artifacts:

| landmark | native | wasm (TCI) | ratio |
| --- | --- | --- | --- |
| iBoot-204.3.14 banner | 2.0 s | 12.7 s | 6.4× |
| Darwin kernel version | 7.3 s | 84.7 s | 11.6× |
| `USBWrangler::start starting` | 7.8 s | 103.9 s | 13.3× |
| `AppleS5L8900XADM::attach` | 7.8 s | 105.1 s | 13.5× |

**This is the go/no-go answer for TCI, and it is not encouraging.** A native
boot to the home screen is on the order of 20-30 s, so 13× puts a browser cold
boot in the several-minute range before any of the asset-loading work counts.
The out-of-tree qemu-wasm JIT is therefore looking like a requirement rather
than an option — consistent with Infinite Mac measuring qemu-wasm *faster* than
hand-ported C emulators. Re-check whether the backend has merged upstream
before committing to carrying the patch set.

### Guest-visible time must be decoupled: run with `-icount`

Without it the guest sees its own driver `start()` calls taking **16944 ms,
22818 ms, 19703 ms** — because `QEMU_CLOCK_VIRTUAL` follows wall clock while
TCI runs the guest 13× slower. The kernel then takes timeout paths that no real
device ever takes, and panicked with a null dereference in
`AppleS5L8900XUSBWrangler` right after the USB PHY registered.

With `-icount shift=3` the same drivers report **539 / 513 / 544 ms** and that
panic is gone. `scripts/wasm/boot-test.mjs` takes `IT_ICOUNT=<shift>`.

**The browser build should always run with icount.** TCI will always be far
slower than native, so every timeout-sensitive driver is exposed without it.
This is the repo's own standing advice (AGENTS.md, "Determinism — use
`-icount`") arriving in a new context.

*Correction to an earlier claim in this session:* the wrangler's
`phyRegistered` event was described as something "native never produces". That
was measured against a native log that was itself booting the wrong NAND (see
below) and never reached the relevant stage. Against a correct boot, native
produces `phyRegistered` too. The divergence was never the event — only the
panic.

### Both panics were the clock, and `shift=1` clears them

With icount **and** the verified home-screen NAND, the boot now gets much
further — through the USB PHY, the network stack, the LCD — and then:

```
IOIpodUSBDevice::start
panic(cpu 0 caller 0xC012D963):
```

Native runs the same driver at the same point and simply continues
(`Registering: ../usb-device/AppleS5L8900XIpodHAL/IOIpodUSBDevice`), reaching
the home screen at 74.3% non-black. The panic message is empty, which is
unusual and worth chasing.

**Resolved by using a faithful clock rate.** The panic point tracks the virtual
clock, which is how we know these are guest timeouts rather than bad values out
of the device models:

| icount | ns per instruction | outcome |
| --- | --- | --- |
| none | wall clock | USB wrangler null-deref (`caller 0xC00638CC`) |
| `shift=5` | 32 (~31 MIPS) | same null-deref |
| `shift=3` | 8 (~125 MIPS) | passes it; panics in `IOIpodUSBDevice::start` |
| **`shift=1`** | **2 (~500 MHz)** | **both panics gone** |

`shift=1` printed `Registering: ../usb-device/AppleS5L8900XIpodHAL/IOIpodUSBDevice`
— the same line native prints — and continued. **No device-model change was
needed.**

Note the direction is counter-intuitive: a HIGHER shift means more virtual
nanoseconds per instruction, i.e. a guest that believes it is running on a
SLOWER CPU. The real S5L8900 is 412 MHz ≈ 2.4 ns/instruction, so `shift=1` is
the faithful setting; `shift=3` already presents a machine ~3× slower than the
hardware iPhone OS was written for.

The cost is wall clock: at `shift=1` the guest executes 4× more instructions per
virtual millisecond than at `shift=3`. Under TCI that is slow. **Another
argument that the JIT is required** — a ~10× faster engine makes the faithful
clock rate affordable.

### The trap that cost this session two wrong conclusions

`m68ap-artifacts/builds/4A102/nand` was **not** the home-screen NAND. The
migration filed `stage/nand-m68ap-fresh` there; it is a full, valid tree built
from an *unpatched* root, so it boots and renders **nothing** (kernel
framebuffer 0.0%). Every wasm boot test today used it, and the native
comparison run used it too — which is how `phyRegistered` looked wasm-only.

A wrong NAND at the canonical path is worse than a missing one: missing fails
loudly, wrong boots to a black screen. Fixed three ways:

- the tree was renamed to `nand-prepack-not-product`, so the canonical path is
  now empty and fails loudly until the recipe regenerates it;
- `build-m68ap-nand.py` gained `--recipe`, and the product recipe stamps
  `"recipe": "home-screen"` into `nand-provenance.json`, so the question is
  answerable from the tree itself;
- the migration script records why that mapping is wrong.

### Every failure so far has had a cause OUTSIDE the wasm build

Worth stating plainly, because it changes how the next failure should be
triaged. Four separate symptoms all looked like "the WebAssembly port is
broken", and none of them were:

| symptom | actual cause |
| --- | --- |
| `AppleS5L8900XUSBWrangler` null dereference | wall-clock virtual time → guest timeout path |
| `IOIpodUSBDevice::start` panic | the same, at a different threshold |
| black framebuffer, 0% non-black | a NAND built from an unpatched root at the canonical path |
| emulator `abort()` shortly after `BSD root` | the **test harness** had not created the NAND bank directories |

**No defect has yet been found in the device model, in QEMU's Emscripten
support, or in TCI's correctness.** The only code change the port has required
is removing OpenSSL. Triage the next wasm-only symptom as environment, clock,
or harness before suspecting the port.

### Dead end: the harness abort after `BSD root`

`nand_flush_buffered_page()` opens `<nand>/bank<N>/<page>_new.page` for WRITING
on every guest page write, and calls `hw_error()` — which aborts the entire
emulator — when that open fails. `boot-test.mjs` created `/fw/nand` but not the
eight bank directories, so the first guest write after the root filesystem
mounted killed the run. `fb-snapshot.py` and `ipod-app-launcher.sh` both create
them; the harness now does too.

**This is a hard requirement for W6, not just a harness fix.** In a browser
there is no filesystem to write to at all, so that path must be replaced by the
copy-on-write overlay. Leaving it as-is means the first guest write aborts the
emulator in the page.

### Dead end: `-lnodefs.js` / NODEFS

Two attempts (LDFLAGS environment, then `--extra-ldflags`) both left the
emitted module stubbing NODEFS out with "no longer included by default". QEMU's
`configs/meson/emscripten.txt` sets its own link arguments; the flag reaches
`config-meson.cross` but not the effective link. Abandoned: MEMFS is what the
browser needs anyway, and staging 301.5 MiB into it costs 0.7 s.

### Native-build improvements this work produced

The browser port paid for itself in the native tree three times over:

- **OpenSSL is no longer needed by the S5L8900 device models.** SHA1 goes
  through glib (already a hard QEMU dependency) and AES through QEMU's own
  `crypto/aes.h` plus `include/hw/arm/ipod_touch_aes_cbc.h`. One less external
  dependency for every build, native included.
- **`fb-snapshot.py` can verify again.** Its QMP socket lived in `/tmp`, which
  is swept during long runs; the socket vanished mid-boot and the failure
  surfaced as a bare `FileNotFoundError` that read like a guest hang. Now
  `/var/tmp`, and it waits for the socket with an explanatory error. This had
  been silently breaking home-screen verification for every board.
- **NAND provenance can now answer "is this the product NAND?"**
  `build-m68ap-nand.py --recipe` records the recipe, and the home-screen recipe
  stamps `"recipe": "home-screen"`. Before this, a tree built from an unpatched
  root was indistinguishable from the real one until you booted it and saw a
  black screen.

`-icount` also stopped being advice and became a measured requirement — see
AGENTS.md, which now points here for the worked example.

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

**Measurements still owed:** the cold-boot working set for 1.0.2 and 1.1.1
(1.0 and 1.1.4 are done), and how much chunk content the versions share
(`measure-pack.py --cross`, which needs the other packs built first).

### Reproducing the measurements

```sh
# whole-pack compression and dedup
scripts/wasm/measure-pack.py <nand.pack>            # sampled, fast
scripts/wasm/measure-pack.py <nand.pack> --full     # exact, slow
scripts/wasm/measure-pack.py a.pack b.pack --cross  # sharing between versions

# cold-boot working set: trace a VERIFIED home-screen boot, then reduce it
cp -Rc m68ap-artifacts/builds/<BUILD>/nand /tmp/nand-clone   # APFS clone, ~0 B
IT_NAND_WRITABLE=1 IT_NAND_TRACE_PAGES=/tmp/boot.trace \
    python3 scripts/fb-snapshot.py --board m68ap --build <BUILD> \
    --boot-wait 420 --nand-m68ap /tmp/nand-clone --logs /tmp/fb
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
  One lever exists on the guest side: SpringBoard currently composites in
  SOFTWARE (`LK_ENABLE_MBX2D=0`) because the MBX is a stub, so that work is
  guest ARM instructions running through the interpreter. Modelling MBX 2D
  would move it into compiled wasm — a bigger win under TCI than natively.
  Unmeasured; sequencing and how to measure it are in
  [`MBX_HANDOFF.md`](MBX_HANDOFF.md) §4-5. It is NOT a substitute for the
  TCI-vs-JIT decision, and should not be attempted alongside the display
  bridge.
- **`crypto.subtle` has no streaming digest**, so the loader hashes the whole
  300 MiB pack in memory. Acceptable on desktop; revisit for constrained hosts.

### Reproduction

```bash
scripts/wasm/setup-toolchain.sh
scripts/wasm/build-deps.sh
scripts/wasm/build-qemu.sh
scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G (iOS 1.1.4).app" \
    --board m68ap --firmware 1.1.4
scripts/wasm/serve.py            # then open http://localhost:8010
```
