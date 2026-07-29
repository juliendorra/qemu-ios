# Browser/WebAssembly port — live status

The dated, working state of the browser port. The design of record is
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md);
this file records what is actually built and proven.

> **Starting a session? Read [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md)**
> — the ordered next steps, their acceptance criteria, and the traps.

**Target:** iPhone 2G (M68AP) running **iPhone OS 1.1.4**, in a browser: fast to
start (chunked assets) and running at **real-time speed**. Then 1.0, once its
button problems are fixed elsewhere, followed by 1.0.2 and 1.1.1 as a version
picker. Assets are prepared offline here and self-hosted. iPod touch (N45AP)
comes after.

---

## Session — 2026-07-29 (Session A): making it visible and interactive

Parallel session A (`BROWSER_WASM_SESSION_A.md`): paint the framebuffer, take
input, skip the boot. **The browser now renders the guest panel** — the Apple
logo appears at ~17 s of a cold JIT boot, in correct colours.

### The display seam as committed could never have worked

`ui/wasm.c` published `Module.qemuDisplay` from an `EM_JS` body. Under
`-sPROXY_TO_PTHREAD=1` — which `configs/meson/emscripten.txt` sets — `main()`
runs on a *worker*, and an `EM_JS` body executed there sees **that worker's**
`Module`, a different JavaScript object from the page's. Nothing assigned to it
is visible to the page, ever.

It had never been exercised: the committed `jit-boot` page ran `-display none`,
so the backend was registered-but-unused and the defect was invisible.

Replaced with a seam that does not care which thread is asking:

- a static `WasmDisplayInfo` struct in the wasm heap, all `u32` fields so JS can
  read it out of `HEAPU32` with no layout guesswork (the surface pointer is
  split into two words because a wasm64 pointer does not fit in one);
- `EMSCRIPTEN_KEEPALIVE uint32_t wasm_display_info_addr(void)` returning its
  address. **Exported wasm functions are callable from any thread holding the
  instance, and every thread addresses the same shared memory** — which is the
  property `EM_JS` lacks.

`seq` is a **seqlock** (odd while the emulator thread is mid-update), not a bare
counter. This is not fastidiousness: a torn *pointer* read is not a torn frame,
it is an arbitrary index into `HEAPU8`.

**Generalise this.** Anything that has to reach the page from the emulator
thread has the same problem, and the same answer: put it in memory and export
an accessor. `EM_JS` is only safe for code that runs on the thread that owns the
`Module` you mean.

### The heap views are not exported — and touching one kills the emulator

First run with `-display wasm` died 2.2 s in:

```
Aborted('HEAPU32' was not exported. add it to EXPORTED_RUNTIME_METHODS)
```

`Module.HEAPU8`, `HEAPU32`, `wasmMemory` and `wasmExports` are all **absent, and
absent in the worst way**: the property is a stub that calls `abort()`, tearing
down the runtime and taking the emulator with it. A missing display feature
killed a boot.

Two fixes, both needed:

1. `configs/meson/emscripten.txt` now exports `HEAPU8,HEAPU32` alongside
   `addFunction,removeFunction,TTY,FS`.
2. **The page reaches every `Module` property through a try/catch** and switches
   painting off after the first failure. Presentation is a nice-to-have; it must
   never be able to end a run.

**Trap worth its own line: meson reads a cross file's `[built-in options]` only
at CONFIGURE time.** Editing `emscripten.txt` and rebuilding is a silent no-op —
ninja's own regenerate does not re-read them, and the link succeeds with the old
flags. It needs `build-qemu.sh --configure`.

### Pixel format: `x8r8g8b8`, read from the source rather than diagnosed

`draw_line32_32()` in `hw/arm/ipod_touch_lcd.c` stores
`rgb_to_pixel32(r, g, b)` = `(r<<16)|(g<<8)|b` as a native `u32`, so memory
holds **B, G, R, X** while `ImageData` wants **R, G, B, A**. The page does one
`u32` shuffle per pixel:

