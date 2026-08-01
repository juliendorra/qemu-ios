# MBX (PowerVR) — session handoff: the stub, and the two hacks that stand on it

> **T1 BREAKTHROUGH (2026-07-31, night): 4A102 reaches the HOME SCREEN with
> the swap-device zero-window REMOVED (`IT_TVOUT_WA=0`), completing the TVOut
> swap through its own driver.** The missing hardware signal was the **SDO
> field interrupt**, now modelled (`IT_TVOUT_SDO=1`, `hw/arm/ipod_touch_tvout.c`).
> Watched live in guest RAM: `[swapdev+0x160]` went `0xc2afbd00 → 0` — the
> in-flight swap request, issued and then completed by the guest itself.
> No MBX register was involved at all: the TVOut swap completes display-side
> (the T1 task text's "model MBX swap completion" was a misattribution;
> "and/or connect the SDO IRQ" was the half that's true).
>
> The register contract, decoded from the guest ISR (0xc0383c64, AppleH1CLCD
> kext) and confirmed by pc/lr-attributed traces:
> `[TVOUT3+0x280]` bit 0 = field-interrupt status, W1C (the ISR's ack pc is
> 0xc0383c8c); `[TVOUT2+0x004]` bit 1 = field parity; `[TVOUT3+0x040]` bit 1
> = which parity completes a frame (reads 0 → even fields). TVOUT3
> (0x39300000) is the DT's `tv-out@1300000`, `interrupts <0x1e 0x26>` — the
> SoC SDO line is now wired to instance 3 (it was inertly on instance 2).
> The first (v1) run had the latch on the wrong instance so acks never
> landed — the storm guard self-throttled it and the boot STILL completed
> the swap and reached home; v2 implements the decoded contract.
>
> **Verification matrix (IT_TVOUT_WA=0 + IT_TVOUT_SDO=1 unless noted):**
>
> | config | result |
> |---|---|
> | 4A102 boot → home (v1 model) | **PASS**, `[swapdev+0x160]` 0xc2afbd00 → 0 in guest RAM |
> | 4A102 boot → home (v2 contract) | **PASS**, quiescent: ~5 field ticks total, guest disables SDO after its swap; serial shows `detach(AppleH1TVOut)` → `attach(AppleH1CLCD)` |
> | N45AP lock-unlock-probe, 3 cycles | **PASS 3/3** (booted home 29.7% nb, touch delivered every cycle, sleep/wake clean) — the board the window was invented for |
> | 1A543a (1.0) inertness: no TVOut driver, knob on | **PASS** — home 45.8% nb, zero SDO lines |
> | 4A102 lock-unlock battery under SDO (staged NAND, `--icount 1`) | **PASS 3/3** (touch delivered, sleep/wake clean) |
> | 4A102 DEFAULT config (no env) after the flip | **PASS** — home, window never mapped, "[TVOUT-WA] swap-device window NOT armed" |
> | 3A109a / 1C28 home render | **BLOCKED — NAND artifacts not on disk** (only nor.bin+ipsw); regenerate per BUILD.md before verifying these two |
>
> **DEFAULTS FLIPPED (2026-07-31, end of session): `IT_TVOUT_SDO` defaults
> ON.** One knob A/Bs the whole thing: `IT_TVOUT_SDO=0` restores the stub +
> derived window exactly as before. The window code stays for that A/B and
> for the two unverified builds. `lock-unlock-probe` grew `--icount` (the
> 4A102 boot trap), and note it does NOT stage the NAND — clone before
> pointing it at a build artifact.
>
> ### The bundles, and a real regression the first flip caused (2026-08-01)
>
> All three packaged apps were updated to this engine and re-run through
> `app-button-probe.py`, which drives the BUNDLE through its own launcher.
> The first version suppressed the window **globally** (in
> `tvout_wa_enabled()`); that regressed iPhone OS 1.0:
>
> | run | result | failing step |
> |---|---|---|
> | 1.0, pre-SDO engine (v383) | 5/5 | — |
> | 1.0, global gate, ×2 | 3/5, 4/5 | `2_touch_in_app`, **0.31% both times** |
> | 1.0, same engine, `IT_TVOUT_SDO=0` | 4/5 | step 2 **PASSED, 35.95%** |
> | 1.0, placement-level fix | **5/5** | — (step 2 back to 35.94%) |
> | 1.1.4, global gate, ×2 | 4/5, 5/5 | `1_open_app` 2.29% / — |
> | 1.1.4, `IT_TVOUT_SDO=0` | 5/5 | — |
> | 1.1.4, placement fix | 4/5 | `1_open_app`, 2.29% again |
> | 1.1.4, placement fix, `IT_PROBE_WAIT=4` | **5/5** | — |
> | iPod | 5/5 | — |
>
> **1.0 has no TVOut driver and never receives a window either way, so the
> global gate changed NOTHING functional there — and still flipped the
> result, reproducibly.** The cause is phase: under `-icount` the guest is
> deterministic while the probe's input rides host wall-clock, so removing
> that branch's console prints moved the tap to a different guest instant
> and 1.0's 5-of-6 in-app event delivery dropped it. Hence the rule now
> encoded in the code: **a model that does nothing on a board must touch
> nothing on that board.** The suppression moved to the placement decision,
> after the not-a-TVOut check (`fa5a604c06`).
>
> **1.1.4's `1_open_app` is a separate, PRE-EXISTING harness flake** — it
> failed and passed under *both* configurations, always at exactly 2.29%
> when it failed, and went 5/5 once the verdict window was widened. Use
> `IT_PROBE_WAIT=4` on `m68ap-114`; the default 2x is marginal for that
> build's launch, exactly as the probe's own comment warns for icount.
>
> **Disk hygiene, learned the hard way again:** these MBX probes each stage
> a ~300 MB NAND clone per run; eight runs filled the data volume to 100%
> and aborted an engine install mid-flight (harmlessly — the installer
> packs the NAND before it touches the bundle). They now delete the stage
> in their `finally` by default (`--keep-stage` to preserve). Also: the
> build directory's binary carries `com.apple.FinderInfo`/`ResourceFork`
> xattrs, so a hand-copy into a bundle needs `xattr -c` before `codesign`
> or signing fails with "resource fork ... not allowed".

> **§4 GATE MEASURED (2026-07-31, late evening): software compositing is a
> SINGLE-DIGIT share of guest CPU — MBX modelling is FIDELITY-ONLY work.**
> (Measurement window closed; the machine is free.)
>
> Two `mbx-composite-probe.py` runs on M68AP 1.1.4, ~100 Hz `info registers`
> sampling, attribution via the firmware's own prebinding:
>
> | phase (what was really on screen) | samples | non-idle | CG+LayerKit, of non-idle | CG+LK, of TOTAL |
> |---|---|---|---|---|
> | idle home screen | 1505+1425 | 2.5% / 15.6% | 0% / 3% | ~0% |
> | lock-screen shimmer (continuous LayerKit animation) | 1790+1882 | 5.7% / 11.8% | 8% / 19% | **0.4-2.2%** |
> | lock screen + taps (run 1 "appzoom", invalid as zoom) | 2802 | 22.8% | 17% | 3.9% |
> | wiggle-mode alert (run 2 "appzoom", invalid as zoom) | 2839 | 6.9% | 1% | ~0% |
>
> The kernel idle loop (`0xc005a9cc` on 4A102) dominates every phase; even
> the busiest window put LayerKit+CoreGraphics at **under 4% of guest CPU**.
> Neither run captured a true app-zoom burst (run 1: a telephony "Repair
> Needed" alert stranded the lock screen; run 2: **under icount a 0.12 s
> host-side tap stretches into a guest long-press** — it opened icon-wiggle
> edit mode plus the "Edit Home Screen" alert instead of an app; that
> input-duration distortion is a new trap, note it when scripting taps under
> `-icount`). But a zoom burst is ~1 s of a 6 s cycle and both invalid
> phases bound compositing from above at a few percent, so the conclusion
> does not hinge on it. The M68AP UI also already animates at ~55 Hz
> natively (the dismissal measurements in IN_APP_BUTTON_INVESTIGATION.md),
> i.e. nothing is performance-starved that MBX offload would rescue.
>
> **Per §4/§7, that settles the justification NATIVELY: T1/T2 proceed (if
> at all) on the fidelity argument alone — deleting the TVOut zero-window
> and the LK_ENABLE_MBX2D plist edit — not on performance.**
>
> **The wasm-side caveat (raised in review, and it is real): the number
> that transfers to the browser is not the 2-4% total share — it is
> compositing's share of NON-IDLE guest instructions (8-19% here), because
> TCI compresses idle out of the wall clock.** Against the wasm session's
> own vCPU profile (BROWSER_WASM_STATUS.md 2026-07-31: only ~21% of the
> busy vCPU thread executes guest code; the rest is BQL/longjmp/dispatch
> overhead), an MBX 2D offload today buys ~21% x 8-19% ≈ **2-4% of the
> wasm vCPU thread** — same single digits, and far below the engine levers
> being worked there. But that bound RISES as those overheads fall (BQL
> is already 47% → 8%): if guest execution comes to dominate the thread,
> the ceiling approaches the non-idle share itself. So T2's performance
> case is **parked pending the wasm speed campaign, not dead** — re-run
> this arithmetic (this probe gives the guest-side ratio; the V8 profile
> gives the thread mix) when the campaign converges. T2 also buys the
> explicit frame-completion events §5 wants for the display bridge.
> Stopped here for review, per the session brief. The next concrete step is
> already staged and is measurement, not device code:
> `scripts/tvout-swap-probe.py --build 4A102 --kernelcache /tmp/kc114r.raw
> --logs /tmp/tvout-swap` names the teardown poller and any writer of the
> faked field in one instrumented boot.


> **2026-07-29 — the in-app HOME/POWER bug on iPhone OS 1.0 is an MBX symptom.**
> After the first in-app press, `com.apple.driver.AppleMBX` enters
> `do { v = mbx_read(base, 0x12C); } while (!(v & 0x40));` and spins forever at
> 0.97 host cores, starving every other process — which is why no further
> GSEvent is delivered, why touch dies with the button, and why the LCD stops
> flipping. Verified by PC-sampling the spin (30/30 samples in the kernel, an
> exact 9-instruction cycle) and resolving the addresses through the RELEASE
> kernelcache's `kmod_info` list (`scripts/spin-locate.py`,
> `scripts/kernel-addr-symbolize.py`). So **T1 has a second, much more visible
> symptom than the swap-device window**, and the cheapest experiment is to make
> bit 6 of register 0x12C readable-as-set in the stub and re-run
> `app-button-probe.py --board m68ap-10`.
>
> **DONE, and it worked in part (2026-07-29).** `0x12C` now returns `0x140`
> instead of `0x100` (`IT_MBX_READY=0` reverts). A/B on the same binary:
> **0.99 cores with the bit clear vs 0.10 with it set**, and event delivery goes
> from 1 DOWN/1 UP over ten presses to 5/6 over six, with taps reaching the app
> again. **But the app still does not visually dismiss** (`3_home_returns` 0.00%),
> and the guest is now IDLE rather than spinning -- i.e. something waits on an
> MBX completion that never arrives, which is the other half of T1: the MBX
> region still has no IRQ connected. Installed in the 1.0 bundle only; 1.1.4 and
> the iPod have NOT been re-tested with it. Note that the MBX driver is the SAME
> in all three builds: 1.0 (1A543a), 1.1.4 (4A102) and the iPod's **1.1
> (3A101a)** each contain the same two unbounded bit-6 spin loops plus one
> bounded `tst #0x40` site, so the stub answer is not 1.0-specific -- the working
> builds simply never reach the unbounded loops. Full record:
> [`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md).

> **2026-07-31 (this session, working log — the MBX modelling thread proper):**
>
> * **§1 below is STALE**: the model is no longer a 25-line stub. The in-app
>   button work grew it measured event registers (0x12C status / 0x130 enable /
>   0x134 clear), three kick sites (0x6d8, 0x1020 bit 0/8, 0xA00000 fire), an
>   optional register-sparing RAM backing (`IT_MBX_RAM`), an IRQ line pluggable
>   via `IT_MBX_IRQ` (12 = the real MBX interrupt, measured on 1.0), and traces
>   (`IT_MBX_TRACE[=all]`). See the comment blocks in `hw/arm/ipod_touch.c` and
>   `IN_APP_BUTTON_INVESTIGATION.md` from "FIRST EVER 3_home_returns PASS"
>   onward. The register-trick moratorium at the end of that file binds this
>   thread too: completions must be written into the engine-owned guest-RAM
>   structures (`[1a4]`/`[1a8]`, +0x60) before any event bit is raised.
> * **§4 measurement**: `scripts/mbx-composite-probe.py` (new) boots M68AP to
>   the home screen and PC-samples three phases (idle, lock-screen shimmer,
>   app-zoom), attributing samples through the firmware's own prebinding
>   (`otool -l` __TEXT ranges; SpringBoard and any app main binary share
>   0x1000, so that bucket is ambiguous during appzoom). Two traps already
>   burned: a 4A102 boot without `-icount 1` panics in `IOIpodUSBDevice::start`
>   (the exact fb-snapshot trap in BROWSER_WASM_STATUS.md), and icount boots
>   are slow enough that a fixed boot-wait is wrong — the probe now polls for
>   the home screen.
> * **The 1.1.4 ISR, read from the RELEASE kernelcache** (AppleMBX at
>   0xc03ad000..0xc03bf000 in 4A102, ISR at **0xc03b2e5c**; kexts are stripped,
>   so this was structural): cause = `status(0x12C) & enable(0x130)`, acked
>   low-16 to 0x134. Dispatches 0x20 (recovery: 20 soft-event-16 retries),
>   0x400, 0x10 (render complete), 0x4 → latch `[1a4]+0x4c`, 0x8 → latch
>   `[1a4]+0x24`, 0x40 → latch `[1a8]+0x2c`, 0x100, 0x200, 0x1. **The swap
>   completion is a THREE-BIT JOIN**: when `[1a8]+0x2c && [1a4]+0x4c &&
>   [1a4]+0x24` are all latched (bits 0x40+0x4+0x8, across any number of ISR
>   entries), the ISR clears all three, and calls `[obj+0x23c]->vtbl+0x9c(
>   [1a8]+0x1c, 0)` — a completion callback into the swap-device side, i.e.
>   the thing that would clear the field our TVOut window fakes. Unlike 1.0's
>   ISR, **1.1.4 DOES dispatch bit 0x40**.
> * `AppleMBX::addSwapDevice` is at 0xc03af104 (1.1.4); the announced `id` is
>   obtained from the swap device's own vtbl call, so the polled `+0x160`
>   field lives in a display-driver-owned object, not in AppleMBX.
> * **The polled field is a swap-request QUEUE slot, decoded from AppleH1CLCD**
>   (the `AppleH1TVOut` class lives inside the AppleH1CLCD kext,
>   0xc037f000..0xc0388000 in 4A102). `[swapdev+0x160]` = the in-flight swap
>   request pointer; `+0x164/+0x168` = a doubly-linked request queue whose
>   list-head sentinel is `swapdev+0x164` itself (obj+356); the issue function
>   at 0xc0381b84 refuses to start while `+0x160 != 0`, and the
>   promote/complete function at 0xc0383b9c clears an armed bit
>   (`[swapdev+0x1f4] &= ~4`) and advances the queue. So the always-zero
>   window fakes "no swap in flight", which is why teardown proceeds. The
>   honest clear is whatever runs on swap completion — in AppleMBX's ISR
>   that is the three-bit join above ending in `vtbl+0x9c` into this kext.
> * The TVOut-window MMIO handlers now log **guest pc/lr** under `IT_FB_TRACE`
>   (both reads and the previously-dropped writes), so one instrumented 1.1.4
>   boot names the teardown poller and any writer of the field. Not yet run —
>   the compositing measurement owns the machine first.

> **T2 UNBLOCKED (2026-08-01): the MBX has an MMU, and its page directory is
> in registers `0x1000`–`0x101c`.** The blocker recorded at the end of
> `IN_APP_BUTTON_INVESTIGATION.md` — *"learn the guest-physical location of
> the `[1a4]`/`[1a8]` structures; that translation is the missing piece"* —
> is answered, and derived from the guest rather than declared.
>
> Evidence, from a boot trace that was already on disk (`IT_MBX_TRACE=all`)
> plus the 4A102 kext disassembly:
>
> ```
> WR 0x01000 = 0x08b1b000   WR 0x01004 = 0x08b1c000    <- guest PHYSICAL
> WR 0x01008 = 0x08b5d000   WR 0x0100c = 0x08b5e000       page addresses,
> WR 0x01010 = 0x08ba0000   WR 0x01014 = 0x08ba1000       all in DRAM
> WR 0x01018 = 0x08b82000   WR 0x0101c = 0x08be3000       (RAM_MEM_BASE+)
> WR 0x01020 = 0x00010001                               <- MMU enable
> ```
>
> The writer is a loop at `0xc03b7334`: it walks eight memory descriptors,
> calls a VA→PA helper on each (`blx r10`), and stores the result at
> register offset `r5` starting at `#4096` (0x1000), stepping 4, ending at
> the literal `0x1020` — exactly eight entries. So `0x1000..0x101c` is an
> **8-entry page directory** and `0x1020` is its control/enable.
>
> Three consequences, in order of importance:
>
> 1. **The model can translate MBX addresses to guest physical memory**, by
>    reading the directory it is already handed and walking it in DRAM. The
>    engine-owned words (`[[1a4]+0x60]` et al.) can therefore be written
>    honestly — which is the precondition the six failed register tricks all
>    violated. No per-build constant, no kernel-heap guess.
> 2. **`0x8000` / `0x1b000` / `0x1d000` / `0x21000` / `0xa00000` are MBX
>    VIRTUAL addresses, not offsets into our MMIO window.** 8 directory
>    entries x 4 MiB = 32 MiB of MBX address space covers every one of them.
>    This retro-explains two measured mysteries: why the truthful trace saw
>    **zero** aperture reads (the structures live in DRAM, the CPU reaches
>    them by its own mapping), and why backing the aperture with private
>    storage was neutral-to-harmful (it was the wrong memory).
> 3. **A correction to the shipped model:** `s5l8900_mbx_write` treats
>    `0x1020` bit 0 as a render KICK that completes an operation. It is the
>    MMU enable. The completion it currently signals there is spurious;
>    revisit when the 2D path is modelled.
>
> **First experiment for T2 — RUN, and the walk WORKS (2026-08-01,
> `scripts/mbx-mmu-probe.py`, 4A102 at the home screen):**
>
> ```
> directory: 08b1b000 08b1c000 08b5d000 08b5e000 08ba0000 08ba1000 08b82000 08be3000  (all DRAM)
> MBX 0x08000  -> PA 0x08b4f000  empty
> MBX 0x1b000  -> PA 0x08ba2000  244 nonzero bytes  e0000000 a7700000 0e000000 d6887610 2222 0e80 ...
> MBX 0x1d000  -> PA 0x08bc9000  empty
> MBX 0x21000  -> PA 0x08bad000  24 nonzero bytes   e0000000 a8800000 0e000000 d6887610 2222 0e80 ...
> MBX 0xa00000 -> PA 0x08be4000  page FULL of 0xBAD43210
> ```
>
> Three verdicts:
>
> 1. **The two-level walk is correct** — every PDE and PTE resolves into
>    DRAM, flags are 0, and the content is sensible.
> 2. **The CPU shares these pages.** This boot performed ZERO aperture
>    writes at offsets ≥ 0x2000 (measured, full IT_MBX_TRACE=all), yet
>    0x1b000/0x21000 hold live driver-written structures at their
>    translated addresses. The kernel writes them through its own mapping;
>    the aperture is a second window onto the same memory, exactly what an
>    MMU predicts.
> 3. **0xa00000 is the command buffer, allocated and poisoned** — a full
>    page of `0xBAD43210` ("bad" fill) waiting for commands.
>
> **So the T2 model core is: translate every aperture access through the
> guest's own page table into guest DRAM.** IT_MBX_RAM's private backing
> was "the wrong memory" precisely because this mapping exists. And the
> "answering 0 is load-bearing" wedge now has a mechanism: forwarding
> READS alone would hand the driver its own values back while the
> engine-owned completion words stay unwritten — forwarding and
> engine-side completion writes must land together, which is the same
> conclusion the six failed register tricks converged on from the other
> side. The probe grew `--exercise` (open app → HOME → wait out the
> dismissal) to capture the 1.0 command stream in flight and diff every
> traced aperture write against the mapped DRAM.
>
> **The second exercise run answered a question nobody had asked: 1.0 can
> dismiss WITHOUT the MBX at all.** Adaptive wait, dismissal ran to a real
> home screen — and the trace shows **zero** command-stream writes over ten
> minutes, with 0xa00000 still full of poison. What it shows instead:
> MBX2D's init arming (0x108=3, ack 0xfff, arm 0x130=0xffff from the
> FinishSurface sleep wrapper 0xc032e0xx), ONE timeout disarm
> (0x130=0 from the thread-call), the capability reads, and then a context
> TEARDOWN whose final act is `WR 0x1020 = 0x00010000` — the MMU switched
> off. So userland's MBX2D init failed its first handshake, LayerKit fell
> back to software, and the dismissal completed anyway. Two consequences:
>
> * **The 33-block stream is conditional on MBX2D init surviving its first
>   FinishSurface round-trip**, which under today's model is a ~1 s timeout
>   race — the historical 33.8 s dismissals and this run's stream-less one
>   are the two sides of a bistable init. The ~33.8 s "latency residual"
>   is therefore not inevitable; the software fallback exists and works.
> * The aperture-vs-DRAM diff harness in `--exercise` is armed but has not
>   yet caught a stream in flight; rerun until init lands on the MBX2D
>   side, or decode from the historical traces (the words are all in
>   IN_APP_BUTTON_INVESTIGATION.md's captures).
>
> **The 1.0 walk matches 4A102 byte for byte** (run 2026-08-01): different
> physical pages, same structure — 244 nonzero bytes at MBX 0x1b000 with
> IDENTICAL content (`e0000000 a7700000 0e000000 d6887610 2222 0e80 …`),
> same 24 bytes at 0x21000, same full-page `0xBAD43210` poison at
> 0xa00000. The layout is firmware-invariant across the 1.x line, so one
> decoder serves all builds. (First exercise run caught the dismissal too
> early — guest time under icount ran ~6× slower than wall clock and the
> fixed 90 s wait covered ~13 guest seconds; the probe now waits on the
> trace itself. The HOME press *was* delivered and the dismissal path ran
> to the 0x12C soft event before the window closed.)
>
> **And the command format is not a black box either.** The userland
> `MBX2D.framework` on the 1.0 root filesystem keeps **74 defined symbols**,
> including the packers themselves:
>
> | symbol | what it gives us |
> |---|---|
> | `_pack2DCtxBlitCopy` (0x30b3a974) | **the command-block writer** for a copy blit |
> | `_pack2DCtxBlitColor` (0x30b3994c) | the same for a solid-colour fill |
> | `_mbx2DCtxSetSourceSurface` / `…SetDestinationSurface` | surface base/stride/format fields |
> | `_mbx2DCtxSetBlendEquation[Complex]` | the blend/ROP encoding |
> | `_mbx2DCtxSetScissor` / `…SetScaleFactor` / `…SetRotation` | clip, scale, rotate |
>
> So step 3 is *reading a documented packer* against command words we have
> already traced (`0xa00040..0xa00068`, terminator `0x70000000`, fire
> `0xf0000000`), not reverse-engineering an undocumented GPU. `pack2D…`
> computes its block size from context flags (20 or 28 bytes, +12, +24),
> which matches the variable-length blocks in the trace.
>
> **The block is a REGISTER-WRITE LIST** (read out of `_pack2DCtxBlitColor`,
> the simpler of the two packers, 2026-08-01):
>
> ```
> [+0x00] = 0xA0000000 | sel     [+0x04] = value
> [+0x08] = 0x94000000 | sel     [+0x0c] = value
> [+0x10] = …                    (+0x14, +0x18 conditional)
> ```
>
> i.e. alternating (tagged selector, data) pairs, the classic PowerVR
> "load register" stream, terminated by a `0x7…` word and fired by
> rewriting word 0 with `0xf0000000`. Only five tag values appear as OR
> immediates in the whole framework — `0x3`, `0x6`, `0x8`, `0x9`, `0xa` —
> so the decoder has a small, closed alphabet to learn, one tag at a time,
> against a captured block.
>
> **The encoding is tagged words.** Inside `_pack2DCtxBlitCopy` each word is
> built by OR-ing an opcode into the top bits before the store —
> `0x80000000`, `0xA0000000`, `0x94000000`, `0x30000000` are all visible as
> immediates — and the traced stream ends blocks with `0x70000000` and fires
> with `0xf0000000`. So the block is a sequence of `opcode | payload` words,
> which is the easiest possible format to decode incrementally: implement
> the opcodes one at a time and log the unknown ones.
>
> **And the caller chain is symbol-documented too.** LayerKit itself carries
> `_LKRenderMBX2DNew`, `_LKRenderMBX2DCollect`, `_mbx2d_emit_layer`,
> `_mbx2d_fill_opaque`, `_mbx2d_bind_texture`, `_mbx2d_prepare_texture` and
> — note the name — **`_mbx2d_shmem_volatile`**, confirming the shared-memory
> half that the register-trick moratorium is about. The full path
> LayerKit → MBX2D → MBXConnect → user client is readable end to end on the
> 1.0 root filesystem.

**Date:** 2026-07-28 · **Branch:** `ipod_touch_1g` · **State:** nothing started —
this is the short path *into* the problem, not a report of work done.

The S5L8900's PowerVR MBX is a do-nothing MMIO stub. Two separate shortcuts
exist only because of that, one in the emulator and one edited into the guest
filesystem, and both are on the open-task list as
[T1 and T2](M68AP_RENDER_HANDOFF.md). This file collects what is known about
them in one place so the work can be picked up without re-deriving it.

Related docs:

* [`M68AP_RENDER_HANDOFF.md`](M68AP_RENDER_HANDOFF.md) — §0 carries T1/T2 as
  the authoritative task list, and §"the MBX/LayerKit lead" has the 2026-07-25
  measurements that this file summarises.
* [`IPHONE_2G_BRINGUP_HANDOFF.md`](IPHONE_2G_BRINGUP_HANDOFF.md) — the
  long-form working log (every run, every trace).
* [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md) /
  [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) — why this work interacts
  with the browser port, and why it must not overlap with it. See
  §"Sequencing against the wasm port" below.
* [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md) — where the per-firmware
  artifacts live, if you need to rebuild a NAND to change the plist edit.
* [`NEXT_SESSION_HANDOFF.md`](NEXT_SESSION_HANDOFF.md) — start here for the
  currently live thread, which is *not* this one.

---

## 0. T2 implementation status (2026-08-01)

| step | state |
|---|---|
| 1. MMU translation + aperture forwarding | **DONE** (`d3f3f39b5e`, `IT_MBX_MMU=1`). 4A102 boots to the home screen with it on; the guest's MMU enable/disable is visible in the trace. Default off. |
| 2. Command-stream capture | **DONE** (`IT_MBX_2D_TRACE=1`): dumps the block out of guest memory on the fire word, logging tags rather than interpreting them. |
| 3. Decode the block | format known (register-write pairs, five tags, `0x7…` terminator); needs one captured stream to confirm. |
| 4. Software blitter | not started — plain C, no host GL/threads. |
| 5. Memory-side completion, then the ISR bit | not started; this is the half all six failed register tricks were missing. |
| 6. Delete `LK_ENABLE_MBX2D=0` and verify by pixels | not started. |

### The order changed: the guest no longer exercises MBX2D at all

Three `--exercise` runs on 1.0 (one without forwarding, two with) produced
**zero** command-stream writes, with `0xa00000` still full of poison — and
the app genuinely opened and dismissed each time (screenshots confirm
Settings frontmost, then the home screen). So the earlier "bistable init"
reading was too kind: on this engine the 33-block stream does not happen
at all. What the guest does instead, from its own console:

```
AppleMBXUserClient::attach(AppleMBXDevice)      <- userland MBX2D connects
MPVD Sleep: … AppleMBXDevice::setPowerState(0)  <- then the device idles out
```

plus, in the register trace, MBX2D's init arm (`0x108=3`, ack `0x134=0xfff`,
arm `0x130=0xffff`), ONE FinishSurface timeout, and a teardown ending in
`0x1020 = 0x00010000` (MMU off). The client attaches and never commits
work; SpringBoard's snapshot render falls back to software and the
dismissal completes. (Incidentally that means the ~33.8 s dismissal
latency is not being paid in these runs either — worth measuring properly,
because the "latency residual" in the button investigation may already be
gone. **One attempt made, 2026-08-01, and DISCARDED:** `dismiss-latency.py
--board m68ap-10` on the current bundle delivered all 8 ladder presses per
KEYTRACE yet reported "never settled" — but the big change it caught was
the auto-lock DIM, its own documented artifact, and its 6-wall-second
press ladder collapses to ~2 guest seconds under the bundle's icount, so
the guest got a press burst inside the animation. The instrument predates
icount pacing; measure instead from the model's virtual-timestamped
`[LCD]` flips around a SINGLE press, which is what named the 33.76 s
constant originally. The acceptance battery's clean `3_home_returns` PASS
on this same bundle bounds the dismissal well under its 80 s window.)

