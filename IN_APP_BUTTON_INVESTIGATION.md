# The in-app HOME/POWER bug on iPhone OS 1.0 — full record

**Status: OPEN.** Authoritative record for this investigation; the sections in
`NEXT_SESSION_HANDOFF.md` are the raw chronological log and several of their
conclusions are retracted here.

## The bug

On `/Applications/iPhone 2G (iOS 1.0).app`, pressing HOME or POWER while an app
is frontmost does nothing. `iPhone 2G (iOS 1.1.4).app` and `iPod Touch.app` both
work. Touch inside the app works on 1.0. It only surfaced once 1.0's touch was
fixed (2026-07-28), because before that nobody had ever opened an app there.

Reproduce:

```bash
IT_PROBE_WAIT=8 python3 scripts/app-button-probe.py --board m68ap-10
```

| board | open app | touch in app | HOME returns | POWER sleeps |
|---|---|---|---|---|
| iPod Touch (N45AP) | PASS | PASS | PASS | PASS |
| iPhone OS 1.1.4 | PASS | PASS | PASS | PASS |
| **iPhone OS 1.0** | PASS | PASS | **FAIL** | **FAIL** |

## What is established, by measurement

1. **The press reaches the model.** `IT_KEY_TRACE=1` shows `keycode=35` then
   `keycode=163` for every press.
2. **The kernel services the interrupt, always.** SYSIC group 1 bit 8
   (IRQ 0x28): read INTSTAT, read INTLEVEL, ACK, re-read — byte-identical to
   1.1.4 and to the iPod, and it keeps happening on *every* press, including
   after the failure begins (`ACK INTSTAT group 1` continuing to n=16).
3. **SpringBoard is the event router.** It reads the HID event and re-sends it
   into the purple port with `_GSGetPurpleSystemEventPort` + `_GSSendEvent`, for
   its own consumption *and* for the foreground app's. This is why touch dies
   together with the button.
4. **The FIRST in-app press works completely, end to end.**

   ```
   t=10.05  _GSGetPurpleSystemEventPort   SpringBoard
   t=10.05  _GSSendEvent                  SpringBoard
   t=10.07  event type1000 (MENU DOWN)    SpringBoard -> menuButtonDown: REAL HIT
   t=10.20  _GSGetPurpleSystemEventPort / _GSSendEvent
   t=10.21  event type1001 (MENU UP)      SpringBoard -> menuButtonUp:   REAL HIT
   t=10.24  _GSSendEvent                  SpringBoard
   t=10.27  event type2002                Preferences  (the app is deactivated)
   t=14.25  callback                      SpringBoard  (the last one, ever)
   ```

5. **Then delivery stops, for everything.** Ten presses in-app on 1.0 deliver
   **1 DOWN and 1 UP**; ten presses from the home screen deliver **10 and 10**;
   ten presses in-app on 1.1.4 deliver **10 and 10**. With `--taps`, taps after
   the first press produce nothing either, and `_PurpleEventCallback` never
   fires again in *any* process.
6. **The handler chain is innocent.** Single-stepped on both builds:
   `-[SpringBoard menuButtonUp:]` is structurally identical, all its gates pass
   (`_screenShooting` 0, `isRestoring`/`isResetting`/`isSoftwareUpdating` all NO,
   `shouldRunFieldTestScript` NO, `_menuButtonTimer` non-nil), and it dispatches
   `[_uiController clickedMenuButton]`.
7. **`-[SBUIController clickedMenuButton]` runs to completion** (1.0 `0xd794`):
   `launchState == 3`, not locked, top display is not an `SBAlert`,
   `deactivateAlertForMenuClick` returns YES, the jump table at `0xd85c`
   dispatches to `0xd890` → `topApplication` non-nil → `0x10794`, the
   display-stack unwinder (`while (![stack isEmpty]) pop`, over two stacks) —
   then it returns normally.
8. **The unwind after it is healthy too.** `-[UIApplication handleEvent:]` →
   `-[SpringBoard handleEvent:]` → `_PurpleEventCallback` → CoreFoundation's run
   loop, after which SpringBoard keeps working for at least 15 000 instructions
   (48% libobjc, 26% libSystem, 18% CoreFoundation — the shape of notification
   dispatch, including an `NSThread` being started).

**So nothing goes wrong synchronously at any level.** The press is fully
handled, and only afterwards does event delivery die permanently.

9. **THE WEDGE IS A SPIN: SpringBoard burns a full core forever** (2026-07-29,
   `scripts/gsqueue-poll.py`). Measured on the QEMU process's own CPU time --
   completely non-invasive -- with the mapped process read from guest memory in
   the same run:

   | phase | QEMU host CPU | process currently mapped |
   |---|---|---|
   | before any press | **0.07 cores** | healthy mix: BTServer 38, BlueTool 14, CommCenter 9, syslogd 4, mediaserverd 4 |
   | press 1 (the one that works) | **0.12 cores** | healthy mix: BTServer 47, BlueTool 30, CommCenter 11, ... |
   | press 2 | **0.97 cores** | **SpringBoard 98/98** |
   | press 3 | **0.98 cores** | **SpringBoard 98/98** |

   From the home screen on the same build the daemon mix returns to normal after
   a press (BTServer 64, BlueTool 56, CommCenter 41, SpringBoard 9) and the CPU
   stays low.

   So after the first in-app press SpringBoard enters an **infinite loop** and
   **starves every other process**. That single fact explains the whole
   downstream picture at once: nothing else is scheduled, so no further
   `_GSSendEvent`, so `_PurpleEventCallback` never fires again in any process, so
   the button and touch both die, the LCD stops flipping and `[MT] frame
   consumed` stops. It also means the loop most likely IS the CF/libobjc work
   seen in the after-return walk (48% libobjc, 26% libSystem, 18%
   CoreFoundation, still going at step 15 000) -- which was read at the time as
   "carrying on doing ordinary work".

   **This retracts the older "the guest goes IDLE and never wakes / parked at
   WFI with interrupts masked" conclusion.** That PC-sampling run pressed
   nothing (qcode `"home"`), so it was sampling an idle machine.

10. **THE LOOP IS NAMED: `com.apple.driver.AppleMBX` polling a register bit that
    our MBX stub never sets** (2026-07-29, `scripts/spin-locate.py` +
    `scripts/kernel-addr-symbolize.py`).

    30 PC samples during the spin all land at `0xc033563c`, and 6000 single
    steps are **100% kernel**, in an exact 9-instruction cycle:

    ```
    0xc0336010  MOV r0, r4
    0xc0336014  MOV r1, #0x12c
    0xc0336018  BLX r5          -> 0xc0335638: LDR r0,[r0,r1] ; BX lr   (register read)
    0xc033601c  STR r0, [sp]
    0xc0336020  LDR r3, [sp]
    0xc0336024  TST r3, #0x40
    0xc0336028  BEQ 0xc0336010  -- loop while bit 6 is CLEAR
    ```

    i.e. `do { v = mbx_read(base, 0x12C); } while (!(v & 0x40));`

    Both addresses are inside **`com.apple.driver.AppleMBX`**
    (`0xc0329000..0xc033c000`), resolved from the kernelcache's `kmod_info`
    list. **The MBX region in this emulator is a do-nothing stub with no IRQ
    connected at all** (tasks T1/T2, [`MBX_HANDOFF.md`](MBX_HANDOFF.md)), so bit
    6 of register 0x12C can never become set and the driver spins forever.

    So this is **not an iPhone OS 1.0 software bug at all** -- it is our
    unimplemented MBX, reached by 1.0's app-dismissal path.

    **Attribution trap, recorded because it nearly produced a wrong answer:**
    the first symbolization used
    `m68ap-artifacts/unpacked/1.0_1A543a/kernelcache.restore.release.s5l8900xrb`
    -- the RESTORE cache, which the device does not boot. Its kext ranges are
    shifted (AppleMBX `0xc0327000` vs `0xc0329000`), and under it the documented
    `AppleM68Buttons` anchor at `0xc032792c` fell inside AppleMBX, which is what
    exposed the mistake. Use the RELEASE cache from the root filesystem,
    `/System/Library/Caches/com.apple.kernelcaches/kernelcache.release.s5l8900xrb`,
    and check the anchor: with it, `0xc032792c` correctly resolves to
    `com.apple.driver.AppleM68Buttons`.

11. **The requester is `IOCoreSurface`, NOT TVOut** (2026-07-30,
    `spin-locate.py --kernelcache`). With `IT_MBX_READY=0` the spin reproduces on
    demand, so the kernel stack can be walked at leisure:

    ```
    pc  0xc033563c   AppleMBX  (the register-read accessor)
    lr  0xc033601c   AppleMBX+0xd01c   (the poll loop)
        AppleMBX+0x5094 -> AppleMBX+0x69cc -> kernel(IOKit) -> AppleMBX+0x8f90
    stack also holds: com.apple.iokit.IOCoreSurface+0x27a8
    ```

    No TVOut, no AppleH1CLCD anywhere in the chain. So the MBX work is requested
    by the **surface/compositing** layer during the dismissal, which is why
    `LK_ENABLE_MBX2D=0` (a LayerKit *userland* knob) does not prevent it.

    *Precondition trap, cost one run:* the spin only happens with an app
    FRONTMOST, and a run whose icon tap missed looks exactly like "the bug did
    not reproduce" (0.14 cores, idle loop). `spin-locate.py` now verifies the
    screen is app-like (>80% lit) before pressing and calls the run INVALID
    otherwise.

12. **The MBX register protocol, read off the guest** (`IT_MBX_TRACE=1`):

    ```
    rd 0x12c = 0x140          status
    WR 0x134 = 0x00000040     clear bit 6          <-- the guest ACKs the event
    WR 0x134 = 0x0000ffff     clear everything
    WR 0x130 = 0x00000000     host enable = 0      <-- interrupts MASKED OFF
    WR 0x824..0x83c           a command descriptor
    WR 0x6d8 = 0x09000000     the KICK, then it polls 0x12c
    ```

    So 0x12C = event status, 0x130 = host enable, 0x134 = host clear -- PowerVR
    event registers. **Two consequences.** First, this path *polls* with all
    events masked, so **an interrupt is not what it is waiting for** and T1's
    "wire the TVOut SDO IRQ" is not the lever for this bug. Second, the shipped
    stub's permanently-set bit 6 is a lie the guest can observe: it writes
    `0x134 = 0x40` to acknowledge, re-reads, and the bit is still set.

## Hypotheses killed, with the measurement that killed each

Do not re-try any of these.

| hypothesis | how it died |
|---|---|
| Wrong GPIO pin / IRQ | Both device trees encode `function-button_menu` identically: GPIO 0x1600, IRQ 0x28. Only the phandle differs. |
| The interrupt is not enabled | INTEN writes identical on both builds, both ending group 1 = `0x04003f00`, which includes bit 8. |
| `INTLEVEL` always reads 0 | Published the real pin level; `3_home_returns` still 0.00%. Reverted. |
| Interrupt not delivered / not ACKed | Byte-identical SYSIC sequence to 1.1.4 and the iPod, press and release. |
| `AppleM68Buttons` differs | Instruction-for-instruction identical between builds; only vtable slot offsets differ. |
| The guest polls a GPIO we do not drive | 1.1.4 makes ZERO GPIO accesses after the press and works fine. |
| It is a BOARD problem | 1.1.4 passes all five steps on the same board once its first-launch modal is dismissed. |
| It is just slow / latency | Watched 7 minutes. (This run was later invalidated anyway — it pressed nothing. See below.) |
| The host presenter stops | Runs 1:1 with the guest frame timer on the cocoa path (`present:8 vsync:8`). VNC-only artifact. |
| The model raises the wrong LCD interrupt bit | Disassembling `AppleH1CLCD` showed bit 0 IS the frame interrupt. |
| The event never reaches userland | It does; both handlers hit for real on press 1. |
| **Event ROUTING / the port** | `_ResetEventPortSet` and `_GSRegisterApplicationPort` NEVER fire on 1.0. The port is never reset, torn down or re-registered. |
| **The DOWN event is rarely delivered** | An artifact of the instrument — see the QMP trap below. With it fixed, press 1 delivers both edges. |
| `menuButtonUp:`'s gates swallow it | All gates pass; it dispatches. |
| `clickedMenuButton` blocks or bails | Runs to completion and returns. |
| The guest goes IDLE / parks at WFI and never wakes | **Backwards.** It spins at 0.97-0.98 host cores. The PC-sampling run that "showed" the idle loop had pressed nothing. |
| Events pile up unread (the DRAIN side died) | No persistent backlog: 200 SpringBoard samples over 40 s of wedge, queue head always 0. (See the instrument caveat below before leaning on this.) |