```js
dst = 0xff000000 | ((p & 0xff) << 16) | (p & 0xff00) | ((p >>> 16) & 0xff);
```

The handoff predicted "if the panel comes out blue, this is why". It never came
out blue, because the conversion was written from the source before the first
run. Recorded because the cheap move (read the drawing function) beat the
expensive one (recognise the symptom).

### Input: an SPSC ring drained by a QEMU timer

QEMU's input queue belongs to the emulator thread and expects the BQL, so
nothing may call into `ui/input.c` from JS. The exported entry points
(`wasm_input_touch`, `wasm_input_button`) **only write a slot** in a
single-producer/single-consumer ring; a 15 ms `QEMU_CLOCK_REALTIME` timer on the
emulator thread drains it and dispatches.

- **`dpy_refresh` was the tempting free drain point and was rejected.** It
  already runs on the right thread at roughly the right rate, but QEMU throttles
  the display refresh interval when a console looks idle — input latency would
  then depend on how much the guest happens to be drawing.
- The page speaks **buttons** (`home`/`power`), not QKeyCodes, so the
  `Q_KEY_CODE_H` / `Q_KEY_CODE_P` mapping stays in C where the enum lives.
- Touch is sent in the panel's own 320x480 coordinates and
  `qemu_input_queue_abs` rescales; the **multitouch model is what flips Y**
  (`fy = 1 - y/2^15`), exactly as for a native display, so the page must not
  pre-flip.

### The damage rectangle needed an ack to mean anything

As written the union only ever grew: `wasm_gfx_update` accumulated into one
rectangle with nothing to say when accumulation could restart, so within a
second it was permanently full-screen. The struct now carries an `ack` field —
the only field written by the page — holding the seq it last painted. The page
repaints in full and ignores the rectangle, but a partial-blit consumer can now
exist.

### The shared working tree is a live hazard between the two sessions

Both sessions edit one checkout, and B's in-flight work broke A's build twice
(first `no member named 'pack_entry_count'`, then undefined `it_nand_pack_*`
symbols) — unavoidable, and cheap to wait out.

**What was not cheap:** B editing `hw/arm/meson.build` made ninja auto-regenerate,
and *that* enabled curl, zstd and libssh for a WebAssembly build — the disaster
this repo documents as "never run meson/ninja by hand", reached **without anyone
running meson by hand**.

Root cause, and it is a real bug in our tooling:

> `build-qemu.sh` exported `PKG_CONFIG_PATH`, which only **prepends** to
> pkg-config's search path. The initial configure was clean only because
> `emconfigure` sets `PKG_CONFIG_LIBDIR`, which **replaces** it. Ninja's
> `meson --internal regenerate` escapes `emconfigure` entirely, so pkg-config
> fell back to its built-in path and found `/opt/homebrew`.

`build-qemu.sh` now exports `PKG_CONFIG_LIBDIR` itself. Verified: a regenerate
after the fix reports `libcurl found: NO`, `libssh found: NO`, `libzstd found:
NO`.

**This retires "never run meson/ninja by hand" as sufficient advice.** Ninja
runs meson on its own, whenever any `meson.build` changes — which, with two
sessions in one tree, is constantly.

---

## Session — 2026-07-29 (Session B): fast and small

Parallel session B (`BROWSER_WASM_SESSION_B.md`): JIT tuning, the pack-access
seam, and chunked delivery. Builds in `build-wasm-b/`, serves on port 8011, and
does not touch `ui/wasm.c` or `web/public/*/index.html`.

### The JIT threshold is now tunable at RUN TIME, so a sweep is three page loads

`INSTANTIATE_NUM` was a `#define`, which made every point in a sweep a full
wasm rebuild (~15 min) before a ~15 min boot. It is now `jit_instantiate_num`,
resolved once in `init_wasm()` from, in order:

