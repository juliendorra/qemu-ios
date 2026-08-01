# The browser speed campaign — full record (2026-07-31 → 08-01)

Everything measured, fixed, refuted and learned while making the WebAssembly
port fast, in one place. [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md)
carries the dated session summaries; this file is the detailed version —
including the dead ends, because three of them cost hours and would cost them
again.

> **Browser support: Chrome-family only.** Safari crashes mid-boot; see
> §8. The viewer shows a banner on JSC.

---

## 1. What "fast" means here, precisely

The machine runs under **`-icount shift=1`**, which is not a tuning knob but a
correctness requirement (three independent confirmations; without it the guest
takes timeout paths and panics in `IOIpodUSBDevice::start`). At `shift=1` one
virtual second is **5×10⁸ guest instructions** — a faithful ~500 MHz against
the real S5L8900's 412 MHz.

That gives an exact definition of the goal:

```
guestRatio = guest seconds elapsed / wall seconds elapsed
real-time  = guestRatio 1.0 under load
```

**Boot duration and guest speed are different quantities under icount** — a
halted vCPU warps virtual time forward, so an idle machine trivially shows
`guestRatio` ~1 while a busy one shows the truth. Always quote the *busy*
ratio. The page samples `guest_ms` from a 15 ms real-time timer precisely so
the sampling rate does not depend on what is being measured.

Starting point (2026-07-31 morning): **~0.06 busy, i.e. ~6% of real time.**

---

## 2. The measurement toolchain (built during the campaign)

None of this existed at the start. Each tool answers a question the previous
one could not.

| tool | question it answers | how |
| --- | --- | --- |
| bench landmarks | when did the guest reach iBoot/kernel/BSD/launchd/home screen | serial + framebuffer, printed by the page |
| busy `guestRatio` | are we real-time *while working* | `guest_ms` delta ÷ wall delta, 15 ms timer |
| `?display=none` | what does the display path cost | page A/B switch (measurement only — resume/touch need the drain timer) |
| **QSP** (`?qsp=1`) | **which lock, from which callsite** | QEMU's own sync profiler, driven without a monitor via a staged `/fw/qsp` file; report every 10 s |
| `[MLOOP]` counter | is the main loop spinning | one line per 10 s: iterations, naps, ready fds |
| **V8 `--prof`** | **where CPU time goes, by function name** | `--js-flags=--prof`, then `node --prof-process` |
| `scripts/wasm/profile-run.mjs` | same, via CDP, dependency-free | attaches to every worker; **cannot collect from busy pthreads** (§7) |
| fps + blit readout | is the panel actually delivering frames, at what cost | measured EWMA in the page's stats line |
| **`?sweep=calc`** | **tap → app usable, in wall AND guest seconds** | the app-launch benchmark; 60 ms poll resolution |

### Running them

```bash
# landmarks, solo, headless (always solo — see §9)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir=/tmp/run1/profile --headless=new --enable-logging=stderr --v=0 \
  --disable-background-timer-throttling --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  "http://localhost:8031/public/jit-boot/" 2> /tmp/run1/chrome.log
```

```bash
node scripts/wasm/profile-run.mjs --url 'http://localhost:8031/public/jit-boot/' --warmup 45 --duration 45 --out /tmp/prof
```

Lock profile: append `?qsp=1`. Launch benchmark: `?resume=1&sweep=calc`.
Display A/B: `?display=none`.

---

## 3. Fixes that landed, with mechanism and numbers

### 3.1 The second-visit black screen — a `preRun` race (commit `ed29c7ba00`)

**Symptom:** `?resume=1` worked cold, failed on every warm load with
`-incoming file:/fw/state: Could not open '/fw/state'`, exit status 1, black
panel for ever.

**Mechanism:** Emscripten **does not await an async `preRun`** — `run()`
proceeds at the first `await`. All asset staging therefore raced `main()`, and
the race was decided by wasm compile time: cold compile is slow and staging
wins (snapshot staged at 1.4 s); warm cache starts the engine in 0.7 s and
staging loses.