## Retracted conclusions from earlier sessions

* **"THE DISCRIMINATOR: it is event ROUTING, not the button."** Void. It rested
  on `springboard-button-breakpoint.py`, which pressed qcode `"home"` —
  discarded by `ipod_touch_input_event`, which accepts only `Q_KEY_CODE_P` and
  `Q_KEY_CODE_H` — so every "NO HIT" was a run with no button press.
* **"The stack at 1.1.4's in-app hit: Foundation → SpringBoard 0x5a3c."** Void.
  That hit was in **`iapd`**, not SpringBoard: every main executable links
  `__TEXT` at 0x1000, so a breakpoint on an IMP fires in every process. One
  1.1.4 run produced **440 bogus hits against 1 real one**.
* **"1.0 does not return slowly; it does not return"** (the 7-minute watch) and
  the PC-sampling histograms — same cause, no press was ever made.
* **"The DOWN event is rarely delivered."** Instrument artifact; see below.

## Harness traps — every one of these cost a run, and all fail SILENTLY

1. **`ipod_touch_input_event` accepts only P and H.** `key(q, "home")` is a
   valid qcode that the machine discards via `default: return`. Send `"h"`.
   Control: `IT_KEY_TRACE=1` must show `keycode=35` and `keycode=163`.
2. **QMP `input-send-event` is REFUSED while the VM is stopped.**

   ```c
   /* ui/input.c, qmp_input_send_event() */
   if (!runstate_is_running() && !runstate_check(RUN_STATE_SUSPENDED)) {
       error_setg(errp, "VM not running");
       return;
   }
   ```

   A breakpoint landing inside the 150 ms hold swallows the RELEASE, and
   `QMP.cmd` never looked at the reply. Measured: **10 downs but only 5 ups**
   reached the model under gdb, against 10/10 without. This manufactured the
   fake "the DOWN event is rarely delivered" result. Always check the reply.
3. **A breakpoint below 0x100000 fires in every process.** Validate each hit by
   fingerprinting the Mach-O header at 0x1000 *and* comparing the code bytes at
   the breakpoint against the binary. Arming
   `-[SBUIController clickedMenuButton]` at `0xd794` collected **295 270**
   rejected stops from another process and starved the guest so badly the press
   never completed — use `--follow-sel` (single-step into the callee) instead.
4. **Never `c` while sitting on a breakpoint.** QEMU re-traps at the same PC and
   the guest makes no progress; two runs reported 628 104 and 430 068 "stops"
   that were all the same one. Delete / single-step / re-set.
5. **A bare `recv(1)` for the RSP '+' ack desyncs** the moment a stop packet
   races the ack, after which the session wedges with the guest stopped — which
   reads exactly like "no events". Buffer packets properly.
6. **PC sampling at 50 Hz stops even 1.1.4 from completing the transition.**
   `pmemsave`/`memsave` are memory reads and do not stop the vCPU; use those.
7. **`touch-probe.py` runs `-display none`**, so QEMU never calls `gfx_update`
   and every touch is refused. It reports `no-response` for known-good builds.
8. **1.1.4 shows a first-launch "Edit Home Screen" modal**, and every launch is
   a first launch (the launcher clones a pristine NAND). Until dismissed no icon
   can be tapped. `DISMISS` in `app-button-probe.py` handles it; 1.0 has none.
9. **`grab()` must read the current scanout, not the brightest buffer.** A
   brightness heuristic cannot see a transition to a DIMMER screen, which is
   exactly app → home.
10. **Do not `codesign`/reinstall a bundle in /Applications while the user has it
    open** — macOS SIGKILLs the running process and it looks like an emulator
    crash. Announce it first.
11. **`resolve_imp` is unreliable for arbitrary selectors** — it mis-resolved
    `clickedMenuButton` to `0x732f0`, inside the string section. Resolve from the
    ObjC method table (`objc-xref.methods`).
12. **Function end must come from the method table**, not the first
    `pop {…,pc}`: 1.1.4's `menuButtonUp:` has a shared epilogue in the MIDDLE of
    the function, so the heuristic cut it at 0x7bd4 when it runs to 0x7c28.

## Constants worth not re-deriving

* **GSEvent types:** `_GSEventGetType` is
  `t = [ev+8]; if t != 3001 return t; else map [ev+0x38] (1..6) -> {1,6,3,4,5,2}`
  (literal `0xbb9`, both builds). **type1000 = MENU DOWN, type1001 = MENU UP**,
  type2002 = the app's deactivation. 2000/2001/2003/2006/2009 are background.
* **GraphicsServices** (not stripped — `nm` just cannot parse it; parse
  `LC_SYMTAB`):

  | symbol | 1.0 | 1.1.4 |
  |---|---|---|
  | `_PurpleEventCallback` | 0x3098ce60 | 0x30ab624c |
  | per-event dequeue | **0x3098d028** | **0x30ab642c** |
  | `_GSEventGetType` | 0x3098ad68 | 0x30ab3fb4 |
  | `_GSSendEvent` | 0x3098bdb0 | 0x30ab50e4 |
  | `_GSSendSystemEvent` | 0x3098c730 | 0x30ab5bd8 |
  | `_GSGetPurpleSystemEventPort` | 0x3098b8f4 | 0x30ab4bd0 |
  | `_GSRegisterApplicationPort` | 0x3098b74c | 0x30ab4a28 |
  | `_ResetEventPortSet` | 0x3098b5e4 | 0x30ab48c0 |
  | **GSEvent queue-head global** (`__bss`) | **0x38988a0c** | **0x38ab2d00** |

  Break at the **dequeue**, not at the function top (1.0 `0x3098ceac`): the top
  is reached conditionally, after `_GSEventTakeLater` coalescing, and a run that
  broke there recorded zero events in 75 s of a live SpringBoard.
* **SpringBoard ivars** (from the old-ABI class metadata): `+0xc _uiController`,
  `+0x10 _menuButtonTimer`, `+0x40` (1.1.4 `+0x44`) `_screenShooting`. 1.1.4
  additionally has `+0x38 _menuButtonClickCount` and `_handleMenuButtonEvent:`,
  which is a red herring — its only caller is `menuButtonUp:+0x23c`, downstream
  of the same `_menuButtonTimer` gate.
* **IMPs:** `menuButtonDown:` 1.0 `0x6ae0` / 1.1.4 `0x78dc`; `menuButtonUp:`
  1.0 `0x6bd8` / 1.1.4 `0x79d4`; `-[SBUIController clickedMenuButton]` 1.0
  `0xd794` / 1.1.4 `0xf75c`.
* **The hold timer is 5.0 s on both builds** — a 150 ms press can never fire it.

## Tools built for this

| tool | what it is for |
|---|---|
| `scripts/gsevent-type-probe.py` | the workhorse: breaks at the GSEvent dequeue, decodes the type, names the process from the Mach-O header at 0x1000, verifies handler hits against SpringBoard's own bytes. `--presses N` counts DOWN vs UP per press, `--taps` adds the touch control, `--watch-port` watches the port calls, `--no-gdb` is a breakpoint-free control. |
| `scripts/menubutton-step-trace.py` | single-steps a handler and logs the branches; `--follow-sel` steps INTO a callee whose entry cannot be armed; `--after-steps` walks past the return logging every function change. |
| `scripts/macho-symbols.py` | dumps `LC_SYMTAB` — these binaries are not stripped, `nm` just cannot read them. |
| `scripts/objc-method-disasm.py` | disassembly with every selector named from `__message_refs`. Caveat: the RECEIVER is sometimes mis-resolved (e.g. `&"__TEXT"`); the SELECTOR always is right. |
| `scripts/objc-xref.py` | who sends this selector, and `--list-methods` for the whole method table. |
| `scripts/gsqueue-poll.py` | polls the GSEvent queue head with QMP `memsave` — no gdb, no vCPU stops, so it can watch for minutes. |

## The GSEvent queue poll: what it did and did not settle

`scripts/gsqueue-poll.py` derives the queue-head global from
`_PurpleEventCallback`'s own prologue (1.0 `0x38988a0c`, 1.1.4 `0x38ab2d00`,
GraphicsServices `__bss`) and reads it with QMP `memsave` -- guest VIRTUAL
memory, through the current CPU mapping, with **no vCPU stop**, so it can watch
for minutes. Every sample also reads the Mach-O header at 0x1000 and
fingerprints it, because that `__bss` is per-process at a fixed VA and a sample
means nothing without knowing whose address space it came from.

**Result: the queue head is 0 in every sample** -- 200 SpringBoard samples
spanning 40 s of the wedge.

**And the control is NEGATIVE, so read that carefully.** With `--burst 40`
(40 taps flat out, sampling between every event, on the WORKING home-screen
configuration) the poller caught a non-empty queue **0 times in 240 samples**.
The queue drains faster than a QMP round-trip, so *this instrument cannot
observe a queued event at all*. By this project's own rule -- a negative from an
instrument that cannot produce a positive is not evidence -- "the queue is always
empty" does **not** establish that nothing is enqueued.

What it does establish is narrower and still useful: **there is no persistent
backlog.** Had the drain side died while events kept arriving, the head would
have stayed non-zero for tens of seconds and 200 samples would have caught it.
Combined with the port watch (no `_GSSendEvent` after press 1), the sender side
is what stops.

The run's real payoff was accidental: the per-phase process mix and the host CPU
figure, which is finding 9 above.

## PARTIAL FIX SHIPPED: the spin is gone, the events are back, the screen is not

`hw/arm/ipod_touch.c`: MBX register `0x12C` returned `0x100` -- bit 8 set, **bit 6
clear** -- which is exactly the bit `AppleMBX` polls. It now returns `0x140`.
`IT_MBX_READY=0` restores the old value.

**A/B on the SAME binary, in the 1.0 bundle** (this matters: the rebuilt engine
also carries unrelated in-tree changes, so only a knob toggle isolates the
cause):

| | QEMU host CPU after the press | process mapped | loop |
|---|---|---|---|
| `IT_MBX_READY=0` | **0.99 cores** | SpringBoard 12/12 | the 9-instruction AppleMBX poll |
| default (bit set) | **0.10 cores** | healthy daemon mix | only the kernel idle delay at `0xc005a2f0` |

**What the fix bought, measured:**

* **The starvation is gone.** 0.97 -> 0.10 host cores; other processes are
  scheduled again.
* **Event delivery is restored.** Six presses in-app now deliver **5 DOWN and
  6 UP** (before: 1 and 1 over TEN presses), the app receives its `type2002`
  deactivation again, and taps reach the foreground app (`Preferences` types 1
  and 2). So the button and touch both work in-app on 1.0 now.

**What still fails:** `app-button-probe.py --board m68ap-10` still reports

```
PASS  1_open_app       97.10%
PASS  2_touch_in_app   35.95%
FAIL  3_home_returns    0.00%   lit 99.0% -> 99.0%
FAIL  4_power_sleeps    0.00%
```

The app is still on screen. So the remaining failure is **display/compositing
only** -- the event path is healthy and the guest is idle rather than spinning,
which means something is now waiting on an MBX completion that never arrives
instead of busy-polling for it. The MBX region still has **no IRQ connected at
all**, which is the other half of T1.

### OBSERVATION for whoever owns the TVOut workaround (2026-07-30, from Session A)

**Not a verdict — a correlation worth an A/B, reported because it is blocking the
snapshot work and I did not want to revert another session's file in a shared
tree.**