1. `IT_WASM_INSTANTIATE_NUM` — native and Node;
2. `instantiate=<n>` in **`/fw/jit-tune`** — the browser.

The second exists because Emscripten's `ENV` object is not in
`EXPORTED_RUNTIME_METHODS`, so a page **cannot set an environment variable** —
but it can write a file into MEMFS before startup. `max_instances=<n>` rides
along in the same file and is clamped to the compile-time `MAX_INSTANCES`,
which still sizes a static array. Defaults are unchanged (100 / 48000), and the
resolved values are printed once: `[JIT] tuning: instantiate=… max_instances=…`.

`web/bench-b/index.html` turns those into URL parameters
(`?instantiate=50&max=48000`), alongside the landmark table and the `[JIT]`
counters.

### The pack-access seam (B2) — landed, with golden fixtures

`nand_read_packed_page()` used to binary-search a `GMappedFile` and `memcpy`
straight out of it. That lookup now lives in **`hw/arm/ipod_touch_nand_pack.c`**
behind `it_nand_pack_record(pack, vpn)`, with two sources:

| source | opened from | used by |
| --- | --- | --- |
| mapped | `nand.pack` (whole file) | native — still the oracle |
| chunked | `nand.pack.idx` (header + index) + a chunk fetcher | the browser |

The module deliberately has **no device-model dependencies** (osdep + glib), so
`tests/unit/test-nand-pack.c` links it directly. Four tests, all passing:
mapped output matches committed SHA-256 digests; chunked and mapped agree
byte-for-byte on every page and both return NULL on every absent page; a page
whose chunk is not resident reads as absent rather than as neighbouring data;
and malformed packs (short, bad magic, wrong version, **unsorted index**) are
rejected at open rather than misread at lookup.

The fixture is generated by `scripts/wasm/make-pack-fixture.py`
(`--check` verifies it is current) into `tests/data/nand-pack/`: 24 pages,
50,804 B, real 2,112-byte stride.

**Two traps found writing it**, both worth keeping:

- The absent-page list has to be *derived*, not guessed. VPN = `page * 8 +
  bank`, so a fixture holding page 0 of banks 0/3/7 occupies VPNs 0, 3 and 7 —
  the first hand-written "absent" list named pages the pack actually carried.
- A chunk source that returned a pointer into the same mapping would make the
  comparison vacuous, so the test's cache **copies** each chunk into its own
  buffer and asserts the two pointers differ.

**Native is unchanged and still reaches the home screen**: after the refactor,
`fb-snapshot.py --board m68ap --build 1A543a` reports `kernel_0x0f496000` at
**59.02% non-black** — the documented figure for 1.0 is 59.04%.

### Chunked delivery (B3) — the pieces

- **`scripts/wasm/chunk-pack.py`** writes the `ipod-nand-chunks-v1` asset set:
  `nand.pack.idx`, `chunk-hashes.bin` (32 B per chunk, in chunk order),
  `chunk-config.txt` (what the *emulator* reads — two integers and a string
  need no JSON parser inside a device model), `chunk-manifest.json` (what the
  *loader* reads), and `chunks/<sha256>` stored **already Brotli-compressed**.
- **Chunks are served with `Content-Encoding: br`**, so the browser
  decompresses them in the network stack and **no Brotli decoder is linked into
  the emulator**. `scripts/wasm/serve.py` does this; a CDN does it natively.
- **`hw/arm/ipod_touch_nand_chunks.c`** (EMSCRIPTEN-only) fetches a chunk with a
  **synchronous `XMLHttpRequest`** — legal on a worker thread, which is where
  `-sPROXY_TO_PTHREAD` puts the emulator — into a 64-slot LRU (~8 MiB at 62
  pages/chunk). The emulator never awaits, so QEMU's MMIO path is untouched.
- **`web/sw.js`** intercepts those requests, answers them from Cache Storage,
  prefetches the recorded boot order, and counts bytes.
