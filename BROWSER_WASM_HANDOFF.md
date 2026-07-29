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

## W3 — A pack-access seam in the NAND model

`nand_read_packed_page()` in `hw/arm/ipod_touch_nand.c` binary-searches a
`g_mapped_file` and `memcpy`s straight out of it. Chunked delivery needs the
lookup to go through a function that can also be satisfied from a chunk cache.

- Native keeps the mapped-file implementation, unchanged and still the oracle.
- The browser supplies a chunk-backed one.

**Acceptance:** native boot is byte-identical in behaviour; a unit test proves
both implementations return the same page for the same VPN. **Do this before**
any frontend work depends on it, and land it with golden pack fixtures — those
are still owed.

## W4 — Chunker, service worker, prefetch

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

## W5 — Display and input bridges

Nothing produces frames or consumes input yet; `web/src/workers/emulator-worker.js`
has the message protocol but no producer.

**Display.** The LCD is a normal QEMU graphic console:
`hw/arm/ipod_touch_lcd.c` registers `GraphicHwOps s5l8900_gfx_ops`, resizes with
`qemu_console_resize(con, 320, 480)` and calls `dpy_gfx_update()` on dirty
lines. So the browser needs a **DisplayChangeListener**, not SDL: take the
surface from `qemu_console_surface()`, push dirty rectangles to an
`OffscreenCanvas` in the worker. One full frame is 614,400 B; the panel refreshes
at 10 Hz, so even whole-frame updates are affordable — dirty rects and a reused
buffer are still the target.

**Input.** Two separate paths, both already present natively:

- **Touch** — `qemu_add_mouse_event_handler(ipod_touch_lcd_mouse_event, …, 1, …)`
  in `ipod_touch_lcd.c`; absolute coordinates. The frontend already normalises
  pointer events into the panel's 320×480 space.
- **Home / Power** — key events through `ipod_touch_input_event()` in
  `hw/arm/ipod_touch.c`, which maps `Q_KEY_CODE_H`/`Q_KEY_CODE_P` to guest
  keycodes. The web shell already emits these as `home`/`power` and binds `H`/`P`.

**Acceptance:** tap an icon and it launches; Home returns to SpringBoard; Power
sleeps and wakes. Compare against the native oracle, which is what the still-owed
native regression baseline is for.

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

## W7a — Regenerate 4A102's product NAND

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
- **Don't run `meson`, `ninja` or `configure` on the wasm build by hand.** A
  reconfigure outside the toolchain environment re-probes dependencies without
  the wasm sysroot's `PKG_CONFIG_PATH`, finds **host Homebrew** libraries, and
  enables curl/zstd/libssh for a WebAssembly build — which then fails on
  `curl/curl.h`. Always go through `scripts/wasm/build-qemu.sh [--configure]`.
  A correct reconfigure says `libcurl found: NO (tried pkgconfig)`.
- **Don't invoke the build scripts from inside `build-wasm`.** QEMU build
  directories symlink `scripts/`, so the command resolves but computes the repo
  root as the build directory and reports `native toolchain missing` — a
  misleading error pointing at an unrelated remedy. Run from the repo root.
- **Don't judge an upstream by its default branch.** `ktock/qemu-wasm`'s master
  is QEMU 8.2.0; the branch we needed (`wasm64-tcg-b`) is 10.2.50 with our exact
  TCG layout. Enumerate branches and read each `VERSION` before estimating a
  port.
- **Don't expect byte-reproducible packs across rebuilds**: `hdiutil` stamps
  timestamps into the HFS images, so `hfs_sha256` changes even when the recipe
  does not. Reproducibility is at the level of the recipe.
