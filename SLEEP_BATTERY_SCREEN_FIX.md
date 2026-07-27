# Sleep shows the low-battery charging image — investigation and fix

Status: **fixed and verified** on QEMU 11 (`ipod_touch_1g`), 2026-07-20.
Fix commit: "Keep the panel dark during iBoot's pre-warm charging scanout"
(`hw/arm/ipod_touch_lcd_panel.c`).

> **Was N45AP-only until 2026-07-26.** Everything below describes the iPod.
> On `-M iPhone-2G` this code had never executed: the M68AP device tree puts
> the PMU on **i2c0** while the machine attached our `pcf50633` model to
> **i2c1**, so every PMU read returned 0xFF, the guest concluded it was
> permanently on external power, and `ApplePCF50635PMUPowerSource` logged
> `disabling idle sleep` — the iPhone never auto-locked and never reached
> OOCSHDWN. That also caused the iPhone's "always shows the charging battery"
> and "clock stuck at the epoch" symptoms: one defect, three symptoms.
>
> **Fixed by c5ea96a7e1**, and verified on 1.1.4 (`mbcs1-3 00 00 00`,
> `ext 0 … cap 61`, `enabling idle sleep`, RTC reading correct UTC, wallpaper
> restored). The iPhone now auto-locks and reaches this sleep path for the
> first time — and it works: three consecutive Power/Home wake cycles each
> relight the lock screen within 2 s, hold it ~9 s, then switch the display
> off, exactly as N45AP does under the same probe. Everything below therefore
> now applies to **both** boards. See the 2026-07-26 PMU-bus section of
> `IPHONE_2G_BRINGUP_HANDOFF.md`, including the recorded dead end: a single
> post-wake `screendump` lands past the lit window and looks like a dead
> panel — always sample a time series.

## Symptom

When the emulated iPod Touch 1G went to sleep (auto-lock after 1 minute, or a
Power press), the host window showed the red **low-battery / charging image**
(empty battery + lightning bolt) with the QEMU title bar reading
`QEMU [Sleeping]`, instead of a dark screen. A real device shows a black
screen when merely asleep.

This was intermittent-looking at first and easy to mis-diagnose:

- It only appeared after a sleep, and the interactive dev harness had set
  **Auto-Lock = Never**, so day-to-day testing never triggered it. The
  packaged app ships with the stock **Auto-Lock = 1 minute**, so it showed
  reliably there.
- The `QEMU [Sleeping]` title (QEMU's own "all vCPUs halted / in WFI"
  indicator) made it look like the guest had *wedged*. It had not — the guest
  was asleep and waiting for a wake button.

## Dead-ends / wrong turns (recorded so they are not repeated)

1. **"The packaged app is wedged by `nand.pack`."** Because the app hung on
   the charging image while a loose-page boot reached SpringBoard, the pack
   looked guilty. It was not:
   - The pack's bytes are identical to the loose pages — verified across all
     132,894 pages (`0` content diffs, same page set).
   - The app boots to SpringBoard from the pack every time; it then
     auto-locks and sleeps. What looked like "wedged at charging screen" was
     "asleep, showing the wrong sleep image, CPU idle in WFI."
   - Confirmed by waking it: Home returns to the lock screen.
   Do not re-investigate the pack for this symptom.

2. **"It's a PMU battery-level / charger-detection bug."** The battery ADC
   reports a healthy 3.80 V (`ADCS1=0xA2`, `ADCS3` low bits 0 → 648/1023·6 V),
   and `MBCS1` reports no charger during normal runtime. The battery *level*
   is not why the image appears. (The `MBCS1` USB-present hack is the deeper
   cause of iBoot drawing it at all — see below — but changing the reported
   level does not fix the visible image.)

3. **Assuming `panel_off` alone gated it.** The retained-wake reset sets
   `panel_off = true` specifically to hide iBoot's temporary scanout, and the
   LCD honours it — yet the image still showed. Something was clearing
   `panel_off` back to false during the pre-warm.

## Root cause (proven by instrumentation)

On an **untouched sleep**, the machine does not simply idle. It *pre-warms*
the retained-RAM wake: it reloads iBoot and runs it up to the type-4 handoff,
then parks the whole VM (`vm_stop(RUN_STATE_SUSPENDED)`) awaiting a Power/Home
press. This makes a later wake cheap. (See `ipod_touch_pcf50633_pmu.c`,
`PMU_OOCSHDWN` handler and `pcf50633_prewarm_park`.)