- **Selection needs no configuration**: a chunked NAND directory has no
  `nand.pack` at all, so the presence of `chunk-config.txt` +
  `chunk-hashes.bin` is what selects the browser path. A native tree can never
  take it by accident.
- `serve.py` also answers **`/__chunk-stats`**, so "a cold boot downloads
  18.6 MiB, a warm boot downloads nothing" is measured by the *server* rather
  than by the page under test.

---

## Session — 2026-07-28 (later): grafting the WebAssembly JIT

TCI is ~13× slower than native and the product needs real-time speed, so the
out-of-tree WebAssembly TCG backend moved from "option" to "requirement". This
records how it was adopted, including the wrong turns.

### False start: "the JIT is on an old QEMU, so grafting means crossing the refactor"

The first assessment looked at `ktock/qemu-wasm`'s **default** branch, found
QEMU **8.2.0** with the pre-rename TCG layout (`tcg/arm`, `tcg/i386`,
`tcg/mips`), and concluded that adopting it meant porting a backend across the
QEMU 10.x TCG rework — days of expert work, possibly justifying a rewrite.

**That was wrong, and cheap to have checked.** The repository has 22 branches.
Enumerating them and reading each `VERSION` takes one command:

| branch | QEMU | backend |
| --- | --- | --- |
| `master` | 8.2.0 | `tcg/wasm32` |
| `dev-e` | 9.2.92 | `tcg/wasm32` |
| `bench64` | 10.2.50 | none (benchmark branch) |
| **`wasm64-tcg-b`** | **10.2.50** | **`tcg/wasm64`** |

`wasm64-tcg-b` is based on the development line that *became* 11.0, and its
`tcg/` layout is **identical** to ours (`aarch64 loongarch64 mips64 ppc64
riscv64 s390x sparc64 tci x86_64`). The backend is therefore written against
essentially our interface.

**Lesson: check every branch and its `VERSION` before judging a port's cost.**
Judging an upstream by its default branch nearly turned a copy job into a
rewrite.

### The right path: copy 9 files, then fix what upstream guards on TCI

The backend is `tcg/wasm64/` plus `tcg/wasm64.{c,h}` — 9 files, ~118 KB,
~4,000 lines. Everything else it needs was **already upstream in 11.0.2**:
`os-wasm.c`, `coroutine-wasm.c`, the `wasm64` cpu in `configure`, and the
emscripten cross file's `ALLOW_TABLE_GROWTH` / `addFunction` exports, which
exist for precisely this backend.

The integration work was five hooks, all the same shape — upstream guards
things on `CONFIG_TCG_INTERPRETER`, and the wasm backend needs the same
treatment because it behaves like TCI in those respects:

1. `meson.build` — delete upstream's `error('WebAssembly host requires
   --enable-tcg-interpreter')`. That check exists only because upstream has no
   wasm backend.
2. `tcg/meson.build` — build `wasm64.c` and link libffi on an emscripten host.
3. `include/tcg/helper-info.h` — libffi declarations and the `ffi_cif` field.
   Without it: `use of undeclared identifier 'ffi_cif'`.
4. `tcg/tcg.c` — four guards (`typecode_to_ffi`, the ffi layout macros, both
   `tcg_qemu_tb_exec` sites).
5. `include/tcg/tcg.h` — `tcg_qemu_tb_exec` must be declared a **function**.
   Like TCI, the backend supplies its own dispatcher rather than a pointer to a
   generated host prologue, so the pointer declaration collided with it.

### The one real integration defect: `WASM64_MEMORY64_2`

First run threw:

```
WebAssembly.Module(): BufferSource argument is empty
```

The backend's `EM_JS` glue encodes pointers and table indices differently in
Emscripten's 32-bit-address-limit mode, selected by `WASM64_MEMORY64_2`. Their
tree sets it from a meson option; our 11.0.2 handles the limit in `configure`
(`--wasm64-32bit-address-limit` → `-sMEMORY64=2`) and never passed the define.
`configure` now defines it whenever `-sMEMORY64=2` is in effect.