**Consequence for the implementation order.** You cannot decode a format
you cannot capture, and you cannot capture a stream the guest declines to
emit. So the sequence is now:

1. ~~MMU + forwarding~~ **done** — and it is the prerequisite for both
   remaining paths.
2. **Make the guest commit to MBX2D**, by either:
   * **(a) removing `LK_ENABLE_MBX2D=0`** from the guest plist so LayerKit
     composites through MBX2D — step 6 promoted from "final acceptance" to
     "development driver", since it is the only reliable stream source.
     Cheapest route is the extract-HFS → edit → overlay loop
     (`scripts/extract-hfs-from-nand.py`, `overlay-hfs-into-nand.py`), NOT
     a full NAND rebuild; or
   * **(b) making FinishSurface complete**, which is step 5 (memory-side
     completion) — now tractable because the MMU gives us the addresses.
     The 244 live bytes at MBX VA 0x1b000 (reg 0x60c, "engine base") are
     the natural place to look for the op-state words the driver polls;
     the pc/lr trace names which offsets it reads.
3. Only then: decode (step 3) and blit (step 4).

Route (b) is the more principled one and unblocks the latency residual as
well; route (a) is the faster way to get a stream on the bench. They are
not exclusive.

### Correction: the op-state structures are NOT MMU-mapped