**Fix:** `addRunDependency('stage-assets')` before the first `await`, released
after staging, with the failure path reporting instead of releasing the gate
over a half-staged filesystem.

**Result:** two consecutive warm reloads → home screen at 0.7 s and 1.7 s.
This would have hit *every* visitor's second page load on a deployment.

### 3.2 Zero-copy scanout (commit `ed29c7ba00`)

**What was wrong:** the backend registered a `DisplayChangeListener`, so the
console machinery drove the LCD model's surface path. Three costs, none of
which the page needed:

1. `framebuffer_update_memory_section()` enables **`DIRTY_MEMORY_VGA` logging**
   on the framebuffer pages → every guest STORE to them leaves the TCG fast
   path. This is the expensive one, and it is invisible to any display-side
   profile because it is paid *inside generated code*.
2. `framebuffer_update_display()` syncs and walks the dirty bitmap per refresh.
3. `draw_line32_32()` converts BGRX into a surface nobody reads — its output
   bytes equal its input; the page swizzles straight out of `HEAPU32`.

**Fix:** no DCL at all. The LCD model exports `it_lcd_scanout_pa()` (guest PA,
or 0 while the panel is off); `ui/wasm.c` maps it once per base flip
(`cpu_physical_memory_map`) and republishes the same `WasmDisplayInfo` seqlock
struct. The page is unchanged.

**Result:** cold-boot kernel **276 s → 138 s**, and the `?display=none` control
on the same engine read 153 s — i.e. the display cost went from ~2× to *below
the display-off floor within variance*.

**Deliberate behaviour change:** panel-off no longer blanks the canvas (the old
path memset the surface); the page keeps the last frame. Blanking from here
would mean writing 600 KiB into guest-visible memory, which zero-copy exists to
avoid. `IT_FB_TRACE`'s "present" line went away with the DCL; the vsync line
remains.

### 3.3 Frame delivery at the panel's true rate (commits `3b451596f2`, `d2b81ff002`)

Shipped first at 10 Hz, which was invisible only because a guest at ~6% of real
time produces ~4 frames a wall second anyway — it was an animation ceiling
waiting for the engine to get fast enough to hit it.

Now: **the emulator offers every frame** (one seq bump per 15 ms drain tick,
~66 Hz measured at the seqlock) and **the page accepts on
`requestAnimationFrame`** — the browser's vsync, ~60 Hz, which is the real
panel's rate. A 30 Hz cap survives as an opt-in checkbox for weak hosts.

**Do not rate-limit in the publisher "to 60":** the 15 ms tick grid aliases a
16.7 ms limit down to ~33 Hz.

The stats line shows measured accepted fps and the measured blit cost
(EWMA, 1.4–11 ms depending on host load) so the claim "capping saves CPU"
ships with its own evidence, and a silent regression is visible.

### 3.4 The main loop never slept — the BQL fix (commit `be964328c4`)

**The biggest single win of the campaign.**

QSP named the wait to the line: the vCPU lost **47% of wall time** on the BQL at
`accel/tcg/cputlb.c:1983` (the MMIO path), **65.8k acquisitions/s**, average
wait **7.17 µs**. The `[MLOOP]` histogram then explained why: the main loop was
iterating **~45,000 times per second**, 93% of those iterations handed a
timeout ≥ 1 ms and returning instantly with nothing ready (~3 genuinely ready
fds per second).

**Mechanism: Emscripten's `poll()` does not block** on the fds the QEMU main
loop watches (in-memory pipe/eventfd emulation). The loop degenerated into a
spin that took the BQL on every lap, starving the vCPU.