**That define was necessary but NOT the cause — the error was unchanged with it
applied.** Recorded rather than quietly deleted, because the wrong diagnosis is
instructive: it was inferred by reading the macro and reported as settled before
a rebuild confirmed it.

**Memory mode was not the cause either.** `-sMEMORY64=1` (the mode the backend
is developed against) fails identically. Testing that needed a browser, because
full wasm64 requires **Node v23** and this host has 22.22.3 — see "Testing
wasm64" below.

### The real cause: `tcg_out_tb_end` does not exist in QEMU 11.0.2

The backend assembles its module in `tcg_out_tb_end()` — that function writes
`h->wasm_ptr` and `h->wasm_size` into the TB header. `tcg_out_tb_end` is a
backend hook **added by the patch series**: in 10.2.50 every backend defines it
(a no-op returning 0 for the native ones), `tcg.c` forward-declares it and calls
it after relocations are resolved.

QEMU 11.0.2 has `tcg_out_tb_start` but **no `tcg_out_tb_end` anywhere** — not in
`tcg.c`, not in any backend. So the wasm backend's generator was dead code:
never called, `wasm_size` never written, and `WebAssembly.Module()` handed an
empty view. Exactly the "structural mismatch" candidate, and findable in one
grep once the right question was asked.

**Fix:** declare and call the hook in `tcg/tcg.c`, guarded by `EMSCRIPTEN`.
Upstream's series instead adds a no-op to all nine backends; scoping it to the
WebAssembly host keeps native builds — the correctness oracle — bit-identical.

**Result:** the empty-buffer error is gone. Generated modules compile,
instantiate and execute — and then trapped one layer deeper with
`RuntimeError: unreachable` raised *inside* a generated module
(`wasm://wasm/<hash>`).

### The second missing hook: `tcg_out_label_cb`

Same class of bug, found by asking the same question. The WebAssembly backend
cannot branch to an arbitrary address, so it compiles a TB into

```
loop { if (BLOCK_IDX <= 0) {…} if (BLOCK_IDX <= 1) {…} … }
unreachable          ← only reached if the loop falls through
```

and branches by assigning `BLOCK_IDX`. **`tcg_out_label_cb` is what opens a new
block at each label and records the label → block mapping.** In 10.2.50,
`tcg_out_label()` calls it; in 11.0.2 it does not exist. So no blocks were
created, branches selected an index no block matched, the body ran to
completion, and control fell out of the loop into the guard.

Rather than wait for the next crash, the remaining hooks were enumerated in one
pass — extract every `static … tcg_out_*` declaration from both trees and diff:

```sh
git show FETCH_HEAD:tcg/tcg.c | grep -oE "^static [a-z0-9_ ]+\**tcg_out_[a-z0-9_]+" …
```

`tcg_out_label_cb` was the only one left. **Two hooks total, both invisible at
compile and link time**, because a hook that is never called is just an unused
static function.

**Lesson for any future backend graft: diff the hook surface first.** A TCG
backend integrates through a set of `tcg_out_*` callbacks; a series that adds
new ones will link cleanly and fail at runtime in ways that look like codegen
bugs.

With both hooks wired, the emulator runs past the trap and drives the machine
model (`[PMU] RESUME_STATUS read`), i.e. the JIT is compiling and executing
translation blocks for real.

### Measured: the JIT is at least 13.6× faster than TCI

Same page (`jit-smoke` vs `tci-smoke`), same artifacts, same `-icount shift=1`,
same browser, **each run solo** — time to `[LCD] Merlot panel woke from sleep`:

| engine | first Merlot landmark |
| --- | --- |
| **WebAssembly JIT** | **31.4 s** (second at 206.4 s) |
| TCI | **not reached by 428 s** |

So **≥13.6×**, and the true figure is larger because TCI had still not arrived
when the run was stopped. That is the order-of-magnitude the real-time goal
needs, and it matches Infinite Mac's independent finding that qemu-wasm beats
hand-ported C emulators.