Since `23cc69c033` ("TV-out does not exist in 1.0, and our swap-device workaround
was inert there"), 1.0 boots here have been ending in

```
panic: We are hanging here...
```

with kernel thread dumps, before or shortly after the home screen. Tally on the
SAME NAND, same firmware, same `-icount shift=1`, `scripts/wasm/snapshot-probe.py`:

| engine | 1.0 boots | hang panics |
| --- | --- | --- |
| before `23cc69c033` | ~8 (snapA–snapG) | **0** |
| after | 4 (snapH–snapK) | **3** |

And the workaround is confirmed active in the failing boots, which it never was
on 1.0 before:

```
[TVOUT-WA] board default 0x089c8560 is WRONG for this kernel
           (AppleH1CLCD swap device at VA 0xc0813200) - moving the window
[TVOUT-WA] window moved 0x089c8560 -> 0x08813360
```

So the change did exactly what it says — it stopped being inert on 1.0 — and the
panics start there. **It is not 1-for-1**: snapH booted, rendered, and slept
normally with the same binary, so if this is the cause it is timing-dependent
rather than deterministic.

**What would settle it in one run each: an `IT_TVOUT_WA=0` knob.** The MBX fix
shipped exactly that (`IT_MBX_READY=0`) and it is what made *that* A/B possible
on one binary. Without it the only A/B is checking the file out at the previous
commit and rebuilding, which is disruptive in a tree two sessions are building
in.

Session A is not pursuing this further — it is your file and your call. Raising
it because a workaround that newly fires on a build is exactly the kind of change
worth confirming against a long boot, and the browser port's snapshot work needs
a 1.0 boot that reaches a live home screen.

### Cross-check from the BROWSER port (2026-07-30): same verdict, same signature

The WebAssembly build was rebuilt onto this fix and driven from the page
(`web/public/jit-boot/index.html?sweep=home`: launch an app, press Home, watch
the framebuffer). It agrees with `app-button-probe.py` step for step:

| | native | browser |
| --- | --- | --- |
| open an app | PASS 97.10% | PASS 45.6% -> **94.7%** |
| Home returns | **FAIL 0.00%** | **FAIL** -- stays at 96.3% |

Two things this adds rather than repeats:

* **An independent confirmation that the guest now IDLES rather than spins.**
  The browser page samples `QEMU_CLOCK_VIRTUAL` and reports guest-seconds per
  wall-second; after the Home press it swings to **0.24-0.69** with the frame
  counter frozen. Under `-icount` a high ratio means virtual time is being
  WARPED FORWARD because the cpu is halted -- the signature of an idle guest. A
  spinning guest shows a low, steady ratio (~0.02, as this boot did while
  working). So "waiting on an MBX completion that never arrives" is visible from
  a second, independent instrument.
* **It is not a browser artifact.** The event path, the button mapping and the
  press duration are all exercised end to end there, so the remaining failure is
  confined to display/compositing on both hosts.

### DEAD: "1.0 uses MBX because it lacks the software-compositing knob" (2026-07-30)

The obvious explanation for why 1.0 reaches the MBX loops and 1.1.4 does not was
`LK_ENABLE_MBX2D=0`, the guest plist edit that forces LayerKit to composite in
software. **It is set in BOTH shipped bundles**, read out of each one's own NAND:

```
1.0   bundle: com.apple.SpringBoard.plist EnvironmentVariables {'LK_ENABLE_MBX2D': '0'}
1.1.4 bundle: com.apple.SpringBoard.plist EnvironmentVariables {'LK_ENABLE_MBX2D': '0'}
```

(Check the BUNDLE, not `m68ap-artifacts/builds/<BUILD>/root.img` -- the 4A102
artifact has no `EnvironmentVariables` at all, because the plist edit is applied
by `build-m68ap-homescreen-nand.py`, which only the packaging script runs.)
1.0's LayerKit also contains the `LK_ENABLE_MBX2D` string, so it understands the
key.

So: same MBX driver code in all three builds, same knob set in both bundles, and
still only 1.0 enters the spin. **The knob is not the difference, and the
difference remains unknown.**

### CAUTION: the TVOut half of T1 is inherited, not verified for 1.0

T1 reads "model MBX swap completion / the TVOut SDO IRQ", and the coupling comes
from a guest log line seen during the RENDER investigation --
`AppleMBX: Added swap device: AppleH1TVOut id: c09c8400`. That is the basis for
believing MBX completion is signalled through the TVOut block. **It has not been
observed on 1.0 in this investigation**: no `AppleMBX` or `Added swap device`
line appears in any of the 1.0 logs collected here (those runs did not enable
`S5L8900_DEBUG=1`, so this is "unconfirmed", not "contradicted").

Before implementing an IRQ, confirm the mechanism for THIS failure. The button
path is healthy and is not what needs fixing; what fails is the app -> SpringBoard
display transition, and the question is **who asks the MBX to do work at that
moment**. That is directly measurable: with `IT_MBX_READY=0` the guest spins
deterministically inside the MBX poll, so attaching gdb and walking the kernel
stack names the CALLER -- the driver or subsystem that requested the operation --
and then one can check whether 1.1.4 ever calls the same entry. Do that before
building hardware on an assumption.

### The honest event model: implemented, at parity, OFF by default

`IT_MBX_EVENTS=1` replaces the always-set bit with real semantics -- 0x130 is the
mask, 0x134 clears, and a kick raises the completion bit. Getting the kick right
took one disproof:

| kick modelled as | result |
|---|---|
| `0x1020` bit 0 (guessed from a read-modify-write pair) | **WRONG** -- the guest span **46 million** times on `rd 0x12c = 0x100` |
| `0x6d8` (the last write before the poll, from the ordered trace) | no spin, 0.18 host cores |

With the correct kick the modelled mode matches the shipped default
functionally -- no spin, events flow -- while no longer lying to the guest about
an event it has acknowledged. It is **off by default**: promoting it means
reinstalling the engine in all three bundles and re-running the regression, and
it buys fidelity rather than function.

The MBX event line is also now **plumbed but unbound**: `IT_MBX_IRQ=<n>` attaches
it to SoC interrupt n (30 = TVOUT_SDO), so T1's TVOut hypothesis is testable with
an env var instead of a rebuild. Unbound, `mbx_update_irq()` is a no-op, so
nothing can regress -- and since the dismissal path masks all MBX events, an
interrupt is not expected to help *there*.

### TV-out does not exist in 1.0 -- and our swap-device workaround was inert there

The user's observation, confirmed by counting strings in each RELEASE kernelcache:

| | `AppleH1TVOut` | `TVOut` | `Added swap device` |
|---|---|---|---|
| 1.0 (1A543a) | **0** | **0** | 1 |
| 1.1.4 (4A102) | 4 | 4 | 1 |

TV-out arrived in the 1.1 line. 1.0's own boot log says what it uses instead:

```
AppleMBX: Added swap device: AppleH1CLCD  id: c0813200
AppleMBX: Using AppleH1CLCD as legacy swap device
```

against 1.1.4's `Added swap device: AppleH1TVOut  id: c09c8400`. **So "wire the
TVOut SDO IRQ" could never have helped 1.0: the driver is not there.** That is a
stronger reason than the polling argument above, and it settles the question --
the IRQ hook is coverage for the 1.1 line, not a fix for this bug.

It also exposed a real emulator bug. The swap-device workaround matched the
literal `"Added swap device: AppleH1TVOut"`, so on 1.0 it **never fired** --
measured: zero `[TVOUT-WA]` lines in a full 1.0 boot, i.e. the field was never
neutralised and nobody noticed. Exactly the silent-inertness the code's own
comment warns about. Now generalised to any swap device:

```
[TVOUT-WA] board default 0x089c8560 is WRONG for this kernel
           (AppleH1CLCD swap device at VA 0xc0813200) - moving the window
[TVOUT-WA] window moved 0x089c8560 -> 0x08813360
```

**It did not fix the transition** (`3_home_returns` still 0.00%), so the
swap-device field was not the blocker on 1.0 -- but the fix stands on its own: a
workaround that silently does nothing is worse than none.

**Which build announces what** (from each guest's own console, with the
generalised match in place):

| build | swap devices announced | window ends up on |
|---|---|---|
| 1.0 (1A543a) | `AppleH1CLCD` @ VA 0xc0813200 only ("legacy swap device") | CLCD -- a window it **never had before** |
| 1.1.4 (4A102) | `AppleH1CLCD` @ 0xc09c8800 **and** `AppleH1TVOut` @ 0xc09c8400 | TVOut = its board default, i.e. UNCHANGED |
| iPod 1.1 (3A101a) | `AppleH1CLCD` @ 0xc0a25c00 **and** `AppleH1TVOut` @ 0xc0a25800 | TVOut = its board default, i.e. UNCHANGED |

A plain last-one-wins would have silently moved 1.1.4 and the iPod OFF the TVOut
object the workaround was written for (measured: window at 0x089c8960 instead of
0x089c8560 on 1.1.4). They still passed 5/5 that way, which is exactly how a
silent misplacement hides. So the match now **prefers TVOut** when a build
announces both, and falls back to whatever is announced otherwise.

**Regression-verified with the final engine, all three bundles:**

| board | 1_open | 2_touch | 3_home_returns | 4_power | 5_wake |
|---|---|---|---|---|---|
| iPod (N45AP) | PASS | PASS | **PASS 98.58%** | PASS | PASS |
| iPhone OS 1.1.4 | PASS | PASS | **PASS 96.99%** | PASS | PASS |
| iPhone OS 1.0 | PASS | PASS | **FAIL 0.00%** | FAIL | -- |

**Regression note before shipping this more widely:** the generalisation changes
behaviour on any build whose swap device is not TVOut. Only the 1.0 bundle has
this engine; re-run the probe for `m68ap-114` and `n45ap` (the iPod runs 1.1 /
3A101a, so check which device IT announces) before installing it there.

### Still open: the transition itself

`3_home_returns` remains 0.00% in every mode. The guest no longer spins, events
flow, the app is told to deactivate, SpringBoard runs its dismissal to
completion -- and no new frame appears. The next candidates, in order:

1. ~~**The MBX registers we answer with zero**~~ **DEAD.** `0xff8`, `0xffc`,
   `0xf00` and `0xf10` are each read **exactly once**, at log lines 974-977 --
   probe/init time, ~650 lines before the press at line 1625 -- and the driver
   went on to attach, start and register its swap device. Nothing re-reads them
   during the dismissal. They are capability/ID registers, not a stall.
2. ~~**Does anything ask the LCD to flip afterwards?**~~ **INCONCLUSIVE, and it
   corrects an old reading.** `IT_LCD_TRACE=1` over a full run shows only **two**
   window-base programs, the last long before the press -- but step 1 (open an
   app) PASSES with none of them. So this path updates the screen by drawing
   into the CURRENT base, not by flipping windows, and "no base flip after the
   press" is not evidence of anything. It also means the old observation that
   "the LCD base flips stopped after the press" was a symptom of the STARVATION
   (now fixed), not of the transition.
3. **What is actually true after the press:** MBX traffic continues and
   completes (7x `WR 0x134`, 3x `rd 0x12c`, 3x `WR 0x130` after the keypress),
   the guest idles, and SpringBoard simply never repaints -- the screen stays at
   99% lit. So the compositor decides not to draw, rather than being blocked in
   the MBX or starved of events.
4. ~~The next instrument has to look at the CoreSurface/LayerKit decision~~
   **DONE -- and it found the discriminator.**

### THE DISCRIMINATOR: 1.0 never re-points the display (2026-07-30)

`gsevent-type-probe.py --break-sym LIB:SYMBOL` breaks on arbitrary library
symbols (unambiguous across processes, unlike an executable's IMPs). LayerKit
carries **1843** defined symbols and CoreSurface **118**, so the compositing
entry points can be watched by name. Two presses in-app, both builds:

| | `_LKBackingStoreSwap` after the press | `_LKImageQueueFlush` / `_LKRenderImageQueueShow` / `CoreSurfaceClientBufferFlushProcessorCaches` | **LCD base programs, whole run** |
|---|---|---|---|
| 1.0 | **yes**, in SpringBoard | never fire, on either build | **2** |
| 1.1.4 | **yes**, in SpringBoard | never fire | **182** |

**Both builds composite. Only 1.1.4 ever tells the display controller.** 1.0
programs the LCD window base twice in an entire boot-plus-session; 1.1.4 does it
182 times. That is why the app opens fine on 1.0 (it draws into the CURRENT
base, in place) and why the dismissal shows nothing: SpringBoard repaints into a
backing store, swaps it, and the base is never re-pointed at the result.

### The hypothesis this hands over, and why it is credible

Recall the swap-device announcements: **1.0 uses `AppleH1CLCD` as its "legacy
swap device"**, while 1.1.4 and the iPod also register `AppleH1TVOut`. So on 1.0
the MBX's swap target IS THE DISPLAY ITSELF -- which suggests the flip on 1.0 is
supposed to be performed BY THE MBX SWAP, and our MBX performs no swaps at all.
1.1.4 does not depend on that: it re-points the base itself, 182 times.

That is a single mechanism explaining every remaining symptom, it is consistent
with everything measured, and it is exactly T1's "model MBX swap completion" --
but for the **legacy CLCD** path, not the TVOut one.

### Where the base programs come from: the SAME driver function on both

`IT_LCD_TRACE=1` now prints the guest pc and lr of whoever programs the window
base (the idiom the `[BTN]` trace already used). Over a full probe run:

| build | writer | kext | calls |
|---|---|---|---|
| 1.1.4 | `pc=0xc0380eec` | **`AppleH1CLCD`+0x1eec** | **522** |
| 1.0 | `pc=0xc0300edc` | **`AppleH1CLCD`+0x1edc** | **2** (both at boot) |
| (both) | `pc=0x1800xxxx` | iBoot, once | 1 |

**Same kext, same function** -- the offsets differ by 0x10 between builds, i.e.
it is the same code. So 1.0 is not missing the ability to re-point the display;
**nothing ever calls it** after boot. 1.1.4 calls it ~500 times a run.

### The repaint exists -- it just is not on screen

The same run makes that visible. With the CLCD swap-device window in place,
1.0's `3_home_returns` now reports **94.62% changed while the lit fraction stays
99.1% -> 99.1%**. The displayed screen did not change; some OTHER buffer gained
94% new content. That is SpringBoard painting the home screen into a buffer the
display is not aimed at -- exactly what "nothing re-points the base" predicts.

**CORRECTION (2026-07-30, same day): that off-screen repaint is INTERMITTENT.**
A later run with the scanout-aware verdict recorded `diff_any_buffer = 0.00` for
step 3 -- no buffer changed anywhere. So "SpringBoard paints the home screen into
an invisible buffer" rests on ONE observation out of two and is NOT established.
What is solid is the scanout verdict below and the caller chain.

**Harness flaw this exposes, and it now produces a FALSE PASS:** `changed()`
takes the largest per-base difference across the three framebuffers (a
deliberate fix, so a transition to a DIMMER screen could not be missed). It
therefore passes when an off-screen buffer changes. Steps 3 and 4 report PASS on
1.0 today while the screen is visibly unchanged, and step 5 then fails. The
verdict needs the CURRENT SCANOUT buffer (the model's own `lcd_scanout_base()`
rule), not the best of three.

### The scanout verdict, fixed and validated

`app-button-probe.py` now reads the LCD's current window base out of the model's
own `IT_LCD_TRACE` output (`scanout_index()`, w1 falling back to w2, the model's
own rule) and judges THAT buffer; the other two are still captured and reported
as `diff_any_buffer`, with an `[off-screen buffers changed N%]` annotation when
they diverge. Validated on both builds in one parallel run:

| board | before the fix | after the fix |
|---|---|---|
| 1.0 | `3_home_returns` **PASS 94.62%** (false) | **FAIL 0.00%** -- honest |
| 1.1.4 | PASS | **PASS 96.98%**, 5/5 unchanged |

1.0's scanout is `FB_BASES[0]` = 0x0FE00000, matching its only w1 program.

### WHO drives the flip: IOMobileGraphicsFamily, from USERLAND

`spin-locate.py --break-kaddr 0xc0380eec --kernelcache ...` breaks at the writer
and names the stack (kernel addresses are global, so a hit needs no process
disambiguation). Three hits, identical each time:

```
AppleH1CLCD+0x1eec                       <- programs the window base
  AppleH1CLCD+0x2c00
  com.apple.iokit.IOMobileGraphicsFamily+0x23b4
  com.apple.iokit.IOMobileGraphicsFamily+0x254c
  kernel+0x138ca3                        (IOKit dispatch)
  com.apple.iokit.IOMobileGraphicsFamily+0x3d34
  kernel+0x14b013 / +0x14b7e7 / +0x149f79 / +0x4e8d7 / +0x13405
                                         (IOUserClient + syscall path)
```

So the flip is an **explicit userland request through the IOMobileFramebuffer
user client** -- not a side effect of an MBX swap. **That weakens the
MBX-legacy-swap hypothesis considerably**: on the build that works, nothing in
this chain involves the MBX.

And the path exists on 1.0: `com.apple.iokit.IOMobileGraphicsFamily` is present
in both kernelcaches (1.0 `0xc02fa000..0xc02ff000`, 1.1.4
`0xc037a000..0xc037f000`), as is `AppleH1CLCD`. So 1.0 has the whole mechanism
and simply never asks for it.

**Next:** find the USERLAND caller on 1.1.4 -- the CoreSurface/LayerKit code that
issues that IOMobileFramebuffer request ~500 times a run -- and check whether
1.0's equivalent exists and is reached. Break on the user-client entry from the
userland side (CoreSurface has 118 exported symbols, LayerKit 1843, so
`--break-sym` can watch the candidates by name).

### Is the change 1.0-specific? No -- all three builds ship the same MBX code

Checked statically against each build's own RELEASE kernelcache (the iPod's root
volume was reconstructed from its NAND with `extract-hfs-from-nand.py`; it runs
**iPhone OS 1.1, 3A101a** -- a third build, not 1.1.4):

| build | `mov r1,#0x12c` sites | unbounded spin loops on bit 6 |
|---|---|---|
| 1.0 (1A543a) | 6 | **2** -- `0xc0336014`, `0xc0336914` |
| 1.1.4 (4A102) | 8 | **2** -- `0xc03ba074`, `0xc03ba974` |
| iPod 1.1 (3A101a) | 8 | **2** -- `0xc03b1074`, `0xc03b1974` |

The two loops are structurally identical in all three, and bit 6 (`0x40`) is
consulted in exactly three places per build: those two unbounded loops plus one
further `tst #0x40` that has a *conditional* backward branch (1.0 `0xc0337bec`,
1.1.4 `0xc03bbc4c`, iPod `0xc03b2c4c`) -- a bounded wait. Every other 0x12C read
tests something else (`tst #0x100` with an `add #1` / `cmp #0x3e8` retry
counter, or `and #0xff0000` / `cmp #0x174`, a revision field).

So the driver is the same everywhere and **1.0 is not special in its code** --
only in the path it takes to reach it. Since 1.1.4 and the iPod demonstrably do
NOT spin today (they pass every probe step and idle at 6-15%), they cannot be
reaching the two unbounded loops; for them the change should be a no-op there.
The one place it could change behaviour on a working build is the third,
bounded, `tst #0x40` site.

**Regression-checked on all three bundles (2026-07-30).** The engine is now
installed in all three and `app-button-probe.py` gives:

| board | 1_open | 2_touch | 3_home_returns | 4_power | 5_wake |
|---|---|---|---|---|---|
| iPod (N45AP, 1.1 / 3A101a) | PASS | PASS | **PASS 98.57%** | PASS | PASS |
| iPhone OS 1.1.4 | PASS | PASS | **PASS 96.98%** | PASS | PASS |
| iPhone OS 1.0 | PASS | PASS | **FAIL 0.00%** | FAIL | — |

So the stub answer costs the working builds nothing: 5/5 on both, unchanged from
before. Backups of the previous engines: `/tmp/qemu-{1.0,114,n45}-engine.bak`,
and `IT_MBX_READY=0` disables the change at runtime without reinstalling.

### "Spins" then, "idles" now -- both are true, in that order

Two sessions measured this with different instruments and the results agree once
the order is stated explicitly:

| | native (host CPU + PC sampling) | browser (guest-seconds per wall-second) |
|---|---|---|
| BEFORE the fix | **spins**: 0.97 cores, 30/30 PC samples in the 9-instruction AppleMBX loop | not measured |
| AFTER the fix | **idles**: 0.10 cores, only the kernel idle delay at `0xc005a2f0` | **idles**: ratio 0.24-0.69 with the frame counter frozen (under `-icount` a high ratio means virtual time is being warped forward because the cpu is halted; a spinning guest reads ~0.02) |

The browser note above is therefore a confirmation of the POST-fix state, not a
contradiction of the spin. And the two instruments cross-validate each other:
the native A/B shows that a halted guest here burns almost no host CPU
(0.10 cores post-fix), so the pre-fix 0.97 cores cannot have been idle-warping --
it was real execution.

## Where it stands: this is the MBX gap (T1), not a 1.0 software bug

**Root cause: `AppleMBX` spins on `(mbx[0x12C] & 0x40)`, which our do-nothing
MBX stub never sets.** Everything else this investigation chased -- the pin, the
IRQ, the port, the routing, both SpringBoard handlers, `clickedMenuButton` -- is
downstream or irrelevant. The correct home for the fix is
[`MBX_HANDOFF.md`](MBX_HANDOFF.md) / task **T1**, which already reads: *"the MBX
region is currently a do-nothing stub with no IRQ connected at all."*

Why 1.1.4 and the iPod do not hit it: both are run with
`LK_ENABLE_MBX2D=0` (a guest plist edit that forces LayerKit to composite in
software -- see `build-m68ap-homescreen-nand.py`). That is a userland knob,
whereas this spin is in the KERNEL driver, so the first thing to establish is
which path on 1.0 still asks the MBX to do work. Note the honest reading: the
difference is not that 1.0 is "harder", it is that 1.0 exercises hardware we
never modelled.

Next steps, in order of cost:

1. ~~**Set bit 6 of MBX register 0x12C**~~ **DONE** -- see the partial fix above.
   It removed the spin and restored event delivery; the visual transition still
   does not complete.
2. **Then do T1 properly**: model swap completion and wire the TVOut SDO IRQ, so
   the driver clears the swap-device field itself. That also retires the
   address-dependent TVOut window hack and the `LK_ENABLE_MBX2D=0` plist edit.
3. **Check whether the 1.0 bundle actually carries `LK_ENABLE_MBX2D=0`**, and
   whether 1.0's LayerKit honours the same key. Cheap, and it explains the
   build-to-build difference.
4. Re-check, once MBX responds, whether POWER-in-app (step 4) and the "touch is
   blocked too" symptom clear at the same time. They should: they are all
   downstream of the same starvation.


## Session close, 2026-07-30: state of 1.0, and three things not to repeat

**iPhone OS 1.0 is in working order except the one defect this doc is about.**
Verified at HEAD with the current engine: boots to the home screen (45.4% lit),
touch works, an icon tap opens an app (`1_open_app` PASS 97.10%), in-app touch
works (`2_touch_in_app` PASS 35.93%). Outstanding: `3_home_returns` and
`4_power_sleeps` at 0.00% -- the in-app display transition, i.e. T1.

### Step 0 from MBX_T1_BRIEF.md: ATTEMPTED, REGRESSES 1.1.4, REVERTED

"Map nothing until a TVOut device is announced" was implemented and **reverted
uncommitted**. Two findings, both worth keeping:

* A bug of mine inside it: `tvout_workaround_move()` early-returns when the
  derived address equals the board default -- the NORMAL case on 1.1.4 -- so with
  nothing mapped at init the window was never placed at all and the guest never
  reached a stable home screen. Fixed by gating that early return on
  `tvout_wa_mapped`.
* With that fixed, 1.1.4 passed steps 1-4 but **`5_home_wakes` failed 2 of 2**
  (0.00%, panel stays at 0.6-22.6% lit) where it had passed at 56% before. So
  the window is needed BEFORE the TVOut announcement on the WAKE path. Step 0 as
  written is not free; do not retry it in that form.

### OPEN, and not mine: 1.1.4 aborts at HEAD in the NAND model

Three consecutive 1.1.4 runs died early with a HOST-side abort, not a guest
panic and not the slowness that first looked like the cause:

```
qemu: hardware error: Unable to read file!      (hw/arm/ipod_touch_nand.c:374)
Abort trap: 6
```

Ruled out: the bundle NAND is intact (`nand.pack` 314,886,212 bytes, dated
Jul 28, untouched by engine installs -- the installer replaces only the binary,
frameworks and signature); not Step 0 (reverted, tree clean); 1.0 boots fine on
the same engine. **HEAD moved during the session** (now `11090d07be`, newer than
the brief), so today's shared-tree changes are the leading suspect. The cheap A/B
is the pre-session engine, kept at `/tmp/qemu-{1.0,114,n45}-engine.bak`.

### The display-client trap recurred, and produced a false ALL-FAIL

One 1.0 run reported every step failing, including `1_open_app` at 0.26%. The
cause is in this document already (trap 2/7): the probe printed
**`display client attached: False`**, and with no display client QEMU never calls
`gfx_update`, so every touch is refused. The very next run, with the client
attached, was normal. **Read that line before believing a touch verdict** -- and
note host load (18-62 during these runs) makes the client attach flakier.


## T1 attempt, 2026-07-30: the MBX-swap fix is WRONG, and so is the LCD IRQ

Went to implement "model the MBX swap" and stopped, because two measurements
taken first killed it. Recording them so nobody writes that code.

**1. The MBX traffic after the press is IDENTICAL to before it.** With
`IT_MBX_TRACE=all` (new: removes the per-register collapse, because the swap
descriptor is written once per swap and a cap of 12 hid exactly it), the whole
post-press sequence is the same one seen at idle:

```
WR 0x130=0  WR 0x134=0xfff  WR 0x080=0x111  WR 0x85c=0x80  WR 0x81c=0
WR 0x1000..0x101c = 0x08ae3000, 0x08ac4000, ... 0x08aac000   (an MMU page list)
WR 0x1020=0x00010001   WR 0x824=0x1d000  0x828=0x22  0x82c=0x25
WR 0x838=1  0x83c=0x21000  WR 0x6d8=0x09000000   (the kick)
```

**None of those values is a framebuffer base** (0x0FE00000 / 0x0F400000 /
0x0F496000), and the sequence does not change across the dismissal. So this is
not the display flip, and "make the MBX perform the swap" would have been
emulating the wrong thing -- with a destination address I could not identify
because there isn't one here.

**2. The LCD frame interrupt is alive and being serviced.** `IT_FB_TRACE=1`
after the press, every second:

```
[FB] vsync status=0x00000000 mask=0x00003f01 irq=0 acked_since_last=yes
```

`mask = 0x3f01` -- **bit 0 IS enabled** -- and the guest acks every tick. So the
frame interrupt is delivered and consumed, which confirms the earlier retraction
(bit 0 is the frame interrupt) and rules the LCD IRQ out as the gap.

### What that leaves

Everything below the compositor is now measured healthy after the press: events
delivered, both handlers run, `clickedMenuButton` completes, the display stack
unwinds, vsync alive and acked, MBX behaving exactly as it does at idle. And
still: **no buffer anywhere changes** (`diff_any_buffer` 0.00) and
`AppleH1CLCD+0x1edc` is never reached.

So SpringBoard is not failing to *present* a repaint -- it is not *producing*
one. The remaining question is a guest-software one at the LayerKit/CoreSurface
level: after the app is dismissed, why is the home screen's layer tree never
rendered? Candidates: its window/layers released when the app went fullscreen
and not restored, or a render context still bound to the app's surface. Note
`_LKImageQueueFlush` and `_LKRenderImageQueueShow` never fire on EITHER build,
so the presentation path in use is `_LKBackingStoreSwap` -- start there.


## Two bugs found on the way, 2026-07-30 (late)

### FIXED: an unreadable NAND page killed the whole emulator

`nand_read()` reached a branch where `stat()` had SUCCEEDED but `fopen()` then
failed -- the file vanished between the two calls, or the descriptor table was
exhausted, or the clone directory went away under a running guest -- and called
`hw_error("Unable to read file!")`, which aborts QEMU.

It killed three consecutive 1.1.4 boots, ~20 lines into the kernel, and the
abort looks nothing like its cause: no guest panic, no message naming the page,
just a register dump. Real silicon has no such failure mode, and an unreadable
page is indistinguishable from an erased one. Now it complains ONCE with the
path and `strerror(errno)` and hands back an erased page, so a transient I/O
failure degrades into a guest-level problem instead of destroying the machine.

### OPEN, and not in 1.0's path: today's tree stops 1.1.4 booting

Clean A/B on the SAME bundle and NAND, only the engine binary swapped:

| engine | result |
| --- | --- |
| pre-session (`/tmp/qemu-114-engine.bak`) | **5/5 PASS**, 449 log lines |
| today's tree (`4f76a0baf5`) | **17 log lines**, stalls right after the kernelcache decrypt, no panic, no abort |

So a change committed today breaks 1.1.4 at early kernel boot. **1.0 boots and
behaves normally on the same engine**, so this does not block the in-app button
work -- but it does mean 1.1.4 can only be used as a control with the OLD engine
(which lacks the MBX bit-6 fix; it passed 5/5 before that fix anyway).

Not bisected. Today's commits span both sessions; the browser-port session's
snapshot/migration work (`6be281f387`, `11090d07be`) is the larger and newer
part, and the MBX bit-6 change is ruled out because 1.1.4 passed 5/5 with it
repeatedly earlier in the day.

**Current bundle state:** 1.0 on today's engine (works, except the transition),
1.1.4 on the pre-session engine (5/5), iPod on today's engine (untested since).


## CORRECTION: SpringBoard DOES render after the dismissal (2026-07-30)

I said one step earlier that SpringBoard "is not producing a repaint". That was
wrong, and watching LayerKit's own dirty/render path says so plainly. 1.0, two
presses in-app:

| phase | LayerKit call | process | count |
| --- | --- | --- | --- |
| p1 | `-[LKLayer setNeedsDisplay]` | **SpringBoard** | 3 |
| p1 | `_LKLayerDisplayIfNeeded` | **SpringBoard** | **20** |
| p1 | `_LKBackingStoreGetRenderImage` | **SpringBoard** | 2 |
| p1 | `_LKBackingStoreSwap` | **SpringBoard** | 2 |

So after the press SpringBoard marks layers dirty, displays them, takes render
images and swaps its backing stores. **It renders.** The pixels simply never
reach the scanned-out framebuffer, and `AppleH1CLCD+0x1edc` is still never
called.

### The break, now pinned to one hop

Chain after the press on 1.0, each link measured:

```
SpringBoard renders (LayerKit)                                   OK
  -> IOMobileFramebufferSwapBegin / SwapEnd / SwapWait  x3       OK  (userland submits)
     -> IOMobileFramebuffer user client -> IOMobileGraphicsFamily   ??
        -> AppleH1CLCD+0x1edc  (programs the window base)        NEVER REACHED
```

On 1.1.4 the identical submission produces ~522 base programs through
`IOMobileGraphicsFamily+0x3d34 -> +0x254c -> +0x23b4 -> AppleH1CLCD+0x1eec`.

So the failure is inside the KERNEL swap path, between the user client and the
CLCD, on a build whose MBX is registered as the *legacy* swap device. That is
the one hop nobody has instrumented yet, and it is instrumentable the same way
the 1.1.4 chain was: `spin-locate.py --break-kaddr <IOMobileGraphicsFamily addr>
--kernelcache`, using 1.0's own kext offsets.


## Two boot/wake regressions from 2026-07-30, both fixed

Neither is the in-app button bug; both were introduced today and both broke
SHIPPED bundles, so they are recorded here with their mechanisms.

### 1. The TVOut window was never placed (1.1.4 and the iPod would not boot)

`a4619ce78e` made the window wait for the kernel's announcement instead of being
mapped blind at init -- correct, and it is what stops the window faulting 1.0.
But `tvout_workaround_move()` still opened with

```c
if (!tvout_wa_region || pa == tvout_wa_addr) return;
```

and **the board default is exactly what 1.1.4 and the iPod announce**. So the
derived address equalled `tvout_wa_addr`, the function returned before mapping
anything, and with nothing mapped at init the window was never placed at all.
The log says it outright -- `derived 0x089c8560 ... matches the board default`
and then no placement line -- and the guest never reaches a stable home screen.

Fixed by gating that early return on `tvout_wa_mapped`: the board constant is
now only a prediction to check against, and must not double as "already there".

### 2. Parking with PAUSED made the machine unwakeable by injected input

The same commit changed the pre-warm park from `vm_stop(RUN_STATE_SUSPENDED)` to
`RUN_STATE_PAUSED`, reasoning that "nothing here uses the suspend semantics".
Something does -- **QEMU's own input gate**:

```c
/* qmp_input_send_event() */
if (!runstate_is_running() && !runstate_check(RUN_STATE_SUSPENDED)) {
    error_setg(errp, "VM not running");
```

SUSPENDED is deliberately wakeable by injected input; PAUSED is not. Parked with
PAUSED the wake keypress was refused by QMP and never reached
`ipod_touch_key_event()` at all -- measured on 1.1.4 AND the iPod as
`[WAKE] Pre-warmed wake parked; awaiting Power/Home` followed by **no further
`[BTN]` line**, and `5_home_wakes` at 0.00%.

Note the shape: a UI keypress still worked (`ui/cocoa.m` calls
`qemu_input_event_send_key()` with no runstate check), so this did not break the
feature for a person sitting in front of the app -- it broke **every automated
sleep/wake test**, which is worse, because the tests are what would have caught
it.

Fixed by parking SUSPENDED again -- which is also what is actually true (the
guest performed a system suspend and awaits a wake source) -- while KEEPING the
half of `a4619ce78e` that fixes the migration bug: `ipod_touch.c` calls
`vm_set_suspended(false)` before every `vm_start()` on the wake path, so the
sticky flag no longer leaks into snapshots taken after a wake.

### All three bundles, after both fixes

| board | 1_open | 2_touch | 3_home_returns | 4_power | 5_wake |
|---|---|---|---|---|---|
| iPod (N45AP) | PASS | PASS | **PASS 98.58%** | PASS | **PASS 56.46%** |
| iPhone OS 1.1.4 | PASS | PASS | **PASS 96.99%** | PASS | **PASS 74.63%** |
| iPhone OS 1.0 | PASS | PASS | FAIL 0.00% | FAIL | -- |

1.0 is unchanged by either fix, as expected: it announces no TVOut device, so it
places no window, and it never reaches the in-app transition that would sleep.

### Correction to an earlier entry in this document

"1.1.4 stops booting on today's tree" (committed in `d84422b6fe`) named the
snapshot/migration work as the suspect. That was wrong on the cause: the boot
failure was regression 1 above, and the `hw_error("Unable to read file!")` abort
that first pointed at the NAND was a SEPARATE fault under a full disk. The NAND
robustness fix in that commit stands on its own merits; the attribution does not.


## The break, narrowed to one value: LayerKit's framebuffer handle is NULL (2026-07-30)

Walking down from userland with `--break-sym` and the IOKit user-client funnel.

**1. The swap DOES reach the kernel on 1.0** -- 74 `IOConnectCall*` hits after the
press. But look at WHICH IOMobileFramebuffer functions make them:

| build | user-client calls after the press |
| --- | --- |
| 1.1.4 | `SwapBegin`+0x48, `SwapEnd`+0x38, `SwapWait`+0x58 -- 41 each |
| **1.0** | `SwapWait`+0x58, `EnableVSyncNotifications`+0x68, `DisableVSyncNotifications`+0x50, `GetVSyncRunLoopSource`+0x3c |

**`SwapBegin` and `SwapEnd` never reach the kernel on 1.0**, although both
functions are entered (measured earlier: 3 calls each after the press).

**2. Why: the framebuffer handle is NULL.** `_IOMobileFramebufferSwapBegin` on
1.0 (`0x31bf9378`) skips its `IOConnectCallScalarMethod` on exactly one path:

```
31bf937c  subs r4, r0, #0          ; the fb handle
31bf9390  beq  0x31bf940c          ; NULL -> load an error code and return
...
31bf93bc  bl   _IOConnectCallScalarMethod    ; the only kernel call
```

Measured at the entry: **`r0(fb) = 0x00000000`**, called from `LayerKit+0x39400`.
So LayerKit asks for a swap with no framebuffer, the function returns before
touching the kernel, and nothing re-points the display. That is the whole
mechanism, and it explains every downstream symptom: an app can open (it draws
in place, no swap needed) while the dismissal repaint can never be presented.

### What is NOT yet established

* **Whether the NULL handle is the MAIN display or a secondary context.** 1.0 has
  no TVOut, so a TVOut display context would legitimately carry a NULL fb. What
  argues against that being harmless: no `SwapBegin` from ANY context reaches the
  kernel.
* **Why it is NULL.** `_IOMobileFramebufferOpen` (`0x31bf9cfc`) recorded 0 hits,
  but the probe attaches gdb after the home screen is up and the open would
  happen at SpringBoard startup -- so this does not distinguish "never called"
  from "called before the window and failed". Arming that breakpoint from boot
  is the next measurement.
* **Why `SwapWait` reaches the kernel while `SwapBegin` does not**, if both take
  the same handle. Either `SwapWait` has no NULL guard, or it is called on a
  different context. Worth reading before concluding.


## IOMobileFramebufferOpen IS called (2026-07-30, `scripts/boot-break.py`)

The previous entry left "never called" and "called at startup and failed"
undecided, because every probe here attaches gdb only once the home screen is
up -- they need to tap an icon first -- and the open happens long before that.
`scripts/boot-break.py` exists for that gap: it arms breakpoints **before the
guest boots**, sends no input at all (so nothing depends on the VM being
resumable by QMP), and logs every hit with its argument registers.

Result on 1.0, armed from t=0:

```
  8 hits   IOMobileFramebuffer:_IOMobileFramebufferOpen
 36 hits   LayerKit:_LKBackingStoreSwap   in SpringBoard
```

**So the open IS called** -- eight times -- and the handle LayerKit later passes
to `SwapBegin` is still NULL. "Never called" is dead. What remains is the pair:
the open fails, or it succeeds for some contexts while the one LayerKit swaps on
is a different, never-opened context.

The next measurement is the return value, not the entry: break on the open, step
to its return, and read r0. `boot-break.py` records entry args only, so that
needs a small extension -- and note the caller (`lr`) is worth capturing at the
same time, since it distinguishes which display context is being opened.

### Dead end recorded: kext offsets are not transferable between builds

Before this, the plan was to reuse 1.1.4's kernel chain offsets
(`IOMobileGraphicsFamily+0x3d34 -> +0x254c -> +0x23b4`) on 1.0, since both kexts
are 0x5000 bytes. **They share only 5.4% of their bytes** -- measured -- and the
words at all three offsets differ. So the two builds' graphics kexts are not the
same binary and offsets must be derived per build. Two related facts from the
same attempt:

* Nothing branches to the LCD base-programming address `0xc0300edc`, because that
  is an instruction INSIDE the function, not its entry. The entry is
  `0xc0300c6c` (`AppleH1CLCD+0x1c6c`), reached from four call sites in the same
  kext (`0xc0301bec`, `0xc03021ac`, `0xc03021fc`, `0xc0302238`).
* Breaking at that entry after the press: **no hit in 60 s**. So the base
  programmer is not entered and bailing -- it is never called at all, which is
  consistent with the NULL handle upstream.
* 1.0's `IOMobileGraphicsFamily` has **no C++ symbol table** (`_ZN22IOMobile...`
  count 0; only two plain `IOMobileFramebufferUserClient` strings, from the
  OSMetaClass registration). The "1.0 kexts keep full C++ symbols" note in this
  project applies to other kexts, not this one.