**Fix** (`util/main-loop.c`, EMSCRIPTEN-only): when the poll returns
empty-handed with time left on the clock, **nap** —
`g_usleep(min(timeout, 2 ms))`, which on an Emscripten pthread is the real
futex wait `poll()` failed to be. Timer deadlines are already inside `timeout`
(`qemu_soonest_timeout` upstream of it); a cross-thread `qemu_notify_event`
lands at worst one nap late. The `[MLOOP]` counter stays as a canary.

| | before | after |
| --- | --- | --- |
| main-loop iterations | 45,000/s | **475/s** |
| vCPU wall time lost to BQL | 47% | **~8%** |
| MMIO lock acquisitions | 65.8k/s | 215–245k/s (**3.3–3.7×**) |
| average lock wait | 7.17 µs | **0.37 µs** |
| cold boot → kernel | 138 s | **84 s** |
| cold boot → home screen | 232.6 s | **152 s** |

### 3.5 The 64 KiB stack meets the TB compiler (commit `a41d4f14d7`)

**Symptom:** launching Calculator killed the vCPU worker —
`RangeError: Maximum call stack size exceeded` — and the machine became
unrecoverable (touch ignored, `[MLOOP] ready=0` for ever, because nothing
notifies a main loop whose vCPU is gone).

**Diagnosis** — the name section (§7) turned the stack into names:

```
rr_cpu_thread_fn → tcg_cpu_exec → cpu_exec → cpu_exec_setjmp   (setjmp #1)
→ cpu_exec_loop → tb_gen_code → setjmp_gen_code                (setjmp #2)
→ tcg_gen_code → tcg_optimize   ← overflow
```

Two setjmp scopes is *normal* nesting — this was not runaway recursion. The
overflow is inside `tcg_optimize` while **compiling** a TB. Emscripten pthreads
default to a **~64 KiB stack**, the vCPU thread compiles TBs on its own stack,
and native QEMU threads get MiB-scale stacks. A large-enough Calculator basic
block was simply the first to exceed 64 KiB; the campaign's speedups only found
it sooner.

**Fix:** `-sSTACK_SIZE=8MB` + `-sDEFAULT_PTHREAD_STACK_SIZE=4MB` in
`configs/meson/emscripten.txt` (~48 MiB of a 2 GiB heap across all threads).

**Verified:** same scenario rode the compile burst through 6,480+ blocks where
it had died at 5,328. This retires a class of crashes — any guest code with
large basic blocks (Safari, YouTube are candidates) would have hit it.

---

## 4. Where the time goes now (V8 `--prof`, vCPU isolate)

**Before the BQL fix** (65,674 ticks, cold boot 45–110 s):

| share | what |
| --- | --- |
| ~21% | executing generated TBs |
| ~20% | **blocked** — `__pthread_mutex_lock → futex_wait` (BQL) |
| ~13% | `emscripten_longjmp` under `cpu_loop_exit` |
| ~13% | dispatch: `tcg_qemu_tb_exec` + `helper_lookup_tb_ptr` + tree lookups |
| ~5.5% | MMU/MMIO slow paths |
| ~5% | JS glue / builtins |
| ~1.8% | `cpu_io_recompile` (icount artifact) |

**After** (prof6, 78k ticks; C++ share fell 38% → 23% as the futex collapsed):

| share | what |
| --- | --- |
| ~25% | executing generated TBs |
| **~15%** | **dispatch** — `tcg_qemu_tb_exec` **8.5%** (hottest named function), `helper_lookup_tb_ptr` 4.3%, `g_tree_lookup`/`tb_tc_cmp` ~2.4% |
| ~6% | MMU/MMIO (`do_ld4_mmu`, `mmu_lookup`, `memory_region_dispatch_read`) |
| ~5.6% | JS glue — **including `MapPrototypeSet`/`ArrayFrom` builtins: a JS Map operation is on the per-TB path** (see the EM_JS glue near `instantiate_wasm` in `tcg/wasm64.c`) |
| ~2.4% | `cpu_io_recompile` |