Measured (4A102, `mbx-mmu-probe` reverse-mapping every PTE against the
`AppleMBXDevice` address the guest prints on its own console):

```
AppleMBXDevice at VA 0xc0a52000       page table maps 39 pages
[obj+0x1a4] = 0xc0b85600 -> PA 0x08b85600   NOT MMU-mapped
[obj+0x1a8] = 0xf275a000                    NOT MMU-mapped, and not even
                                            kernel-linear (a separate
                                            IOKit mapping)
```

So the assumption that "the engine owns these words, therefore they live
in MMU-mapped memory" is **wrong**. The MBX's page table maps only 39
pages (~156 KiB) — the command and state area, nothing else. Two things
follow:

* A model that writes completions must address `[1a4]` **directly** by
  physical address, derived at runtime the way this probe does it
  (console-announced object → kernel-linear PA → read the pointer). That
  is derivable, not declarable — the pattern this project already trusts
  for the TVOut window — but `[1a8]`'s pointer is NOT kernel-linear, so
  its physical address needs a different derivation before anything writes
  there.
* Whatever updates those words on real hardware, it is not a walk through
  this page table. Do not build the completion path on that premise.

### And `+0x60` is a POINTER, not a completion flag

The structure at `[obj+0x1a4]` (PA 0x08b85600 on this boot), dumped live:

```
0000  00 20 a5 c0   00 a0 75 f2   d0 9f 86 c0   01 00 00 00
      ^ 0xc0a52000  ^ 0xf275a000  ^ 0xc0869fd0
      the device    = [obj+0x1a8]
0010  01 00 00 00   01 00 00 00   00 00 00 00   2c 81 74 f0
0050  01 00 00 00   01 00 00 00   00 00 00 00   00 00 00 00
0060  00 54 b8 c0   01 00 00 00   00 00 00 00   00 80 74 f0
      ^ +0x60 = 0xc0b85400
```

`+0x60` holds **0xc0b85400** — a kernel VA, 0x200 below the structure
itself, i.e. a pointer to a sibling. So the note carried forward from the
button investigation — *"the recovery sleep is taken when
`[[obj+0x1a4]+0x60] != 0`, and nothing in the kext writes it, so the
engine owns it"* — cannot be read as "clear it to signal completion":
that word is essentially always non-null, and zeroing it would corrupt a
pointer. Either the offset differs on 4A102 from the 1.0 kext it was
derived from, or the predicate tests something reached *through* that
pointer.

**This is why the completion path was not implemented tonight.** Writing
`0` there was the obvious next move and it would have been the seventh
wedge. Re-derive the predicate from the disassembly of the sleep site
against a live dump of `0xc0b85400`'s own structure before any device code
touches it. `mbx-mmu-probe` now prints both structures on every run, so
the evidence is one boot away.