> **Both park paths must call `vm_stop()` from a bottom half, never from a
> timer callback** (2026-07-27). `vm_stop()` → `pause_all_vcpus()` disables
> `QEMU_CLOCK_VIRTUAL` and waits for its timerlists to finish their callbacks,
> so calling it *from* such a callback deadlocks the entire main loop — the VM
> parks and then ignores QMP and every key forever. This is what broke iPhone
> OS 1.0's park (its iBoot-159 never writes the type-4 commit, so it parks off
> the deadline timer rather than the BH). Finding #96 in
> `SLEEP_WAKE_INVESTIGATION.md`.

During that pre-warm iBoot run, iBoot enters its pre-boot **charging
dispatcher** — it believes external power is present because the emulator's
`MBCS1` register is deliberately reported as `USBPRES | USBOK` during the
wake window (a hack that keeps iBoot's warm image-verification path from
selecting the low-battery UI). The charging dispatcher then:

1. draws the battery/charging image into iBoot's scanout at `0x0fe00000`, and
2. sends the Merlot panel a **MIPI DCS 0x11 (Sleep Out)** to light it up.

The panel model (`ipod_touch_lcd_panel.c`) honoured that Sleep Out and set
`panel_off = false`. With the active framebuffer base still iBoot's
`0x0fe00000`, the LCD scanned out the battery image. When the pre-warm parked,
`vm_stop` froze that last frame on screen.

Instrumented proof at the park point:

```
[WAKE] Pre-warming retained-RAM wake after OOCSHDWN
[LCD] Merlot panel woke from sleep      <- iBoot's Sleep Out during pre-warm
[LCD] Merlot panel woke from sleep
[WAKE] Pre-warm reached type-4 commit; parking
[DBG-PARK] panel_off=0 invalidate=0 base=0x0fe00000 retained_resume=1
```

`panel_off=0` (panel on) + `base=0x0fe00000` (iBoot battery scanout) +
`retained_resume=1` (kernel has **not** reclaimed the display yet) =
battery image visible on a device that is only asleep.

## Fix

The codebase already has a flag for exactly this window: `retained_resume`
stays true from the wake reset until the resumed *kernel* reprograms the OS
framebuffer base (`0x0f400000` / `0x0f496000`) in the LCD `0x60` register
handler, which is where `retained_resume` and `panel_off` are cleared
together (`ipod_touch_lcd.c`). The bug was that the panel's Sleep-Out path
bypassed that gate.

The fix gates the Merlot Sleep-Out relight on `retained_resume`:

```c
/* ipod_touch_lcd_panel.c, MIPI DCS 0x11 (Sleep Out) */
if (s->lcd->retained_resume) {
    /* iBoot's pre-warm charging dispatcher is relighting the panel to show
     * its temporary battery/logo scanout at 0x0fe00000, before the resumed
     * kernel owns the display. Keep the panel dark until the kernel reclaims
     * scanout by programming the OS framebuffer base. */
    return 0;
}
```

While iBoot's scanout is active, the panel stays dark, so a sleeping device
shows black. When the kernel resumes and writes the OS framebuffer base, the
`0x60` handler clears `retained_resume` + `panel_off` and the real UI lights.

## Is this a true fix or a hack? Downsides.

**It is a true fix in the sense that matters here, and it is consistent with
the existing design** — it extends the same `retained_resume` gate that
already suppresses iBoot's logo/battery scanout at the framebuffer-base level
to the panel Sleep-Out command that was slipping past it. It does not weaken
or race the fragile sleep/wake state machine, and wake still works (verified).

Honest caveats:

- **It is symptom-level, not cause-level.** The deeper cause is that iBoot
  runs its *charging* dispatcher during the pre-warm because `MBCS1` is
  reported as `USBPRES | USBOK`. The truly "root" fix would make iBoot not
  enter the charging UI at all. That was **not** done because `MBCS1`'s
  USB-present report is load-bearing for the warm image-verification path
  (reporting `USBPRES` without `USBOK` selects the low-battery UI; reporting
  nothing risks the warm path rejecting its power source). Touching it risks
  regressing wake, which took a long bring-up to stabilise. Suppressing the
  scanout is the surgical, low-risk choice.
- **iBoot still does the work.** It still draws the battery image into
  `0x0fe00000` during the pre-warm; we simply never scan it out. This is
  harmless (that buffer is iBoot-owned and overwritten every wake) but it is
  not "we stopped the device from thinking it is charging."