**Only about a quarter of the busy vCPU thread executes guest code.** The
dispatcher is now the top lever.

---

## 5. Dead ends and negative results (do not re-run without new evidence)

### 5.1 `-sSUPPORT_LONGJMP=wasm` — impossible, toolchain-level

QEMU's CPU loop unwinds via `setjmp`/`longjmp`; under Emscripten's default that
is a **JS `throw` across the wasm/JS boundary** through `invoke_*` trampolines,
measured at **~13% of the vCPU thread**. `-sSUPPORT_LONGJMP=wasm` replaces it
with native wasm exception-handling instructions.

**It cannot be used here.** The build mandatorily carries **`-sASYNCIFY=1`**
(QEMU coroutines *and* the TCG backend's `ffi_call_js` helper path depend on
it). Binaryen's asyncify pass cannot process a module containing wasm-EH — and
it does not fail cleanly: `wasm-opt --asyncify … --enable-exception-handling`
**hangs after ~1.3 s of CPU and is immune to `kill -9`**, leaving a ~28 MB
partial in-place rewrite. Reproduced twice with the flag verifiably in every
compile and the link (2,139 hits in `build.ninja`, all 1,998 objects compiled).

Two load-bearing features, mutually exclusive; asyncify is non-negotiable.
**The surviving attack on that 13% is reducing `cpu_loop_exit` FREQUENCY**
(icount window sizing, interrupt cadence), not its unit cost.

*First attempt was invalid* — see §6.1 — which is also how the flags trap was
found.

### 5.2 Enlarging `TB_JMP_CACHE_BITS` — worse, twice measured

Motivated by the profile (`helper_lookup_tb_ptr` 4.3%, `g_tree_lookup` 1.7%:
misses fall through `tcg_tb_lookup` into a GTree, all of it wasm).

- **16 bits** (64K entries): solo cold boot **~10% slower** — kernel 84 → 88 s,
  home screen 152 → 169.4 s. The cache is swept whole on every `tb_flush` and
  mode change; 16× the sweep plus worse locality outweighed the misses.
- **14 bits**, judged on the *launch* benchmark instead (three solo runs each):
  12-bit 3.0/3.0/3.0 s, 14-bit 3.0/2.8/3.0 s. A 0.07 s mean difference under a
  0.25 s poll resolution, inside run-to-run noise. **Not a result.**

**Verdict: keep 12**, recorded in `accel/tcg/tb-jmp-cache.h` itself so nobody
retries it blind.

### 5.3 Upstream `ktock/qemu-wasm` — nothing to pull

`wasm64-tcg-b` tip is still the 2026-01 emsdk bump. The graft is *ahead* of
upstream (tuning, counters, adaptive threshold). Engine speedups will not
arrive from upstream.

### 5.4 Second-chance eviction (earlier, still true)

Tried and reverted (`427cfb972e`): recompile rate 22% (FIFO) → 41%, plus a
`memory access out of bounds` crash at the cap.

---

## 6. False assumptions, corrected by data

**6.1 "`--extra-cflags`/`--extra-ldflags` configure the wasm build."** They do
**not**. `configure` passes `configs/meson/emscripten.txt` as a *second* meson
cross file, and meson's `[built-in options]` **replace rather than merge**
across cross files — so everything routed that way is silently discarded.
`-lnodefs.js`/`-sFETCH` only ever worked because `emscripten.txt` carries its
own copies, and `--profiling-funcs` never took effect through configure at all.
**Consequence: the first longjmp A/B benchmarked the baseline against itself**
(landmarks matched to 0.1 s — which at least measured run variance at ~1%).
Flags that must apply now live in `emscripten.txt`, with the trap documented in
both files.

**6.2 "Module instantiation dominates the profile."** My hypothesis from the
first (nameless) profile, where 37% sat in V8 C++ and `ArrayFrom`/
`MapPrototypeSet` were visible. The named profile killed it: instantiate
bookkeeping is inside the ~5% glue bucket. The 20% was the **futex**. This is
why the profile came before the code.