### Two harness bugs found and fixed this session

* **`scripts/guest_root.py`** -- a disk image can only be attached ONCE, and the
  other session keeps the root images mounted at `/private/tmp/m68_10` and
  `/private/tmp/m68_114`. Every probe had its own `hdiutil attach` and so failed
  with "Resource busy", then died later on a `FileNotFoundError` naming a path
  inside a mountpoint nobody populated -- which reads like missing firmware, not
  a busy image. The helper reuses an existing attachment and detaches only what
  it attached. It also records the parsing detail that got it wrong first time:
  the mountpoint line can have only TWO fields
  (`/dev/disk5\t\t/private/tmp/m68_10`), so a `len(parts) >= 3` test never
  matches.
* **`boot-break.py` created phantom breakpoints.** On an unexpected stop it ran
  the same del/step/set sequence used to step off a real breakpoint -- which
  SETS a breakpoint at an address never asked for. One spurious stop in BlueTool
  became 156 recurring hits and capped the run at its hit limit before the
  interesting window. Fixed: only re-arm when the stop address is one of ours.


## The open SUCCEEDS (2026-07-30, `boot-break.py --capture-return`)

`--capture-return` puts a temporary breakpoint on the caller's return address
(`lr` at function entry) and reads r0 there, so entry args are no longer the only
thing available. On 1.0, armed from boot:

```
t=14.9   _IOMobileFramebufferOpen  r0=0x1e03 r1=0x0807  lr=GraphicsServices+0x7904  -> r0=0x0
t=14.9   _IOMobileFramebufferOpen  r0=0x1e03 r1=0x0807  lr=LayerKit+0x383bc          -> r0=0x0
t=18.3   _IOMobileFramebufferOpen  r0=0x1e03 r1=0x0103  lr=GraphicsServices+0x7904  -> r0=0x0
   ... the same three again at t=163-167 (a second process)
```

**Every call returns 0 = `KERN_SUCCESS`.** So "the open fails" is dead too. The
handle comes back through an out-parameter, not the return value, and the callers
are `_GSHeartbeatCreate+0x204` and **`_new_display+0x80`** -- the latter being
LayerKit building a display object, which is exactly where `d->fb` is set.

Note `r1` differs between calls: `0x0807` twice and `0x0103` once. Two different
displays are being opened, which makes the "multiple display contexts" reading
concrete rather than speculative.

### Caveat that weakens an earlier claim in this document

"`SwapBegin` and `SwapEnd` never reach the kernel on 1.0" was read off a
histogram printed with `most_common(6)`. That run had **74** IOConnect hits in
the press window and the six shown account for 59 of them, so ~15 were never
displayed -- `SwapBegin+0x48` (which would appear as `IOMobileFramebuffer+0x13c0`)
could be among them. The NULL-handle measurement stands on its own -- r0 was read
directly at the entry -- but "never reaches the kernel" should be re-derived from
the full histogram before anyone builds on it.

