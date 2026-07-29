# Session B — make it fast, and make it download small

One of two parallel browser-port sessions. **Session A**
(`BROWSER_WASM_SESSION_A.md`) makes the emulator visible and interactive at the
same time; the two touch different files on purpose.

Read first: [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md) for the ordered
plan and the traps, [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) for what
is proven, every measurement, and every dead end so far.

**Goal: a cold browser boot that downloads ~18 MiB instead of 216, and runs
fast enough to be worth watching.**

---

## You own these files

```
tcg/wasm64.c, tcg/wasm64/       the WebAssembly TCG backend and its tuning
hw/arm/ipod_touch_nand.c        the pack-access seam
scripts/wasm/                   chunker, measurement tools
web/src/emulator/, service worker   the chunk loader
```

Do **not** touch `ui/wasm.c` or `web/public/*/index.html` — Session A has them.

## Avoiding collisions with Session A

| | |
| --- | --- |
| Build directory | **use `WASM_BUILD_DIR=build-wasm-b`** — A owns the default `build-wasm/` |
| Dev server | `scripts/wasm/serve.py --port 8011` — A owns 8010 |
| Native build | `build-ipod11/` is the shared correctness oracle — keep it green |

```sh
WASM_BUILD_DIR=build-wasm-b IT_WASM_MEMORY64_FULL=1 scripts/wasm/build-qemu.sh
```

---

## B1 — Finish the JIT tuning

Two constants in `tcg/wasm64.c`, and they must be tuned **as a pair**. The
instrumented counters (`compiled / recompiled / evicted / live`, printed every
16 compiles) are what turn this into a measurement — keep them.

| constant | upstream | now | why |
| --- | --- | --- | --- |
| `INSTANTIATE_NUM` | 1500 | 100 | at 1500 only **208 blocks** compiled in an entire boot |
| `MAX_INSTANCES` | 12000 | 48000 | at the cap the JIT stops compiling **entirely** |

What is already known, so you do not repeat it:

- **Thrashing was disproved** at 1500 — zero evictions, 1.7% of the cap.
- **The threshold was the blocker.** At 100 the boot first reached kernel
  (524.6 s), BSD root (584.7 s) and launchd (791.0 s).
- **Second-chance (CLOCK) eviction was tried and reverted** (427cfb972e). The
  recompile rate went from 22% to **41%**, and the run crashed with
  `memory access out of bounds` at the cap, inside the rewritten path. Do not
  retry it without new evidence.
- **Boot and app use want different settings**: a boot is a long cold tail, a
  running app is a small hot working set. One static value cannot be right for
  both — an adaptive threshold is the interesting experiment.

Sweep 50 / 100 / 300 with the counters and landmark timings, on an otherwise
idle machine. **Run one engine at a time**: an early A/B ran two browser tabs
concurrently and halved the CPU for each, which invalidated it.

## B2 — The pack-access seam

`nand_read_packed_page()` in `hw/arm/ipod_touch_nand.c` binary-searches a
`g_mapped_file` and `memcpy`s straight out of it. Chunked delivery needs that
lookup behind a function a chunk cache can also satisfy.

- Native keeps the mapped-file implementation and stays the oracle.
- The browser supplies a chunk-backed one.
- Land it with **golden pack fixtures** — still owed, and this is the moment.

**Acceptance:** native boot behaviour unchanged; a test proves both
implementations return the same bytes for the same virtual page number.

## B3 — Chunker, service worker, prefetch

Format is specified in the plan: `ipod-nand-chunks-v1`, **62 pages/chunk**
(130,944 B — chosen from our own measurements, not inherited), Brotli,
content-addressed.

Measured for 1.0 (`1A543a`), so you have the target:

| | |
| --- | --- |
| pack | 215.6 MiB, 106,858 pages |
| cold boot touches | 20,397 pages = **19.1%** |
| **first boot, chunked + brotli** | **18.6 MiB** |
| whole pack, brotli | 63.8 MiB |

Tools exist: `scripts/wasm/measure-pack.py` (compression, dedup, `--cross` for
sharing between versions) and `scripts/wasm/analyze-nand-trace.py`
(`--prefetch` emits the boot chunk order). Traces come from
`IT_NAND_TRACE_PAGES` on a **verified** home-screen boot.

The service worker must keep the emulator's reads **synchronous** — it
intercepts, the emulator never awaits. That is what preserves QEMU's MMIO path.

**Acceptance:** a cold browser boot downloads ≈18.6 MiB for 1.0, not 216; a
warm boot downloads nothing.

---

## Standing constraints (from the whole port)

- **Always `-icount shift=1`.** Anything slower makes the guest take timeout
  paths and panic — reproduced natively too, not a browser artifact.
- **The NAND needs its `bank0..bank7` directories** even when packed and
  read-only, or the first guest write calls `hw_error()` and kills the
  emulator. This is also why W6 (the copy-on-write overlay) is a hard
  requirement, not a nicety: in a browser there is no filesystem to write to.
- **Verify by framebuffer, not serial.** SpringBoard never announces itself,
  and 1.0's iBoot-159 prints nothing before the kernel — a silent run is not a
  hung one.
- **Never run `meson`/`ninja` on the wasm build by hand** — outside the
  toolchain environment it re-detects host libraries and enables curl/zstd for a
  WebAssembly build. Always go through `scripts/wasm/build-qemu.sh`.
- **Disk is tight** (~2.8 GiB free). A NAND rebuild needs ~1.2 GiB.
