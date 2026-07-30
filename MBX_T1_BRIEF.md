# T1 — model MBX swap completion, and delete an address-dependent hack

Brief for the session that owns the MBX / in-app-button work. Written
2026-07-30 by the browser-port session, which is handing this over rather than
taking it: you have the MBX context and the `app-button-probe.py` harness, and
we would otherwise both be editing the same region.

Background you already own: [`MBX_HANDOFF.md`](MBX_HANDOFF.md) §2 (T1) and
[`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md). What this
file adds is a small, self-contained first step, plus the evidence from a
regression that surfaced on 2026-07-30.

---

## The objective, and the acceptance test

**`app-button-probe.py --board m68ap-10` reporting `3_home_returns` non-zero.**
Today it is `0.00%` — the app stays on screen. Re-run it for `m68ap-114` and
`n45ap` too; all three ship the same MBX driver.

The secondary prize is that the TVOut zero-window hack becomes unnecessary, on
every build.

## Where it already stands (your own measurements)

Bit 6 of MBX `0x12C` now reads set (`0x140`, `IT_MBX_READY=0` reverts). That
ended the **spin**: 0.99 → 0.10 host cores, and event delivery went from 1 DOWN
/ 1 UP over ten presses to 5/6 over six, with taps reaching the foreground app.

But the app still does not visually dismiss, and the guest is now **idle rather
than spinning** — something waits on an MBX completion that never arrives.
Your conclusion, unchanged: *the MBX region still has no IRQ connected at all,
which is the other half of T1.*

**Independent confirmation from the browser port**, if useful: the WebAssembly
build reproduces this exactly (`?sweep=home` in `web/public/jit-boot/`: opens an
app 45.6% → 94.7%, Home does not return). It also samples `QEMU_CLOCK_VIRTUAL`,
and after the Home press the guest-seconds-per-wall-second ratio jumps to
0.24–0.69 with the frame counter frozen. Under `-icount` a HIGH ratio means
virtual time is being warped forward because the CPU is halted — a second,
independent instrument agreeing that the guest is idle, not spinning.

## Step 0 — a small cleanup, worth doing first

`hw/arm/ipod_touch.c` still maps the 4-byte always-zero window at a **hardcoded
board-default PA at machine init**, before the kernel has announced anything:

```c
tvout_wa_addr = (board == M68AP) ? TVOUT_WORKAROUND_M68AP_MEM_BASE
                                 : TVOUT_WORKAROUND_MEM_BASE;
```

Since T3 the real address is *derived* from the guest's own
`AppleMBX: Added swap device: … id: …` line, so the blind default has no
remaining job — it is just four bytes of guessed kernel heap. `MBX_HANDOFF.md`
is blunt about the cost of exactly that: *"when it is wrong nothing complains …
That silence cost this project the entire M68AP render investigation."*

**Suggested: map nothing until a TVOut device is announced.** Then 1.0 and the
iPod carry no window at all, and 1.1.4 gets one only at an address the kernel
supplied. Small, independent of T1, and A/B-able with the knob below.

## Step 1 — T1 proper

1. **Connect the MBX interrupt.** This is the missing half you identified.
2. **Model swap completion** so the teardown poll clears on its own.

Then the window can default to off (see the note on deletion below), and 1.0's
back-to-home repaint should follow, since both are the same wait.

`MBX_HANDOFF.md` §7 has the reading path: `com.apple.driver.AppleMBX` around the
`c03b9698` accessor and the swap path. The 1A543a kexts retain full C++ symbols.

## What changed under you on 2026-07-30, and why

`23cc69c033` generalised the swap-device window from the `AppleH1TVOut` literal
to **any** swap device, on the correct observation that it was silently inert on
1.0. **It was inert for a reason: placing the window on 1.0 breaks it.**

Measured on one binary via a new `IT_TVOUT_WA` knob — same NAND, same firmware,
`-icount shift=1`, 320 s per boot:

| | scanout | serial lines |
| --- | --- | --- |
| `IT_TVOUT_WA=1` (generalised) | **0.00%** | 1738, 1736 |
| `IT_TVOUT_WA=0` (no window) | **45.39%** | 3241, 3228 |

2 of 2 each way, deterministic. The failing boots end in

```
panic(cpu 0 caller 0xC00628CC): kernel abort type 4:
      fault_type=0x1, fault_addr=0x0
panic: We are hanging here...
```

**`fault_addr=0x0` is the mechanism.** The window is a 4-byte MMIO region whose
reads return ZERO, punched over a field of the swap-device object. On a TVOut
object that field is the one the hung teardown polls, so reading zero is the
entire point. On 1.0 the same offset lands inside `AppleH1CLCD` — a *live*
object the kernel dereferences — and it faults on the zero it reads.

So targeting was restored to TVOut-only (`f8780af198`). What your commit got
right is kept: it names the device and now says out loud when it *declines* to
place the window, which was the real complaint — a workaround that silently does
nothing is worse than none. 1.1.4 and the iPod are unaffected; both keep the
behaviour they had before `23cc69c033`.

**`IT_TVOUT_WA=0` removes the window entirely.** It exists because this
comparison was otherwise only possible by checking the file out at an older
commit and rebuilding — disruptive enough in a tree two sessions build in that
the comparison does not get made, which is exactly how a boot-breaking change
sat unnoticed. Same pattern as your `IT_MBX_READY`.

## On deleting the window afterwards

Do **not** delete it the moment T1 lands. Flip `IT_TVOUT_WA` to default-off and
keep the code:

- it is a fallback for builds not yet modelled or packaged (1.0.2, 1.1.1);
- it keeps the A/B possible, which is how you would prove the real fix works;
- deleting later is cheap, un-deleting is not.

Remove it only on evidence: `app-button-probe.py` green on all three profiles
with the window off, sustained.

## File ownership right now

The browser-port session is in `ui/wasm.c`, `web/`, `scripts/wasm/`. It has
**also** added `VMStateDescription`s to shared device models for the snapshot
work — expect to see them, they are migration-only and cannot affect runtime:

`hw/arm/ipod_touch_lcd.c`, `ipod_touch_pcf50633_pmu.c`,
`ipod_touch_multitouch.c`, `ipod_touch_sysic.c`, `ipod_touch_spi.c`,
`hw/intc/pl192.c`.

`hw/arm/ipod_touch.c` is yours; the browser session touched only the TVOut
targeting and the knob, and does not intend to return to it.

## Two notes on MBX_HANDOFF.md, now stale

§5 ("Sequencing against the wasm port") is out of date. Its steps 1, 2 and 4 are
done: the WebAssembly **JIT** was adopted (there is a `tcg/wasm64` backend in
tree now), the browser reaches the SpringBoard home screen in ~250 s from
chunked assets, and the display and input bridges exist. Its advice not to
overlap MBX work with the display bridge no longer binds — the bridge is
written and measured.

Its §4 question ("would modelling it speed anything up?") is now cheaply
answerable and more interesting than before: the browser page reports guest
seconds per wall second directly, so a PC-sample of compositing can be turned
into a real-time-ratio delta rather than an argument.
