# Browser/WebAssembly port — next session, start here

Ordered work for getting iPhone OS in a browser. Read
[`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) for what is already proven
and measured; [`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md)
is the design of record. This file is the to-do list, with the traps that cost
time the first time round.

**Goal:** iPhone 2G, iPhone OS 1.0 first, then 1.0.2 / 1.1.1 / 1.1.4 as a
version picker, served as prepared chunked assets from our own origin.

---

## Where things stand

- **Assets: ready for 1.0 and 1.1.4.** Both packs exist, both boot to a verified
  home screen, both measured. 1.0 costs **18.6 MiB** to first boot when chunked
  and Brotli'd (215.6 MiB raw pack). See [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md).
- **Toolchain: installed and proven.** Emscripten 4.0.10 natively, no Docker.
  zlib, libffi and pixman cross-compile to wasm64.
- **Blocked:** glib did not finish (host disk full), so **no browser build of
  QEMU has ever been produced**. Everything after W1 is unstarted.

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

## W1 — Finish the toolchain and get a wasm binary

**Blocked on disk only.** Nothing here is known to be hard; it has simply never
run to completion.

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

## W8 — Package the remaining versions

1.0.2 and 1.1.1 both boot natively but have no staged `root.img`, so each needs
its IPSW decrypted first. The per-build constants (root DMG name, VFDecrypt key,
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

**Unverified:** `decrypt-m68ap-rootfs.sh --build` was reworked on 2026-07-27 and
its resolution path is tested for all four builds (`--dry-run`), but no
end-to-end decrypt has been run since the change — the host disk was full. The
decrypt/convert/slice pipeline itself was not modified. Running step 2 for 1C28
is both the next packaging task and the test.

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

## Do not repeat these

- **Don't grep serial for "SpringBoard"** to decide a boot worked — it is never
  printed. Two runs were wasted on this.
- **Don't boot with a read-only NAND** and conclude the firmware hangs.
- **Don't sample the pack at raw byte offsets** when measuring compression or
  dedup: chunks are page-aligned, and byte-aligned windows understated dedup by
  3× (6% vs the real 16.4%).
- **Don't treat `libffi`/`ASYNCIFY_IMPORTS=ffi_call_js` as JIT plumbing** — it is
  TCI's own helper-call path (`tcg/tci.c`).
- **Don't expect byte-reproducible packs across rebuilds**: `hdiutil` stamps
  timestamps into the HFS images, so `hfs_sha256` changes even when the recipe
  does not. Reproducibility is at the level of the recipe.
