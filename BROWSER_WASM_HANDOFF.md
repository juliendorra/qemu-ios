# Browser/WebAssembly port — next session, start here

Ordered work for getting iPhone OS in a browser. Read
[`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) for what is already proven
and measured; [`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md)
is the design of record. This file is the to-do list, with the traps that cost
time the first time round.

**Goal:** a booting, usable iPhone 2G on **iPhone OS 1.1.4** in a browser —
fast to start (chunked assets) and running at **real-time speed**. Then 1.0
(once its button problems are fixed in the parallel session), then 1.0.2 and
1.1.1 as a version picker. Assets are prepared here and served from our own
origin.

Real-time speed is a stated requirement, not an aspiration: it is why the
WebAssembly JIT backend is being adopted rather than shipping TCI.

> **Split into two parallel sessions (2026-07-29).** See
> [`BROWSER_WASM_SESSION_A.md`](BROWSER_WASM_SESSION_A.md) — make it visible and
> interactive (display painting, input, post-boot snapshot) — and
> [`BROWSER_WASM_SESSION_B.md`](BROWSER_WASM_SESSION_B.md) — make it fast and
> small (JIT tuning, pack seam, chunked delivery). They own disjoint files; B
> builds with `WASM_BUILD_DIR=build-wasm-b` and serves on port 8011 so the two
> do not collide.

---

## Where things stand

- **Assets: ready for 1.0 and 1.1.4.** Both packs exist, both boot to a verified
  home screen, both measured. 1.0 costs **18.6 MiB** to first boot when chunked
  and Brotli'd (215.6 MiB raw pack). See [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md).
- **Toolchain: installed and proven.** Emscripten 4.0.10 natively, no Docker.
  zlib, libffi and pixman cross-compile to wasm64.
- **The browser build EXISTS and RUNS** (2026-07-28): `build-wasm/qemu-system-arm.wasm`,
  53 MB, boots iPhone OS 1.1.4 through iBoot-204.3.14 and the Darwin kernel
  under Node. W1 is done and W2 is measured.
- **Blocked on one guest panic** inside `IOIpodUSBDevice::start`, which native
  passes. See W2a.

---

## Standing constraints

Violating any of these silently wastes a session.

| | |
| --- | --- |
| **Disk** | ~5 GiB free needed: ~1–2 GiB glib, ~2–3 GiB the wasm build tree, ~300 MiB per staged asset set. The volume has been sitting at 100% |
| **Never assume a firmware** | every tool takes an explicit `--build`; a wrong epoch wedges iBoot with an *empty* serial log that looks exactly like a hang |
| **Boot verification is by framebuffer** | SpringBoard never announces itself on serial. Check `kernel_0x0f400000`'s `nonzero_pct` in `fb-snapshot.py`'s report; scanout reads ~0% because the panel sleeps |
| **NAND must be a writable throwaway clone** | `cp -Rc` (APFS clone, ~0 bytes) + `IT_NAND_WRITABLE=1`. Read-only, the boot stalls after launchd forever |
| **Native build is the oracle** | `build-ipod11/` must keep working; the browser is compared against it, never the reverse |

---

## W1 — Toolchain and wasm binary — DONE (2026-07-28)

`scripts/wasm/build-deps.sh glib` then `scripts/wasm/build-qemu.sh` produce
`build-wasm/qemu-system-arm.{js,wasm}`. What it took is recorded in
`BROWSER_WASM_STATUS.md`; the one structural change was **removing OpenSSL from
the device model** (it does not cross-compile to wasm64), replaced with glib's
`GChecksum` and QEMU's own `crypto/aes.h` plus a CBC helper.

Original notes follow, kept because the "expect real work here" list was
accurate.

```sh
scripts/wasm/build-deps.sh glib      # ~1-2 GiB, the only missing dependency
scripts/wasm/build-qemu.sh           # arm-softmmu, wasm64 + TCI -> build-wasm/
```

**Acceptance:** `build-wasm/qemu-system-arm.js` and `.wasm` exist.

**Expect real work here.** This is a 43-file device model that has never seen a
non-POSIX host. Likely to surface:

- `hw/arm/ipod_touch_nand.c` maps the pack with `g_mapped_file`; Emscripten's
  `mmap` over a 300 MiB MEMFS file copies it into the heap. This is the first
  memory question and it may force W3 earlier than planned.
- Everything takes host paths (`bootrom=`, `iboot=`, `nand=`) and `fopen`s them.
- `-pflash` pulls in the block layer.
- The display/UI backends: build headless (`-display none`) first; the browser
  gets a purpose-built listener in W5, not SDL.

**Trap:** do not "fix" the device model to make it build. Note what breaks, then
choose per item: real portability fix vs. browser-specific seam. A hack here
will be indistinguishable from a device bug later.

## W2 — Boot headless, and measure TCI

**This is the go/no-go for the whole approach.**

Run the wasm build under Node first (no browser, no COOP/COEP, fast iteration),
then in the browser via `scripts/wasm/serve.py`.

**Acceptance:** iBoot serial output appears, then the kernel's. Then time it to
a rendered home screen and compare against native.