**Two methodology errors were made getting this number, both caught:**

1. **Comparing across icount settings.** The JIT run sitting at 206 s with no
   iBoot banner looked like a slowdown against "TCI reached the banner at
   12.7 s" — but that TCI figure was from a run with **no icount**, where the
   guest's delay loops finish almost instantly because virtual time tracks wall
   clock. At `shift=1` each 1 ms delay costs a full 500,000 instructions, and
   iBoot is mostly waits. Different amounts of work; not comparable.
2. **Running both engines at once.** The first A/B had the two pages in two
   tabs competing for CPU, which halves each. Re-run one at a time.

Before speculating further on the first, the obvious mechanism was checked and
ruled out: icount does **not** push the backend back onto TCI. The compile
decision in `wasm64.c` is a per-TB execution counter (`INSTANTIATE_NUM` 1500)
with no icount interaction.

**Caveat on what this measures.** Neither run reached the iBoot banner: the page
is iBoot-only and `shift=1` makes its delay loops expensive. This is a
landmark-to-landmark engine comparison, not a boot time. A boot-time number
needs the NAND and a faster clock setting, and should be taken once the JIT
completes a full boot.

### The display backend, and two QAPI traps

`ui/wasm.c` adds `-display wasm`. It does **not** draw: the emulator runs on a
pthread while the canvas lives on the main thread, so the backend publishes
`Module.qemuDisplay = {width, height, stride, ptr, generation, damage}` — `ptr`
being an address inside the wasm heap — and bumps `generation` on damage. The
page reads those pixels straight out of `HEAPU8`.

That split is the point: wasm memory is SharedArrayBuffer-backed, so the main
thread reads what the emulator thread wrote with **no copy and no postMessage
per frame**, and presentation policy stays in the page.

Two build errors worth knowing before adding a display type, both fatal rather
than advisory:

- every QAPI enum value needs a doc comment (`value 'wasm' lacks documentation`);
- those comments are limited to **70 characters** per line.

`error_report()` also needs `qemu/error-report.h`, which `ui/console.h` does not
pull in.

### Tuning the JIT: what was measured, what failed

Getting the backend to build was not the end of it. Once it ran, the browser
boot was still slow, and four experiments were needed to find out why. The
instrumented counters (`compiled / recompiled / evicted / live`, in
`tcg/wasm64.c`, reported every 16 compiles) are what made each of these a
measurement rather than a guess — **add them back first if this is ever
revisited.**

**1. Instance thrashing — hypothesis, disproved.** The obvious suspicion for
"mysteriously slow JIT" is the browser's cap on live WebAssembly instances,
with the backend evicting and recompiling in a loop. The counters said no:

```
[ 4.8s] compiled=176 recompiled=0 evicted=0 live=176/12000
[22.0s] compiled=208 recompiled=0 evicted=0 live=208/12000   ← and then nothing
```

Zero evictions, zero recompiles, **1.7% of the cap in use**.

**2. The real cause: the compile threshold.** `INSTANTIATE_NUM` was upstream's
1500 — a TB must execute 1500 times before it is compiled. That suits a
long-running Linux guest; a **boot** is thousands of moderately-warm blocks and
few very hot ones, so only 208 qualified and essentially the whole boot ran on
the forked TCI interpreter, the engine already measured at ~13× slower than
native.

Lowered to **100**, and the boot went from never reaching the kernel to:

| landmark | threshold 1500 | threshold 100 |
| --- | --- | --- |
| kernel banner | never (silent past 500 s) | **524.6 s** |
| BSD root | never | **584.7 s** |
| launchd | never | **791.0 s** (with the cap raised) |

This also qualifies the earlier "≥13.6× faster than TCI" figure: that was taken
on a smoke test dominated by iBoot polling loops, which *do* cross 1500 quickly.
It describes hot loops, not boot code.

