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

## Where it stands

**The bug is an infinite loop in SpringBoard, entered after the first in-app
press, that starves the whole system.** Everything else observed is downstream
of that.

Now that it is known to be a SPIN rather than a block, the instrument problem
inverts: a spinning guest reveals its loop immediately, and perturbation no
longer matters because the guest does not need to make progress. The next steps,
in order:

1. **Name the loop.** Sample the PC a handful of times during the spin (the
   guest is in one place, so a few samples suffice) or extend
   `menubutton-step-trace.py --after-steps` to tens of thousands of steps and
   look for the repeating cycle. The after-return walk already points at
   CF/libobjc notification dispatch with `-[NSCFString isEqual:]` against
   `-[NSCFArray getObjects:range:]`.
2. **Compare with 1.1.4 at the same point.** The same walk on 1.1.4 will show
   what it does instead, and the divergence should be visible directly.
3. **The `NSThread` started during the unwind** may be the loop's owner rather
   than the main thread.
4. **The app side.** On 1.1.4 the dismissed app calls `_ResetEventPortSet` in the
   APP process; on 1.0 it never does. Neither UIKit binary imports that symbol
   and GraphicsServices has no direct branch to it, so it is reached through a
   function pointer -- break on it on 1.1.4 (a safe library address) and read the
   caller from `lr`. If 1.0's app never gets far enough to reset its port, the
   spin may be SpringBoard waiting on that app in a busy loop.