## 1. What exists today

`hw/arm/ipod_touch.c`, `s5l8900_mbx_read` / `s5l8900_mbx_write` — about 25
lines total, mapped at `MBX_MEM_BASE 0x3B000000` over a 16 MiB region
(`include/hw/arm/ipod_touch.h`):

| access | behaviour |
|---|---|
| read `0x12c` | `0x100` |
| read `0xf00` | `(1 << 0x18) \| 0x10000` — "seems to be some kind of identifier" |
| read `0x1020` | `0x10000` |
| any other read | `0` |
| any write | **dropped** |

**No IRQ is connected to the MBX region at all.** The machine does wire
`S5L8900_TVOUT_SDO_IRQ` (`0x1E`), but to the TVOut device, not to MBX.

That is the whole model. Everything below is a consequence of it.

## 2. The two hacks standing on the stub

### T1 — the TVOut swap-device window (emulator-side)

SpringBoard attaches the `AppleH1TVOut` framebuffer, then waits for the TVOut
swap device to tear down before it re-attaches `AppleH1CLCD` and paints.
AppleMBX's teardown polls one field of the swap-device object and never sees it
clear, because our MBX completes no swaps. Upstream got past this
(`f59f20f60e`) by overlaying a **4-byte always-zero memory window** on that
field — `TVOUT_WA_FIELD_OFFSET 0x160` within the object, relative to
`KERNEL_VA_BASE 0xC0000000`.