**3. Second-chance (CLOCK) eviction — tried, worse, REVERTED (427cfb972e).**
FIFO eviction drops the oldest half of the ring, which discards by *age*: the
kernel and libc blocks hot since early boot are the oldest, while one-shot
driver-matching code compiled seconds ago survives. A reference bit per instance
should have fixed that. It did the opposite:

| policy | at first eviction | recompile rate |
| --- | --- | --- |
| FIFO | compiled=19840 recompiled=4399 | 22% |
| CLOCK | compiled=20688 recompiled=8483 | **41%** |

Most likely because skipping hot entries frees far less per sweep than FIFO's
unconditional half, so the cap is re-hit almost immediately — more sweeps, more
total evictions (12513 vs 12000), more recompiles. **That run also crashed**
with `RuntimeError: memory access out of bounds`, exactly as `live` reached the
cap, inside the rewritten eviction path; FIFO never crashed. The fault was not
found by inspection, which is a reason to keep it reverted rather than a reason
to trust it.

**4. The cap itself — the actual fix.** Hitting `MAX_INSTANCES` is far worse
than it sounds: `can_add_instance()` then returns false, so the JIT **stops
compiling entirely** and every newly-hot block runs interpreted, while reclaim
waits on a JS `FinalizationRegistry` that may not run for a long time. The
threshold-100 run saturated 12000 during driver matching and then made no
further progress for ~500 s. Raised to **48000** — it is a heuristic guard
against a browser limit, not the limit itself, so this trades resident memory
for keeping compilation alive.

**Standing advice:** treat `INSTANTIATE_NUM` and `MAX_INSTANCES` as a pair.
Lowering the threshold without headroom in the cap just moves the stall. And
note the workload split — a **boot** wants eager compilation (long cold tail),
while **app use** is a small working set hammered repeatedly and tolerates a
high threshold. One static value cannot be right for both; an adaptive
threshold, or a post-boot snapshot that skips the boot phase entirely, is the
real answer.

### Testing wasm64: use a browser, not Node

`-sMEMORY64=1` requires **Node v23** ("This emscripten-generated code requires
node v23.0.0"); browsers have supported memory64 for a while. `web/public/jit-smoke/`
is a self-contained page that boots **iBoot only, no NAND** — enough to cross
the backend's 1500-execution compile threshold in ~2 s — and reports PASS /
FAIL / INCONCLUSIVE. Serve it with `scripts/wasm/serve.py` (the COOP/COEP
headers are required) and open `/public/jit-smoke/`.

Chrome plus Playwright/Patchright are available on this host, so the same page
is the basis for an automated headless check; that is the right long-term
harness for wasm64 work regardless of the Node version.

### Two self-inflicted build failures worth not repeating

**Never regenerate a cross build outside its toolchain environment.** When
ninja reported a stale `build.ninja`, running `meson --internal regenerate` by
hand from inside `build-wasm` re-probed dependencies without
`PKG_CONFIG_PATH` pointing at the wasm sysroot. Meson found **host Homebrew**
libraries and enabled curl, zstd and libssh for a WebAssembly build:

```
-I/opt/homebrew/opt/zstd/include -I/opt/homebrew/Cellar/libssh/0.11.3/include
../block/curl.c:35:10: fatal error: 'curl/curl.h' file not found
```

`scripts/wasm/build-qemu.sh --configure` exists so that environment is always
set; use it. A correct reconfigure reports `Run-time dependency libcurl found:
NO (tried pkgconfig)`.

**A QEMU build directory symlinks `scripts/`.** Leaving the shell inside
`build-wasm` and running `./scripts/wasm/build-qemu.sh` still *resolves* — but
the script computes the repo root from its own path and concludes the root is
`build-wasm`, then reports `native toolchain missing; run
scripts/wasm/setup-toolchain.sh` even though the toolchain is present and
intact. A misleading error with an unrelated remedy; always invoke it from the
repository root.

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
