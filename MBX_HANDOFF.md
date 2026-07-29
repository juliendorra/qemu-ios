# MBX (PowerVR) — session handoff: the stub, and the two hacks that stand on it


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
> the iPod have NOT been re-tested with it. Full record:
> [`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md).

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