### Where this leaves the hunt

Both simple explanations are now excluded by measurement: the open is called, and
it succeeds. So the live question is which display object LayerKit swaps on:

* `_new_display` is called at least twice (r1 `0x0807` and `0x0103`), so there are
  at least two display objects.
* The `SwapBegin` seen with a NULL handle came from `LayerKit+0x39400`; whether
  that is the main display or the second one is undetermined.

The next measurement is to break in `_new_display` and record, per call, the r1
selector together with the fb handle it stores -- then compare against the handle
`LayerKit+0x39400` passes to `SwapBegin`. That identifies whether the swap is
being issued on a display that was never opened, or on one whose open result was
dropped.


## RETRACTION: the NULL framebuffer handle was an artifact (2026-07-30, late)

Static disassembly of 1.0's LayerKit (all stubs resolved through the indirect
symbol table -- `scratchpad stubs.py`, now reproducible with capstone) proves the
pinned mechanism impossible, and a re-measurement with memory dereferencing
confirms it:

* `LayerKit+0x39400` -- the measured SwapBegin caller -- is the return address of
  the call at `_LKDisplayFinishUpdate+0x7c` (0x30b003fc). But FinishUpdate
  **guards `d->fb != 0` at its entry** (0x30b0039c: `ldr r3,[r0,#8]; cmp r3,#0;
  beq epilogue`) and reloads the SAME field into r0 for the call, with no store
  in between. That call site cannot pass a NULL fb.
* Re-measured with the new `--deref` option (gsevent-type-probe now reads guest
  memory at breakpoint hits): every SwapBegin from that lr carries
  **fb=0x12aaa0, valid**, with a live IOKit connect port at [fb+0xc]=0x2517.
  SwapBegin at +0x7c simply runs every frame because the swap token
  `[buffer+0x10]` is cleared at the end of each FinishUpdate.
* A NULL-fb display DOES exist by design: `_LKDisplayByName` creates the main
  display (`{AppleMX31IPU, AppleH1CLCD}`, flag=0, fb kept for life) and a
  SECOND display (`{AppleH1TVOut}`, flag=1) whose fb is deliberately
  CFRelease'd inside `_new_display` (bit-0 path, 0x30aff558). The original
  NULL reading was almost certainly that display or a mid-step register read.
  1.1.4's LayerKit has the identical two-display structure and works.

Structure recovered on the way (1.0 LKDisplay object): [0]=service, [4]=name,
[8]=fb, [0xc]=ID, [0x24]=buffers, [0x30]=current buffer index, [0x38]=buffer
count, [0x84]=pending update, [0xa0]=flags (bit0=release-fb-at-create,
bit1=disabled, bit5=secondary). `_LKDisplaySetEnabled(d,1)` reopens the fb if
NULL and calls `IOMobileFramebufferRequestPowerChange(fb,1)`;
`SetEnabled(d,0)` swaps a NULL layer x3 + RequestPowerChange(fb,0).
Userland selectors: SwapBegin=4 (scalar), SwapEnd=5 (0x80-byte struct in),
SwapWait=6.


## THE ACTUAL SHAPE: the press kills the render loop, and something CRASHES (2026-07-30, late)

Same run (`/tmp/gsev-m68ap-10-swap`, 2 presses, gsevent-type-probe --deref):

* **Baseline, app frontmost: SpringBoard's swap path is fully healthy.** Every
  ~6 s (status bar): `SetEnabled(d=0xaff40,1)` -> `FinishUpdate` (fb valid) ->
  `SwapBegin` -> `SwapEnd`, buffer index cycling 2->0->1->2. The display CAN be
  repointed at any time; nothing is broken until the press.
* Press 2 delivered completely: menu DOWN+UP to SpringBoard, and **Preferences
  received its type2002 deactivation** at t=21.33.
* After the press: **zero** FinishUpdate / SetEnabled / SwapBegin / SwapEnd hits
  in ANY process for the remaining 66 s. Even the periodic status-bar render
  stops. The failure is not "swap not submitted" -- the render loop's
  display-flush section never runs again.
* The gate is in `_render_for_time` (0x30b07cac): if the render context's
  attached-layer count `[ctx+0x24]` is 0, the whole flush section --
  SetEnabled at +0x88, FinishUpdate at +0x49c -- is skipped. After the
  dismissal SpringBoard's context has NO attached layers, forever.
* **`crashdump` ran at t=31.99** -- 10.6 s after the deactivation, the classic
  1.x watchdog shape: the app did not exit within 10 s of its 2002 and was
  killed. SpringBoard then received GSEvent **type2001 sub=31** (app-exited,
  sub = pid) at t=32.06 -- and still never re-attached its layers.

So the standing question is no longer LayerKit bookkeeping. It is:
**why does the 1.0 app hang instead of exiting after its type2002**, and
secondarily why SpringBoard's home-screen layer re-attach (which should follow
the app-exit notification) never happens. The crash report the watchdog kill
produces names the exact thread and PC where the app sat -- capture run in
progress with `S5L8900_STAGE_NAND=0` and a preserved NAND, to be read back with
`extract-hfs-from-nand.py --partition data`.