**6.3 "The wasm build hung because the disk filled."** True once (the linker
did wedge at 535 MiB free), which then misled the *second* hang — that one was
§5.1's asyncify/wasm-EH deadlock, with 3.5 GiB free. Two identical-looking
symptoms, different causes; check free space *and* whether the flag combination
is the known-bad one.

**6.4 "A snapshot resume fixes speed."** It fixes *time-to-usable* (~3 s). Busy
`guestRatio` after resume was unchanged at 0.05–0.07 — the display and engine
work is what moved it.

**6.5 "The display path is where the browser overhead lives."** Half right: it
was worth ~2×, and closing it exposed that the *main loop* was worth more.

---

## 7. Profiling traps (each cost real time)

- **CDP cannot profile Emscripten pthreads.** A pthread never returns to its
  worker event loop, so `Profiler.stop` — delivered on that loop — times out on
  exactly the threads that matter. The workers that *do* answer are idle pool
  spares. Use `--js-flags=--prof`: its sampler thread needs no event loop.
  (`wasm_request_pause` was added to park the vCPU for CDP; it does not rescue
  the design, but it is useful on its own.)
- **`Target.setAutoAttach`'s parameter is `waitForDebuggerOnStart`**, not
  `waitForDebugger`.
- **Large `.cpuprofile` responses arrive as fragmented WebSocket frames.**
  Dropping continuation frames hangs `Profiler.stop` for ever — this cost a
  night. Every CDP call also needs a timeout: a dead worker never answers.
- **Without a name section every engine frame is `wasm-function[31678]`.**
  `--profiling-funcs` (now permanently in `emscripten.txt`) costs ~0.9 MB and
  nothing in speed. It is what turned the Calculator crash into a five-minute
  diagnosis.
