# The MBX session: SDO lands (T1 done), the MBX MMU is discovered (T2 unblocked)

**Dates:** 2026-07-31 → 2026-08-01 · **Branch:** `wasm-jit-graft` ·
**Commits:** `450dd3592f` … `c666f6f4f7` (this thread's only; the wasm
session's commits interleave on the same branch)

This is the session record AND the handoff: what was measured, what was
built, what was believed and then disproved, and exactly where to pick up.
The task-level context lives in [`MBX_HANDOFF.md`](MBX_HANDOFF.md) (which
this file supersedes as the entry point — its banners accreted during the
session and this is the organised version); the pre-history is
[`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md) and
[`M68AP_RENDER_HANDOFF.md`](M68AP_RENDER_HANDOFF.md).

---

## 0. State at the end of the session

| thing | state |
|---|---|
| **T1 — TVOut swap zero-window** | **RETIRED BY DEFAULT.** The missing hardware signal was the SDO field interrupt, now modelled (`hw/arm/ipod_touch_tvout.c`). The guest completes its own swaps; the derived window is only placed under `IT_TVOUT_SDO=0`. |
| **T2 — MBX 2D** | Foundation implemented (MMU + aperture forwarding + stream capture), format known structurally, **blocked on the guest emitting a stream** (it currently declines MBX2D and falls back to software). |
| §4 performance gate | Measured: compositing is 2–4% of guest CPU natively; the wasm-transferable bound is (8–19% non-idle share) × (guest-code fraction of the wasm vCPU thread, ~21% today). Fidelity-only for now; re-run the arithmetic after the wasm speed campaign. |
| Packaged apps | All three updated to the new engine and re-verified: iPod 5/5, 1.0 5/5, 1.1.4 5/5 (`IT_PROBE_WAIT=4`). |
| 3A109a / 1C28 | NOT verified — their NAND artifacts are not on disk (regenerate per BUILD.md). |
| 1.0 dismissal latency | The 33.8 s regime is not reliably present (the guest takes the software-fallback path), but a clean number was NOT obtained — the only instrument tried is icount-blind and its run was discarded. |

### Environment variables added or changed

| var | default | meaning |
|---|---|---|
| `IT_TVOUT_SDO` | **ON** | the SDO field-interrupt model. `=0` restores the old stub AND re-arms the derived zero-window — the one-knob A/B. |
| `IT_MBX_MMU` | off | forward MBX aperture accesses through the guest's own page table into guest DRAM. |
| `IT_MBX_2D_TRACE` | off | dump a 2D command block out of guest memory when the guest fires it (needs `IT_MBX_MMU=1` to see anything the model didn't store). |
| `IT_PROBE_WAIT=4` | (harness) | required for `app-button-probe --board m68ap-114`; the default 2× verdict window is marginal for that build's app launch. |

### New / extended instruments

* `scripts/mbx-composite-probe.py` — PC-samples the guest through UI phases,
  attributing samples via the firmware's own prebinding (`otool -l` __TEXT
  ranges read off the mounted root image). Reports non-idle shares itself.
* `scripts/tvout-swap-probe.py` — instrumented boot naming who reads/writes
  the swap-device field (pc/lr on the window), `--mode sdo` = the T1
  acceptance run (window off, model on, watches `[swapdev+0x160]` in RAM).
* `scripts/mbx-mmu-probe.py` — walks the MBX page table from a live guest,
  translates every address the driver hands the engine, dumps the pages,
  reverse-maps the op-state structures, `--exercise` drives an app
  open/HOME to try to catch the 2D stream in flight.
* `scripts/lock-unlock-probe.py` grew `--icount`.
* All three new probes delete their ~300 MB staged NAND clone by default
  (`--keep-stage` to keep) — inside the `finally`, so early exits clean up.

---

## 1. T1 — how the zero-window became deletable

### 1.1 The gate measurement first (per the brief's §7 order)

Before any device code: PC-sample the guest during home-screen animation to
decide whether MBX modelling had a performance case. Result (two runs,
M68AP 1.1.4, ~100 Hz `info registers` on a second QMP socket): the kernel
idle loop (`0xc005a9cc`) dominates every phase; LayerKit+CoreGraphics peak
at **~4% of total guest CPU** (19% of non-idle in the best window). Verdict:
fidelity-only natively.

**Amended in review (correctly):** under TCI the idle compresses out of the
wall clock, so the number that transfers to the browser is the NON-IDLE
share (8–19%) × the guest-code fraction of the wasm vCPU thread (~21% per
the V8 profile in BROWSER_WASM_STATUS.md). Same single digits *today*, but
the bound rises as the wasm engine levers land — parked, not dead.

*Instrument dead-ends inside this measurement (both recovered):*
* Run 1's "appzoom" phase actually sampled the lock screen — a telephony
  "Repair Needed" alert stranded the re-unlock. Fixed by reordering phases.
* Run 2's "appzoom" opened icon-wiggle edit mode instead of an app:
  **under `-icount` a 0.12 s host-side tap stretches into a guest
  long-press.** New trap, now documented; neither invalid phase changes the
  verdict because both bound compositing from above.

### 1.2 The swap path, decoded then confirmed live

Static (stripped 4A102 kernelcache, structural disassembly):
* `AppleMBX::addSwapDevice` at `0xc03af104`; the announced `id` comes from
  the swap device's own vtable, so the polled field lives in a
  display-driver object.
* The AppleMBX ISR (`0xc03b2e5c`): cause = `status(0x12C) & enable(0x130)`,
  swap completion is a THREE-BIT join (0x40+0x8+0x4 latched across entries)
  ending in a `vtbl+0x9c` callback into the swap device. (Unlike 1.0's ISR,
  1.1.4 dispatches bit 0x40.)
* AppleH1CLCD (which contains the `AppleH1TVOut` class): `[swapdev+0x160]`
  is the **in-flight swap-request pointer** of a linked queue
  (`+0x164/+0x168`, sentinel at obj+356); issue refuses while non-NULL;
  the armed bit is `[swapdev+0x1f4] & 4`.

Live (pc/lr logging added to the window's MMIO handlers — including the
previously silently-dropped WRITES): the poller is the queue-advance at
`0xc0383b9c` via the vtbl+0x354 wrapper, and — the smoking gun — the driver
**writes a real request pointer (`0xc2b06d00`) into `+0x160` and the window
swallows it**. The zero-window "works" by faking "no swap in flight".

Crucially, the swap issue produces **zero MBX register traffic**: the
completion is display-side. T1's task text ("model MBX swap completion")
was a misattribution; "and/or connect the SDO IRQ" was the true half.

### 1.3 The SDO field interrupt

The guest's own ISR (`0xc0383c64`, decoded then confirmed by traced pc)
defines the contract:

* `[TVOUT3+0x280]` bit 0 = field-interrupt status, write-1-to-clear (ack pc
  `0xc0383c8c`). TVOUT3 (0x39300000) is the DT's `tv-out@1300000`,
  `interrupts <0x1e 0x26>` — the SoC line was inertly wired to instance 2
  and moved to instance 3.
* `[TVOUT2+0x004]` bit 1 = field parity (TVOUT2 is `[obj+0x1f4]`; its reg
  0x000 gets the enable `0x5`, bit 2 = shadow-update-pending).
* `[TVOUT3+0x040]` bit 1 selects which parity completes a frame; it reads 0
  → completion on even fields, once per interlaced frame.

Model: ~60 Hz field timer on instance 3, parity toggled on instance 2 via a
peer link, storm guard (an unacked tick LOWERS the line instead of holding
it — the exynos UART lesson).

**The v1 model had the latch on the wrong instance** (2, where the machine
wired the IRQ) so the guest's acks (on instance 3) never landed and the
storm guard self-throttled every tick — *and the boot still completed the
swap and reached home*, watched live: `[swapdev+0x160]` went
`0xc2afbd00 → 0` in guest RAM with `IT_TVOUT_WA=0`. v2 implements the
decoded contract; steady state is ~5 field ticks per boot (the guest
disables the block after its swap), then silence.

### 1.4 Verification and the defaults flip

All on one binary, `IT_TVOUT_WA=0 IT_TVOUT_SDO=1`:

| config | result |
|---|---|
| 4A102 boot (v1, then v2) | home screen both; healthy `detach(AppleH1TVOut)` → `attach(AppleH1CLCD)` in serial |
| N45AP lock-unlock ×3 | 3/3, touch + sleep/wake clean — the board the hack was invented for |
| 1A543a | inert (no TVOut driver; zero SDO lines) and home renders |
| 4A102 lock-unlock ×3 (staged NAND, `--icount 1`) | 3/3 |
| 4A102 no-env default boot after the flip | home, window never mapped |

Defaults flipped: `IT_TVOUT_SDO` on; the window only placed when the model
is off. The window code stays for the A/B and the two unverified builds.

---

## 2. The regression the flip caused — and the rule it produced

**This is the session's most important negative result.** The first flip
suppressed the window globally, inside `tvout_wa_enabled()`. On iPhone OS
1.0 that changes NOTHING functional — 1.0 announces `AppleH1CLCD` only,
never receives a window, has no TVOut driver — yet `app-button-probe`'s
`2_touch_in_app` went from **35.95% PASS to 0.31% FAIL, twice, bit-stable**,
while the pre-SDO engine and `IT_TVOUT_SDO=0` both passed 5/5.

The mechanism is **phase, not function**: under `-icount` the guest is
deterministic while the probe's input rides host wall-clock. Suppressing
the branch removed its console prints, which moved the tap to a different
guest instant, where 1.0's documented 5-of-6 in-app event delivery dropped
it. Nothing about the tap, the touch model, or the MBX changed.

Fix (`fa5a604c06`): the suppression moved to the PLACEMENT decision, after
the not-a-TVOut-device check, so CLCD-only boards keep their old code path
byte for byte. 1.0 back to 5/5 with the step at 35.94%.

> **The rule, now encoded in the code:** *a model that does nothing on a
> board must touch nothing on that board.* Console prints included.

Related but distinct: 1.1.4's `1_open_app` failed at exactly 2.29% in some
runs — under BOTH configurations — and goes 5/5 at `IT_PROBE_WAIT=4`. That
is a pre-existing bistable harness window, not a regression; do not chase
it as one.

---

## 3. T2 — the MBX MMU, and everything it corrected

### 3.1 The discovery

The moratorium at the end of the button investigation said: no more
register tricks; completions must be written into engine-owned guest
memory, and the missing piece is the address translation. That translation
was sitting in a trace already on disk: registers `0x1000..0x101c` receive
eight guest-PHYSICAL page addresses (writer: the loop at kernel
`0xc03b7334` — a VA→PA helper per entry, bound literal `0x1020`), then
`0x1020 = 0x00010001`. **An 8-entry page directory and its enable.** So
`0x8000 / 0x1b000 / 0x1d000 / 0x21000 / 0xa00000` are MBX *virtual*
addresses over 32 MiB, not offsets into our MMIO window.

Verified live (`mbx-mmu-probe`, 4A102 AND 1A543a — byte-identical layout):
every PDE/PTE resolves into DRAM; `0x1b000`/`0x21000` hold live
driver-written structures **in boots with zero aperture writes** (the CPU
shares the pages); `0xa00000` is the command buffer, a full page of
`0xBAD43210` poison.

This retro-explains two old anomalies at once: why the truthful trace saw
zero aperture READS (the data lives in DRAM, reached by the CPU's own
mapping), and why backing the aperture with private storage was
neutral-to-harmful (it was the wrong memory).

### 3.2 What was implemented (`d3f3f39b5e`)

* The model records the page directory and enable **unconditionally**
  (cheap, and everything else depends on it).
* `IT_MBX_MMU=1` forwards aperture accesses ≥ the register limit through
  the table via `cpu_physical_memory_read/write`. **4A102 boots to the home
  screen with it on** — no wedge, in a subsystem with a six-wedge record —
  and a no-env default boot is bit-identical to before (canonical 69.6%
  home, zero new output).
* `IT_MBX_2D_TRACE=1` dumps a command block from guest memory on the fire
  word (`0xa00000` rewritten with `0xf0000000`), logging tags rather than
  interpreting them.
* `0x1020` is documented in the code as the MMU enable; the write-side
  "kick" interpretation survives only under `IT_MBX_EVENTS=1` and is marked
  wrong, to be replaced by the T2 work.

### 3.3 The command format (from named code, not reverse-guessing)

`MBX2D.framework` on the 1.0 root keeps **74 defined symbols**, including
the block writers themselves: `_pack2DCtxBlitCopy` (0x30b3a974) and
`_pack2DCtxBlitColor` (0x30b3994c), plus the setters (source/destination
surface, blend equation, scissor, scale, rotation). Reading BlitColor: the
block is a **register-write list** — alternating `(tag<<28 | selector,
value)` pairs; only five tag nibbles appear as OR immediates in the whole
framework (`0x3 0x6 0x8 0x9 0xa`), `0x7…` ends a block, `0xf0000000` fires.
LayerKit's own renderer keeps its symbols too (`_LKRenderMBX2DNew`,
`_mbx2d_emit_layer`, `_mbx2d_shmem_volatile` — note that last name:
the shared-memory half is real).

### 3.4 The blocker that reordered the plan: the guest DECLINES MBX2D

Three `--exercise` runs on 1.0 (app opens, HOME dismisses — screenshots
confirm both): **zero command-stream writes, ever**; `0xa00000` stays
poison. The guest's own console shows `AppleMBXUserClient::attach` followed
by `setPowerState(0)`; the register trace shows MBX2D's init arm
(`0x108=3`, ack `0x134=0xfff`, arm `0x130=0xffff`), ONE FinishSurface
timeout, then a context teardown ending in `0x1020 = 0x00010000` — the MMU
switched off. Userland's init fails its first handshake and LayerKit
composites in software.

**This also answers the cross-session question about 1.1.4:** it is not a
per-build branch. Both builds carry the same `__LKXRenderClient` MBX2D
code; the steering variable is whether `mbx2DInitialize`'s handshake
succeeds against the emulated registers. When it fails (today's engine,
both builds), the software fallback dismisses fine; when it used to
succeed, 1.0 paid ~1 s per block × 33 blocks. The historical "1.1.4 avoids
the path" observation and this session's "1.0 avoids it too" are the same
mechanism seen from two sides.

Consequence: **you cannot decode a stream the guest declines to emit**, so
the order becomes — make the guest commit first (two routes, §5) — then
decode, then blit.

---

## 4. Dead ends, false paths, and what each one taught

The consolidated table. Items 1–6 are from the button investigation
(pre-session, kept here because the T2 plan must not repeat them); 7–15
are this session's.

| # | belief / attempt | how it died | what replaced it |
|---|---|---|---|
| 1–6 | six register-file completion tricks (DONE-bit fakes, timer-raised bit 4, whole-aperture RAM, 0x85C counter, streamed terminators, bit-4-on-fire) | three wedged the guest outright, three did nothing | the moratorium: completion is memory-side; get the translation first |
| 7 | "T1 = model MBX swap completion / wire the IRQ **to MBX**" | the swap issue makes **zero** MBX register traffic (traced) | the completion is display-side: the SDO field interrupt |
| 8 | v1 SDO: latch + ack on instance 2 | guest acks landed on instance 3; storm guard self-throttled (and it *still* completed — masked the bug) | contract decoded from the guest ISR: status/ack on TVOUT3, parity on TVOUT2; IRQ line moved to instance 3 |
| 9 | "suppress the window globally — it's inert where no window is placed" | 1.0's `2_touch_in_app` 35.95%→0.31%, 2 runs each way; A/B on both engines proved it was this change | **phase, not function**: console prints shift guest input timing under icount. Suppress at PLACEMENT; *a model that does nothing on a board must touch nothing on that board* |
| 10 | "1.1.4's `1_open_app` failure is the same regression" | fails AND passes under both configs, always exactly 2.29% | pre-existing bistable verdict window; `IT_PROBE_WAIT=4` makes it 5/5 — a harness note, not an emulator bug |
| 11 | "0x1020 bit 0 is a render kick that completes an operation" (shipped model) | it is the MMU enable; the driver writes it once after loading the page directory | documented in code; the kick path survives only under `IT_MBX_EVENTS=1`, marked for replacement |
| 12 | "`[1a4]`/`[1a8]` live in MBX-mapped memory (engine-owned ⇒ MMU-visible)" | reverse-mapped every PTE: the table maps only 39 pages; `[1a4]` is plain kernel heap, `[1a8]` isn't even kernel-linear | completions must be addressed by DIRECT derived PA; `[1a8]` needs its own derivation before anything writes there |
| 13 | "`[[1a4]+0x60]` is a completion flag; write 0 to signal done" (the obvious route-1 move) | live dump: `+0x60` holds `0xc0b85400` — a kernel POINTER to a sibling struct; zeroing corrupts it | re-derive the predicate from the sleep site's disassembly against a dump of the pointed-to structure (the probe prints both structs every run) |
| 14 | "capture the stream with a fixed 90 s wall wait after HOME" | guest time ran ~6× slower than wall under icount; the wait covered ~13 guest seconds of a ~34 s dismissal | wait on the model's own trace (aperture-write count stable), capped — *the trace is the clock that cannot lie about guest progress* |
| 15 | "measure the dismissal latency with `dismiss-latency.py`" | all 8 ladder presses delivered, verdict 'never settled' — but it caught the auto-lock DIM (its own documented artifact) and its 6-wall-second ladder collapses to ~2 guest seconds under bundle icount: a press burst inside the animation | run DISCARDED (`c666f6f4f7`). Clean method: the model's virtual-timestamped `[LCD]` flips around a SINGLE press — the method that named 33.76 s originally |

Operational dead-ends worth keeping visible:

* **Disk**: the three new probes each staged a ~300 MB NAND clone and left
  it; eight runs filled the data volume to 100% and aborted an engine
  install mid-flight (harmlessly — the installer packs the NAND before
  touching the bundle). Cleanup is now default, inside `finally`.
* **codesign**: `cp` preserving xattrs (`com.apple.FinderInfo` /
  `ResourceFork` — the build directory's binary carries them!) makes
  `codesign` fail with "resource fork … not allowed" and can leave a bundle
  seal invalid. `xattr -c` before signing. The iPod bundle's seal is broken
  by DESIGN regardless (it persists guest pages inside its own NAND).
* **Host load is a guest input-phase variable.** Don't build, sign, or run
  a second emulator while a probe's input phases matter.

---

## 5. Where to pick up

### Route A (fastest to a stream): make the guest commit to MBX2D
Remove `LK_ENABLE_MBX2D=0` from the guest's `com.apple.SpringBoard.plist`
via the fast loop — `scripts/extract-hfs-from-nand.py` → plist edit →
`scripts/overlay-hfs-into-nand.py` — NOT a full NAND rebuild. Then run
`IT_MBX_MMU=1 IT_MBX_2D_TRACE=1 scripts/mbx-mmu-probe.py --build 1A543a
--exercise` and you should finally see `[MBX-2D] fire` blocks with real
tagged words, plus the aperture-vs-DRAM diff answering whether aperture
writes also land in the mapped pages. Expect the dismissal to be SLOW again
in this configuration (per-block timeouts) until completion is modelled —
that is the point: it produces the stream to decode.

### Route B (the honest completion): FinishSurface
Prerequisites now known: address `[obj+0x1a4]` by derived PA
(console-announced `AppleMBXDevice(0x…)` → kernel-linear → read pointer);
derive `[obj+0x1a8]`'s PA separately (its VA `0xf275a000` is an IOKit
mapping, not kernel-linear — likely an IOMemoryMap of something; find its
phys by content or by the descriptor table at `[fp+0x28..0x40]`,
`0xc032be0c` on 1.0); re-derive the sleep predicate from the disassembly
around `0xc032e098` (1.0) against live dumps of `0xc0b85400`'s structure.
Do NOT write `[[1a4]+0x60]` — it is a pointer (dead end #13). And ship the
handshake together with per-block completion, or route A's slow path comes
back as the default.

### Then
Decode the captured blocks against `_pack2DCtxBlitCopy` (tag alphabet
`3/6/8/9/a`), write the software blitter (plain C — no host GL, no
threads: wasm constraint), raise the ISR bits only after the memory says
done, delete the plist edit from the NAND builders, verify all four builds
by pixels, and re-run the §4 arithmetic in the wasm build.

### Also open
* 3A109a / 1C28 verification (needs NAND regeneration per BUILD.md).
* A clean 1.0 dismissal-latency number (single press, `[LCD]`
  virtual-timestamped flips; do NOT reuse `dismiss-latency.py` under
  icount without fixing its ladder).
* `dismiss-latency.py` itself needs an icount-aware rewrite if it is to be
  kept.

---

## 6. Commit log (this thread)

```
450dd3592f  MBX thread: the §4 gate probe, the swap-queue decode, pc/lr on the TVOut window
af63a4f339  §4 gate answered: compositing is single-digit guest CPU — MBX is fidelity-only
e2c1dab504  Verdict amended: the wasm-transferable number is the NON-IDLE share
f6a2ee2835  T1: model the SDO field interrupt — 4A102 reaches home with NO swap window
a05a58b458  T1 LANDS: SDO model on by default, the swap-device zero-window retires
b5bbd4b00a  Task list catches up: T1 DONE, T2 PARKED, T9 status current
002ed4079f  T2 unblocked: the MBX has an MMU and hands us its page directory
3745201bcd  T2 step 1: a probe that walks the MBX MMU and locates the command stream
579ecd819c  T2 step 3 de-risked: the 2D command packer ships with symbols
7dc0cee0eb  T2: the command encoding is tagged words, and the caller chain has symbols too
b39795daa3  Name 0x1020 for what it is: the MBX MMU enable, not a render kick
fa5a604c06  Suppress the swap window at PLACEMENT, not globally — 1.0 regressed on phase alone
29654231b8  Probes clean up their staged NAND — mine filled the data volume tonight
988de8d759  Record the bundle matrix: a phase-only regression, and 1.1.4's window flake
8cbd5d3a9f  T2 walk verified on a live guest: the aperture is an MMU window onto shared DRAM
b5d01be146  mbx-mmu-probe --exercise: wait for the stream by watching the trace, not the clock
c028a3e646  1.0 walk matches 4A102 byte for byte — the MBX memory layout is firmware-invariant
b9c864c9b3  1.0 can dismiss without the MBX: the 2D stream is gated on a bistable init
d3f3f39b5e  T2 step 1 implemented: MBX MMU translation and aperture forwarding
186ea7f887  T2 status table, and the block is a register-write list
7019bd4f7c  The guest declines MBX2D entirely — reorder T2 around getting a stream
e7ee12fce9  mbx-mmu-probe: locate the op-state structures the completion path polls
ea0e42e93c  Correction: the op-state structures are not MMU-mapped
0d72c99f82  The +0x60 word is a POINTER — the obvious completion write would have wedged
c666f6f4f7  Discard tonight's dismiss-latency run: the instrument predates icount pacing
```

## 7. Evidence on disk (regenerable; none committed)

* Decrypted kernelcaches: `/tmp/kc10r.raw`, `/tmp/kc114r.raw`
  (`scripts/extract-kernelcache.py`).
* Disassemblies: `/tmp/mbx114.asm`, `/tmp/clcd114.asm`, `/tmp/mgf114.asm`,
  `/tmp/mbx10.asm`, `/tmp/mbx2d.asm` (userland MBX2D).
* Probe reports: `/tmp/mbx-mmu*/mbx-mmu-probe.json` (+ dumped pages),
  `/tmp/mbx-composite-4a102*/composite-probe.json`,
  `/tmp/appbtn-*/report.json` (the full bundle matrix).
* Mounted roots (already attached at session end): `/private/tmp/m68_10`
  (1A543a), `/private/tmp/m68_114` (4A102).