- **It relies on `retained_resume` always being cleared by the kernel's
  framebuffer-base write.** That is invariant on this firmware (it is how the
  display is restored on every wake — the existing finding-#92 path), so the
  panel is guaranteed to relight on a real resume. If a future firmware
  resumed the display by a different register write, this gate would need to
  clear on that path too.
- **No real charging scenario is lost.** This emulator has no battery and no
  charger — the charge state is fiction — so the charging image is never a
  desirable state to display. There is nothing legitimate being hidden.

### Is it true to the original hardware?

Observable behaviour: **yes.** A real iPod Touch 1G asleep shows a black
screen; after the fix the emulator shows a black screen. The user-visible
result is now hardware-correct, where before it was not.

Mechanism: **no — and it cannot be, at this layer.** The honest framing is
that the battery image was one visible symptom of a sleep/wake *model* that is
already not how the hardware works:

- On real hardware, "sleep" is not a reboot. The ARM1176 parks in a low-power
  idle (WFI) with the display off; DRAM is self-refreshed; the SoC stays
  powered; iBoot does **not** run again; a button press resumes the kernel
  exactly where it stopped. iBoot's pre-boot charging dispatcher only runs at
  a genuine cold power-on with external power attached — never on a
  sleep/wake.
- This emulator cannot do that suspend-to-RAM, because iBoot-204's warm-boot
  path on this bootrom is a *clean reboot* with no warm-boot detection
  (investigation finding #82). So sleep/wake is re-implemented as: OOCSHDWN →
  full SoC reset → re-run iBoot over retained DRAM → park just before the
  type-4 kernel handoff → resume the retained kernel on the wake press. The
  whole flow (and its supporting fictions — `MBCS1 = USBPRES|USBOK`,
  `retained_resume`, `prewarm_active`/`prewarm_parked`, the pre-warm park) has
  **no hardware counterpart**. It exists only because the real resume path is
  absent in this firmware/bootrom combination.

Because iBoot re-runs on every emulated sleep, it re-enters its charging
dispatcher and legitimately issues MIPI DCS 0x11 (Sleep Out) — something a
real panel would always obey. Our fix makes the **panel model** deliberately
ignore that one command while `retained_resume` is set. So, precisely:

- the fix makes the **device** behave more like hardware (black sleep), by
- making the **panel peripheral** behave slightly *less* like a real panel (a
  real Sleep Out is silently dropped for the duration of the pre-warm).

That trade is the right one — no software ever observes the dropped command
except iBoot's throwaway scanout, and the whole point is fidelity of the
device the user sees, not of an internal MIPI transaction that only happens
because of the reboot-based sleep emulation.

A mechanism-faithful fix (true suspend-to-RAM, so iBoot never re-runs and the
charging dispatcher never fires) is a much larger project: it needs a working
guest resume entry point, which means patching the firmware's warm-boot path
rather than the emulator. Until that exists, suppressing the scanout is the
correct, contained choice.

Net: a correct, contained fix for this emulator. It restores the correct
user-visible behaviour and masks no functional regression; it does not, and at
this layer cannot, restore hardware-faithful suspend-to-RAM. The device sleeps
to black and wakes normally.

## Verification (2026-07-20)

- Instrumented build: park state was `panel_off=0 base=0x0fe00000
  retained_resume=1` (battery visible). After the fix, `0` Merlot relights
  occur during the pre-warm.
- Fixed dev build: sleep screen is **black**; Home wakes to the lock screen;
  `Merlot panel woke from sleep` now fires only on the real post-park wake.
- Packaged app (`/Applications/iPod Touch.app`, engine
  `v11.0.2-20-g151e64d305`): boots to SpringBoard from `nand.pack`, joins
  Wi-Fi, auto-locks to a **black** sleep screen, and Home wakes it to the
  lock screen.
- No regression in the existing sleep/wake, Home, Power, or Wi-Fi behaviour.
- Re-confirmed against the **shipped** bundle (independent boot,
  `/Applications/iPod Touch.app`, QEMU-11 engine dated 2026-07-20 13:04):
  serial shows `[WAKE] Pre-warming retained-RAM wake after OOCSHDWN` with the
  screen captured **black** during the pre-warm park; a Home press then logs
  `System Wake` + `[LCD] Retained kernel enabled scanout at 0x0f400000` and
  the screen shows the lit lock screen. So the fix is present and working in
  the installed application, not only in a local build.