## THE APP'S DEATH, FROM ITS OWN CRASH LOG (2026-07-30, late)

Run with `S5L8900_STAGE_NAND=0` and a preserved NAND clone; the guest's own
crash report recovered from the raw guest-written pages (`bank*/26528_new.page`
-- `extract-hfs-from-nand.py --partition data` cannot follow the FTL remap of a
dirtied NAND, but the report is plain text in the pages). Preferences [31],
parent SpringBoard [15]:

```
Exception Codes: 0x8badf00d            <- the 1.x watchdog ("ate bad food")
Thread 0:
0  libSystem   0x300053f4              mach_msg_trap
1  libSystem   0x30005373              mach_msg
2  LayerKit    0x30afd2bc  __LKSRenderClient+0xb4
3  LayerKit    0x30b0de50  _LKPurpleServerRenderContext+0x7c
4+ UIKit 0x323c1644, 0x323c172c, ...   the suspend path
```

**The app dies waiting on a synchronous Mach RPC to SpringBoard's purple render
server.** During its HOME-press suspend path, UIKit asks LayerKit for the
purple server render context; the MIG call never gets a reply; 10 s later the
watchdog kills the app. The app never touches the framebuffer, MBX, or any
IOKit service on this path -- every hardware-facing theory about the APP side is
dead. The app's death is DOWNSTREAM of SpringBoard.

So one wedged SpringBoard thread explains the entire symptom set:

* it stops calling `_LKDisplayFinishUpdate` (the render loop and the purple
  server run there),
* it never re-attaches the home-screen layers (so `[ctx+0x24]` stays 0),
* it never replies to the app's render-context RPC (so the app is
  watchdog-killed, crashdump runs, SpringBoard receives type2001 sub=pid),
* and GSEvent delivery in SpringBoard's MAIN thread keeps working (menu
  DOWN/UP for press 2, the type2001 -- all measured arriving).