The original form of this hack was a magic physical address baked in for one
kernel build, and when it is wrong **nothing complains**: you get four bytes of
kernel heap that happen to read as zero, and a hang somewhere unrelated. That
silence cost this project the entire M68AP render investigation, because the
iPod's constant does not match the iPhone kernel's heap. It is now *derived* —
the console tap reads the kernel's own `AppleMBX: Added swap device:
AppleH1TVOut id: c09c8400` line and moves the window there, on any board and
any build, reporting a mismatch and warning if the window is never read
(that was T3, done 2026-07-25).

Derived is much better than declared, but it is still an address-dependent hack
in the display path. **T1 is to model swap completion (and/or connect the SDO
IRQ) so the guest clears the field itself and the window can be deleted.**

### T2 — `LK_ENABLE_MBX2D=0` (guest-filesystem-side)

LayerKit — iPhone OS 1.x's pre-CoreAnimation compositor — composites through
the MBX 2D block by default. Against a stub that completes nothing, the kernel
ends in a **tight poll of `c03b9698`**, a register-read accessor
(`ldr r0,[r0,r1]; bx lr`) inside `com.apple.driver.AppleMBX`. So SpringBoard is
forced into software compositing by an environment variable in
`com.apple.SpringBoard.plist`.

**Both boards need this. Only one carries it in its own image.**

* **N45AP** — devos50's distributed iPod image already ships
  `EnvironmentVariables: LK_ENABLE_MBX2D = "0"`. Somebody made that edit before
  the image ever reached this project, which is why the iPod packaging has
  nothing to add and why the iPod appeared to "just render".
* **M68AP** — our NAND is generated from a *stock* 1.1.4 IPSW root filesystem,
  which has no such override, so it is applied at build time by
  `scripts/build-m68ap-homescreen-nand.py` (step 2/4, `plistlib` edit of
  `com.apple.SpringBoard.plist`).

If the iPod NAND were ever regenerated from stock, it would need the same edit.
**T2 is to model MBX 2D so neither board needs it.**

> Evidence note: the plist difference was read directly off both filesystems on
> 2026-07-25 (recorded in `M68AP_RENDER_HANDOFF.md`). A raw `strings` grep of
> the packs does **not** confirm it — M68AP's LayerKit binary contains the
> `LK_ENABLE_MBX2D` getenv literal itself, so string counts cannot distinguish
> the binary from the plist. Don't try to re-verify it that way.

## 3. Already ruled out — do not re-run these

Measured 2026-07-25, all recorded in `M68AP_RENDER_HANDOFF.md`:

* **`LK_ENABLE_MBX2D=0` alone does NOT make M68AP render** (`m68ap-mbx`
  variant): same wedge signature, phase=coresurface, only the iBoot framebuffer
  base, all PCs idle. The mutation itself was verified end-to-end — a replayed
  edit reads back from a fresh mount, and M68AP's LayerKit does contain both
  the getenv string and `"Failed to initialized MBX2D driver"`, so the knob is
  honoured by the iPhone build. It is necessary, not sufficient.
* **`m68ap-mbx-root` and `m68ap-prune` do not render either** — so the
  SpringBoard plist env, the `mobile`-vs-root user, and the thirteen extra
  launch daemons are all eliminated as the render blocker.
* **`AppleMBXUserClient::attach(AppleMBXDevice)`, logged only on N45AP, is a
  consequence rather than a cause** — of SpringBoard getting further, not of
  MBX being modelled.

What actually unblocked the M68AP home screen was the runtime-derived TVOut
window plus a reference-shaped data ark. MBX work is *cleanup and capability*,
not a blocker for anything currently broken.

## 4. Would modelling it speed anything up?

Plausibly yes, and by more in the browser than natively — but **this is
unmeasured and should not be used to justify the work until it is.**

The mechanism is the ordinary emulator one: today software compositing runs as
guest ARM instructions. A modelled MBX 2D would execute the same compositing as
a C loop in the device model. Natively that trades JITted guest code for native
host code. Under the wasm build it trades *interpreted* guest code (TCI) for
compiled wasm — a much larger ratio.

**How to find out cheaply, before writing any device code:** PC-sample the
guest during an animation and see what share of samples sits in LayerKit's
software compositing. `scripts/m68ap-freeze-probe.py` already does PC sampling
and kext mapping; point it at a running home screen instead of a freeze. If
compositing is not a large share, the performance argument evaporates and only
the fidelity argument (deleting two hacks) remains — which is still a real
argument, just a smaller one.

## 5. Sequencing against the wasm port

The browser port's go/no-go is whether **TCI** — the TCG *interpreter*, the
only backend this tree has, with no WebAssembly TCG backend in QEMU master
either (checked 2026-07-27) — can reach a usable SpringBoard, or whether the
out-of-tree qemu-wasm JIT becomes mandatory. That decision dwarfs this work.

Recommended order, and the reasoning:

1. **Get the wasm build booting and measure time-to-SpringBoard under TCI.**
   This does not need a display bridge: acceptance is serial output plus a
   framebuffer-content check, the same trick the native probes use.
2. **Decide TCI vs qemu-wasm.**
3. **Model MBX natively** (T1, then T2), validated against the native build as
   the oracle — the same framing the port already uses for the NAND pack seam.
4. **Then write the wasm display bridge and input path.**

Two reasons not to simply do the whole port first:

* **The display bridge would be written twice.** If the `OffscreenCanvas`
  dirty-rect bridge is built against today's derived TVOut window, and MBX swap
  completion is modelled afterwards, the frame-delivery signal changes
  underneath it. A modelled MBX gives explicit frame-completion events, which
  is exactly what a dirty-rect bridge wants.
* **MBX could move the go/no-go answer.** If TCI comes back "close but too
  slow", offloading compositing is a candidate lever — which is an argument for
  having the measurement in hand first, not for doing MBX blind.

The thing to actually avoid is steps 3 and 4 overlapping. Both wedge
SpringBoard rather than degrade when wrong, and debugging a black screen with
both in flight means two candidate causes and no oracle.

## 6. Risks

* **Silent failure is this subsystem's signature.** The TVOut hack failed
  silently for an entire investigation. Anything added here should report when
  its assumptions do not hold, the way the derived window now does.
* **Swap-completion semantics wedge rather than degrade.** Get them wrong and
  SpringBoard hangs with no diagnostic.
* **Scarce documentation.** PowerVR MBX register-level material is thin; expect
  to reverse-engineer `com.apple.driver.AppleMBX` behaviour. The kexts in the
  1A543a root filesystem retain full C++ symbols
  (`m68ap-artifacts/builds/1A543a/root.img`), which made the multitouch
  protocol readable with nothing but `otool -tV` and `nm | c++filt` — see
  [`TOUCH_INVESTIGATION.md`](TOUCH_INVESTIGATION.md). The same approach should
  work here.
* **Keep it a software blitter.** Plain C compiles to wasm with everything
  else. Reaching for host GL or threads would drag `SharedArrayBuffer` and
  COOP/COEP into the browser port.

## 7. Where to start

1. PC-sample an animation (§4) — decide whether performance is part of the
   case, or whether this is purely fidelity work.
2. Read `com.apple.driver.AppleMBX` around the `c03b9698` accessor and the swap
   path; identify which register the teardown poll actually reads.
3. Do **T1 before T2**. T1 is narrower (one field, one completion path,
   possibly just the SDO IRQ), it deletes an address-dependent hack, and
   succeeding at it tells you the register semantics are understood well enough
   to attempt the 2D command stream.
4. Both boards must be re-verified after either change — `IT_LCD_TRACE`,
   `IT_FB_TRACE`, `scripts/touch-probe.py`, `scripts/lock-unlock-probe.py`, and
   a home-screen render on all four 1.x builds.