**Why it decides everything:** QEMU 11.0.2 has **only TCI** — no WebAssembly TCG
backend exists in this tree or in QEMU master (checked 2026-07-27). If TCI
cannot reach a usable SpringBoard in acceptable time, the out-of-tree
[qemu-wasm](https://github.com/ktock/qemu-wasm) JIT becomes mandatory, and
carrying an out-of-tree TCG backend is a project of its own. Infinite Mac
measured qemu-wasm at 8 s on an MD5 workload against DingusPPC's 13 s — that is
evidence the *JIT* is viable, and says nothing about the interpreter.

**Re-check upstream first:** if the wasm backend has merged since, this stops
being patch-carrying and becomes a version bump.

## W2a — The remaining boot blocker, and the icount rule

**Always run the browser build with `-icount`.** Without it the guest sees its
own driver `start()` calls taking 16-22 SECONDS, because `QEMU_CLOCK_VIRTUAL`
follows wall clock while TCI runs ~13x slower than native. The kernel then takes
timeout paths no real device takes and panics.

The panic point tracks the virtual clock rate, which is how we know these are
timeouts and not bad device-model values:

| icount | ns per instruction | outcome |
| --- | --- | --- |
| none | wall clock | USB wrangler null-deref (`caller 0xC00638CC`) |
| `shift=5` | 32 (~31 MIPS) | same null-deref |
| `shift=3` | 8 (~125 MIPS) | passes it; panics in `IOIpodUSBDevice::start` (`0xC012D963`) |
| `shift=1` | 2 (~500 MHz) | closest to the real 412 MHz S5L8900; **clears BOTH panics** |

**Higher shift means a SLOWER guest**, which is the opposite of what one
reaches for instinctively. The real S5L8900 is 412 MHz ≈ 2.4 ns/instruction, so
`shift=1` is the faithful setting and `shift=3` already presents a machine ~3x
slower than the hardware the OS was written for.

The cost is wall clock: at `shift=1` the guest executes 4x more instructions per
virtual millisecond than at `shift=3`, so a boot takes correspondingly longer
under TCI. **This is another argument that the JIT is required** — with a ~10x
faster engine, the faithful clock rate becomes affordable.

`scripts/wasm/boot-test.mjs` takes `IT_ICOUNT=<shift>` and `IT_NAND_PACK=<path>`.

`shift=1` cleared the `IOIpodUSBDevice` panic: the wasm boot printed
`Registering: ../usb-device/AppleS5L8900XIpodHAL/IOIpodUSBDevice`, exactly as
native does, and carried on. **Use `shift=1`.** No device-model change was
needed for either panic — only an honest clock.

### Cross-check from the native side (2026-07-28, later the same day)

**The icount rule is not a wasm workaround — it is a correctness fix for the
emulator generally, and it fixed a user-visible native bug.**

A user reported the shipped `iPhone 2G (iOS 1.1.4).app` intermittently never
getting past the Apple logo. Reproduced on the native bundle: **1 panic in 3
boots**, at `IOIpodUSBDevice::start` — the same panic this section describes,
with no TCI anywhere. `-icount shift=1` cleared it: **0 panics in 4 boots**, and
the LCD readiness gate armed. `ipod-app-launcher.sh` now passes
`-icount shift=1` for the `iphone-2g` profile (`S5L8900_ICOUNT=0` opts out);
N45AP is left alone, since it does not hit this and its sleep/wake results were
all measured in real time.

Two things that follow, both useful here:

* **The intermittency is the proof.** Native is only *sometimes* slow enough to
  cross the driver's timeout — it depends on host load, and today's runs were
  competing with several parallel probes. A timeout theory predicts exactly
  that; a bad-device-value theory does not. So the diagnosis in this section is
  now confirmed from a second, independent direction.
* **"Real-time speed" has a number, and icount is what makes it measurable.**
  At `shift=1` one virtual second is 2^-1 ns per instruction = **5x10^8 guest
  instructions**. Real time therefore means sustaining ~500M guest instructions
  per wall-clock second. Measure the ratio (guest virtual seconds per wall
  second), not boot duration — with icount those are different quantities, and
  only the ratio answers "is it real-time". Worth checking what NATIVE achieves
  before setting the JIT's target: if native TCG is already below 1.0, the bar
  for the JIT is set by that gap, not by TCI's 13x alone.

Nothing above changes the conclusion of this section. `shift=1` remains the
faithful setting, and the wall-clock cost of it is an argument about engine
speed (TCI vs JIT), not about icount.

## W2b — The WebAssembly JIT (grafted 2026-07-28)

TCI cannot deliver real-time speed, which the goal now requires, so the
out-of-tree backend is being adopted. **Graft, do not reimplement** — and note
which branch:

`ktock/qemu-wasm` branch **`wasm64-tcg-b`** is QEMU **10.2.50**, the line that
became 11.0, and its `tcg/` layout is identical to ours. The default branch
(`master`) is 8.2.0 with the pre-rename layout and would mean crossing the
QEMU 10.x TCG rework; judging the repo by its default branch nearly turned a
copy job into a rewrite.

```sh
git fetch --depth=1 --no-tags https://github.com/ktock/qemu-wasm wasm64-tcg-b
git checkout FETCH_HEAD -- tcg/wasm64 tcg/wasm64.c tcg/wasm64.h
```

Plus five hooks, all already applied on the `wasm-jit-graft` branch: remove
upstream's "WebAssembly host requires --enable-tcg-interpreter" error, build
`wasm64.c` with libffi in `tcg/meson.build`, and extend three
`CONFIG_TCG_INTERPRETER` guards (`helper-info.h`, `tcg.c` ×4, `tcg.h`) with
`|| defined(EMSCRIPTEN)` — the backend calls helpers through libffi and supplies
its own `tcg_qemu_tb_exec` dispatcher, exactly as TCI does.

**And one define that is easy to miss:** `-DWASM64_MEMORY64_2` whenever
`-sMEMORY64=2` (`--wasm64-32bit-address-limit`) is in effect. The backend's
`EM_JS` glue encodes pointers differently in that mode; without it the first
compiled block throws `WebAssembly.Module(): BufferSource argument is empty`.
Now handled in `configure`.

`IT_WASM_TCI=1 scripts/wasm/build-qemu.sh` still builds the interpreter, into
`build-wasm-tci`, for A/B comparison.

**Two hooks the series adds that QEMU 11.0.2 does not have.** Both are invisible
at compile and link time — an uncalled hook is just an unused static function —
and both surface as runtime symptoms that look like codegen bugs:

| hook | called from | symptom when missing |
| --- | --- | --- |
| `tcg_out_tb_end` | end of `tcg_gen_code`, after relocs | `WebAssembly.Module(): BufferSource argument is empty` |
| `tcg_out_label_cb` | `tcg_out_label` | `RuntimeError: unreachable` inside a generated module |

**Diff the hook surface first** when grafting any TCG backend across versions:

```sh
git show FETCH_HEAD:tcg/tcg.c | grep -oE "^static [a-z0-9_ ]+\**tcg_out_[a-z0-9_]+"
```

Both are wired under `#ifdef EMSCRIPTEN` so native builds stay bit-identical.

### Tuning it (measured 2026-07-29)

Two constants in `tcg/wasm64.c`, and they must be tuned **as a pair**:

| constant | upstream | here | why |
| --- | --- | --- | --- |
| `INSTANTIATE_NUM` | 1500 | **100** | at 1500 only 208 blocks compiled in a whole boot; a boot is a long cold tail, not a few hot loops |
| `MAX_INSTANCES` | 12000 | **48000** | at the cap `can_add_instance()` fails and the JIT stops compiling *entirely*, waiting on a JS GC that may not run |

Keep the instrumented counters (`compiled/recompiled/evicted/live`) when
touching either — they turned every guess here into a measurement.

**Do not retry second-chance eviction without new evidence.** It was tried and
reverted (427cfb972e): the recompile rate went from 22% (FIFO) to 41%, and the
run crashed with `memory access out of bounds` at the cap, in the rewritten
path. Details in `BROWSER_WASM_STATUS.md`.

**Boot and app use want different settings.** A boot needs eager compilation; a
running app is a small working set that tolerates a high threshold. Expect an
adaptive threshold, or a post-boot snapshot that skips the boot phase, rather
than one static value.

**Status: working, and ≥13.6× faster than TCI** (hot loops; see the caveat). Same page, same artifacts,
same `-icount shift=1`, each run solo: the JIT reaches the first LCD landmark in
**31.4 s** where TCI had not reached it by **428 s**. Next: a full boot with the
NAND, then a boot-time number at a sensible clock setting.

## W3 — A pack-access seam in the NAND model — DONE (2026-07-29, Session B)

Landed as `hw/arm/ipod_touch_nand_pack.c` + `include/hw/arm/ipod_touch_nand_pack.h`:
`it_nand_pack_record(pack, vpn)` with a mapped-file source (native, the oracle)
and a chunk-backed one (browser, opened from `nand.pack.idx`). Selection needs
no configuration — a chunked NAND directory simply has no `nand.pack`.

Golden fixtures and the promised unit test exist:
`scripts/wasm/make-pack-fixture.py` writes `tests/data/nand-pack/`, and
`tests/unit/test-nand-pack.c` (4 tests) proves the two sources agree on every
page and every absent page, that a non-resident chunk reads as absent rather
than as neighbouring data, and that malformed packs are rejected at open.

```sh
ninja -C build-ipod11 tests/unit/test-nand-pack && \
  (cd build-ipod11 && G_TEST_SRCDIR=$PWD/../tests/unit ./tests/unit/test-nand-pack --tap)
scripts/wasm/make-pack-fixture.py --check     # fixture still current?
```

Original section follows.

## W3 (original) — A pack-access seam in the NAND model

`nand_read_packed_page()` in `hw/arm/ipod_touch_nand.c` binary-searches a
`g_mapped_file` and `memcpy`s straight out of it. Chunked delivery needs the
lookup to go through a function that can also be satisfied from a chunk cache.

- Native keeps the mapped-file implementation, unchanged and still the oracle.
- The browser supplies a chunk-backed one.

**Acceptance:** native boot is byte-identical in behaviour; a unit test proves
both implementations return the same page for the same VPN. **Do this before**
any frontend work depends on it, and land it with golden pack fixtures — those
are still owed.

## W4 — Chunker, service worker, prefetch — BUILT (2026-07-29, Session B)

All four pieces exist; the measured 1.0 asset set is **68.8 MiB stored total,
18.5 MiB for the boot working set** (512 of 1,724 chunks), against a 215.2 MiB
raw pack.

```sh
# 1. trace a VERIFIED home-screen boot, reduce it to the boot chunk order
scripts/wasm/analyze-nand-trace.py /tmp/boot.trace <nand.pack> \
    --pages-per-chunk 62 --prefetch /tmp/prefetch.json
# 2. build the asset set (brotli q11: ~25 min for a 215 MiB pack)
scripts/wasm/chunk-pack.py <nand.pack> --out web/chunked/<BUILD> \
    --pages-per-chunk 62 --base /chunked/<BUILD>/chunks/ \
    --prefetch /tmp/prefetch.json
# 3. boot it, cold then warm, and read the SERVER's byte count
scripts/wasm/bench-run.py --mode chunked --cold --label chunked-cold \
    --profile chunked
scripts/wasm/bench-run.py --mode chunked      --label chunked-warm \
    --profile chunked
```

**`--profile` must be shared between the cold and warm runs**, or the "warm"
run gets a fresh Cache Storage and is cold again.

**Measured (in-app browser):** cold = 503 chunk requests, **18.52 MiB** on the
wire; warm = **0 requests, 0 bytes**; the emulator's own LRU reported 11,095
hits against 320 fetches. Against 216.8 MiB for the whole-pack page.

**The synchronous read works in Chrome now** (2026-07-29): the emulator blocks
on a futex in a mailbox in its own heap, and `web/chunk-fetch-worker.js` — **a
classic worker created by the PAGE** — fetches the chunk, writes it into the
wasm heap and wakes it. Measured in standalone Chrome: kernel 113 s, BSD root
124 s, 63 demand-faulted chunks = 4.2 MB; a warm run pulls **0 bytes**. Boot
time matches the whole-pack run, so chunking costs nothing.

**Do not let the emulator create that worker.** A nested dedicated worker is
serviced through its parent's context, and this parent is blocked in
`Atomics.wait` — its own fetcher then never runs (10 s timeout, versus 5 ms
page-owned). Nor can the emulator fetch on its own thread: Chrome refuses a
synchronous XHR from a module worker, which is what `-sEXPORT_ES6` makes
Emscripten's pthreads, and `emscripten_fetch(SYNCHRONOUS)` fails the same way
with **zero bytes and no error**.

`web/bench-b/worker-selftest.html` exercises the whole handshake in about a
second (`?nested=1` to see the failing arrangement). Use it before rebuilding
the emulator for anything in this protocol.

**Both shipping versions reach the home screen in a browser** (2026-07-29):
1.0 at **252 s** on **18.57 MiB**, 1.1.4 at **296 s** on **20.97 MiB** —
`?build=1A543a|4A102`, with the security epoch carried per build. 1.1.4 needed
`fb-snapshot.py --icount 1` for its NATIVE verification: without it the boot
panics in `IOIpodUSBDevice::start` and renders nothing, which reads as a broken
NAND.

**A cold browser boot reaches the SpringBoard HOME SCREEN from chunked
assets:** first pixels 12 s, kernel 156 s, BSD root 169 s, **home screen 252 s**,
verified at **45.5% non-black** in the kernel framebuffer (59.04% natively,
~1.6% for the Apple logo). 504 chunk requests = **18.57 MiB** for a 215.6 MiB
pack, with a 97% hit rate in the emulator's chunk LRU. Warm pulls **0 bytes**.

Speed, measured rather than inferred: **32.5 s of guest time in 315 s of wall
clock — ~10% of real time** at `-icount shift=1`. That is the gap the
real-time goal has to close.

The **adaptive JIT threshold is now the default** (`adaptive=0` in
`/fw/jit-tune` pins the old static behaviour): same boot, 252 s with zero
evictions against 290 s with 72,000 evictions and 4,557 recompiles.

Three things that boot needs, and each one was a stall until it was there:

- **`overlay=ram` in `<nand>/nand-tune`.** A read-only NAND never reaches
  SpringBoard, and the file-backed writable mode is *worse* in a browser: under
  `-sPROXY_TO_PTHREAD` every MEMFS syscall is proxied to the main thread, so
  the per-read `stat()` becomes a cross-thread round trip and the boot stalls
  outright (816 blocks compiled in 181 s, no landmarks).
- **`hw/arm/ipod_touch_fb_probe.c`** — the home screen has to be seen, and
  `_it_fb_probe_addr` publishes the same three framebuffer percentages
  `fb-snapshot.py` reports, sampled on the emulator's thread.
- **`-display none` for measurement runs.** Verifying through the display
  backend changes the answer: kernel at 276 s with `-display wasm` against
  118 s without, and launchd not reached in 1,000 s.

The "service worker freezes the page on a cold run" turned out to be neither:
`keepalive`/`sendBeacon` share a **64 KiB in-flight quota per origin**, the
reports queued behind the chunk fetches, the quota filled, and the rejections
went into a `.catch(() => {})`. Use a plain `fetch()` for periodic telemetry,
and never swallow its failure.

**Use `scripts/wasm/bench-run.py`, not a tab.** A browser throttles a hidden
page, and the symptom is a run that looks *stalled* — "compiled=352" for
minutes — when it is only backgrounded. The runner launches Chrome with
`--disable-background-timer-throttling`,
`--disable-backgrounding-occluded-windows` and `--disable-renderer-backgrounding`,
and the page posts its landmarks back to `serve.py --results`, so a run killed
on a timeout still leaves data.

Original section follows.

## W4 (original) — Chunker, service worker, prefetch

Format and manifest fields are specified in the plan (`ipod-nand-chunks-v1`,
62 pages/chunk, Brotli, content-addressed, `prefetch` list).

- Build-time chunker producing deterministic output.
- Service worker serving chunks; the emulator's reads stay **synchronous** and
  the worker intercepts — this is what keeps QEMU's MMIO path intact.
- Prefetch from the recorded boot order:
  `scripts/wasm/analyze-nand-trace.py … --prefetch prefetch.json`.

**Acceptance:** a cold browser boot downloads ≈ the measured working set
(18.6 MiB for 1.0), not the whole pack; a warm boot downloads nothing.

**Measure first for each version** (1.0 and 1.1.4 are done):

```sh
cp -Rc m68ap-artifacts/builds/<BUILD>/nand /tmp/nand-clone
IT_NAND_WRITABLE=1 IT_NAND_TRACE_PAGES=/tmp/boot.trace \
  python3 scripts/fb-snapshot.py --board m68ap --build <BUILD> \
  --boot-wait 420 --nand-m68ap /tmp/nand-clone --logs /tmp/fb
scripts/wasm/analyze-nand-trace.py /tmp/boot.trace \
  m68ap-artifacts/builds/<BUILD>/nand/nand.pack --pages-per-chunk 62
```

## W4a — The last join: chunked delivery in the VISIBLE page — DONE (2026-07-29)

**The viewer streams the NAND.** 18.57 MiB cold, **0 bytes warm**, home screen at
249.5 s cold and 241.0 s warm — *faster* than the 215 MiB whole-pack path
(268 s), so chunking is not a trade-off here. Verified by bytes at
`/__chunk-stats`, and the cold figure matches Session B's independent
measurement exactly: two different pages, same seam, same bytes.

Done by following B's pasteable brief,
[`BROWSER_WASM_CHUNKED_IN_THE_VIEWER.md`](BROWSER_WASM_CHUNKED_IN_THE_VIEWER.md),
unchanged — nothing in B's files needed touching. The four load-bearing details
are in that brief; the one bug worth repeating here is that the page's own byte
readout must use **`bytesFromNetwork`**, not `bytesServed` (which counts cache
hits, so a warm boot looks like it downloaded everything) and not a `bytes`
field, which does not exist and reports zero forever through `??`.

**A first visit costs ~19 MiB and a second costs nothing. What is still missing
for an INSTANT boot is the snapshot, not the assets** — see W5a, which remains
the one unbuilt piece of the "full boot from scratch, plus resume" goal.

## W5 — Display and input bridges — DONE (2026-07-29)

**iPhone OS 1.0 reaches its home screen in a browser in 270-330 s, and launches
an app when you tap its icon.** `ui/wasm.c` plus
`web/public/jit-boot/index.html`; the full account, including what the committed
backend got wrong, is in `BROWSER_WASM_STATUS.md`.

| landmark | headless #1 | headless #2 |
| --- | --- | --- |
| first pixels | 1.0 s | 1.3 s |
| kernel | 138.0 s | 169.0 s |
| BSD root | 150.0 s | 188.0 s |
| launchd | 170.0 s | 212.0 s |
| **home screen** (45.6% non-black) | **268.4 s** | **326.7 s** |

**Measure headless, with the throttles disabled.** The same page watched in a
tab reported 1206 s — inflated ~4x by throttling alone. The page posts to
`serve.py --results` and mirrors landmarks to `console.log`; the exact command
is in `BROWSER_WASM_STATUS.md`. Two runs 22% apart is the honest spread, so
quote a range.

The shape, because it generalises to anything else that has to cross the
thread boundary:

- **Do not use `EM_JS` to reach the page.** Under `-sPROXY_TO_PTHREAD=1` the
  emulator runs on a worker whose `Module` is a *different object* from the
  page's, so an `EM_JS` assignment there is invisible. Put the data in memory
  and export an accessor; exported wasm functions are callable from any thread
  and every thread sees the same linear memory.
- **Display**: a `WasmDisplayInfo` struct (geometry, surface address, damage,
  a seqlock) read straight out of `HEAPU32`. The seqlock is not fastidiousness —
  a torn *pointer* read is an arbitrary index into the heap.
- **Input**: exported entry points only write a slot in an SPSC ring; a 15 ms
  `QEMU_CLOCK_REALTIME` timer on the emulator thread drains and dispatches,
  because QEMU's input queue expects the BQL and its owning thread.
- **`HEAPU8`/`HEAPU32` must be in `EXPORTED_RUNTIME_METHODS`**, and reading an
  unexported runtime method calls `abort()` rather than returning `undefined` —
  it kills the emulator. Reach `Module` properties through a try/catch.

**Two traps that will outlive this step:**

1. **Presses are held 800 ms of WALL time** (`MIN_PRESS_MS`). Guest time runs at
   ~2% of wall time, so a normal 100 ms tap is a few guest milliseconds — less
   than the multitouch model's own 16.7 ms motion report interval — and the
   guest never sees a finger. Measured support: a tap at a working position
   launches an app at 450 and 500 ms (3/3) and not at 30 ms (0/1), so **the
   threshold is somewhere in (30, 450] ms**. 800 ms clears that bracket with
   margin for the twofold run-to-run spread in engine speed. Narrowing it needs
   a ladder that holds the TARGET fixed — see the trap below.

   **Before sweeping input timing, know these four.** Each cost a run:

   - **Icon row 1 on 1.0 never registers a tap, at any hold** (0/10 attempts,
     30–800 ms). This is an open device-model question, written up in
     `TOUCH_INVESTIGATION.md`; the next step there is `IT_MT_TRACE=1` to see
     whether the frame is consumed. **Aim sweeps at row 2 or 3.**
   - **Vary ONE thing per rung.** The sweep gave each rung a different icon so
     that a too-short hold left SpringBoard untouched and failures stayed free
     — good for isolation, fatal for attribution. Rungs 1–4 all landed on the
     dead row 1 and rung 5 on a working row, so three runs "agreed" that rung 5
     wins and produced two confident, wrong conclusions in a row (first "the
     threshold is 450 ms", then "it is a clock, not a threshold"). A DESCENDING
     ladder is what broke the tie. If the design forces two variables,
     cross-tabulate before concluding.
   - **`lcd_update_input_ready()` refuses ALL touch** until it has seen
     `2 * LCD_REFRESH_RATE_FREQUENCY` frames of a stable OS image — two seconds
     of GUEST time, ~100 s of wall clock. Wait for `[LCD] Touch input ready`;
     the boot page mirrors it and every `[TOUCH]` verdict to the console.
   - **Never express the hold in guest milliseconds**, and **keep the ladder
     inside the ~260 s auto-lock window.** With `-icount` QEMU warps virtual
     time forward when the CPU idles, so `guestRatio` at an idle home screen is
     not a conversion factor (it produced 4 ms guest → 10 ms wall next to
     256 ms → 4113 ms); and past auto-lock every rung taps a dead panel and
     reads as a clean failure.

2. **Home from inside an app does nothing on 1.0.** That is the known event
   *routing* bug (`5f019eac7f`), not the input bridge — the bridge is proven to
   deliver and the guest is proven to service it. Do not re-investigate it from
   the browser.

**The real-time ratio is now measurable.** `ui/wasm.c` publishes
`QEMU_CLOCK_VIRTUAL` in ms; the page divides by wall time to get guest seconds
per wall second — the metric this file asks for, where 1.0 is real time. On 1.0
it is **0.015-0.022 while booting**: the guest runs at ~2% of real time. Sample
only while the CPU is BUSY — at idle QEMU warps virtual time forward and the
figure (0.036 and up) means nothing.

Still open here: partial blits (the damage rectangle and its `ack` are
published but unused — the page repaints all 320x480), and `OffscreenCanvas`.
Neither is on the critical path; one frame is 614,400 B at 10 Hz.

## W5a — Skip the boot with a snapshot (A3): worth a session, not a step

Measured 2026-07-29 with `scripts/wasm/snapshot-probe.py`, natively, on 1.0.

`migrate file:` works mechanically — `completed`, 475 ms, a **57.0 MiB** state
file for 128 MiB of guest RAM. And it restores **guest RAM byte-identically and
no device state whatsoever**: the three framebuffer bases come back unchanged
(59.03%) while the **scanout goes from 45.4% to 0.003%**, because
`w1_framebuffer_base` returns as zero.

Cause: **not one of the 26 `hw/arm/ipod_touch*.c` models defines a
`VMStateDescription`.** No LCD window bases, no PMU, no VIC, no FTL controller,
no multitouch.

The prize is worth the work — 24-57 MiB of state replaces a ~20 minute browser
boot, the same order as the 18.6 MiB chunked first-boot working set — but it is
device-model work in files Session B also touches.

- **Start with the LCD**: its loss alone makes a restore look completely dead.
  The VIC is the next suspect — a lost mask means no interrupt ever fires again.
- **Snapshot an AWAKE machine.** Left idle the guest auto-locks and the PMU
  powers the panel off, so a long `--boot-wait` measures a sleeping device at
  ~0% and the comparison is meaningless. `--no-wake` disables the Home press
  that avoids it. On a device already parked in "awaiting Power/Home", a QMP
  `send-key h` did not wake it within 6 s.

## W6 — Copy-on-write overlay

**Now a hard requirement, not a nicety.** `nand_flush_buffered_page()` calls
`hw_error()` when it cannot open a page file for writing, which aborts the
whole emulator. In a browser there is no filesystem to write to at all, so the
first guest write after the root filesystem mounts would kill the page. This
was observed for real (as a harness bug) on 2026-07-28.

Guest writes currently have nowhere to go in the browser. Keyed by
`(basePackSHA256, bank, page)`, full 2,112-byte records, batched and flushed on
program completion / pause / page-hide. Requirements and tests are in the plan.

**Trap:** do not import the native `*_new.page` semantics — those files are
incomplete program captures, and giving them read precedence makes iBoot see an
HFS signature of zero and drop into recovery.

## W7 — The version picker

`catalog.json`, per-version cache namespaces keyed by digest, honest download
sizes (the prefetch working set, not the pack size), offline warm boot, and
switching versions cheaply enough that comparing them is the point.

## W7a — Regenerate 4A102's product NAND — DONE (2026-07-30)

`scripts/build-m68ap-homescreen-nand.py --build 4A102` produced
`m68ap-artifacts/builds/4A102/nand` (600 MB). **Verified natively: 74.32%
non-black at `kernel_0x0f496000`**, against the documented 73.95% acceptance —
and against 0.0% for the `nand-prepack-not-product` tree it replaces. The
provenance now carries a full structured `recipe` (activation patch,
`LK_ENABLE_MBX2D=0`, bridge CA, `/var` from the root's own template, seeded
AddressBook), which is what makes "is this the product NAND?" answerable without
booting it.

Re-measured from the new NAND, and it matches the figures recorded from the
bundle's: **31,524 page fetches, 25,254 distinct pages = 17.0% of the pack, 605
of 2,401 chunks, 21.0 MiB first-boot download** (documented: 16.8% / 20.97 MiB).

`web/chunked/4A102` was rebuilt from it. **Note for anyone rebuilding a chunk
set: pass `--base`.** Omitting it defaults to a RELATIVE `chunks/`, where the
working 1A543a set uses `/chunked/1A543a/chunks/`; the emulator then requests
the wrong URLs and the guest starves — observed as a boot reaching the iBoot
banner and stopping, with **6 chunk requests in 9 minutes**. With the absolute
base the same boot serves 661 chunks and reaches BSD root.

### Still open: 1.1.4 does not finish booting in THIS viewer

With the verified NAND and a correct chunk base, `?build=4A102` reaches first
pixels 0.8 s, iBoot 27 s, kernel 96 s, **BSD root 114 s** — then stalls before
launchd, panel 2.2%, `guestRatio` ~0.5 (which under `-icount` means the CPU is
idle, not fast).

**What has been excluded, each by measurement:**

| candidate | how it was excluded |
| --- | --- |
| the NAND | the same tree renders **74.32%** natively |
| the chunk set | **`web/bench-b/` boots it to the home screen** — first pixels 14 s, kernel 98 s, BSD root 113 s, launchd 129 s, **home screen 214 s at 69.7%** |
| firmware images | bootrom, iBoot and NOR are **byte-identical** between the two pages (same sha256) |
| the `nand-tune` / machine line | both write `overlay=ram`; both pass the same `-M`, `-m 1G`, `-pflash`, `-icount shift=1` |
| **painting** | `?paint=0` skips the per-frame blit and it **still stalls** at BSD root |

Painting was the leading theory — under `-sPROXY_TO_PTHREAD` the main thread
both paints and services the emulator's proxied MEMFS syscalls, so a heavy blit
could starve a guest doing filesystem work, and bench-b never paints. **It is
not that.** `?paint=0` is kept; it is a useful knob and it cost one run to earn.

**Two variables remain, and they are confounded:**

1. **This page's other plumbing** — the overlay-persistence `preRun` read, the
   snapshot code paths, the serial and chunk-stats polling. bench-b has none.
2. **The engine build.** `build-wasm-b` is from **2026-07-29 21:26** and
   predates every engine change made on the 30th; `build-wasm` is current. Note
   that today's engine boots 1.1.4 **fine natively**, so if this is the cause it
   is browser-and-timing specific.

**The decisive next experiment** is to separate those: rebuild `build-wasm-b`
from current sources (`WASM_BUILD_DIR=build-wasm-b scripts/wasm/build-qemu.sh`)
and re-run bench-b on chunked 4A102.

* still reaches the home screen → the engine is fine and it is **this page**;
  bisect its plumbing by disabling the overlay read, then the pollers.
* now stalls → **today's engine regressed the browser 1.1.4 path**, and the
  suspects are the day's `hw/arm/ipod_touch.c` changes (the TVOut window is now
  lazily mapped) and the PMU `vm_stop(RUN_STATE_PAUSED)` change.

Repointing bench-b's engine symlink would have answered it in one run, but
`web/bench-b/` belongs to Session B and the change was refused — correctly.
Rebuilding their build directory is the equivalent move that stays inside the
normal workflow.

## W7a (original) — Regenerate 4A102's product NAND

`builds/4A102/nand` does not exist: what the migration filed there was built
from an unpatched root and rendered nothing, so it was renamed to
`nand-prepack-not-product`. The verified 1.1.4 NAND currently lives only inside
`/Applications/iPhone 2G (iOS 1.1.4).app`.

```sh
scripts/build-m68ap-homescreen-nand.py --build 4A102   # ~1.2 GiB of scratch
```

Then re-measure its working set, since the numbers recorded for 1.1.4 came from
the bundle's NAND.

## W8 — Package the remaining versions

1.0.2 and 1.1.1 both boot natively but are not packaged. **1.0.2's `root.img`
is now staged**; 1.1.1 still needs its IPSW decrypted. The per-build constants (root DMG name, VFDecrypt key,
epoch, FIL signature) are already in `scripts/firmware_profiles.py`.

```sh
# 1. extract the images from the IPSW
python3 scripts/extract-m68ap-images.py --build 1C28 <ipsw-dir> <out-dir>
# 2. decrypt the root filesystem -- DMG name, key and output all come from
#    the profile; add --dry-run to see what it resolved before spending disk
scripts/decrypt-m68ap-rootfs.sh --build 1C28
# 3. NOR + secure-boot-patched iBoot
python3 scripts/build-m68ap-nor.py     --build 1C28 …
python3 scripts/patch-m68ap-iboot.py   …
# 4. the product NAND + pack
python3 scripts/build-m68ap-homescreen-nand.py --build 1C28
# 5. verify it reaches the home screen
python3 scripts/fb-snapshot.py --board m68ap --build 1C28 \
    --boot-wait 420 --logs /tmp/fb-1C28
```

**Disk:** step 2 needs roughly 700 MiB of scratch (the encrypted DMG, the UDIF
output, the raw conversion, and the final image). Step 4 needs ~1.2 GiB.

**Step 2 is done for 1C28** (2026-07-27) and the reworked script is verified
end to end: it picked 1.0.2's own key from the profile, extracted
`694-5298-5.dmg` from the IPSW itself, and produced a 193,699,840-byte HFS+
volume that mounts and reports `ProductVersion 1.0.2 / 1C28`. So 1.0.2 now needs
only steps 3–5.

---

## Decisions still open

| Question | How to close it |
| --- | --- |
| **Is TCI fast enough?** | W2. Decides whether the out-of-tree JIT is mandatory |
| How the pack is reached from the wasm heap | measure MEMFS+mmap vs Blob vs chunk cache during W1/W3 |
| Does 62 pages/chunk survive real latency? | the measured table optimises bytes, not round trips — re-measure over the network in W4 |
| How much chunk content the versions share | `scripts/wasm/measure-pack.py a.pack b.pack --cross`, once more packs exist |
| `OffscreenCanvas` default or optimisation? | W5 profiling |
| Hosting origin, cache headers, IPFS later? | product decision, not blocking |

## Tools

| | |
| --- | --- |
| `scripts/wasm/setup-toolchain.sh` | pinned emsdk + meson (done) |
| `scripts/wasm/build-deps.sh` | wasm64 zlib/libffi/pixman/glib |
| `scripts/wasm/build-qemu.sh` | arm-softmmu for wasm64 + TCI |
| `scripts/wasm/stage-assets.py` | asset set + hashed manifest |
| `scripts/wasm/serve.py` | COOP/COEP dev server; `--check` verifies headers |
| `scripts/wasm/measure-pack.py` | pack size, compression, dedup, cross-version sharing |
| `scripts/wasm/analyze-nand-trace.py` | cold-boot working set + prefetch list |
| `IT_NAND_TRACE_PAGES=<path>` | records page fetches (`hw/arm/ipod_touch_nand.c`) |

## Triage rule

**Every wasm-only failure so far has had a cause outside the wasm build**: the
virtual clock (twice), a wrong NAND at the canonical path, and a missing
directory in the test harness. No defect has yet been found in the device
model, in QEMU's Emscripten support, or in TCI's correctness. Suspect
environment, clock, and harness before suspecting the port.

## Do not repeat these

- **Don't grep serial for "SpringBoard"** to decide a boot worked — it is never
  printed. Two runs were wasted on this.
- **Don't boot with a read-only NAND** and conclude the firmware hangs.
- **Don't sample the pack at raw byte offsets** when measuring compression or
  dedup: chunks are page-aligned, and byte-aligned windows understated dedup by
  3× (6% vs the real 16.4%).
- **Don't treat `libffi`/`ASYNCIFY_IMPORTS=ffi_call_js` as JIT plumbing** — it is
  TCI's own helper-call path (`tcg/tci.c`).
- **Don't run the wasm build without `-icount shift=1`** — the guest takes
  timeout paths and panics, and the panic moves as you change the shift.
- **Don't stage a NAND without its `bank0..bank7` directories.** Even a packed,
  read-only NAND needs them: `nand_flush_buffered_page()` opens
  `<nand>/bank<N>/<page>_new.page` for writing on every guest page write and
  `hw_error()`s — killing the emulator — if the directory is missing. This
  aborted a run seconds after it mounted the root filesystem.
- **Don't trust `builds/<BUILD>/nand` without checking its provenance
  `recipe` field.** A tree built from an unpatched root boots and renders
  nothing; only `"recipe": "home-screen"` is the product NAND.
- **Don't use `EM_JS` to publish anything to the page.** With
  `-sPROXY_TO_PTHREAD=1` the emulator runs on a worker whose `Module` is a
  different JavaScript object from the page's, so the assignment is invisible.
  Export a C accessor and read shared memory instead. The committed display
  backend did this and could never have worked; nobody noticed because the boot
  page ran `-display none`.
- **Don't read a `Module` property you have not exported.** `HEAPU8`,
  `HEAPU32`, `wasmMemory` and `wasmExports` are stubs that call `abort()` —
  not `undefined` — so a missing display feature takes the whole emulator down.
- **Don't expect a cross file's `[built-in options]` to be re-read on rebuild.**
  Meson reads them only at configure time, so editing
  `configs/meson/emscripten.txt` and running a build is a silent no-op. Use
  `build-qemu.sh --configure`.
- **Don't assume a silent browser boot is still working.** One run wedged with
  guest virtual time not advancing at all (`guestRatio` 0.0000) ~1170 s in, past
  BSD root but never launchd; a rerun booted cleanly in 403 s. It is
  intermittent, and only a heartbeat distinguishes it from slow progress — the
  boot page logs one every 15 s.
- **Don't aim a browser input test at icon row 1 on 1.0.** It never registers,
  at any press duration (0/10). Open item in `TOUCH_INVESTIGATION.md`.
- **Don't send a normal-length tap.** Guest time runs far slower than wall clock
  in the browser: a ~100 ms click arrives as a down and an up at the *same guest
  timestamp* and the guest sees no touch at all. Hold ~500 ms.
- **Don't run `meson`, `ninja` or `configure` on the wasm build by hand.** A
  reconfigure outside the toolchain environment re-probes dependencies without
  the wasm sysroot's `PKG_CONFIG_PATH`, finds **host Homebrew** libraries, and
  enables curl/zstd/libssh for a WebAssembly build — which then fails on
  `curl/curl.h`. Always go through `scripts/wasm/build-qemu.sh [--configure]`.
  A correct reconfigure says `libcurl found: NO (tried pkgconfig)`.

  **But this advice is not sufficient on its own, and 2026-07-29 proved it:**
  ninja re-runs `meson --internal regenerate` BY ITSELF whenever any
  `meson.build` changes, which escapes `emconfigure` and did exactly this
  without anyone running meson by hand. The fix was `PKG_CONFIG_LIBDIR` (which
  *replaces* pkg-config's search path) rather than `PKG_CONFIG_PATH` (which
  only prepends to it) in `build-qemu.sh`. With two sessions in one tree,
  `meson.build` changes constantly.
- **Don't invoke the build scripts from inside `build-wasm`.** QEMU build
  directories symlink `scripts/`, so the command resolves but computes the repo
  root as the build directory and reports `native toolchain missing` — a
  misleading error pointing at an unrelated remedy. Run from the repo root.
- **Don't fetch synchronously from the emulator's own thread.** Chrome refuses a
  synchronous XHR from an Emscripten pthread (`-sEXPORT_ES6` makes those module
  workers), and `emscripten_fetch(SYNCHRONOUS)` fails the same way with **zero
  bytes and no error**. The emulator blocks on a futex; a PAGE-OWNED classic
  worker does the fetching.
- **Don't let the emulator create that worker.** A nested dedicated worker is
  serviced through its parent's context, so a parent blocked in `Atomics.wait`
  stalls its own fetcher (10 s timeout, versus 5 ms page-owned).
- **Don't debug a worker protocol through the emulator.** `console.error` from a
  pthread worker reaches nothing the page can read, and each guess costs a
  5-minute rebuild plus a 2-minute boot.
  `web/bench-b/worker-selftest.html` exercises the same handshake in a second.
- **Don't make the NAND writable via files in a browser.** Every MEMFS syscall is
  proxied to the main thread under `-sPROXY_TO_PTHREAD`, so the per-read
  `stat()` becomes a cross-thread round trip and the boot stalls outright. Use
  `overlay=ram` in `<nand>/nand-tune`.
- **Don't verify a boot through the display backend.** `-display wasm` costs
  enough to change the answer: kernel at 276 s with it, 118 s without, and
  launchd not reached in 1,000 s. Use `hw/arm/ipod_touch_fb_probe.c` and
  `-display none`.
- **Don't use `keepalive`/`sendBeacon` for periodic telemetry.** They share a
  64 KiB in-flight quota per origin; under congestion every later report is
  rejected, and a swallowed `.catch` turns that into "the page froze".
- **Don't run `fb-snapshot.py` on M68AP without `--icount 1`.** 1.1.4 panics in
  `IOIpodUSBDevice::start` and renders nothing, which reads exactly like a
  broken NAND. The flag exists now; the default is still off for N45AP's sake.
- **Don't leave a killed run's server behind.** It keeps the port, the next run's
  bind loses silently, and results land in the previous run's file — and both
  sessions' pages post to the same endpoint. `bench-run.py` refuses a busy port
  and `serve.py --results-label` filters foreign posts.
- **Don't measure a browser boot in a hidden tab.** Chrome throttles
  backgrounded and occluded pages, and a throttled run looks exactly like a
  stalled one — JIT counters freeze mid-boot and nothing else says why. Use
  `scripts/wasm/bench-run.py`, which disables the three throttles.
- **Don't judge an upstream by its default branch.** `ktock/qemu-wasm`'s master
  is QEMU 8.2.0; the branch we needed (`wasm64-tcg-b`) is 10.2.50 with our exact
  TCG layout. Enumerate branches and read each `VERSION` before estimating a
  port.
- **Don't expect byte-reproducible packs across rebuilds**: `hdiutil` stamps
  timestamps into the HFS images, so `hfs_sha256` changes even when the recipe
  does not. Reproducibility is at the level of the recipe.