The candidate wedge, and it is exactly T1's shape: at dismissal the render
thread enters kernel `SwapWait` (selector 6; `SwapWait+0x58`'s IOConnect DID
reach the kernel in the press window per the earlier histogram, and never
appears at baseline) and sleeps forever on a swap completion our MBX/CLCD model
never signals. Kernel side, all of SwapBegin/End/Wait funnel into ONE virtual
call on the legacy swap device object at `[fb+0x58]`, vtable+0xec
(`IOMobileGraphicsFamily` 0xc02fd90c / 0xc02fd990 / ...; "AppleMBX: Using %s as
legacy swap device" -- on 1.0 that is AppleH1CLCD, there being no TVOut).
Verification in flight: SwapWait entry vs its return-from-kernel instruction
(0x31bf9818) breakpoint pairing in SpringBoard across the press.


## WEDGE PINNED TO THE INSTRUCTION: `_mbx2DFinish` inside the app-snapshot RPC (2026-07-31)

Three probe runs with the new thread-identity (`sp`) recording:

1. SpringBoard has THREE relevant threads: main (sp 0x2ffffxxx -- menu handlers,
   `_LKRenderServerKick` from lr 0x30b06598), the LayerKit server thread
   (sp 0x5bbxxx -- `_LKRenderServerDispatchMessage`), and the render thread
   (sp 0x53bxxx -- `render_for_time` + `FinishUpdate`).
2. Healthy cadence: main kicks (bursts of 5) -> server dispatches msg 40001 ->
   render thread ticks. After the dismissal press: main KEEPS KICKING (bursts
   every ~15 s, forever) and the server never dispatches again. The wedge is in
   the server thread, and it happened while handling exactly one message.
3. That message is the app's snapshot RPC, and the server thread's last five
   acts are (run 7, `/tmp/gsev-m68ap-10-lkx`):

   ```
   t=23.35  __LKXRenderClient                 entry (server side of the RPC)
   t=23.35  _CoreSurfaceBufferLookup          ok
   t=23.36  _LKDisplayCreateBuffer            ok
   t=23.36  _idle_hardware                    ok (its MBX arm is skipped)
   t=23.38  MBX2D _mbx2DFinish  lr=0x30b0db2c inside _LKRenderMBX2DRenderBuffer
            -- LAST HIT EVER; never returns; guest idle
   ```

**So: `__LKXRenderClient` renders the client's window into its CoreSurface via
`_LKRenderMBX2DRenderBuffer` -- the MBX 2D path -- even though
`LK_ENABLE_MBX2D=0`.** The env var gates SpringBoard's own compositing
(`[r8]/[sl]` globals in `_idle_hardware`), but `__LKXRenderClient+0x1cc` checks
a DIFFERENT global (0 -> lazily `_LKRenderMBX2DNew` at +0x410, then
RenderBuffer). `_mbx2DFinish` then waits for 2D-transfer completion from our
do-nothing MBX stub, idle (a kernel sleep, not a spin -- this is the "waits on
an MBX completion that never arrives" from the 2026-07-29 entry, now with the
exact call chain).

Downstream, in order: no RPC reply -> app watchdog-killed at +10 s
(`0x8badf00d`) -> crashdump + type2001 to SpringBoard -> SpringBoard main
thread kicks the dead server forever -> layers never re-attach -> display never
re-pointed. One sleeping thread, every symptom.

The kernel's own DT names the MBX interrupt: node `mbx`, `interrupts = 0x0C`
(SoC IRQ 12; LCD is 0xD). The emulator already has completion-event modelling
(`IT_MBX_EVENTS=1`: kick at 0x6d8/0x1020 sets event bit 6, host-clear at 0x134,
enable at 0x130) and an unbound IRQ line (`IT_MBX_IRQ=<n>`). A/B in flight:
`IT_MBX_EVENTS=1 IT_MBX_IRQ=12 app-button-probe --board m68ap-10`.


## FIRST EVER 3_home_returns PASS on 1.0 (2026-07-31): the missing register is a WRITE to 0x12C

The `IT_MBX_EVENTS=1 IT_MBX_IRQ=12` A/B failed, but its trace (30 MILLION MBX
lines -- which also filled the disk; `IT_MBX_TRACE=all` output must be deleted
after reading) showed exactly why. After the 0x6d8 command completes and is
acked, the guest runs a start sequence and then:

```
WR 0x00108 = 3            ; start
WR 0x00130 = 0xffff       ; enable ALL events
WR 0x0012c = 0x00000001   ; <-- HOST WRITES THE STATUS REGISTER (soft event)
WR 0x00130 = 0
rd 0x0012c = 0x100 ...    ; then polls for the response, 5.1M times
```

A WRITE to 0x12C is the host raising a soft event at the microkernel; the model
dropped it on the floor, so the response event never appeared and the
conversation died there -- as an idle sleep under the ready-bit lie, as a
5M-read spin under the events model. Fix: `0x12c` write ORs into event status
(the do-nothing microkernel "completes" the soft event instantly).

With that (engine rebuilt AND installed into the 1.0 bundle -- the bundle
carries its own binary; pre-fix copy at /tmp/qemu-1.0-engine.pre-mbx12c.bak):

| run | result |
| --- | --- |
| IT_PROBE_WAIT=8  | 3_home_returns **PASS 68.32%** -- the FIRST pass ever on this step; the PNG catches the zoom-out mid-animation (Settings title bar over SpringBoard pinstripe) |
| IT_PROBE_WAIT=14 | 3_home_returns 0.00% but 4_power_sleeps window shows 99->45.4% lit = the HOME SCREEN arriving late |

So the dismissal now COMPLETES but takes >10 s -- consistent with the app
still being watchdog-killed (or another timeout) before SpringBoard recovers,
where before the fix SpringBoard never recovered at all. Latency measurement in
flight. Env used: IT_MBX_EVENTS=1 IT_MBX_IRQ=12 (DT: mbx interrupts = 0x0C).


## THE FIX LANDS: 3_home_returns passes on 1.0 (2026-07-31)

Three MBX model changes, each derived from a measured register conversation
(never guessed), engine installed into the 1.0 bundle:

1. **`WR 0x12C` = host soft event** -> OR into event status. (The conversation
   previously died here.)
2. **Kick bit 8 at 0x1020** (`0x00010100`, after a microkernel code upload via
   the 0x1024/0x1028 index/data pairs) -> completion, same as bit 0. The old
   `(val & 1)` test missed it.
3. **The command-stream fire at 0xA00000** (descriptor block 0xa00000..0xa0003c
   rewritten with `0xf0000000` word 0, the last write before the guest polls
   0x12C) -> completion.

With all three, plus `IT_MBX_EVENTS=1 IT_MBX_IRQ=12` (the DT's own interrupt
number for the mbx node), on the 1.0 bundle:

| wait | 1_open | 2_touch | 3_home_returns | 4_power | 5_wake |
|---|---|---|---|---|---|
| 8s  | PASS | PASS | FAIL (arrives late) | PASS 98.73% | PASS |
| 14s | PASS | flaky | **PASS 94.74%** | flaky | -- |
| 20s | PASS | flaky | **PASS 97.05%** | PASS | FAIL |

`3_home_returns` non-zero -- the acceptance criterion -- is now met
repeatedly (68.32%, 97.05%, 94.74%). The dismissal takes ~8-14 s (the app is
likely still watchdog-killed and SpringBoard now RECOVERS, where before it
wedged forever; the crash log is still written). Step 2/4/5 vary run to run
with timing; the wedge itself is gone -- the render loop keeps ticking after
every press in every instrumented run.

The env pair is now exported by the launcher's `iphone-2g` profile
(`scripts/ipod-app-launcher.sh`, installed into the 1.0 bundle), so a plain
double-click gets the fix; `IT_MBX_EVENTS=0` reverts to the old stub for A/Bs.

**NOT yet done:** the ~10 s dismissal latency (next lever: the repeated
microkernel re-init loop reading 0xff0..0xffc/0xf10 as zero -- the guest
probably wants a liveness value there); consolidating 1.1.4/iPod onto this
engine (their bundles still carry their own binaries, untouched tonight);
regression runs on both (in flight).

### Acceptance runs, all three boards (2026-07-31, ~01:00)

| board | engine | 1 | 2 | 3_home_returns | 4 | 5 |
|---|---|---|---|---|---|---|
| m68ap-10 (fixed engine + launcher env) | tonight's | PASS | flaky | **PASS 94.74%** | flaky | flaky |
| m68ap-114 (own bundle, untouched) | pre-session | PASS | PASS | PASS 96.98% | PASS | PASS |
| n45ap (own bundle, untouched) | its own | PASS | PASS | PASS 98.58% | PASS | FAIL x2 |

The n45ap `5_home_wakes` failures show off-screen buffers changing 56% with a
dark panel -- the documented one-shot-screendump/panel-sleep artifact on a
binary tonight's work never touched (historical pass rate for that step: 56%).
Recorded, not chased: it is not this investigation's bug.

Also recorded: the FIRST in-app press still loses its DOWN GSEvent about half
the time (UP-only delivery, swallowed at the `_menuButtonTimer == nil` gate);
the second press then works. Pre-existing, unrelated to the MBX work, worth its
own hunt.


## ENGINE CONSOLIDATION: all three bundles on tonight's engine (2026-07-31, ~02:00)

All three app bundles now carry the SAME engine (tonight's build, with the
three MBX completions) and the same launcher (MBX env exported only by the
iphone-2g profile; the iPod profile sets nothing, so its MBX model is
byte-identical to before). Backups: `/private/tmp/engine-backup-*.bak`,
`/private/tmp/launcher-backup-*.bak`, and the 1.0 pre-fix engine at
`/private/tmp/qemu-1.0-engine.pre-mbx12c.bak`. The 1.1.4 and iPod bundles had
been sharing one older engine (md5 32c092a1...); tonight's is 6609a097...

Before/after on the REAL bundles, same probe, same 8 s windows:

| board | before (own old engine) | after (consolidated engine) |
|---|---|---|
| m68ap-114 | 5/5 | **5/5** (3_home_returns 96.99%, within noise of before's 96.98%) |
| n45ap | 4/5 (5_home_wakes dark-panel artifact, off-screen buffers 56%) | **4/5, identical failure signature** -- pre-existing, unchanged |
| m68ap-10 | 2/5 (3_home_returns 0.00% forever) | **5/5 at IT_PROBE_WAIT=10** -- the first full sweep on 1.0 |

**Speed back to SpringBoard (m68ap-10):** fails at 8 s, passes at 10 s -- the
dismissal takes 8-10 s, consistent with the app's 10 s suspend watchdog still
firing inside the loop (SpringBoard then recovers and repaints, where before
the fix it never did). 1.1.4's dismissal remains sub-8 s on the same engine, so
the latency is 1.0's snapshot handshake, not the consolidated engine. The
watchdog also fits the crash log still being written on 1.0 dismissals.
A @12 s run in the same batch desynced on the FIRST-press DOWN swallow (step 2
already 0.30%) and says nothing about the dismissal; the swallow remains the
next quirk worth its own hunt, along with shaving the 8-10 s to animation
speed (lever: the microkernel re-init loop reading 0xff0..0xffc/0xf10 as 0).


## THE LATENCY'S CORE REASON: an MBX interrupt STORM of our own making (2026-07-31)

The user reports ~50 s to dismiss in real interactive use; the probe said
8-10 s. Chased with a new gdb-free probe (`scripts/dismiss-latency.py`,
framebuffer polling over QMP) plus the timestamped MBX trace and a PC-sampled
spin (`spin-locate`):

* The timestamped trace looked clean -- until the per-register collapse was
  read carefully: the `n=` counters on the post-press ack/poll lines were in
  the MILLIONS (`WR 0x130` n=3,075,072 across 4 s and climbing). The spin
  never went away; the collapse had hidden it.
* PC-sampling during the spin: 40/40 samples in kernel interrupt paths, the
  PL192 VIC, and AppleMBX's ISR (+0x5e94/+0x6198), one full host core.
* Mechanism: the model's ack handler re-latched READY (`status |= 0x100`)
  after EVERY host-clear, and with events enabled (`0x130=0xffff` on the
  dismissal path) the permanently-pending bit held level IRQ 12 asserted --
  an interrupt storm, exactly the exynos UART Tx-storm shape this project has
  hit before. The guest's bounded driver retries burned their whole budget at
  100% CPU, which is why the dismissal took 8-10 s here and ~50 s on a loaded
  Mac: a storm scales with everything else the host is doing.

Fix: READY is presentation, not an event -- reads of 0x12C still show it
(`mbx_status_12c` ORs it in), but it is never latched into `mbx_event_status`
on ack and is excluded from the IRQ computation. With that engine in all three
bundles:

| board | @8 s window |
|---|---|
| m68ap-10 | 1-4 PASS, **3_home_returns 97.19%** -- first pass at 8 s; effective press -> home screen measured ~7 s including the exit animation and dim |
| m68ap-114 | 5/5 |
| n45ap | 4/5, same pre-existing wake-screendump artifact |

Remaining, and now clearly the dominant user-facing issue: **press delivery**
-- a HOME press with an app frontmost is still swallowed roughly half the time
(model always sees both edges; the DOWN GSEvent never reaches SpringBoard).
In real use that reads as "I pressed home N times over a minute before it
worked", which is most of the user's 50 s. Separate hunt, recorded at the
2026-07-31 acceptance entry. Note for future probes: continuous pmemsave
polling around a press perturbs delivery itself (BQL + guest-time dilation);
dismiss-latency runs its verdict polling at 1 s for this reason and still
shows the effect -- prefer app-button-probe's press-wait-grab shape for
pass/fail verdicts.


## THE "DOWN SWALLOW" WAS NOT REAL: verdict windows shorter than the dismissal (2026-07-31)

The hunt for the press-delivery swallow ends in a retraction -- the fifth
instrument artifact of this investigation, and the second one reported to the
user as a finding.

**Measured, with a dedicated probe** (`scripts/home-swallow-probe.py`: boot the
1.0 bundle, open an app, press HOME repeatedly, reopen the app after every
dismissal, and correlate each press's verdict with the model's own timestamped
traces -- `[BTN]`/`[KEYTRACE]`/`[SYSIC]` lines now carry QEMU_CLOCK_VIRTUAL):

* At the standard 16 s verdict window: **2/8 and 2/6 presses "dismissed"** --
  and the failures were not random. The pattern was strictly periodic:
  SWALLOWED, SWALLOWED, DISMISSED, after every app-open, twice per run.
* Model-side, swallowed and dismissed presses are **indistinguishable**. Every
  press kept a full ~130-149 ms VIRTUAL width (so `-icount` dilation is not
  the mechanism -- that hypothesis was drafted, implemented, refuted by this
  measurement, and reverted). The guest ACKed group 1's menu bit within ~1 ms
  of BOTH edges of every press, and `AppleM68Buttons` sampled the pin register
  (GPIO 0x2c4, PC 0xc040cb74, five reads per edge -- one per button) reading
  the correct level each time: 0x7 pressed, 0x6 released.
* At a 40 s verdict window (`IT_PROBE_WAIT=5`): **4/4 presses dismissed the
  app.** Every in-app HOME press delivers.

The deterministic S,S,D rhythm was attribution, not delivery: under probe load
(VNC display client + three trace channels) the dismissal takes ~17-40 s, so
press 1's dismissal landed inside press 3's verdict window. Press 1 scored
0.00%, press 3 inherited its repaint. The earlier GSEvent-level "UP arrives,
DOWN never does, dies at the `_menuButtonTimer == nil` gate" observations were
sampled while a previous press's dismissal was still in flight -- a press
landing mid-dismissal is genuinely dropped by a SpringBoard that is busy in the
snapshot RPC, but that is DOWNSTREAM of the latency, not a delivery bug.

**Instrument trap #5, the generalizable one:** a pass/fail verdict window must
be longer than the worst-case latency of the thing it judges, or successes
migrate to later presses and read as a fractional "swallow rate". A periodic
failure pattern (every Nth attempt works) is the signature of this trap --
randomness is what real races look like.

What this leaves as the actual user-facing issue: **dismissal latency**, again.
~7 s on an unloaded host, ~17-40 s with a display client attached and the host
busy -- in real interactive use the user presses HOME again during that
window, those mid-dismissal presses do nothing, and the experience reads as
"the button only works every third press". The open lever is unchanged and
already recorded: the MBX microkernel re-init loop reading 0xff0..0xffc/0xf10
as zero.

Fidelity fix landed alongside (found while ruling the model out, kept on its
own merits): SYSIC `GPIO_INTLEVEL` writes were silently discarded and reads
always returned 0; the register is now stored, traced (`wr INTLEVEL`), and
read back. This register is NOT unused: the traces show the button ISR reads
group 1's INTLEVEL on every edge and toggles the menu pin's polarity bit
(`0x04003900` pressed-watch <-> `0x04003800` released-watch) -- before this
fix every one of those reads returned 0, and delivery still worked because the
driver also samples the pin data register (0x2c4). A/B at 16 s windows shows
identical behavior with and without the store (same S,S,D attribution
pattern), so the store changes observability, not behavior -- but any future
guest that trusts the readback now gets the truth.

Regression with the instrumented engine (standard `app-button-probe`, default
16 s-class windows, host concurrently running a wasm toolchain build): steps
1/2/4/5 PASS, `3_home_returns` FAIL 0.00% -- the dismissal did not finish
inside the window on a loaded host, which is the latency mechanism above, not
a delivery regression (POWER and HOME-wake still work immediately after). The
40 s-window 4/4 run on the same engine is the acceptance evidence for press
delivery.


## THE LATENCY, FULLY NAMED: ~33 x 1.02 s MBX FinishSurface timeouts (2026-07-31, the sampler hunt)

The "microkernel re-init loop reading 0xff0..0xffc/0xf10 as zero" lever in the
memory notes was STALE (item 1 above already marked it DEAD). The real chain,
measured link by link:

**The shape.** With virtual-timestamped `[LCD]` flips (added this session), a
single no-gdb in-app HOME press on 1.0 shows: instant reaction (the snapshot
render repaints a buffer at +0.5..1.2 s, invisible), then ZERO framebuffer
activity, then the dismissal animation runs at full ~55 Hz and completes --
starting at a FIXED **press + 33.76 s of virtual time** (33.75/33.76/33.76/
33.76/33.73/33.75 across six independent runs and two engines). 1.1.4 on the
SAME engine dismisses in 1.34 s. Mid-freeze the guest is IDLE (spin-locate: 0.19
host cores, 38/40 PC samples in the kernel idle path; SpringBoard never
scheduled).

**Where it sits.** The gsevent probe with reply-path breakpoints
(`demux_ret/port_chk/send_ret` in `_LKRenderServerThread`) shows the app's 2002
delivered 0.03 s after the press, `__LKXRenderClient` entered ~0.03 s later --
and the RPC REPLY sent ~37 s after that, correct port and msgh_id 0x9d74,
failing with MACH_SEND_INVALID_DEST because the 10 s suspend watchdog killed
the app (0x8badf00d, crash log recovered from a preserved NAND EVEN ON CLEAN,
"successful" dismissals -- every 1.0 dismissal is the app's corpse being cleaned
up). The ~33.8 s is spent INSIDE `__LKXRenderClient`'s render.

**The mechanism.** IT_MBX_TRACE with guest pc/lr (added this session) plus
disassembly of the 1.0 RELEASE kernelcache and userland:

* The zoom animation renders through `_LKRenderMBX2DRenderBuffer` as ~33
  streamed 2D command blocks in the 0xA00000 window (advancing offsets, one
  0x70000000-terminated block per row band).
* After each block, userland `MBX2D _mbx2DFinish -> MBXConnect
  _mbxFinishSurface` calls AppleMBX user-client method 10 (FinishSurface),
  whose kernel sleep wrapper (0xc032e0xx) acks `0x134=0xfff`, arms
  `0x130=0xffff`, and sleeps ~1 s.
* The wake is the kext's real ISR (0xc032ee48): cause = `0x12C status & 0x130
  enable`, dispatching bits 0x20/0x400/0x10/0x8/0x4; only **bit 4 (0x10,
  render complete)** wakes FinishSurface. Nothing in the model ever raises
  bit 4, so every block sleeps out its full ~1.02 s timeout: 33 x 1.02 =
  the 33.8 s constant.

**Why it cannot be faked from the register file (two wedges).** Latching 0x10
on the 0x130 arm write, and raising it from a 500 us virtual timer after each
kick, both WEDGE the guest at the first dismissal (0/2 presses, no flips at
all, button IRQs no longer acked): the 0x10 ISR path and the woken
FinishSurface consult op-state structures in GUEST RAM that only the real MBX
microkernel writes (their physical pointers are handed to registers
0x614/0x618 at init -- e.g. 0x1e3ce508). With the interrupt faked and the shmem
never updated, the driver's bookkeeping goes inconsistent and it sleeps
forever. Both fakes are reverted; the model is back to DONE-bit (bit 6)
completions only, which the POLLED paths use correctly.

**The honest fix (next lever, precise now):** model the completion in the
shared memory the microkernel owns -- find the op-state layout FinishSurface's
sleep loop checks (`[obj+0x1a4]/[obj+0x1a8]` structures; the ISR's bit-0x10
path clears `+0x4c`, sets `+0xc`/`+0x2c`) relative to the 0x614/0x618 shmem
base, write "complete" there on each kick, THEN raise bit 4. That is T1's
remaining half, now with the exact addresses to start from.

Also killed this session: the dl-10 dismiss-latency runs at 0.3-1 s pmemsave
polling measured the AUTO-LOCK DIM (lit 18.5%, vs-home 35%), not the press --
periodic pmemsave polling around a press SUPPRESSES delivery outright (0/8 at
0.3 s, 0/1 at 1 s polling vs 4/4, 3/3, 2/2 with no in-window polling). Latency
must be measured from the model's own timestamped traces, never by polling
guest RAM. `dismiss-latency.py` grew a `--poll` flag and a warning.
