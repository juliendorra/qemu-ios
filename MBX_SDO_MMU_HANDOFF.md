# The MBX session: SDO complete; MBX legacy retirement active

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
| **T2 — MBX 2D** | **SHIPPED (2026-08-02).** Block format decoded and spec'd (MBX_2D_FORMAT.md); plain-C rasterizer executes 2D streams into the guest's own surfaces; completion chain closed (kick 0x40\|0x400, per-op 0x45c, TA doorbell reg 0x680 → 0x45d). The 1A543a `_mbx2DInitialize` staged patch is retired by default; the snapshot client runs on the modeled engine. Final verification: **all three bundles 6/6 on the strict oracle at shipped defaults** (1.0 unpatched-MBX, 1.1.4, iPod; IT_PROBE_WAIT=4, deflaked probe). Open, in order of tractability: TA quad rasterization is **blocked at a documented boundary** — the dest geometry is captured (axis-aligned screen rects, one shared atlas, 26 text-label strips) but the texture base-address + UV encoding are packed PowerVR MBX TA control words that need the TA datasheet; decoding them by guess is what the moratorium forbids, and no shipping frame depends on it (final composite is guest-rendered, oracle diff 0.38%). Also open: 2D blend equations/rotation, forced LK_ENABLE_MBX2D=1 full-compositor mode (parked), 3A109a/1C28 artifacts. |
| §4 performance gate | Measured: compositing is 2–4% of guest CPU natively; the wasm-transferable bound is (8–19% non-idle share) × (guest-code fraction of the wasm vCPU thread, ~21% today). Fidelity-only for now; re-run the arithmetic after the wasm speed campaign. |
| Packaged apps | All three updated to the new engine and re-verified: iPod 5/5, 1.0 5/5, 1.1.4 5/5 (`IT_PROBE_WAIT=4`). |
| 3A109a / 1C28 | NOT verified — their NAND artifacts are not on disk (regenerate per BUILD.md). |
| 1.0 dismissal latency | The 33.8 s regime is not reliably present (the guest takes the software-fallback path), but a clean number was NOT obtained — the only instrument tried is icount-blind and its run was discarded. |

### What the current MBX model actually does

The current implementation is a **hybrid device model**: it contains real MBX
transport/MMU emulation, but it does not yet contain a complete MBX command
processor or rasterizer. It is not GPU passthrough and sends no MBX command to
macOS, Metal, OpenGL, WebGL, or another host graphics API.

There are two materially different execution paths:

1. **Default/product path (working graphics).** All currently packaged guests
   produce pixels with original guest-side software compositing, but they reach
   that path at different decision points. None of these controls injects a
   QEMU renderer. LayerKit's ARM code runs on the emulated CPU and writes pixels
   into guest surfaces and the framebuffer; the emulated LCD then presents the
   completed framebuffer.

   ```text
   LayerKit guest software compositor
       -> emulated ARM CPU
       -> guest surface/framebuffer memory
       -> emulated LCD
       -> QEMU display window
   ```

   | guest/build | how it reaches software compositing |
   |---|---|
   | **iPod Touch / N45AP** | The supplied devos50 NAND already carries `EnvironmentVariables: LK_ENABLE_MBX2D="0"` in `com.apple.SpringBoard.plist`. LayerKit therefore selects its software compositor without any per-launch binary patch. This setting belongs to the supplied image, not to QEMU's raster code; a newly constructed stock image would need equivalent steering until MBX rasterization is complete. |
   | **iPhone OS 1.1.4 / 4A102** | The prepared product NAND carries the same `LK_ENABLE_MBX2D="0"` steering (the stock IPSW plist did not). Its newer LayerKit transition also does not enter 1A543a's problematic legacy backing-store renderer in the tested app/HOME path. It therefore needs no `_mbx2DInitialize` patch and renders in guest software. |
   | **iPhone OS 1.0 / 1A543a** | `LK_ENABLE_MBX2D=0` alone is not sufficient: it steers the primary compositor, but the older app-snapshot/backing-store client still initializes MBX2D and submits shared-surface work. `scripts/ipod-app-launcher.sh` therefore makes `_mbx2DInitialize` report failure only in the disposable staged NAND, unless `IT_IOS10_SOFTWARE_MBX2D=0` explicitly requests protocol investigation. That measured failure point makes the old LayerKit select its existing software renderer. “Backing store” and the separate-client boundary are defined in [`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md#what-backing-store-means-here-and-why-the-ordinary-flag-is-insufficient). |

   The difference is therefore **how the guest is steered**, not who draws the
   pixels. N45AP and 4A102 avoid MBX2D through their prepared SpringBoard
   configuration/newer path; 1A543a additionally needs a guarded initialization
   failure because its older backing-store renderer otherwise submits work
   despite the ordinary LayerKit setting.

2. **Forced MBX2D path (protocol investigation).** With the staged fallback
   disabled, LayerKit and AppleMBX emit the real legacy command stream. The
   model receives the MMIO writes, translates MBX virtual addresses through
   the guest-programmed eight-entry MMU, forwards aperture accesses into the
   corresponding guest RAM pages, captures the command words, and partially
   models surface-list retirement and ordered completion events. It currently
   does **not** turn those command packets into pixels. Therefore forced MBX2D
   can exercise the device protocol but cannot yet produce a correct screen.

   ```text
   LayerKit MBX2D -> AppleMBX -> emulated MBX transport/MMU/completion
                                    -> command captured, no rasterizer yet
   ```

The boundary by subsystem is:

| subsystem | current implementation |
|---|---|
| Register identity/status | Functional compatibility behaviour; some values and immediate transport completions are approximations. |
| Event mask, acknowledge and IRQ line | Partially modelled hardware behaviour. |
| Eight-entry MBX MMU | Functional emulation using the guest's own page directory and PTEs. |
| Shared aperture | Functional forwarding into the translated guest physical RAM pages. |
| Surface-ring retirement | Partially modelled and still diagnostic (`IT_MBX_2D_RING=1`). |
| Operation-token unlink/release | Incomplete; `state1+0x60/+0x64` remain the current open boundary. |
| Legacy command rasterization | Not implemented. The rejected speculative copy/color decoder is not shipped. |
| Host graphics passthrough | None. The host only displays the guest LCD framebuffer. |

Thus this is more than a register-only mock, but it is not yet full hardware
emulation. Production currently combines an emulated MBX transport device with
the guest OS's native software-rendering fallback. The intended next renderer
is a deterministic, portable command consumer inside QEMU so the same code can
run natively and under WebAssembly; a macOS-only host API is not required.

### Environment variables added or changed

| var | default | meaning |
|---|---|---|
| `IT_TVOUT_SDO` | **ON** | the SDO field-interrupt model. `=0` restores the old stub AND re-arms the derived zero-window — the one-knob A/B. |
| `IT_MBX_MMU` | raw QEMU: off; iPhone launcher: **on** | forward MBX aperture accesses through the guest's own page table into guest DRAM. |
| `IT_MBX_2D_TRACE` | off | dump a 2D command block out of guest memory when the guest fires it (needs `IT_MBX_MMU=1` to see anything the model didn't store). |
| `IT_MBX_2D_EVENT` | raw QEMU: 0; iPhone launcher: **0x4c** | event mask for a legacy 2D fire. Bit `0x10` is suppressed unless the diagnostic ring-completion model is enabled and its memory-side preconditions pass. Ordered `0x5c` is the current forced-path experiment. |
| `IT_MBX_2D_RING` | off | diagnostic surface-list retirement and ordered bit-`0x10` completion; not a rasterizer and not enabled in the product path. |
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

### 0.1 Current continuation: forcing the stream exposed the retirement contract

The earlier blocker was removed without touching the installed firmware:
`mbx-mmu-probe.py --force-mbx2d` edits the same-length `Q0` → `Q1` value in
the cloned NAND's binary plist (sparse page or packed entry), then drives the
whole case through QMP.  On 1A543a this produced the real traffic:

* `0x6d8` is the engine kick; `0x824..0x83c` are its descriptors.
* `0x1020 = 0x10100` is a microcode-upload strobe, not a kick, and must not
  clear the independently latched MMU enable.
* The bootstrap/queue reply is `0x40|0x08|0x04 = 0x4c`.  Raising `0x40`
  alone leaves the Graphics watchdog restarting the engine; entering the
  guessed bit-`0x10` render ISR panics because its shared state is absent.
* `state1+0x60` is a pointer to the live operation entry, not a busy flag.
  Only the guest's `c0336988` retirement path may clear it, after decrementing
  `state1+0x64`, unlinking the `entry+0xb8` token, and releasing resources.
* Event `0x10` enters `c0337c64`, which traverses the surface list at
  `record+0x34c`. Each node must contain a CPU-traversable page-array pointer
  before the event is raised. IRQ-first attempts faulted first at `0x8aae`,
  then at an unretired tail node (`0x8080eb00`).
* The exact first-pass tail is `{ array=0, next=0x8aae }`; `0x8aae` is the
  physical page token for the submitted command block. Consuming it by clearing
  the terminator's `next`, publishing `state2+0x24`, and then raising ordered
  event `0x5c` eliminates both the panic and the Graphics watchdog restart.
  The 30-second staged run retained `state1+0x60/+0x64 = operation/1`, so the
  final operation-token unlink/release boundary is still open; the model does
  not clear either field directly.
* 29 distinct writes in the `0xa00000` stream aperture matched the translated
  DRAM bytes exactly. This validates the MMU and forwarding implementation.

Pixel validation then rejected a tempting partial solution. The first two
packets are a full-screen color operation and a surface copy, but executing
only those yields an incomplete/corrupt SpringBoard frame. The old classifier
mislabelled that image as `home`; direct image inspection caught it. Named
`MBX2D.framework` packers explain the packets, while the scene content depends
on the accompanying PowerVR MBX engine work. A correct implementation is thus
a GPU command processor, not another register or memcpy shim.

Current policy: keep the truthful MMU/transport model and scripted probe, do
not ship the rejected speculative raster decoder, and retain the staged,
hash-guarded LayerKit software-renderer fallback for 1A543a until the full
surface-ring retirement and strict eight-second boot/in-app oracle pass. No
guest FTL read or storage-validation bypass is used.

### 0.2 The 2026-08-02 session: format decoded, rasterizer built, TA doorbell answered

**Session timeline** (history, in order; details in the bullets and the
dead-end table 0.2.1 below):

1. Disassembled the 2D packers + setters (MBX2D.framework, full symbols)
   and MBXConnect's transport → the block grammar and the method-7 record
   scheme.
2. Captured a live forced-mode stream (`--force-mbx2d`, mbx-cap10) — the
   boot fill block matched the decoded grammar word for word; found the
   forced boot then wedges: guest sleeps on event 0x10 after the ISR eats
   the 0x4c reply.
3. Implemented the plain-C rasterizer (`IT_MBX_2D_RASTER`) with a shadow
   for the fire-clobbered word 0; fixed the rect-axis misread (dead end
   #16) against the captured fill.
4. Extracted the kernel side (kc10, −0x2000 slide, dead end #24): ISR bit
   dispatch, retirement (live c0336988), surface handler (c0337c64),
   queue-retire (c0337ce8), retire wrapper's 0x6d8 sync kick polling
   status 0x400 → `0x6d8` now answers `0x40|0x400`.
5. Re-ran forced mode with raster+ring+0x5c: both boot blocks executed
   and completed — but the boot froze at the Apple logo. Diagnosed with
   screendumps: the stalled client is the BOOT PROGRESS PAINTER (dead end
   #18); found the 0x400 blanket-ack wipe (dead end #17) → event set
   0x45c.
6. With 0x45c the boot painter completes, but full-forced
   (LK_ENABLE_MBX2D=1) SpringBoard still stalls in its userland present
   path → PARKED; pivoted to the product-relevant unpatched snapshot
   path (IT_IOS10_SOFTWARE_MBX2D=0).
7. Strict oracle on the unpatched bundle: steps 1-2 PASS, step 3 froze
   with ZERO dismissal-time MBX traffic. Built mbx-freeze-driver +
   dfilter exec tracing (dead end #23 first): userland packs mostly
   mbx3DCtxBlitCopy records and hangs INSIDE the method-7 ioctl.
8. Kernel trace named the taWatchdog loop; decoded the parser's 12-entry
   jump table, the shared 3D handler, and REGISTER 0x680 — the TA
   doorbell — plus the forever-commandSleep on state2+0x74 and ISR
   bit-0x1 parse-resume.
9. Answered the doorbell (deferred events 0x45d): the unpatched 1.0
   dismissal completes — 26 TA ops consumed, home screen restored.
   A parallel subagent decoded the 3D record layout (MBX_2D_FORMAT.md).
13. **TA quad rasterizer: reached its evidence boundary and stopped by
    policy (2026-08-02, after the ship).** Added engine-register capture
    (0x600-0x6ff) + doorbell-time dumps of the TA input pages (regs
    0x608/0x60c) under IT_MBX_2D_TRACE, and captured a full 26-op
    dismissal (mbx-freeze-driver, /private/tmp/mbx-ta-dump2). Decoded:
    the region page's last object is the textured quad — TSP/texture
    words CONSTANT across all ops (one shared atlas), dest quads are
    axis-aligned screen rects in plain floats, unit perspective w; the
    26 quads tile the screen as text-label strips. NOT decoded: the
    texture base-address + per-quad UV mapping, which are packed TA
    control words needing the PowerVR MBX datasheet. Per the moratorium
    (don't invent engine semantics), the rasterizer stops here rather
    than ship guessed sampling; full evidence + the exact restart point
    are in MBX_2D_FORMAT.md ("TA quad geometry"). No shipping frame
    depends on it. Re-verified the trace-instrumented engine still passes
    the oracle (the added code is dump-only + a cheap unconditional
    register store; completion behaviour unchanged).
12. **FINAL: all three bundles re-installed with the new engine + launcher
    and verified 6/6 each at shipped defaults** — iPhone OS 1.0
    (snapshot client on the modeled MBX engine, no patch), iPhone OS
    1.1.4, and the iPod. Reports: /private/tmp/final-{10,114,ipod}
    (regenerable).
11. **VERIFIED, then RETIRED (2026-08-02, end).** At the parameters the
    5/5 record was set with (IT_PROBE_WAIT=4) and with the deflaked
    probe, BOTH configs pass the strict oracle **6/6**: pure-defaults
    product (no regression) and the unpatched MBX engine
    (home_reference_diff 0.38%). The launcher's `_mbx2DInitialize`
    staged patch is now OFF by default (IT_IOS10_SOFTWARE_MBX2D=1
    re-enables it for A/B) and the iphone-2g profile ships
    IT_MBX_2D_EVENT=0x45c, IT_MBX_2D_RASTER=1, IT_MBX_2D_RING=1.
    Earlier same-day runs that failed steps 2/3/4/5 were instrument
    artifacts, all root-caused: one-shot mid-flip grabs (dead end #26)
    and the W=10 auto-lock collision (dead end #25).
10. Full strict oracle, unpatched 1.0, run 1: steps 1/4/5/6 PASS —
    dismissal works end to end (step 4 reopened the app FROM the restored
    home screen; POWER sleeps; HOME wakes). Steps 2 and 3 failed on KNOWN
    pre-existing artifacts, verified by direct image comparison: step 3's
    raw scanout grab caught a mid-flip black buffer (lit_after 0.57%,
    triple-buffer bases cycling at t=308.4s in the LCD trace) while the
    simultaneous screendump is pixel-identical to the home reference
    (<0.7% of pixels differ at any threshold; only carrier text + clock
    changed) — the documented "black screen is scanout" one-shot
    artifact; step 2 is 1.0's documented bistable in-app delivery (it
    passed 35.94% in the previous run of the same config). Re-run +
    product-config control queued. TA quad rasterization remains the
    next milestone (pixels for the zoom texture).

* **The 2D command-block format is no longer structural guesswork.** The
  packers (`_pack2DCtxBlitColor` 0x30b3994c / `_pack2DCtxBlitCopy`
  0x30b3a974) and every `Set*` field writer were fully disassembled
  (MBX2D.framework, 1A543a root, full symbols), and the decode was verified
  word-for-word against a live captured forced-mode stream. Full spec in the
  session scratch (`mbx2d_format_spec.md`) — summary: `0xA`/`0x94` dest/src
  descriptors (stride|format, then a kernel-resolved MBX-VA address word),
  `0x3` src position (x low, y<<14), optional `0x2000_0004`+blend, optional
  scissor (`1`, y-range, x-range), `0x6` scale (1.0=0x20), `0x8` control =
  blend-en 0x20000 | scissor-en 0x40000 | rotation bits | ROP16 (`F0F0`
  fill / `CCCC` copy), operand (fill colour in dest format, or 0xFFFFFFFF),
  two dest-corner words (**Y low half, X high half** — measured off the
  480-axis), 0x7000_0000 terminators (6 for color, 1 for copy).
* **Transport understood:** userland packs `{cmd, words, surfIDs, block}`
  records into a shared buffer (`_mbxGetCommandSpace`), user-client method 7
  submits, and the KERNEL emits the tagged stream into 0xa00000, patching
  surface MBX addresses into the descriptors' second words.
* **A plain-C rasterizer now exists** (`IT_MBX_2D_RASTER=1`,
  `mbx_2d_execute_stream` in hw/arm/ipod_touch.c): parses fired streams
  through the MBX MMU, executes F0F0 fills and CCCC copies (scissor,
  nearest scale) into the guest's own surfaces, executes BEFORE any
  completion event, logs-and-skips anything outside the decoded grammar.
  No host graphics API, no threads — wasm-safe by construction.
* **The retirement mystery is solved.** Full ISR decode (kc10, file base =
  live − 0x2000): bit 0x40 latches state2+0x2c and the 0x40|0x08|0x04 join
  clears the latches and runs queue-retire (live c0337ce8); bit 0x10 runs
  the surface handler (live c0337c64: marks w*h 8-byte entries per
  record+0x34c node with 0x20000000, sets state2+0xc=1, slot=3). The woken
  FinishSurface then calls the retire wrapper (live c032f1ec) which **kicks
  a sync descriptor through 0x6d8 and POLLS STATUS BIT 0x400** (kick live
  c032e4fc) before unlinking token 0x4000 and clearing state1+0x60. Our
  model never raised 0x400 on the 0x6d8 kick — that is why the ordered-0x5c
  experiment survived but left state1+0x60/+0x64 = operation/1 forever.
  Fixed: 0x6d8 now answers 0x40|0x400.
* Forced-mode boot with the pre-fix engine wedges at the first real 2D op:
  the guest fires, the ISR consumes the 0x4c reply, re-arms 0x130=0xffff,
  and sleeps waiting for 0x10 — measured in /private/tmp/mbx-cap10.
* **The stalled forced-mode boot is the BOOT PROGRESS PAINTER, not
  SpringBoard.** The two boot blocks (full-screen fill + bottom-strip blend
  copy) are the boot spinner's; the guest freezes at the Apple logo right
  after the first op completes. Cause found in the trace: after the 0x6d8
  kick the guest deliberately acks only 0x40 and LEAVES 0x400 LATCHED, but
  the FinishSurface sleep wrapper's blanket `0x134=0xfff` ack wipes the
  latched 0x400 before arming `0x130=0xffff` — so the ISR never dispatches
  bit 0x400 and `obj+0x1c4` (the engine-ready byte, set ONLY in the ISR's
  bit-0x400 path) stays 0, which makes every later user-client method bail
  out early. On hardware the engine re-raises 0x400 per completed queue op,
  so the 2D completion event set must include it: `IT_MBX_2D_EVENT=0x45c`.
* Method c032f6f0 (live): kick_sync → enqueue op (c0336064) → 1 s
  commandSleep via `[obj+0x218]` vtbl+0xf0 — the wait every op takes.
* With `0x45c` the boot-time init test blits complete and SpringBoard
  boots normally on the PRODUCT config with the `_mbx2DInitialize` patch
  DISABLED — the strict oracle then passes `1_open_app` and
  `2_touch_in_app`, and fails `3_home_returns` frozen.
* **The dismissal freeze is decoded (exec-trace, three instrumented
  runs).** The snapshot render packs a large stream that is mostly
  `_mbx3DCtxBlitCopy` (the 3D QUAD path — LayerKit uses textured quads
  for scaled blits), submits it via user-client method 7, and the kernel
  never returns from the ioctl: `waitForHWContext - commandSleep` waits
  for a hardware 3D context the engine never frees, while the taWatchdog
  timer loops `Graphics Restart. TA hung while completing.` (live loop
  0xc032b340..b400; watchdog fields: state2+0x44 = tick, +0x48 =
  phase-start tick, +0x4c = TA-active — cleared by the ISR bit-0x10 case).
  The 2D rasterizer alone therefore cannot pass the oracle: the missing
  piece is a TA/3D-QUAD SUBSET — decode the `mbx3D*` packers (full
  symbols on the 1.0 root) + the kernel 3D emit ("3D blit region header /
  object data surface"), execute textured-quad copies in plain C, and
  complete the TA phase protocol (context slots, +0x48/+0x4c, TA events).
* **THE DISMISSAL WORKS (2026-08-02, late).** The 3D path decoded to the
  end: the method-7 parser (live 0xc032e77c, jump table cmd 1..12; 4/5 =
  2D, 6-9/11/12 = shared 3D handler live 0xc032ebf0, 10 = its own) programs
  the engine from the record (live 0xc032bb24: 0x608/0x60c/0x614/0x61c),
  runs the sync descriptor, sets state1+0x34=1 / +0x40=0xabcdabcd, and
  rings **register 0x680 = 1 — the TA doorbell**; it then commandSleeps
  FOREVER on state2+0x74, woken only by queue-retire from the ISR join,
  and ISR bit 0x1 re-enters the parser for the remaining records.  The
  model now answers the doorbell with a deferred (virtual-clock, 200 us)
  event raise of 0x45d — join + render-complete + engine-ready + parse-
  resume.  With that, the UNPATCHED 1.0 snapshot client (the
  _mbx2DInitialize patch disabled) completes a full app dismissal: 26 TA
  doorbells consumed, home screen restored, screendump-verified.  TA quad
  RASTERIZATION is still pending (logged loudly per op); the visible cost
  is limited to the transient zoom-animation texture.
### 0.2.1 Dead ends, false paths, and corrections of this session

| # | belief / attempt | how it died | what replaced it |
|---|---|---|---|
| 16 | first parser cut read rect words as X-low/Y-high | the captured boot fill's rect `0x014001e0` puts 480 in the LOW half of a 320-px-wide, 0x500-stride surface — rows would overlap | rect words are Y-low/X-high (the packers' arg order is (y, x, …)); the 0x3 src-position word stays X-low/Y<<14 |
| 17 | "the guest re-raises nothing; one latched 0x400 will do" | trace: guest deliberately acks ONLY 0x40 after the kick, keeping 0x400 latched — then the FinishSurface sleep wrapper's blanket `0x134=0xfff` wipes it before arming; `obj+0x1c4` never set; every later user-client method bails | the engine re-raises 0x400 per completed op: completion set is 0x45c (now 0x45d) |
| 18 | "the forced-boot stall is SpringBoard's lock screen" then "a telephony Repair-Needed alert" | screendump: the guest sits at iBoot's APPLE LOGO — the stalled client is the boot progress painter, pre-SpringBoard | forced-mode LK_ENABLE_MBX2D=1 full-compositor bring-up is PARKED as a stretch goal; the product-relevant snapshot path was pursued instead |
| 19 | "0x85c is a fence counter the engine must advance" | the join path read 0x85c and PROCEEDED on 0: reads-as-0 is the completed answer (matches the 2026-07-31 'answering 0 is load-bearing' finding) | left as-is; no fence model needed |
| 20 | "state2+0x1c/+0x20 are engine-written swap-completion words" (and c032b44c is a 'swap-join checker') | the function logging `waitForHWContext - commandSleep failed` IS live 0xc032b44c: +0x1c/+0x20 are the hardware-3D-context BUSY words, slept on at &state2+0x1c, woken by the ISR join's commandWakeup | relabeled; the freeze was never swap-side |
| 21 | "retirement is deferred by design, quiescence with state1+0x60 set is fine" | half-true: true for 2D-only quiescence, but the actual dismissal freeze was the un-answered TA doorbell, not retirement | the TA completion (events 0x45d off the 0x680 write) |
| 22 | static hunt for AppleMBXUserClient's IOExternalMethod table | no such table — the dispatch is switch-based; a 5-word-entry scan finds nothing | the method-7 record parser's own 12-entry jump table at file 0xc032c8e4 |
| 23 | mbx-mmu-probe --exercise on the artifacts NAND as the snapshot repro | the icon tap never opened the app (in-app frame classified home; zero fires) — run invalid | scripts/mbx-freeze-driver.py: scripted QMP app-open + HOME against the packaged bundle, with dfilter exec tracing |
| 24 | kernel addresses taken from live traces used directly on kc10r.raw | the restore kernelcache is shifted: file VA = live VA − 0x2000, uniformly (agent burned a pass on 'wrong' addresses that were strings/data) | translate live→file with −0x2000 before disassembling; noted here because every doc's addresses are LIVE |

| 25 | "run the 1.0 oracle at IT_PROBE_WAIT=10 for safety" | the pure-defaults control then failed steps 2/4/5 with DEAD INPUT on a perfectly restored home screen — W=10 stretches every step past 1.0's auto-lock window, reproducing the documented auto-sleep touch death; the verified-5/5 parameter for this board is IT_PROBE_WAIT=4 | verify at the parameters the 5/5 record was set with; W=10 is for 1.1.4's `1_open_app` window only |
| 26 | "one raw scanout grab is a fair judge for 3_home_returns" | three runs in a row (both engine configs) grabbed a mid-flip black buffer (lit 0.57-0.69%) while the simultaneous screendump was pixel-identical to the reference | app-button-probe `regrabs`: re-SAMPLE without re-acting, verdict unchanged (commit 003646244f) |

Operational notes: IOLogs (Graphics Restart spam, EnqueueC…) go to SERIAL,
which the bundle launcher sets to null — a silent-looking stall can be a
logging loop; qemu.log only carries stderr. And `-d exec` PC extraction
must parse the SECOND field of the bracket triple (`[flags/PC/…]`).

* Instrumentation recipe that found this (reusable): freeze-driver
  (scripted app-open + HOME over QMP against the packaged bundle) with
  `-dfilter 0x30b37000+0x9000,0x31baf000+0x2000,0xc032b000+0x11000
  -d exec,nochain` — userland MBX2D/MBXConnect + the AppleMBX kext.
  IOLogs go to serial, not stderr; the bundle launcher uses -serial null.

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
