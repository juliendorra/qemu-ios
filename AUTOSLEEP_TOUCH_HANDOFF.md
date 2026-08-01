# Auto-sleep touch death — session handoff

**Date:** 2026-08-01 · **Branch:** `wasm-jit-graft` · **State:** FIXED and
scripted against the exact masked-idle failure on iPhone OS 1.0.

## Final result (2026-08-01)

The gating question is answered: `0xc005a2ec` is kernel proper symbol
`_cpu_idle`, immediately after the ARM WFI at `0xc005a2e8`; it is not
OOCSHDWN and not a new deep-sleep state. iOS deliberately executes WFI with
IRQ/FIQ masked, runs a 1,200-iteration settling loop, then restores its idle
context. The SYSIC model incorrectly turned the interrupt controller's latched
GPIO output into a 100 ms pulse. A HOME edge could wake WFI but disappear before
the guest reached the unmasked delivery point, leaving `GPIO_INTSTAT` pending
with no controller output asserted.

Production fix: GPIO group outputs now remain asserted until the guest writes
the corresponding `GPIO_INTSTAT` ACK. This is the controller output, not the
source-pin waveform. In the fixed trace, a HOME press at `PC=0xc005a2ec,
I=1,F=1` is followed by the full read-INTSTAT/read-INTLEVEL/ACK conversation,
Sleep Out, and a working slide.

The residual touch-gate asymmetry is fixed too: the stable-frame gate cannot
count stale framebuffer pixels while `panel_off` is true; when a retained
kernel reclaims an OS scanout base, a device that was already interactive
restores input in the same transition.

`scripts/autosleep-touch-probe.py --require-masked-idle` now emits JSON, three
PNGs, relevant log offsets, SYSIC/gate traces, and rejects a run that did not
hit the exact `_cpu_idle` state. It also identifies "relit then re-slept" as
the lock screen's normal unattended timeout instead of reading stale RAM and
calling it a visible dead slider.

Final staged-firmware acceptance:

```text
python3 scripts/autosleep-touch-probe.py --board m68ap-10 \
  --require-masked-idle
masked _cpu_idle press reproduced: True
after wake: lit=59.95%, parks 0 -> 0
slide changed 73.57%, 0 touches refused
VERDICT: PASS
```

Evidence: `/tmp/autosleep-final-10-guarded/report.json`, `1_after_wake.png`,
`2_before_slide.png`, `3_after_slide.png`, and `qemu.log`. The launcher used
staged NAND/NOR copies; no installed firmware source was modified.

This is the short path *into* the problem. The full post-mortem — eleven
hypotheses with their fates, five instrument traps — is the **LEDGER** section
at the end of [`SLEEP_WAKE_INVESTIGATION.md`](SLEEP_WAKE_INVESTIGATION.md).
Read this file first, that one when you need the eliminations.

---

## 1. The bug, as the user sees it

After the device auto-sleeps from idle, pressing **H** wakes it but
**slide-to-unlock does not work — with the slider VISIBLE on screen**.
Reported on iPhone OS 1.0 and 1.1.4. The iPod is reportedly immune. Waking
from a **manual Power-button sleep is fine**.

The visible-slider detail is the whole game: it means `panel_off == false`
(the model blanks the host surface when the panel is off), so a black-screen
failure is **a different bug**. No run in the previous session reproduced the
visible-slider form. **You have not reproduced the user's bug until you have a
lit panel and dead touch.**

## 2. Do this first (15 minutes, answers the only open question)

The 1.0 measured state after auto-sleep is a kernel spin at `0xc005a2ec` with
**IRQ and FIQ masked** (`I=1 F=1`) while `oocshdwn=0` and `parked=0`. A CPU in
that state cannot take the GPIO interrupt, so H is physically incapable of
waking it — and no wake branch in `ipod_touch_key_event()` applies, because
none of the model's five "asleep" flags are set.

**The question that gates everything else: which sleep is the kernel actually
performing there?** Not "how do we wake it" — that is how the last session
produced a regression.

```bash
# symbolize the spin and its caller against the RELEASE kernelcache
python3 scripts/extract-kernelcache.py -o /tmp/kc10.macho \
  /private/tmp/m68_10/System/Library/Caches/com.apple.kernelcaches/kernelcache.release.s5l8900xrb
python3 scripts/kernel-addr-symbolize.py /tmp/kc10.macho 0xc005a2ec
```

If it is the OOCSHDWN path, find why `oocshdwn_fired` never gets set. If it is
a *different* sleep (idle/WFI, a doze the model has never modelled), then the
model is missing a state entirely and that is the finding.

## 3. How to reproduce (the harness does NOT reproduce it)

`scripts/autosleep-touch-probe.py` **passed five configurations on a build that
was visibly broken by hand** — wake at +5-10 s, +30 s, +55 s, after a manual
pre-cycle, and after a 5-minute host freeze. Treat a green run from it as
meaningless for this bug. It is still useful for its two model-side counters
(touches REFUSED, parks INCREASING), which a pixel verdict cannot fake.

Reproduce in the **real window**:

```bash
IT_GATE_TRACE=1 IT_KEY_TRACE=1 \
  "/Applications/iPhone 2G (iOS 1.0).app/Contents/MacOS/iPod Touch" 2>/tmp/it10.log
# wait for the home screen, then leave it alone ~60 s until the panel sleeps,
# press H, and try to slide.
grep -cE "Ignoring input" /tmp/it10.log      # model refused the touch
grep -E "^\[GATE\]" /tmp/it10.log | tail     # why the gate is shut, once a second
grep -E "^\[BTN\]" /tmp/it10.log | tail      # PC + CPSR I/F at the press
```

## 4. Which of the THREE states you are in

The model records no distinction between these, which is why three sets of runs
told three different stories. Identify the state before theorising.

| state | log signature | H does | touch does |
|---|---|---|---|
| pre-warm **parked** | `Pre-warmed wake parked; awaiting Power/Home` | resumes, then re-parks | model REFUSES: `Ignoring input until display/driver startup is stable` |
| plain **panel sleep** | `Merlot panel entered sleep`, guest still scheduling | wakes the panel correctly | works, if you beat the ~10-15 s re-sleep |
| **masked-interrupt spin** | `[BTN] PC=0xc005a2ec I=1 F=1`, `oocshdwn=0 parked=0` | **nothing at all** | n/a, screen stays black |

## 5. Traps that already cost a session

* **`panel_off` blanks the host surface** (`lcd_refresh` memsets it). A probe
  reporting `lit=59.8%` with dead touch is NOT the user's symptom — it is a
  black screen with a stale framebuffer behind it. Always check `panel_off`.
* **`input_ready` is not a statement about the device.** Its generic re-arm
  counts visible pixels at `known_bases[]` and never checks `panel_off`, and
  QEMU never clears guest framebuffer memory — so it declares touch ready 2 s
  after ANY panel sleep, on the strength of a frame nobody can see.
* **`--wake-settle 45`** (added to make the probe "see" the bug) makes it report
  FAIL for *correct* behaviour on the shallow path: a real iPhone woken to the
  lock screen and then ignored sleeps again after ~15-20 s.
* **Port collisions with a parallel session** (VNC :96) look exactly like a
  broken probe (`QMP never appeared`). `pgrep -f qemu-system-arm` first.
* Every recorded sleep/wake validation in this repo drives the **POWER** button.
  Wake-from-auto-sleep was never in the regression net on any board — which is
  how this stayed broken quietly.

## 6. What NOT to do

**Do not add another wake heuristic.** The model already carries five separate
notions of "asleep" — `oocshdwn_fired`, `prewarm_active`, `prewarm_parked`,
`wake_reset_pending`, and `RESUME_STATUS` arming. The previous session added a
sixth failure mode by injecting a synthetic Home press into one of them
(`ipod_touch_wake_activity()`): model counters improved (refusals 5 → 0, parks
3 → 1) and the user immediately reported real use got WORSE — sleeping with
SpringBoard displayed, H blanking the screen, no slide-to-unlock at all. It is
now `IT_WAKE_ACTIVITY=1` opt-in and **OFF by default**. Leave it off.

**One green probe run is not grounds for enabling anything by default.** The
probe measures two model-side counters; the user measures the device.

## 7. Already eliminated — do not re-run these

* This session's SYSIC `INTLEVEL` / MBX changes. The pre-session binary
  (`/private/tmp/engine-backup-iPhone_2G_(iOS_1.1.4).bak`, md5 `32c092a1`)
  fails **identically**: parks 0→0, 0 refusals, slide 0.00%.
* Long dwell / host Mac sleeping (5-minute `SIGSTOP` freeze passes).
* A touch on the dark screen before pressing H (the user did not do this).
* The OOCSHDWN commit window as a timing race (+5/+30/+55 s all pass).
* The touch gate failing because the lock screen is scanned out from a base
  absent from `known_bases[]` (`IT_GATE_TRACE` shows `w1_base=0x0f400000`,
  `visible=4/6`, gate arms normally).

## 8. Still open, cheapest first

1. **Which sleep is 1.0 performing?** (§2 — gates everything.)
2. **Reproduce the visible-slider form.** Without it there is nothing to verify
   a fix against.
3. **Prior app usage as a trigger** — the user had been opening apps before the
   sleep; `--pre-app` exists for this and was never run.
4. **Re-test the iPod for genuine immunity.** Its bundle still runs a DIFFERENT
   binary from the two iPhone bundles, so "immune" may just mean "older engine".

## 9. Files

| what | where |
|---|---|
| full post-mortem, 11 hypotheses + traps | `SLEEP_WAKE_INVESTIGATION.md`, LEDGER section |
| the probe (green runs are meaningless here) | `scripts/autosleep-touch-probe.py` |
| the touch gate | `lcd_update_input_ready()`, `hw/arm/ipod_touch_lcd.c` |
| panel sleep/wake, `input_ready` clearing | `hw/arm/ipod_touch_lcd_panel.c` (DCS 0x10 / 0x11) |
| wake branches, the reverted injection | `ipod_touch_key_event()`, `hw/arm/ipod_touch.c` |
| traces | `IT_GATE_TRACE=1`, `IT_KEY_TRACE=1`, `IT_LCD_TRACE=1` |