- **A hidden tab suspends `requestAnimationFrame`**, freezing anything that
  polls painted frames (the calc benchmark's oracle included). Watch the tab or
  run headless.

---

## 8. Browser support

**Chrome-family only** (Chrome, Edge, Arc, Brave — anything V8).

**Safari 17 crashes mid-boot**, and it is a *different* stack from §3.5: it dies
at `compiled=7232` (*past* the old Chrome crash point, proving the linear-memory
fix is present and working), ~20 frames deep:

```
disas_t32 ← thumb_tr_translate_insn ← translator_loop ← arm_translate_code
← setjmp_gen_code ← tb_gen_code ← cpu_exec_loop ← … ← rr_cpu_thread_fn
```

That shallow depth means the **native JS call stack** overflowed, not the wasm
linear stack: JSC places wasm frames on the worker's native stack, its
pre-optimizer tiers use very large frames for very large functions (QEMU's
Thumb decoder is among the biggest in the binary), and Safari worker stacks are
small. **No Emscripten flag reaches that stack.**

Newer Safari (18+, IPInt interpreter tier) is untested and plausibly better;
WebKit shipped wasm stack fixes through 2025, but undersized-stack crashes
remain an open WebKit bug class. Leads: (1) does a *second* visit survive, when
JSC may start from cached optimized tiers; (2) WebKit trackers for wasm stack
sizing on workers; (3) shrinking `disas_t32`'s frame is upstream surgery, last
resort. The viewer detects JSC and says so plainly.

---

## 9. Benchmarking rules learned the hard way

- **One run at a time.** A `display=none` control started seconds after killing
  another Chrome hung at "runtime initialized" with zero guest output —
  environmental, never reproduced solo. Pause between headless runs.
- **Run variance is ~1%** on landmarks (measured accidentally by the A/A).
  Differences under that are noise.
- **The poll interval is the resolution.** The calc benchmark polled at 250 ms
  and therefore could not resolve a 0.07 s effect; now 60 ms.
- **A settled-resume launch is a weak discriminator for JIT work** — 2.9 s wall
  of which 0.4 s is guest, with most TBs already compiled. For engine changes,
  exercise the **cold-JIT race**: cold boot, tap as soon as the gate arms, or
  launch a heavier app.
- **Never bare-`ninja` after touching `emscripten.txt`** — the cross file is a
  regen dependency and the regen runs with the caller's environment, leaking
  homebrew paths (`zlib.h` failures, every object dirtied). Always go through
  `scripts/wasm/build-qemu.sh`.
- `pkill -f "a\|b"` does not alternate — kill toolchain stragglers by PID.

---

## 10. Results

| | start of campaign | now |
| --- | --- | --- |
| second-visit resume | **broken** (black screen) | **~3 s** to home screen |
| cold boot → kernel | 276 s | **84 s** |
| cold boot → home screen | 252 s | **152 s** |
| vCPU time lost to the BQL | 47% | ~8% |
| guest MMIO throughput | 65.8k/s | 215–245k/s |
| panel | 10 Hz cap, unmeasured | 60 fps vsync, fps + blit cost on screen |
| tap → Calculator usable | unmeasured (and it crashed the machine) | **2.8–3.0 s wall / 0.4 s guest** |
| busy `guestRatio` | ~0.06 | ~0.06–0.13 measured; **launch case ~1/7 real time** |

The headline honesty: **boot and time-to-usable improved 2–3×, and the
steady-state execution gap is now the whole remaining story.** The launch
benchmark puts it at roughly **7× slower than the real device** for an
interactive action on a settled machine.

---

## 11. Next levers, ranked, with protocol

1. **The dispatcher (~15%, the top lever).** `tcg_qemu_tb_exec` is the hottest
   named function at 8.5%. Two concrete threads: (a) the **JS `Map` operation
   on the per-TB path** implied by `MapPrototypeSet`/`ArrayFrom` in the profile
   — find it in `tcg/wasm64.c`'s EM_JS glue and get it off the hot path;
   (b) a dispatcher-side chain cache so `goto_tb` exits stop re-entering
   `helper_lookup_tb_ptr`.
2. **`cpu_loop_exit` frequency (~13% in longjmp emulation, unit cost
   unfixable).** Why does the vCPU exit so often? icount window sizing and
   interrupt cadence are the suspects; `cpu_io_recompile` at 2.4% is related.
3. **Cold-JIT launch behaviour.** The settled launch is 2.9 s; the *cold* one is
   the ~90 s grind users hit. Compile-burst policy (threshold, batching,
   ahead-of-need compilation) is unexplored, and the benchmark can measure it
   by tapping early.
4. **MBX2D modelling** — running as a separate session (native-first,
   measurement-first; see [`MBX_HANDOFF.md`](MBX_HANDOFF.md)). Would move guest
   compositing into compiled C, worth more in the browser than natively — but
   the PC-sample measurement must justify it first.

**Judge every change on both** the boot landmarks and `?sweep=calc`, solo, with
~1% variance in mind. The jmp-cache episode is the cautionary tale: it looked
right in the profile, cost 10% on boot, and was invisible to the launch test.

---

## 12. Commit trail

`ed29c7ba00` zero-copy scanout + preRun gate · `3b451596f2`/`d2b81ff002` frame
rate · `56522d111e` profiler fixes · `c991b77431` named vCPU profile ·
`be964328c4`/`06ccc052cd` the BQL nap fix · `12eae609ae` cross-file flags trap ·
`ee2f305990` longjmp verdict · `a41d4f14d7`/`f572431b2b` stack fix + verification ·
`0207d69503`/`b0be7fda3d` Safari analysis + warnings · `1fa2145d68` calc
benchmark + jmp-cache negative · `f714f8c05f` launch baseline · `e3b5011bb6`
jmp-cache retest verdict.
