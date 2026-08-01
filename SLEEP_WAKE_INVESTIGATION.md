# iPod Touch 1G Sleep/Wake Investigation

> **Historical provenance:** Carried forward on 2026-07-21 from
> `ipod_touch_1g-qemu6-legacy` at `4221943495`. This investigation spans the
> QEMU 6 implementation and the work that led into the QEMU 11 promotion.
> Commands and status statements are dated records; current firmware tests must
> stage both NAND and NOR copies. The historical self-launching
> `scripts/ipod-acceptance-test.py` mentioned below is intentionally not present:
> it cloned NAND but passed the installed application's NOR directly as writable
> `-pflash`. `TESTING_WORKFLOW.md` documents the replacement QMP workflow and
> the firmware-safety rationale.


## Problem

Neither the Power (P) nor Home (H) button wakes the emulated iPod Touch once it goes to sleep and the screen goes black. Mouse clicks also have no effect.

## Immediate Symptom (Confirmed)

The guest OS (iPhone OS 1.x) **disables all CPU interrupts** when it enters display sleep:

```
CPSR = 0xa00000d3
  I = 1  (IRQs disabled)
  F = 1  (FIQs disabled)
  Mode = 0x13 (SVC / Supervisor)
  halted = 0  (CPU is running, NOT in WFI)
```

The CPU is actively executing code in a sleep loop with **both IRQs and FIQs masked**. No interrupt of any kind — regardless of source — can be taken by the CPU in this state.

## Executive Summary — Genuine Device Sleep Strategy

### Underlying bug: components without a complete power lifecycle

The underlying bug was that QEMU emulated the individual components, but not
the device's complete power lifecycle.

What the real device expects:

1. iPod OS powers down LCD, multitouch, USB, timers, and other drivers.
2. It masks IRQ and FIQ.
3. It writes `OOCSHDWN=0x02` to the PCF50633 PMU.
4. The S5L8900 application processor loses power. The terminal `b .` loop is
   never supposed to return.
5. A wake button powers the processor back up and restarts the boot chain with
   selected RAM retained.
6. iBoot recognizes the `"MOSX"`/`"SUSP"` markers and `iBootSleepValid` token,
   then enters its type-4 handoff path.

What QEMU previously did:

- Stored the PMU register value but did not remove SoC power.
- Left the CPU spinning forever with IRQ and FIQ masked.
- Continued scanning out the last framebuffer, causing the leaked status bar.
- Delivered P/H as interrupts to a CPU deliberately unable to accept them.
- Preserved volatile iBoot and peripheral state that real power loss would
  erase.

That combination explains the apparently dead device. Returning from the
terminal loop could restore CPU execution and pixels, but it could not undo the
guest's earlier device shutdown. In particular, `AppleMultitouchZ2SPI` had
already disabled power, so the restored screen did not imply working touch.

### Why it was so hard

Several partial fixes produced convincing false positives:

- Restoring framebuffer memory made the screen visible, but the guest
  touchscreen driver remained powered off.
- Forcing IRQ/FIQ delivery moved the CPU but corrupted kernel critical sections
  and caused crashes.
- Returning from the terminal sleep function skipped the power cycle the
  kernel expected.
- Resetting the CPU initially reached iBoot, but reused iBoot's dirty heap and
  panicked.
- Resetting iBoot without resetting multitouch left the guest driver and
  emulated controller disagreeing about their protocol state.
- The LCD uses triple buffering, so inactive buffers could look awake while
  the active scanout showed only the status bar.
- Input could arrive during the fade, after OOCSHDWN, or during iBoot: three
  states requiring different handling.
- The host-side QEMU suspension fix avoided all of this, which is why it
  appeared reliable, but it intercepted only manual P and missed timed sleep.

### Accuracy of the current implementation

The current implementation is a functional power-cycle emulation, not the
previous host-suspension trick, but it is not yet cycle-accurate.

| Area | Current accuracy |
|------|------------------|
| Guest P/timer shutdown | Proper: iPod OS performs its own driver shutdown and `OOCSHDWN` |
| LCD power-off | Properly modeled; output becomes completely black |
| Masked terminal loop | Correctly recognized as awaiting power loss |
| P/H wake | Approximated with the PMU's generic `ONKEYF` latch |
| RAM retention | Main SDRAM is retained |
| Volatile memory | iBoot RAM, SRAM, and scanout buffers are cleared/reloaded |
| Peripheral reset | Multitouch/SPI is reset; not every SoC peripheral has a complete power-domain reset |
| Boot chain | Starts from reloaded iBoot, not the real bootrom/NOR sequence |
| Resume semantics | Boots kernel/SpringBoard instead of restoring the foreground application |

The honest verdict is: **sleep entry is genuinely guest-driven; wake is a
hardware-informed approximation of the missing S5L8900 power cycle.** Wake
latency and loss of the foreground application are the strongest evidence that
it is not yet complete.

A faithful implementation should begin at the bootrom, model the reset domains
and wake-cause registers exercised by this path, and reproduce iBoot's exact
type-4 retained-memory handoff. It is not yet proven where `boot(type=4,
addr=0)` transfers control after MMU shutdown: the earlier inference that
`BLX 0` necessarily means a clean boot ignored the possibility of an
S5L8900 low-memory/remap alias. The current emulator has no mapping at physical
address zero and skips the bootrom by resetting directly to `IBOOT_BASE`, so it
cannot settle that question (corrected finding #82).

### Implemented strategy

The host-suspend shortcut from approach #44 is rejected and removed. P is once
again delivered to iPod OS through GPIO and PCF50633 ONKEY, and both manual P
and the idle timer reach the guest's real `OOCSHDWN=0x02` path.

The current wake model emulates application-processor power loss:

1. OOCSHDWN powers the emulated LCD panel off, producing a completely black
   output instead of leaking the last status-bar framebuffer.
2. P or H at the terminal `b .` loop requests a SoC reset. A press during the
   preceding status-bar-only transition is detected from the active scanout,
   queued, and completed immediately after OOCSHDWN.
3. Main SDRAM and the `MOSX`/`SUSP` sleep markers are retained.
4. Volatile iBoot RAM (4 MiB) and SRAM (64 KiB) are cleared, and a pristine
   iBoot image is reloaded before CPU reset. This fixes the first reset
   prototype's `heap error: free` panic.
5. The three scanout buffers are cleared on reset so stale retained pixels
   cannot mark the UI ready before iBoot/SpringBoard redraws.
6. Volatile SPI/multitouch protocol state is reset so the freshly booted guest
   driver can download firmware and receive touch frames again.
7. Touches during the Apple-logo/startup interval are ignored until the
   SpringBoard framebuffer has been stable for two seconds. Idle SPI padding
   and unsupported bytes are non-fatal, matching hardware behavior.
8. The present PMU model has one boot-visible wake latch, ONKEYF, so both P and
   H map to that generic latch across the reset.

This is a guest-driven sleep and SoC/iBoot wake. It currently follows the
emulator's existing iBoot boot path rather than preserving a foreground app as
an instantaneous software resume; refining the exact iBoot type-4 semantics is
future accuracy work.

### Timeline of progress

| Phase | Approaches | What was tried | Outcome |
|-------|-----------|---------------|---------|
| 1. Interrupt routing | #1–#14 | Fix button/PMU storms, add PCF50633 ONKEY, fix VIC, SYSIC, INTLEVEL, edge/level modes | **Dead end.** GPIO ISR is a generic acknowledge-only handler — it NEVER dispatches to the PMU driver (finding #31). No amount of interrupt timing fixes this. |
| 2. Direct kernel manipulation | #15–#19 | Warm-reset to kernel reset handler, stack-unwind from sleep, ONKEY with auto-clear storm prevention, LCD force-wake, state variable patch | **Partial.** Can return from sleep function, storm eliminated. But kernel re-sleeps or enters UART poll with interrupts disabled. |
| 3. Sleep function patch + wake path | #20–#21 | Patch `b .` loop in guest memory, direct jump to wake function 0xc0019790 | **Partial.** Sleep function returns, but kernel enters UART serial poll. The "wake function" is just a scheduler hint — doesn't wake the display. |
| 4. Combined approach | #22 | Framebuffer snapshot/restore + UART char injection + sleep patch + IRQ enable | **Best result at time.** First visible wake — framebuffer content restored, CPU active, not in sleep loop. But kernel doesn't truly process the power button event. |
| 5. PMU handler dispatch fix | #27 | Clear stuck byte[1], deferred INT1 clear, wake assist timer | Visual wake + VIC/SYSIC interrupts processed. PMU handler dispatched. But touch still not responsive. |
| 6. FIQ + timing fixes | #28–#30 | FIQ enable (finding #58), extended delay, remove deferred clear, ONKEY re-injection | Timer FIQs now fire (finding #58 fix). Deferred clear required (finding #59). Re-injection approach in progress. |
| 7. PM suspend bypass | #31–#33 | INT1 shadow (chicken-and-egg fix), pre-patch sleep function on OOCSHDWN, VIC priority stack cleanup | Bypasses the B . loop entirely. VIC IRQ delivery confirmed working. |
| 8. Sleep function return value | #34–#37 | CPSIE IF + MOV R0,#0 + POP; auto-recurring timer; CPU register tracing; force-enable IRQ/FIQ in post-sleep callback | Sleep function returns with R0=0 (PM success path). Timer FIQs fire. VIC priority reset. But PM resume stuck in "Security Modules" serial console check. |
| 9. Serial console bypass | #38–#39 | Patch UART poll wrapper to return -1; patch delay function BGE→B unconditional; patch inner wrapper 0xc00536d0 (broke get_ticks) | Delay function exits, but get_ticks itself is stuck in big_function at 0xc0062462. Kernel timebase structure frozen — FIQ handler not updating it. Each fix reveals the next stale state assumption. |
| 10. get_ticks bypass + bootrom/iBoot analysis | #40 | Rewrote get_ticks to read hardware timer directly; fixed QEMU timer TICKSLOW stale value bug; analyzed bootrom + iBoot warm boot path | get_ticks unblocked. The initial conclusion that type 4 was necessarily a clean reboot was later withdrawn because the meaning of address zero after the handoff/remap was not established (corrected finding #82). |
| 11. Security Modules + KDP bypass | #41 | Patch secmod entry+caller, KDP BL, outer loop, debugger wait loop | No panics but **CPU stuck in debugger protocol loop** (finding #90). BEQ→B patch at 0x10066 loops back via disconnect handler at 0x10080. Previous "success" was false positive. |
| 12. Deferred input wake | #43 | Install the sleep-return trampoline only when wake input arrives; clean VIC/timer state; restore framebuffer and original sleep instructions; reconnect the 40-pulse wake assist | Restores a visible screen, but real interaction still fails because the guest disabled `AppleMultitouchZ2SPI` before OOCSHDWN. Also exposed a panic when normal H started wake assist outside sleep. |
| 13. Host display suspend | #44 | Intercept P before guest GPIO/PMU handling; blank the host surface; suspend guest time while retaining host input; resume on P/H/click | **Rejected workaround.** It made manual P look reliable but bypassed device sleep entirely and did nothing for timed OOCSHDWN. |
| 14. Naive retained-RAM reset | #45a | Issue QEMU system reset from the terminal sleep loop while preserving main RAM | Reached `iBoot start`, proving the power-cycle direction, but reused dirty iBoot heap RAM and panicked with `heap error: free`. |
| 15. Pristine iBoot power cycle | #45b | Clear/reload the 4 MiB iBoot region and 64 KiB SRAM on reset; preserve main SDRAM; model PMU panel power | iBoot and the kernel boot successfully from P/H; timed sleep is fully black with no status-bar leak. |
| 16. Volatile input/reset race fixes | #45c | Reset SPI/multitouch state; ignore boot-time touches/idle padding; inspect active scanout; queue P/H during final shutdown; clear scanout buffers; carry generic wake latch | P sleep→P wake→Safari and status-bar transition→H queued wake→Safari both pass. Removed the fatal unknown-command crash. |

### Current state (validated July 2026)

Direct GUI/QMP validation with the current working tree confirms:

- Normal touch opens Safari before sleep.
- Manual P is delivered to the guest PMU; iPod OS reads ONKEY and writes
  `OOCSHDWN=0x02` itself.
- Untouched timed sleep reaches the same OOCSHDWN loop.
- PMU panel-off output is completely black; the stale status bar is gone.
- P wake reloads pristine iBoot, boots SpringBoard, reinitializes multitouch,
  and a post-wake tap visibly opens Safari.
- H pressed during the status-bar-only transition is queued through OOCSHDWN,
  reaches the same retained-RAM SoC reset path, and a post-boot tap visibly
  opens Safari.
- No `RUN_STATE_SUSPENDED`, `vm_stop()`, or host-side P toggle remains.
- Normal P/H no longer run the unconditional IRQ/FIQ assist that caused the
  earlier kernel panic or stalled a legitimate sleep transition.
- Idle `0x00` SPI clocks and unsupported multitouch commands no longer abort
  the emulator.

The older OOCSHDWN trampoline remains documented as an experimental dead end:
it can restore a visible framebuffer, but the guest has already powered the
touch driver off.

### Confirmed DEAD ENDS — never retry these

| Dead end | Why | Approaches wasted |
|----------|-----|------------------|
| Interrupt-based PMU event delivery | GPIO ISR is generic acknowledge-only | #1–#14 |
| PMU re-assertion timing (sync/deferred/edge/level) | All cause storms or have no effect | #8, #12, #13 |
| INTLEVEL tracking | OS never reads INTLEVEL at runtime | #14 |
| LCD render register | Already 0x1 during sleep, not the issue | #18 |
| Single state variable patch (0xc01a6170) | Multiple sleep paths exist | #19 |
| Kernel reset handler (0xc005fff4) | Dead `b .` trap | #15 |
| Stack-unwind alone | Kernel re-sleeps without wake signal | #16 |
| Direct wake function call (0xc0019790) | Just a scheduler hint, not display wake | #21 |
| CPSR I-bit clearing alone | Kernel re-disables immediately | multiple |
| Patching only `b .` loop | Kernel enters UART poll with IRQs disabled | #20 |
| Unmasking FIQ (F bit) | Causes immediate FIQ, corrupts state | tested |
| Patching inner wrapper 0xc00536d0 | Shared with get_ticks() — breaks timer reads | #38 iter 2 |
| Patching individual blocking functions one by one | Each fix reveals the next stale state; cascade never ends | #34–#39 |
| Returning from OOCSHDWN instead of power cycling | The guest has already powered devices off; CPU/VIC/timer/framebuffer repair cannot restore driver state | #20–#43 |
| Host-side QEMU suspension | Bypasses iPod sleep and cannot handle the independent timer-triggered OOCSHDWN path | #44 |
| Resetting into already-used iBoot RAM | Stale volatile heap metadata causes `heap error: free` | #45a |

### Recommendation — next steps

Keep the guest OOCSHDWN plus retained-RAM iBoot reset as the N45AP default and
add a repeatable smoke test for manual-P and timed sleep, P/H wake, complete
boot, and visible Safari interaction. The next accuracy milestone is to execute
and trace the real bootrom/NOR/LLB/iBoot wake path, including the address-zero
mapping and any memory-remap writes, to determine and reproduce the type-4
retained-memory handoff. Foreground application retention, low wake latency,
no normal SpringBoard boot, and immediate touch response are the acceptance
criteria. The existing guest addresses remain firmware-specific and must move
into guarded board profiles before M68AP work.

## Detailed Findings

### 1. Button events are received correctly

The SDL key handler fires for every P/H press and release:
```
[BTN] keycode=25 group=1 sel=13   (P press)
[BTN] keycode=153 group=1 sel=13  (P release)
[BTN] keycode=35 group=1 sel=14   (H press)
[BTN] keycode=163 group=1 sel=14  (H release)
```

### 2. GPIO interrupt routing works

- Power button: GPIO_BUTTON_POWER_IRQ = 0x2D → group 1, selector 13
- Home button: GPIO_BUTTON_HOME_IRQ = 0x2E → group 1, selector 14
- `gpio_int_enabled[1] = 0x6000` (bits 13 and 14 enabled)
- SYSIC sets `gpio_int_status[1]` and raises `gpio_irqs[1]`

### 3. VIC interrupt delivery works

GPIO group 1 → VIC IRQ 0x20 (32) → VIC1 input 0 → daisy chain → VIC0 → CPU IRQ

- VIC1 `intenable = 0x405e3` (bit 0 enabled for GPIO group 1)
- VIC1 properly detects the interrupt (`rawintr |= 1`)
- VIC1 raises through daisy chain to VIC0
- VIC0 asserts CPU IRQ line (`RAISING! highest=32 current=33`)

### 4. CPU never enters ISR

VIC0's `current` stays at 33 (PL192_NO_IRQ) — the guest never reads VECTADDR to acknowledge. This is because CPSR I=1 prevents the CPU from taking the IRQ exception.

### 5. Comparison: working vs non-working interrupt groups

| Group | VIC     | Path          | Runtime acks? |
|-------|---------|---------------|---------------|
| 1     | VIC1:0  | Daisy → VIC0  | **Never**     |
| 2     | VIC0:31 | Direct to CPU | Yes           |
| 4     | VIC0:2  | Direct to CPU | Yes           |

Groups 2 and 4 get acked during normal operation. Group 1 never does during sleep. This is not a VIC routing problem — it's because the CPU masks all interrupts during sleep.

### 6. Original interrupt storm bug (fixed)

Before the auto-lower timer fix, pressing a button during normal operation caused:
1. `qemu_irq_raise()` → VIC1 input HIGH
2. VIC delivers interrupt → ISR runs → ISR does EOI
3. `pl192_irq_fin()` → `pl192_update()` → sees input still HIGH → re-raises immediately
4. Infinite interrupt storm → OS disables the interrupt → buttons permanently broken

**Fix applied**: Added auto-lower timer to SYSIC GPIO IRQs, converting them from level-triggered (stuck HIGH) to edge-triggered (brief pulse). This prevents the storm.

### 7. Other bugs fixed

- **Duplicate sysbus_connect_irq calls** in machine init (lines 413-419) overwrote `gpio_irqs[0]` — removed
- **Unused g_malloc0** for `nms->sysic` — fixed to use the actual qdev object

### 8. PMU is NOT polled during sleep (disproves original hypothesis)

I2C read logging on `pcf50633_recv()` shows:
- During boot: guest reads registers 0x57 (ADCC1), 0x4B (MBCS1), 0x69 (boot count), 0x67 (debug UART), 0x59-0x5F (RTC), 0x13-0x17
- **After entering sleep: ZERO PMU reads.** The guest never reads INT1 (0x02) at any point.
- The last PMU activity before sleep is reading regs 0x13-0x17, writing zeros to them, then writing 0x80 to reg 0x76.

**The original hypothesis ("sleep loop polls PMU INT1 register for ONKEY via I2C") is WRONG.** The sleep loop does not poll the PMU at all.

### 9. CPSR I bit CAN be cleared, but CPU still doesn't take interrupts

Tested: clearing CPSR I via `cpsr_write()` + `cpu_interrupt(CPU_INTERRUPT_EXITTB)`:
```
[BTN] keycode=25  PC=0xc005a6d0  CPSR=0x600000d3  I=1 F=1 halted=0
[BTN] Clearing CPSR I bit to allow IRQ
[BTN] keycode=153  PC=0xc005a6d0  CPSR=0x60000053  I=0 F=1 halted=0   ← I cleared, stays cleared
[BTN] keycode=25   PC=0xc005a6d0  CPSR=0x60000053  I=0 F=1 halted=0   ← still stuck at same PC
```

- The I bit IS successfully cleared (goes from 1→0 and stays at 0 on subsequent presses)
- But the **CPU never leaves PC=0xc005a6d0** — it never vectors to the IRQ exception handler
- The sleep loop does not re-enable interrupts itself (I stays 0 after we clear it)

### 10. Sleep loop instruction dump (CONFIRMED: `b .` at 0xc005a6d0)

Memory dump around the sleep PC:
```
0xc005a6c0: 0xe5840054   str r0, [r4, #0x54]
0xc005a6c4: 0xe5841058   str r1, [r4, #0x58]
0xc005a6c8: 0xeb001aa6   bl  0xc0061168      ← calls sleep setup function
0xc005a6cc: 0xee073f9a   mcr p15,0,r3,c7,c10,4  ← DSB (Data Synchronization Barrier)
0xc005a6d0: 0xeafffffe   b   .                ← INFINITE LOOP (branch to self)
0xc005a6d4: 0xe92d4090   push {r4, r7, lr}    ← start of next function (unreachable)
```

Register state during sleep:
```
r0=0x00000000 r1=0x00000000 r2=0x00000000 r3=0x00000000
sp=0xc0003fb8 lr=0xc005a6cc
```

The sleep sequence is: call function at 0xc0061168 (likely platform_halt / power_down), execute a DSB, then enter `b .` forever. The CPU expects to be woken by a hardware interrupt vectoring to the exception table, NOT by polling.

### 11. CPU_INTERRUPT_HARD is set but interrupt still not taken

```
interrupt_request=0x12 before wake    (0x12 = CPU_INTERRUPT_HARD | CPU_INTERRUPT_EXITTB)
Clearing CPSR I bit
interrupt_request=0x12 after force
```

Both `CPU_INTERRUPT_HARD` (0x02) and `CPU_INTERRUPT_EXITTB` (0x10) are set. CPSR I was cleared to 0. In the initial test (where the sleep loop was a `b .` at 0xc005a6d0), the CPU appeared stuck. However, in subsequent tests the sleep PC was different and the CPU DID move.

### 12. BREAKTHROUGH: CPSR force-clear + CPU_INTERRUPT_HARD WORKS — CPU wakes up!

In the latest test, after clearing CPSR I, the CPU's PC bounced between multiple kernel locations:
```
P press:   PC=0xc048c934  I=1 → cleared I
P release: PC=0xc01604a4  I=1 → cleared I    ← CPU moved!
H press:   PC=0xc0061650  I=1 → cleared I    ← CPU moved again!
H release: PC=0xc01604d4  I=0                ← I finally stayed clear!
...later:  CPU bouncing between 0xc0061650, 0xc01604d4, 0xc016284c etc. with I=0
```

The CPU is actively executing kernel code (timer/scheduler idle loop). It IS awake. **The interrupt delivery works.**

### 13. But the screen stays black — missing PMU interrupt connection

The CPU wakes up and processes interrupts, but the OS doesn't know the power button was pressed because:

1. The GPIO interrupt fires and gets acknowledged, but the OS doesn't treat it as a "wake display" event
2. On real hardware, the PCF50633 PMU has an **INT pin** that asserts when ONKEY is pressed
3. The INT pin connects to a GPIO/VIC interrupt, triggering the OS to read INT1 via I2C
4. The OS sees the ONKEYF bit and triggers the power management state machine to wake the display

**Current emulator is missing**: The PMU has no interrupt output connected to the VIC. The ONKEY bits are set in INT1 but the OS never reads them because there's no PMU interrupt to trigger the read.

**Fix needed**: Add a `qemu_irq` output to the PMU, wire it to the appropriate VIC interrupt, and assert it when ONKEY events occur.

### 14. PMU INT GPIO identified: GPIO 0x55 = group 2, bit 21

From the iDroid kernel source (`arch/arm/mach-apple_iphone/iphone.c`):
```c
{
    I2C_BOARD_INFO("pcf50633", 0xe6),
    .irq = IPHONE_GPIO_IRQS + 0x55,
}
```

GPIO interrupt 0x55 = 85 decimal:
- Group: 85 >> 5 = **2**
- Bit: 85 & 0x1F = **21**
- VIC IRQ for group 2: **0x1F** (31) — direct to VIC0

Confirmed by GPIO int_enabled dump during runtime:
```
gpio_int_enabled[2] = 0x00203000  → bit 21 IS enabled
```

The interrupt flow: PMU nIRQ → GPIO 0x55 (group 2, bit 21) → VIC0 IRQ 31 → CPU IRQ.

### 15. Apple remapped INT status registers to 0x13-0x17

The guest reads PMU registers 0x13-0x17 during boot (not standard 0x02-0x06):
- 0x13 = INT1 (standard: 0x02)
- 0x14 = INT2 (standard: 0x03)
- 0x15-0x17 = INT3-INT5

The **mask registers** stay at standard addresses: 0x07-0x0B (confirmed by guest writes during boot).

### 16. PMU I2C write handler was broken

The original `pcf50633_send()` blindly set `s->cmd = data` for every byte, meaning the register address (first byte) was overwritten by the data (second byte). This meant all guest writes to the PMU were silently lost.

**Fixed**: Added `has_reg_addr` state tracking. First byte sets register address, subsequent bytes write to that register (with auto-increment).

### 16b. Wrong PMU INT mask register addresses (FIXED)

Initially assumed Apple remapped the INT mask registers too (to 0x0D-0x11, following the pattern of status registers at 0x13-0x17). This meant all guest mask configuration writes were silently ignored — the emulator stored them at wrong offsets.

Debug logging of I2C writes revealed the guest writes INT masks to **standard PCF50633 addresses** 0x07-0x0B (e.g., `write reg 0x07 = 0xF0` for INT1M). The remapping only applies to status registers, not masks.

**Fixed**: Changed PMU_INT1M-5M defines from 0x0D-0x11 to 0x07-0x0B.

### 17. INT1M masks ONKEY during normal operation

Guest writes to INT1M (0x07) during boot: 0xFF → 0xF7 → 0xF3 → 0xF1 → **0xF0**

INT1M = 0xF0 means bits 4-7 are masked, including ONKEYR (bit 6) and ONKEYF (bit 7). On real PCF50633 hardware, the nIRQ pin won't assert for masked interrupts.

However, ONKEY is a hardware wake source — on real hardware, the ONKEY event always asserts nIRQ regardless of the software mask (the mask only affects the INT status register clearing behavior, not the pin). Emulator fix: ONKEY bits bypass the mask check in `pcf50633_update_irq()`.

### 18. PMU auto-lower timer on GPIO group 2 caused freeze (FAILED)

**Attempt**: Used the SYSIC auto-lower timer (100ms pulse) for the PMU interrupt on GPIO group 2, same as button interrupts use on group 1. Combined with a SYSIC fix that only lowered the IRQ if all status bits were cleared.

**Result**: QEMU froze during normal operation. The device couldn't go to home (H broken), touch stopped responding, and QEMU eventually froze completely.

**Root cause**: The auto-lower timer approach creates a race condition:
1. PMU sets `gpio_int_status[2] |= bit 21` and raises `gpio_irqs[2]`
2. Auto-lower fires 100ms later, lowers `gpio_irqs[2]` (VIC input goes LOW)
3. But `gpio_int_status[2]` bit 21 is still set (OS hasn't cleared it yet)
4. When the SYSIC fix keeps IRQ HIGH (because status != 0), VIC re-fires → interrupt storm
5. Without the SYSIC fix, bit 21 stays in status permanently → spurious PMU interrupts on any future group 2 interrupt

**The auto-lower timer approach is fundamentally wrong for the PMU** because the PMU's nIRQ is level-triggered, not edge-triggered. On real hardware, nIRQ stays LOW until the OS reads the INT registers via I2C (which clears them), then nIRQ goes HIGH.

**Fix applied**: Reverted the SYSIC conditional-lower change. Rewrote `pcf50633_update_irq()` to be level-triggered: it raises GPIO group 2 IRQ when INTs are pending, and lowers it + clears the GPIO status bit when the OS reads/clears all INT registers. The PMU now manages its own GPIO state directly, not relying on the auto-lower timer.

### 19. SYSIC conditional lower caused interrupt storm (REVERTED)

**Attempt**: Changed SYSIC GPIO_INTSTAT write handler to only lower the IRQ when all status bits for a group are cleared (instead of unconditionally lowering).

**Rationale**: Prevent losing interrupts when multiple sources share a GPIO group (e.g., PMU bit 21 and other peripherals on group 2).

**Result**: Combined with the PMU auto-lower timer, this caused a freeze. The auto-lower timer lowers the VIC input, but if bits are still pending in status, the next GPIO group 2 interrupt re-raises the line and the OS keeps seeing stale PMU status bits it can't clear → infinite re-fire.

**Action**: Reverted back to unconditional lower on GPIO_INTSTAT write. This is safe because the PMU now manages its own GPIO state (assert/de-assert) separately from the auto-lower timer infrastructure.

### 20. CPSR force-clear + CPU_INTERRUPT_HARD on every keypress crashed QEMU

**Symptom**: Pressing H during normal operation froze QEMU, scrambled the screen, and eventually crashed it.

**Root cause**: The key handler called `cpu_interrupt(s->cpu, CPU_INTERRUPT_HARD)` on **every** button press/release, even during normal operation when I=0 (interrupts already enabled). This injected a spurious hardware interrupt that bypassed the VIC, corrupting CPU state mid-execution (e.g., re-entering an ISR while one was already running, or taking an exception at an unexpected PC).

**Attempted fix (attempt #9)**: Guard the CPSR hack behind a check for I=1. This partially helped — it stopped the crash when I=0 — but introduced a new regression (see finding #21).

### 21. I=1 check too broad — H button dropped during normal IRQ handlers (FAILED)

**Symptom**: After guarding the CPSR hack with I=1 check, pressing H during normal use did nothing (no Home action). The calculator stayed on screen. After some time, the device froze.

**Root cause**: ARM1176 **automatically sets I=1** when entering any IRQ handler. The I=1 check was meant to detect sleep, but it also matched the CPU being inside a normal ISR. Since the OS spends significant time in IRQ handlers (timer ticks, LCD refresh, etc.), many H presses landed during I=1 and were silently dropped.

The `return` statement also skipped the GPIO IRQ raise entirely, so the button event was completely lost — the OS never saw the Home button press.

### 22. Silently dropping non-power buttons during "sleep" broke H entirely (FAILED)

**Symptom**: Same as #21. Added code to detect sleep (I=1) and `return` early for non-power buttons. But I=1 during normal ISR execution triggered the same false positive.

**Fix (attempt #10)**: Three changes:
1. **Always process GPIO interrupts** for all buttons — never drop events
2. **Restrict CPSR hack to Power only** — only P triggers force-wake
3. **Detect actual sleep using I=1 AND F=1** — the sleep loop has both IRQ and FIQ disabled (CPSR=0x...d3), while normal IRQ handlers only have I=1 (F stays 0). Checking both bits eliminates false positives from normal ISR execution.

```c
// Always raise GPIO interrupt for all buttons
s->sysic->gpio_int_status[gpio_group] |= (1 << gpio_selector);
qemu_irq_raise(s->sysic->gpio_irqs[gpio_group]);

// Force wake: Power button only, true sleep only (I=1 AND F=1)
if (is_power && s->cpu) {
    uint32_t cpsr = cpsr_read(env);
    bool deep_sleep = (cpsr & (1 << 7)) && (cpsr & (1 << 6));
    if (deep_sleep) {
        cpsr &= ~(1 << 7);
        cpsr_write(env, cpsr, CPSR_I, CPSRWriteRaw);
        cpu_interrupt(s->cpu, CPU_INTERRUPT_HARD);
    }
}
```

### 23. Device appears awake but CPU is sleeping — LCD keeps refreshing

The LCD controller continues refreshing the framebuffer independently of the CPU state. After the OS enters the sleep loop (I=1 F=1), the screen still shows the last rendered content (calculator, home screen, etc.). This makes the device **appear** awake when it has already auto-slept. User may press H thinking the device is active, but the CPU is in the `b .` sleep loop. The auto-sleep timer is very short.

This confirms the I=1 AND F=1 check is correct for detecting true sleep — during normal active use, H presses land with I=0 (or I=1 F=0 inside an ISR) and work normally.

### 24. P press during sleep: CPU wakes but PMU interrupt never serviced

**Test results** (first real P-during-sleep test with full PMU wiring):
```
[BTN] keycode=25  PC=0xc048c934  I=1 F=1  power=1
[BTN] Power button — forcing CPU wake (clearing I, I=1 F=1)
[PMU] ONKEY pressed  int1=0x80
[PMU] nIRQ assert: int1=0x80 mask=0xf0
[BTN] keycode=153  PC=0xc01604d4  I=1 F=1  power=1   ← CPU moved! was at 0xc048c934
[BTN] Power button — forcing CPU wake (clearing I, I=1 F=1)
[PMU] ONKEY released  int1=0xc0
...
(multiple P presses: CPU wakes each time but always returns to I=1 F=1)
(ZERO "[PMU] INT1 read" entries — OS never reads INT1 via I2C)
```

**Confirmed**: PMU nIRQ assertion works (GPIO group 2 bit 21 HIGH), ONKEY bits set correctly, CPU wakes from CPSR hack. But the OS handles the button GPIO ISR (group 1) and immediately re-enters sleep without ever servicing the PMU interrupt (VIC0 input 31).

**Suspected cause**: OS disables VIC0 input 31 before entering sleep. Need to verify by logging VIC0 INTENABLE register.

### 25. VIC0 INTENABLE bit 31 is enabled during sleep — NOT the issue

**Test**: Added logging to `pl192.c` to track when VIC0 INTENABLE bit 31 (GPIO group 2 / PMU) is set or cleared.

**Result**: VIC0 bit 31 is enabled during boot and **never cleared**. The OS does not disable the PMU's VIC input during sleep. This eliminates the #1 suspected cause from finding #24.

### 26. ROOT CAUSE FOUND: SYSIC unconditionally lowers PMU GPIO after INTSTAT clear

**Critical VIC trace analysis** with deep logging (VECTADDR reads, input 31 level changes):

```
P press → force wake → PMU ONKEY → VIC0 input 31 HIGH
VIC0 irq_status=0x80000004 (bit 31 PMU + bit 2 GPIO group 4)
VIC selects irq 2 first (higher priority — lower number wins)
VECTADDR read → acks irq 2 (GPIO group 4)
VECTADDR read → acks irq 31 ← OS DOES service PMU interrupt!
Input 31 immediately goes LOW ← THIS IS THE BUG
```

**The full chain of failure**:
1. OS GPIO handler writes to GPIO_INTSTAT[2] to acknowledge bit 21 (PMU)
2. SYSIC write handler clears the status bit: `gpio_int_status[2] &= ~val`
3. SYSIC **unconditionally** calls `qemu_irq_lower(gpio_irqs[2])` → VIC0 input 31 goes LOW
4. PMU still has pending INT1 bits (ONKEYF=0x80) but nobody re-asserts gpio_irqs[2]
5. VIC0 sees input 31 LOW → removes it from irq_status
6. OS PMU interrupt handler never dispatches because the interrupt was cleared before it could read INT1 via I2C

**On real hardware**: The PMU nIRQ pin is level-triggered — it stays LOW as long as any INT register has unread pending bits. When the SYSIC clears gpio_int_status, the PMU would immediately re-assert its nIRQ pin because INT1 still has ONKEYF set.

**Fix**: After SYSIC clears GPIO_INTSTAT for the PMU's GPIO group, call `pcf50633_update_irq()` to let the PMU re-assert if it still has pending interrupts. This implements the level-triggered nIRQ behavior.

Changes:
- Added `Pcf50633State *pmu` pointer to `IPodTouchSYSICState`
- Made `pcf50633_update_irq()` non-static, declared in PMU header
- SYSIC GPIO_INTSTAT write: after lowering group 2, calls `pcf50633_update_irq(s->pmu)` to allow PMU re-assertion
- Machine init: wires `sysic_state->pmu = PCF50633(pmu)`

### 27. Synchronous SYSIC→PMU callback caused interrupt storm (approach #12 FAILED)

**Different from finding #18** (auto-lower timer storm). The mechanism here:

1. OS GPIO ISR writes GPIO_INTSTAT[2] to acknowledge bit 21
2. SYSIC write handler clears bit 21, lowers gpio_irqs[2] → VIC0 input 31 LOW
3. SYSIC handler **immediately** calls `pcf50633_update_irq(s->pmu)`
4. PMU re-asserts: sets GPIO_INTSTAT[2] bit 21, raises gpio_irqs[2] → VIC0 input 31 HIGH
5. All of steps 2-4 happen **synchronously within a single MMIO write handler**

The VIC log shows VIC0 input 31 oscillating HIGH/LOW in a tight loop with **zero VECTADDR reads** in between — the CPU never exits the ISR. The ISR's "clear INTSTAT" loop (`while (read(INTSTAT)) { write(INTSTAT, val); }`) never exits because bit 21 is immediately re-set after each write. The ISR never dispatches to the PMU handler, so INT1 is never read via I2C.

**Comparison with finding #18:**
- Finding #18: Auto-lower timer + SYSIC conditional-lower created a race where timer fires, PMU re-asserts, and the VIC keeps re-delivering → interrupt storm between ISR returns
- Finding #27: Synchronous callback traps the ISR **inside its own INTSTAT clear loop** — the ISR never even returns, let alone dispatch to the PMU handler

**Fix (approach #13)**: Deferred timer-based re-assertion. Instead of calling `pcf50633_update_irq()` synchronously, schedule a 1ms timer. This gives the ISR time to finish clearing INTSTAT (loop exits because bit 21 stays 0 during the delay), dispatch to the PMU handler, read INT1 via I2C (clearing pending bits), and return. When the timer fires, `pcf50633_update_irq()` checks INT1 — if already cleared, no re-assertion happens.

### 28. Deferred timer (approach #13): ISR pattern revealed — NO dispatch to PMU handler

**SYSIC MMIO trace with targeted read/write logging** reveals the EXACT ISR behavior for VIC0 irq 31 (GPIO group 2):

```
[PMU] nIRQ assert: int1=0x80 mask=0xb0
[VIC] VECTADDR read (ack) → irq=31                  ← ISR starts
[SYSIC] read INTSTAT[2] = 0x00200000                 ← ISR reads: bit 21 set
[SYSIC] write INTSTAT[2] = 0x00200000 (was 0x200000) ← ISR writes: clears bit 21
[VIC] input 31 LOW                                    ← SYSIC lowered IRQ
[SYSIC] read INTSTAT[2] = 0x00000000                  ← ISR re-reads: all clear
[VIC] VECTADDR read (ack) → irq=32 (EOI)             ← ISR returns
... (1ms later, timer fires)
[PMU] nIRQ assert: int1=0x80 mask=0xb0               ← Timer re-asserts → REPEAT
```

**Critical discovery**: The ISR does **exactly three SYSIC operations** then returns:
1. Read INTSTAT[2] → sees bit 21
2. Write INTSTAT[2] = bit 21 → clears it
3. Read INTSTAT[2] → verifies 0

The ISR **NEVER**:
- Reads INTLEVEL (0 reads during ISR)
- Reads INTTYPE (0 reads during ISR)
- Reads INTEN (0 reads during ISR)
- Initiates ANY I2C transaction (0 PMU reads after P press)
- Dispatches to any PMU handler

**The ISR is a simple acknowledge-and-return function.** It clears INTSTAT and exits. There is NO per-bit handler dispatch within the ISR.

The deferred timer re-assertion just creates a slower version of the same storm: ISR clears → timer re-asserts → VIC re-delivers → ISR clears → repeat forever. Zero PMU reads after 500K+ iterations.

### 29. INTTYPE[2] = 0x00203000 — OS configured PMU GPIO as edge-triggered

Boot-time SYSIC trace reveals:
```
[SYSIC] read INTTYPE[2] = 0x00201000   ← bit 21 set = edge-triggered for PMU GPIO
[SYSIC] read INTEN[2] = 0x00203000     ← bits 12, 13, 21 enabled
```

GPIO 0x55 (group 2, bit 21) is configured as **edge-triggered** by the OS. This contradicts our assumption in finding #18 that the PMU nIRQ is level-triggered. On real hardware, the edge detector would:
1. Detect the falling edge on PMU nIRQ
2. Latch INTSTAT bit 21
3. ISR handles and clears the latch
4. No re-assertion until the pin goes HIGH then LOW again (new edge)

This means **re-assertion is fundamentally wrong** for this interrupt. The OS expects a single edge event, not a sustained level.

### 30. INTLEVEL never read at runtime — hypothesis disproved

INTLEVEL[2] reads occur ONLY:
- During boot initialization (lines 27019-27288)
- Once before sleep entry (line 44585, returned 0x00000000)
- **NEVER during the ISR or after a P press**

Adding `gpio_int_level` tracking to `pcf50633_update_irq()` had no effect because no code reads INTLEVEL at runtime. The hypothesis that the ISR or a deferred handler uses INTLEVEL to determine the interrupt source is **disproved**.

### 31. FUNDAMENTAL INSIGHT: The GPIO ISR does not dispatch — it only acknowledges

Combining findings #28, #29, and #30, the complete picture is clear:

**The GPIO group 2 ISR is a generic acknowledge handler that clears INTSTAT and returns. It does NOT dispatch to per-GPIO handlers.** No variation of re-assertion timing (synchronous, deferred, or otherwise) can cause INT1 to be read, because the ISR never initiates I2C transactions.

**Why this means approaches #12 and #13 were both doomed:**
- #12 (synchronous): ISR loops forever because INTSTAT immediately re-set
- #13 (deferred): ISR exits, but timer re-asserts → VIC re-delivers → ISR clears again → infinite loop. Even with infinite time between re-assertions, the ISR would never read INT1.

**The original analysis in finding #26 was partially correct** (SYSIC lowering kills the interrupt), but **the root cause is deeper**: even if the interrupt persists perfectly, the ISR code path for GPIO group 2 does not include PMU I2C reads.

**The real wake mechanism on S5L8900 is likely NOT interrupt-driven at all.** The `b .` sleep loop with I=1 F=1 (all interrupts disabled) suggests the SoC uses a **warm reset / resume vector** mechanism: certain wake sources (like PMU ONKEY) trigger a hardware reset that jumps the CPU to a resume address, bypassing the interrupt system entirely.

## Approaches tried

| # | Approach | Result | Notes |
|---|----------|--------|-------|
| 1 | Auto-lower timer (edge-triggered pulse) for buttons | ✅ Fixed button interrupt storm | Prevents VIC re-fire after ISR EOI |
| 2 | PMU ONKEY INT1 register (no interrupt) | ⚠️ Bits set but never read | Guest never reads INT1 because no PMU interrupt fires |
| 3 | CPSR force-clear (gated on halted) | ❌ CPU never kicked | cpu_interrupt only called when halted=true, but halted=0 |
| 4 | CPSR force-clear (unconditional kick) | ⚠️ Partial | I bit cleared but CPU appeared stuck at `b .` |
| 5 | CPSR force-clear + CPU_INTERRUPT_HARD + 100ms pulse | ✅ CPU wakes! | CPU leaves sleep loop, runs kernel code. Screen stays black. |
| 6 | PMU GPIO 0x55 + auto-lower timer | ❌ FREEZE | Auto-lower conflicts with level-triggered PMU nIRQ. Caused interrupt storm. |
| 7 | SYSIC conditional lower (only if all bits clear) | ❌ FREEZE | Combined with auto-lower timer, caused infinite re-fire. Reverted. |
| 8 | PMU GPIO 0x55 + level-triggered nIRQ | ⚠️ PMU asserts, OS ignores | PMU nIRQ fires correctly, but OS never reads INT1. CPU wakes briefly then re-sleeps. |
| 9 | Guard CPSR hack: only fire when I=1 (sleeping) | ❌ H broken | I=1 also true during normal IRQ handlers → H presses silently dropped → Home button broken, then freeze |
| 10 | Drop non-power buttons when I=1 | ❌ H broken | Same false positive: I=1 during ISR → H events lost entirely |
| 11 | Always raise GPIO + CPSR hack for P only + I=1&&F=1 sleep detect | ⚠️ No regression, wake still fails | H works during normal use. P triggers wake + PMU ONKEY. But screen stays black. |
| 12 | SYSIC→PMU synchronous callback: re-assert nIRQ after GPIO_INTSTAT clear | ❌ FREEZE (storm) | Finding #26 fix. Synchronous re-assertion traps ISR in infinite INTSTAT clear loop (finding #27). |
| 13 | SYSIC→PMU deferred timer: re-assert nIRQ after 1ms delay | ❌ FREEZE (slower storm) | ISR exits loop but never dispatches to PMU handler. Timer re-asserts → VIC re-delivers → infinite cycle. Zero INT1 reads. Finding #28. |
| 14 | INTLEVEL tracking in pcf50633_update_irq | ❌ No effect | Set gpio_int_level[2] bit 21 on assert. OS never reads INTLEVEL at runtime (finding #30). |
| 15 | Warm-reset to kernel reset handler | ❌ Dead trap | 0xc005fff4 is `b .` — kernel never expects reset while running. Finding #35. |
| 16 | Stack-unwind from sleep function | ⚠️ Partial | Pop {R4,R7,LR}, jump to caller. Works but kernel re-sleeps. Finding #37. |
| 17 | ONKEY with auto-clear storm prevention | ⚠️ Partial | Auto-clear INT1-5 in SYSIC eliminates storm! But ISR still doesn't dispatch. Finding #38. |
| 18 | LCD force-wake (render register + test pixels) | ❌ Not the issue | render was already 0x1. FB content is black (kernel cleared it). Finding #40. |
| 19 | State variable patch (0xc01a6170=2) + FB write | ❌ Different path | State guards only one sleep path. Kernel re-sleeps via different code path. Finding #42. |
| 20 | Patch `b .` in guest memory (CPSIE I + POP) | ⚠️ Partial | Sleep function returns! But kernel enters UART serial poll with I=1 F=1. Findings #43-46. |

## Current Status — What is proven and what is NOT

### ✅ What works (keep these):
1. **CPU wake mechanism** — CPSR force-clear + CPU_INTERRUPT_HARD breaks the `b .` sleep loop (attempt #5)
2. **Sleep detection** — I=1 AND F=1 correctly distinguishes sleep from normal ISR (attempt #11)
3. **H button** — works during normal operation, safely ignored during sleep (attempt #11)
4. **PMU ONKEY assertion** — INT1 bits set correctly (0x80 ONKEYF, 0xc0 ONKEYF+ONKEYR)
5. **PMU nIRQ assertion** — GPIO group 2 bit 21 asserted, logged as `[PMU] nIRQ assert`
6. **PMU register emulation** — Apple-remapped INT status (0x13-0x17), standard masks (0x07-0x0B), I2C handler fixed
7. **VIC0 INTENABLE bit 31** — enabled and never cleared (finding #25)

### ❌ What doesn't work:
8. **OS never reads PMU INT1** — zero `[PMU] INT1 read` after boot. The GPIO group 2 ISR only acknowledges INTSTAT and returns — it NEVER dispatches to a PMU handler or initiates I2C (finding #28).
9. **CPU goes back to sleep** — after handling the GPIO ISR, the CPU re-enters sleep (I=1 F=1) because it never got a "wake display" signal.

### 🔍 True root cause (finding #31):

**The GPIO interrupt path CANNOT deliver PMU events.** The GPIO group 2 ISR is a generic acknowledge-and-return handler (finding #28). No variation of re-assertion timing fixes this (approaches #6, #7, #12, #13, #14 all failed). The ISR code path does not include PMU I2C reads.

**The real wake mechanism on S5L8900 is likely a warm reset / resume vector:**
- The sleep loop has I=1 F=1 (ALL interrupts disabled) — the OS does NOT expect to be woken by interrupts
- The `b .` loop is a dead-end — code after it is unreachable
- On real hardware, wake sources probably trigger a SoC-level reset that jumps to a resume address
- The function at 0xc0061168 (called before sleep) likely configures the resume vector

### 32. Memory dump reveals pre-sleep function is a timestamp save (not resume setup)

Guest memory dump from `cpu_physical_memory_read()` with manual VA→PA translation (KVA 0xc0000000 → PA 0x08000000):

- **0xc0061168 is a D-cache flush** (clean by set/way, DSB, BX LR) — NOT the sleep setup function as previously assumed
- **Real pre-sleep function is at 0xc006155c** (called at 0xc005a6bc via `bl`)
- The pre-sleep function reads a 64-bit HW timer counter at 0xe0099000+0x80/0x84 and adds an offset → it's just a **timestamp save**, not resume vector configuration
- Return values stored to [R4+0x54] = 0x206492be and [R4+0x58] = 0x00000000 (64-bit timestamp)
- R4 = 0xc01d0200 is a platform/machine descriptor struct containing: physical RAM base (0x08000000), kernel addresses, function pointers, HW register bases, ARM MIDR value (0x412fc0f1)

### 33. High exception vectors and reset handler identified

- SCTLR.V=1 → exception vectors at 0xFFFF0000 (mapped via page tables)
- Page table walk: VA 0xFFFF0000 → L1 coarse page table → L2 small page → PA 0x0805f000
- All vectors use `LDR PC, [PC, #0x18]` (standard XNU vector table)
- **Reset vector → 0xc005fff4** (kernel's installed reset handler)
- **IRQ vector → 0xc0060404**
- **FIQ vector → `MOV PC, R9`** (direct jump to R9 = 0xc0824b34 at sleep time)
- TTBR0=0x091a0000, TTBR1=0x08664000, TTBCR.N=2

### 34. PMU pre-sleep register sequence captured

Full PMU register state at sleep time (via `regs[256]` capture):
```
OOCSHDWN (0x0C) = 0x02   ← standby trigger
OOCWAKE  (0x0D) = 0x00
GPMEM0   (0x67) = 0x01   ← possible warm-boot flag!
GPMEM1   (0x68) = 0xFF
GPMEM2-3         = 0x00
reg 0x76         = 0x80
INT1M    (0x07) = 0xB0   ← ONKEYF(0x80)+ALARM(0x10)+SECOND(0x20) masked
```
Pre-sleep write sequence: various regulator configs → INT1M=0xB0 → clear INT1-5 → reg 0x76=0x80 → OOCSHDWN=0x02 (final standby trigger)

### 35. Approach #15: Warm-reset via kernel reset handler — ❌ FAILED

**Rationale:** Based on findings #31-34, the real wake mechanism is hardware reset. We simulate by jumping to the kernel's reset handler at 0xc005fff4 (from high vector table at FFFF0020), keeping MMU on since kernel memory is intact.

**Implementation:**
```c
env->regs[15] = 0xc005fff4;     // kernel reset handler
cpsr_write(env, 0x000000D3, ...); // SVC mode, I=1, F=1 (as after reset)
tb_flush(s->cpu);                // clear TB cache
cpu_interrupt(s->cpu, CPU_INTERRUPT_HARD);
```

**Result:** The reset handler at 0xc005fff4 is `eafffffe` = **`b .` (another infinite loop!)**. The kernel installs a dead reset handler because it never expects a reset exception while running — real hardware resets go through bootrom, not the kernel's exception vector. The CPU gets stuck in the new `b .` loop.

**Why it failed:** On real S5L8900 hardware, a PMU-triggered reset restarts the CPU from the bootrom at PA 0x0, which detects warm boot and jumps to iBoot, which then resumes the kernel. The kernel's own Reset exception handler at 0xFFFF0000 is never used for this purpose — it's just a safety trap.

### 36. Approach #15 caused FIQ trap (unmasking F bit)

**Finding:** During approach #15 testing, the P release (keycode 153) also detected I=1 F=1 at the reset handler's `b .` and triggered a second warm-reset. Between press and release, the CPU took an FIQ to 0xffff001c (`MOV PC, R9`). This happened because approach #16 (next) initially unmasked both I and F bits. The FIQ vector does `MOV PC, R9` which jumps to R9=0xc0824b34.

**Lesson:** Never unmask F bit (FIQ) — only unmask I (IRQ). The kernel has FIQ masked for a reason.

### 37. Approach #16: Stack-unwind (return from sleep function) — ❌ PARTIAL

**Rationale:** Since the kernel's reset handler is a dead trap, and we can't simulate the full bootrom→iBoot→kernel resume path, try "returning" from the sleep function by popping its saved registers from the stack and jumping to the caller.

**Implementation:**
```c
// The sleep function at 0xc005a6ac pushed {r4, r7, lr}
// Stack at SP=0xc0003fb8: r4=0xc01d0200, r7=0xc0003fc4, lr=0xc0157b23
cpu_physical_memory_read(KVA_TO_PA(env->regs[13]), stack_buf, 12);
env->regs[4] = saved_r4;        // 0xc01d0200
env->regs[7] = saved_r7;        // 0xc0003fc4
env->regs[13] += 12;            // pop 3 words
env->regs[15] = saved_lr & ~1;  // 0xc0157b22 (Thumb)
// CPSR: clear I (enable IRQs), KEEP F set, set T for Thumb, SVC mode
cpsr_write(env, new_cpsr, ...);
tb_flush(s->cpu); cpu_interrupt(s->cpu, CPU_INTERRUPT_HARD);
```

**Iteration 16a — FIQ trap:** Initially set CPSR with F=0 (both I and F cleared). This caused an immediate FIQ to 0xffff001c. The P release then detected I=1 F=1 in FIQ mode and did a second corrupt unwind from the FIQ stack, jumping to data address 0xc01d0200. **Fix:** Keep F=1.

**Iteration 16b — P release double-unwind:** Fixed FIQ by keeping F=1. But P release (keycode 153) also detected I=1 F=1 (CPU was in IRQ handler at 0xffff0018) and triggered another unwind. **Fix:** Only trigger unwind on keycode 25 (P press), not 153 (P release).

**Iteration 16c — No ONKEY, clean unwind:** Skipped ONKEY for both press and release during wake (using `wake_unwind_active` counter=2). Cleared PMU INT1-5 to prevent any re-assertion. Result: clean stack unwind, no storm. But **kernel went back to sleep** — the caller at 0xc0157b22 is `POP {R7, PC}` which returns through the call chain back to the idle loop, which calls the sleep function again. Without a wake signal, the kernel has no reason to turn on the display.

**Iteration 16d — With ONKEY but storm prevention:** Fired ONKEY normally and let interrupt deliver once. Prevented storm by skipping the ONKEY for the wake P press/release. Result: PMU ONKEY fired → nIRQ asserted → but then P release still triggered ONKEYR → storm resumed for the release.

**Why partial success:** The stack unwind itself works correctly — the kernel resumes execution and even handles one IRQ cleanly. But:
1. Without ONKEY: kernel goes back to sleep (no wake signal)
2. With ONKEY: GPIO ISR still only acknowledges and returns, doesn't dispatch to PMU handler (finding #28 remains the core issue)

### 38. Approach #17: ONKEY with auto-clear storm prevention — ❌ PARTIAL

**Rationale:** The storm happens because the deferred timer re-checks PMU INT1, finds it still pending (ISR never reads INT1 via I2C), and re-asserts. If we auto-clear INT1 in the SYSIC INTSTAT handler when the ISR acknowledges bit 21, the deferred timer will find INT1=0 and not re-assert. This gives one clean interrupt delivery.

**Implementation:** In `ipod_touch_sysic.c` GPIO_INTSTAT write handler:
```c
if (group == PMU_INT_GPIO_GROUP && s->pmu) {
    // Auto-clear PMU INT1-5 when ISR acknowledges INTSTAT
    s->pmu->int1 = 0; s->pmu->int2 = 0; ...
    // Deferred timer will find INT1=0, no re-assertion
    timer_mod(s->pmu_reassert_timer, ...);
}
```

**Result:** ✅ Storm completely eliminated! The interrupt sequence was:
1. ONKEY → INT1=0x80 → GPIO asserted → VIC irq 31
2. P release → INT1=0xc0 (ONKEYF+ONKEYR) → GPIO still asserted
3. ISR reads INTSTAT[2] = 0x00200000 (bit 21)
4. ISR writes INTSTAT[2] → SYSIC auto-clears INT1 (was 0xc0 → now 0x00)
5. ISR reads INTSTAT[2] = 0x00000000 → verified clear
6. Deferred timer fires → INT1=0 → no re-assertion
7. **No storm! Clean single interrupt delivery.**

**But:** Kernel still went back to sleep. The ISR acknowledged the GPIO interrupt but never dispatched to the PMU handler (finding #28). The ONKEY event was consumed by the auto-clear without reaching the power management system.

### 39. Root cause confirmed: GPIO ISR is a generic acknowledge-only handler

After approaches #15-#17, the root cause is conclusively confirmed:

1. **The GPIO group 2 ISR** (IRQ 31 handler) is a **generic acknowledge-and-return** handler. It does NOT dispatch to registered GPIO handlers for individual bits. It ONLY reads INTSTAT, writes INTSTAT (clear), reads INTSTAT (verify), and returns.

2. **There is no PMU GPIO handler registered.** On real hardware, the PMU ONKEY press triggers a hardware reset, not a GPIO interrupt dispatch. The GPIO ISR is designed for normal operation (not sleep wake).

3. **The interrupt chain cannot deliver PMU events.** No amount of timing, re-assertion, or storm prevention can fix this — the ISR code path simply doesn't include PMU I2C reads or handler dispatch.

4. **The real wake mechanism requires simulating what iBoot does after warm boot** — which is to call the kernel's resume entry point (NOT the reset exception handler) with appropriate state restoration. Finding this entry point requires further kernel binary analysis.

### 40. LCD render register is ALREADY ON during sleep — framebuffer is black

**Approach #18** added LCD state dumping to the key handler wake path. Results:

```
render = 0x00000001 (ON)
w1_framebuffer_base = 0x0f496000
lcd_con = 0x00000001  lcd_con2 = 0x01118001
wnd_con = 0x00000001  unknown1 = 0x00003f00
FB sample: 000000ff 000000ff ... (all pixels)
FB content: HAS CONTENT (but all black — BGRA 00,00,00,FF)
```

**Critical discovery:** The kernel does NOT set `render=0xFF` during sleep. The LCD controller render register stays at 0x1 (display ON). The QEMU `lcd_refresh()` gfx_update callback runs independently of the render register and always copies from the framebuffer. The screen is black because the **framebuffer content itself is all-black pixels** (BGRA 0x00000FF). The kernel cleared the framebuffer before entering sleep.

**Implications:**
- Setting render=0x1 has no effect (it was already 0x1)
- The display pipeline works — it's showing exactly what's in the framebuffer
- To show content, we need the kernel to REDRAW the framebuffer (or write pixels directly)
- The framebuffer is at PA 0x0f496000 (NOT the static `FRAMEBUFFER_MEM_BASE = 0xfe00000` — kernel changed it)

### 41. Sleep decision function identified — state variable at 0xc01a6170

Disassembly of the function containing frame 2 return (0xc005a8d8) reveals a sleep/wake decision:

```arm
0xc005a8c0: LDR R3, =0xc01a6170   ; load state variable address
0xc005a8c4: LDR R3, [R3]           ; R3 = *0xc01a6170 (dereference)
0xc005a8c8: CMP R3, #2             ; check state
0xc005a8cc: BEQ 0xc005a8e0         ; if state==2, skip sleep → take wake path
0xc005a8d0: LDR R0, [R4, #0x2C]   ; load object for sleep call
0xc005a8d4: BLX <sleep_method>     ; call into sleep chain
0xc005a8d8: SUB SP, R7, #8        ; ← return from sleep
0xc005a8dc: POP {R4, R5, R7, PC}  ; return
; --- wake path (state==2) ---
0xc005a8e0: LDR R0, =0xc01667d4   ; load argument
0xc005a8e4: BLX 0xc0019790         ; call wake function
0xc005a8e8: B 0xc005a818           ; loop back to re-check
```

- State variable at VA 0xc01a6170 (PA 0x081a6170), normally = 0
- If value == 2: kernel takes **wake path** (calls function at 0xc0019790 with R0=0xc01667d4, then loops)
- If value != 2: kernel takes **sleep path** (calls into sleep chain → eventually `b .`)
- The wake path loops back to 0xc005a818 to re-check the state

### 42. Approach #19: State variable patch + framebuffer write — ❌ FAILED (different sleep path)

**Rationale:** Set state at 0xc01a6170 to 2 to make kernel take the wake path instead of sleeping. Also write white test pixels to framebuffer for visual feedback.

**Implementation:**
```c
// Write 2 to state variable
uint32_t new_state = 2;
cpu_physical_memory_write(0x081a6170, &new_state, 4);

// Write white pixels to first 10 rows of framebuffer
uint8_t white_pixel[4] = {0xFF, 0xFF, 0xFF, 0xFF};
for (int row = 0; row < 10; row++)
    for (int col = 0; col < 320; col++)
        cpu_physical_memory_write(fb_base + (row*320+col)*4, white_pixel, 4);
```

**Result:** State was patched (0→2), white pixels written, ONKEY fired. But the kernel went back to sleep anyway! CPU ended up at PC=0xc0061650 (timestamp save routine in sleep prep) with I=1 F=1, from a **deeper stack** (SP=0xc0003d8c vs original 0xc0003fb8).

**Why it failed:** The kernel has **multiple code paths** to the sleep function. The state variable at 0xc01a6170 guards only ONE path (through 0xc005a8c8). After the stack unwind and call chain return, the kernel re-entered sleep via a DIFFERENT code path that doesn't check this variable. Patching one guard is insufficient.

**Key observation:** The state variable remained 2 even after the kernel re-entered sleep, confirming the different-path hypothesis. The new sleep entry came from a deeper call chain (SP grew from 0xc0003fb8 to 0xc0003d8c = 572 bytes deeper).

### 43. Approach #20: Patch `b .` in guest memory — ⚠️ PARTIAL (kernel enters UART poll)

**Rationale:** Replace the `b .` instruction at 0xc005a6d0 with CPSIE I + POP {R4,R7,PC}, making the sleep function return immediately regardless of which code path invoked it. Combined with stack-unwind and state variable set to 2.

**Implementation:**
```c
// At PA 0x0805a6cc: replace ARMv6 DSB (0xee073f9a) with CPSIE I (0xf1080080)
// At PA 0x0805a6d0: replace B. (0xeafffffe) with POP {R4,R7,PC} (0xe8bd8090)
cpu_physical_memory_write(0x0805a6cc, &cpsie_i, 4);
cpu_physical_memory_write(0x0805a6d0, &pop_ret, 4);
tb_flush(s->cpu);  // flush translation block cache
```

**Observations:**
- The ARMv6 DSB encoding is `MCR p15,0,R3,c7,c10,4` (0xee073f9a), NOT the ARMv7 `DSB` (0xf57ff04f). Both are valid for ARM1176.
- Patch applied on first P press, static `sleep_patched` flag prevents re-patching.

**Result — CPU moves but enters UART polling loop:**
1. Stack unwind returns from sleep successfully
2. ONKEY interrupt delivered once (VIC IRQ 31 ack'd, INTSTAT cleared, auto-clear zeroed INT1)
3. CPU PC moved from 0xc005a6d0 (sleep loop) to new locations
4. CPU now busy-loops at **0xc01604d4** with PSR=0x200000d3 (I=1, F=1 — deep sleep flags persist!)
5. R0/R12 change between samples → CPU IS executing code, not stuck at a single instruction
6. SP went from 0xc0003fb8 (sleep) to 0xc0003dcc (deeper by 492 bytes)

### 44. UART0 status register is what's being polled

**Page table walk for the polled address:**
- Getter function at 0xc01604d4 reads: `*(*(0xc01ce3e0) + 0x10) & 1`
- `*0xc01ce3e0` = 0xe0000000 (kernel VA for hardware register base)
- VA 0xe0000000 → L1 coarse (0x08669001) → L2 small page (0x3cc00012) → **PA 0x3cc00000**
- PA 0x3cc00000 = **UART0_MEM_BASE** (`#define UART0_MEM_BASE 0x3CC00000`)
- Offset 0x10 = **UTRSTAT** (Samsung UART TX/RX status register)
- Bit 0 = **Receive buffer data ready** (1 = data received, can be read from URXH)

**The kernel is polling UART0 for serial input.**

### 45. Polling loop structure: serial_getc_poll() called from idle context

**Code flow traced through disassembly:**

```arm
; serial_getc_poll at 0xc01609f0 (ARM):
0xc01609f0: ldr r3, =*0xc01ce3d8    ; check if serial initialized
0xc01609fc: ldr r3, [r3]; cmp r3, #0
0xc0160a04: bne 0xc0160a10          ; if init'd, proceed
0xc0160a08: mvn r0, #0; pop {r4,r7,pc}  ; return -1
0xc0160a10: ldr r4, =*0xc01ce3dc    ; load serial ops vtable
0xc0160a1c: ldr pc, [r3, #16]       ; CALL vtable[4] → 0xc01604d4 (UTRSTAT poll)
0xc0160a20: cmp r0, #0
0xc0160a24: beq 0xc0160a08          ; if no data → return -1
0xc0160a34: bx r3                   ; tail-call vtable[5] (getc)

; UART getter at 0xc01604d4 (ARM):
; Returns: *(*(0xc01ce3e0) + 0x10) & 1  (UTRSTAT bit 0)

; Thumb wrapper at 0xc01603ee calls serial_getc_poll:
0xc01603ee: push {r7, lr}
0xc01603f0: add r7, sp, #0
0xc01603f2: blx 0xc01609f0          ; call serial_getc_poll
0xc01603f6: pop {r7, pc}            ; return result
```

**Serial vtable at 0xc01bf150:**
```
[0] 0xc01603f8 = init/open
[1] 0xc0160458 = configure
[2] 0xc01604a4 = tx_ready (UTRSTAT bit 2)
[3] 0xc01604bc = putc (write UTXH)
[4] 0xc01604d4 = is_data_ready (UTRSTAT bit 0) ← being polled
[5] 0xc01604ec = getc (read URXH)
```

**Stack backtrace from UART poll context (frame pointer walk):**
```
0xc0003dcc: serial_getc_poll frame → lr=0xc01603f7 (Thumb wrapper)
0xc0003dd8: Thumb wrapper frame    → lr=0xc04bc4a0 (kernel ext/driver)
0xc0003dec: driver frame           → lr=0xc000f0d5 (kernel text)
0xc0003e68: kernel frame           → lr=0xc000fc3f (kernel text)
(continues up to original sleep SP area at ~0xc0003fb8)
```

The CPU is in SVC mode with I=1, F=1 — interrupts fully disabled. The serial poll can never succeed because no UART data can be received with interrupts off (UART DMA/FIFO fill requires interrupts or DMA, neither available).

### 47. Approach #21 FAILED: direct wake function call ends in same UART poll

**Test result:** Set PC=0xc005a8e0 (wake path entry), SP=0xc0003fd8, R7=0xc0003fe0, R4/R5 from stack, CPSR with I=0 (IRQs enabled), ARM mode, SVC.

**What happened:**
1. P press detected sleep → applied approach #21
2. Wake function at 0xc0019790 was called (BLX from 0xc005a8e4)
3. ONKEY interrupt fired, VIC0 input 31 went HIGH
4. On P release: PC=0xffff0018 (IRQ vector!) — interrupt was actually taken
5. But CPU ended up at 0xc01604d4 again (UART poll), PSR=200000d3 (I=1, F=1)
6. Framebuffer still all black (BGRA 00,00,00,FF)

**Conclusion:** The wake function at 0xc0019790 is likely a scheduler/thread wakeup hint, NOT a display wake function. It runs, the scheduler processes it, but the kernel has no "power button pressed" event to act on. The kernel simply returns to its idle loop (which polls UART with interrupts disabled).

**Key insight:** The IRQ was actually taken (PC=0xffff0018), meaning the ONKEY interrupt got delivered during the window when I=0. But the GPIO ISR still only acknowledges — it doesn't dispatch to the PMU driver (finding #31). So even with interrupts enabled and the interrupt delivered, the kernel doesn't process it as a wake event.

### 46. State variable remains at 2 — kernel may or may not have reached wake path

Checked state variable at PA 0x081a6170: still contains 2 (our written value). This means either:
1. The kernel never reached the 0xc005a8c8 CMP check (different code path entirely)
2. The kernel reached it, took the wake path (BEQ to 0xc005a8e0), called the wake function at 0xc0019790, and the wake function itself led to the UART polling loop
3. The wake function at 0xc0019790 set state back to something and then it was re-set by our repeated patching

The wake function at 0xc0019790 is **Thumb code** (QEMU monitor decoded it as garbled ARM because it was treating Thumb as ARM). Manual Thumb decode of the prologue:
```thumb
0xc0019790: b40f      PUSH {R0-R3}        ; save args
0xc0019792: b5f0      PUSH {R4-R7, LR}
0xc0019794: 465e      MOV R6, R11
0xc0019796: 4655      MOV R5, R10
0xc0019798: 4644      MOV R4, R8
0xc001979a: b470      PUSH {R4-R6}        ; save high regs
0xc001979c: af06      ADD R7, SP, #24     ; frame pointer
0xc001979e: b084      SUB SP, SP, #16     ; local variables
```

This is a substantial function with many saved registers. It's called with R0=0xc01667d4 (a string or struct pointer). Without Thumb disassembly capability, the function body can't be easily analyzed.

### 48. Framebuffer is triple-buffered between 3 addresses

The kernel cycles `w1_framebuffer_base` rapidly between three addresses:
- 0x0FE00000 (iBoot original SFN address)
- 0x0F400000
- 0x0F496000

During active display, the LCD controller register at 0x38900060 switches between these addresses as the kernel double/triple-buffers. When the device sleeps, the base settles at one address (typically 0x0F496000).

### 49. Display IS rendering — lock screen is mostly black with dark gray dock

During normal operation (before auto-sleep), the framebuffer **does have non-black content**:
- Row 450 (bottom area, dock region): 0xFF232323 (dark gray, B=G=R=0x23)
- All other sampled areas: 0xFF000000 (black)

This means the kernel IS rendering a lock screen, but it has a black wallpaper. Only the dock/toolbar area at the bottom (rows ~440-470) has visible non-black pixels. The QEMU display window should show this as a mostly-black screen with a subtle dark gray band at the bottom.

### 50. Framebuffer snapshot capture works with multi-buffer scanning

By scanning all three known buffer addresses (not just the current `w1_framebuffer_base`), the snapshot code can capture framebuffer content even during triple-buffering. Snapshot captured from 0x0F400000 during early boot, before auto-sleep blanked it.

### 51. Framebuffer restore works — writing to all 3 buffers makes content persistent

On wake (P press), writing the captured snapshot to all three framebuffer addresses ensures the content is visible regardless of which buffer the LCD controller is currently reading. After restore:
- Current W1 base (0x0F400000): dock area has 0xFF232323 ✅
- Other buffers also have snapshot content ✅

### 52. UART character injection breaks the serial poll temporarily

`qemu_chr_be_write(serial_hd(0), "\n", 1)` successfully injects a character into UART0. The kernel's `serial_getc_poll()` at 0xc01609f0 detects data (UTRSTAT bit 0 set), calls `getc` (0xc01604ec) to read it, and returns the character to its caller. However, after consuming the single character, the kernel returns to polling for the next one. The poll loop is: `is_data_ready → getc → process → is_data_ready → ...`

### 53. Approach #22 result: framebuffer restore + UART inject + sleep patch

Combined approach:
1. Sleep function patched (CPSIE I + POP instead of b . loop) ✅
2. Framebuffer snapshot restored to all 3 buffer addresses ✅
3. Serial character injected into UART0 ✅
4. CPSR I-bit cleared (IRQs enabled) ✅

**Result:** CPU is NOT stuck in sleep loop. It cycles between 0xc0061650 (new code path, LR=0xc0062461) and UART getc (0xc01604ec). However:
- I=1, F=1 persists — kernel re-disables interrupts immediately
- Framebuffer has restored content (dock area visible)
- The kernel does NOT process the power button as a wake event
- No full UI restoration — kernel doesn't redraw the lock screen

**Verdict: PARTIAL SUCCESS** — best result so far. Framebuffer visually restored, CPU active, but kernel doesn't truly "wake."

### 54. CRITICAL: PMU sub-IRQ handler byte[1] is STUCK (approach #27 — ROOT CAUSE FIX)

**The GPIO ISR DOES have a registered PMU handler — it was blocked by a stuck flag.**

Traced the full interrupt dispatch chain:
1. IRQ vector 0xFFFF0018 → handler at 0xC0060404 (ARM)
2. Dispatches via per-CPU handler info → 0xC014D0DC (Thumb IOKit IC)
3. Top-level descriptor table at 0xC089D640, entry 0 → handler 0xC033A8F8
4. VIC0 handler reads VECTADDR, indexes into per-interrupt table at [0xC0913550] = 0xC080F000
5. Entry 31 (GPIO group 2 = PMU): handler = 0xC04888C4, arg0 = 0xC08D9C00, arg3 = 2
6. GPIO handler reads INTSTAT[2], finds bit 21, computes sub-IRQ 85
7. Sub-interrupt table at [0xC08D9C50] = 0xE0248000
8. **Entry 85 (PMU): handler = 0xC014047A (Thumb), arg0 = 0xC09DCC40, enabled = YES**

**Finding #54**: Entry 85 at 0xE0248AA0 has byte[1] = 0x01 ("currently handling" flag) STUCK at 1. This prevents the GPIO ISR from dispatching to the handler. All other entries have byte[1] = 0.

The entry layout (32 bytes):
- byte[0]: in-service flag
- byte[1]: "currently handling" flag — **THIS WAS STUCK**
- byte[2]: re-run requested flag
- byte[3]: enabled flag (1 = yes)
- offset +8: arg2, +12: arg3, +16: arg0, +20: handler function, +24: arg1

**Fix**: Clear byte[1] to 0 using `cpu_memory_rw_debug()` on virtual address 0xE0248AA1. Physical address confirmed via `gva2gpa`: 0x08867AA1.

**Result**: After clearing, the GPIO ISR dispatches to the PMU handler! VIC IRQ 31 is taken, SYSIC INTSTAT[2] bit 21 acknowledged. The handler queues deferred work (sets byte[1]=1, byte[2]=1) and returns.

### 55. SYSIC auto-clear of PMU INT1-5 was sabotaging wake

**Finding #55**: The auto-clear code in the SYSIC INTSTAT write handler (added for finding #27 storm prevention) cleared PMU INT1-5 BEFORE the PMU handler could read them. The GPIO ISR clears INTSTAT (triggering the SYSIC write callback) BEFORE dispatching to the sub-IRQ handler. So INT1 (ONKEY=0x80) was wiped to 0x00 before the PMU driver's deferred handler could see it.

**Fix**: Removed auto-clear of INT1-5. Instead, the SYSIC schedules a deferred timer (200ms) that clears INT1-5 after the PMU handler has had time to read them. The 200ms delay allows the wake assist timer (50ms pulses) to enable IRQs so the deferred work handler can run and read INT1.

### 56. Kernel idle loop re-disables interrupts (approach #24 complement)

After the sleep function returns (patched to CPSIE + POP), the kernel's idle loop immediately re-disables interrupts with CPSID I. The CPU alternates between PC=0xC01604EC (LR=0xC01603F7) and PC=0xC01604D4 (LR=0xC0160A20) with I=1 F=1.

**Fix**: Added a "wake assist" timer that fires 40 pulses at 50ms intervals after wake, each time clearing the I bit in CPSR. This gives the kernel scheduler windows to process the deferred PMU work.

**Result**: After the first wake assist pulse, the CPU state changes to CPSR=0xA0000053 (I=0, F=0 — IRQs enabled!) at PC=0xC0061650. Timer interrupts (VIC IRQ 2) begin firing normally.

### 57. Approach #27 test results — visual wake achieved, touch not yet responsive

**Approach #27 combined implementation:**
1. Clear stuck byte[1] at 0xE0248AA1 → PMU handler dispatch unblocked ✅
2. Sleep function patch (CPSIE I + POP) → CPU exits sleep loop ✅
3. Framebuffer snapshot restore to all 3 buffers ✅
4. UART character injection ✅
5. CPSR I-bit clear ✅
6. Wake assist timer (40 pulses × 50ms) ✅
7. Deferred INT1 clear (200ms delay) to prevent storm while allowing handler to read ONKEY ✅

**What works:**
- Display shows restored framebuffer content (lock screen with dock) ✅
- CPU exits sleep loop and enters normal execution ✅
- VIC IRQ 31 (PMU via GPIO) fires and is processed by kernel ✅
- PMU handler dispatched (byte[2]=1 confirms deferred work queued) ✅
- Wake assist enables IRQs → timer interrupts resume ✅
- CPU reaches CPSR with I=0 (IRQs enabled) ✅

**What doesn't yet work:**
- Touch/click on app icons has no effect ❌
- Kernel may not be processing the ONKEY as a wake event (timing issue between deferred clear and handler read)
- Full UI redraw doesn't happen — display shows static snapshot, not kernel-driven content

**Status: IN PROGRESS** — significant improvement over approach #22. The interrupt dispatch chain is fixed (the real root cause finding #31 was WRONG — the handler existed but was blocked by a stuck flag). Remaining work is timing the deferred INT1 clear so the PMU handler reads ONKEY before it's cleared.

### 58. Timer IRQ routed to FIQ — F-bit must be cleared (CRITICAL)

**Finding #58**: VIC0 INTSELECT=0x00000080, meaning IRQ 7 (Timer) is routed to FIQ (not IRQ). VIC0 FIQSTATUS=0x00000080 confirms the timer FIQ is pending. But during sleep, CPSR has F=1 (FIQ disabled). Our wake code only cleared the I-bit (IRQ enable), not the F-bit (FIQ enable). Without FIQ delivery, the timer never fires and the kernel scheduler never runs — workqueues don't execute and deferred PMU work never processes.

**Fix**: Modified Step 4 in wake handler and `wake_assist_timer_cb` to clear BOTH I-bit (bit 7) and F-bit (bit 6) in CPSR:
```c
new_cpsr &= ~(1 << 7);   // clear I (enable IRQs)
new_cpsr &= ~(1 << 6);   // clear F (enable FIQs — timer uses FIQ)
```

**Result**: After this fix, timer interrupts (VIC IRQ 7) started firing — 272 timer FIQ interrupts counted. CPU running at PC=0xC01604D4/EC with CPSR=0x20000013 (I=0, F=0). The kernel scheduler is now running.

### 59. Deferred INT1 clear is REQUIRED for clean idle loop

**Finding #59**: Without the deferred INT1-5 clear, the kernel's idle loop uses `CPSID IF` (disabling BOTH IRQ and FIQ), blocking timer FIQs entirely. All 40 wake assist pulses fire (each finding I=1, F=1). With the deferred clear (which de-asserts PMU nIRQ), the kernel uses a different idle path that only sets I=1 (leaving F=0), allowing timer FIQs to drive the scheduler.

**Root cause**: Pending PMU nIRQ (INT1≠0) causes the kernel to take a more aggressive idle path (`CPSID IF` with A=1) instead of the normal idle path (`CPSID I` only).

**Implication**: The deferred clear is both REQUIRED (for clean idle state) and HARMFUL (wipes ONKEY before handler reads it). This is a fundamental conflict.

### 60. Approach #28: Extended deferred delay (5 seconds) — FAILED

Increased `PMU_REASSERT_DELAY_NS` from 200ms to 5 seconds to give the PMU workqueue handler time to read INT1 before the deferred clear fires.

**Result**: FAILED. Even after 5 seconds, the PMU handler's deferred work never ran. Debug log showed:
```
[PMU] read reg 0x13
[PMU] INT1 read -> 0x00 (cleared)
```
The workqueue handler eventually ran but found INT1 already cleared by the deferred timer. The workqueue scheduling in the kernel appears to need the timer FIQ running (which requires the deferred clear to have already happened — a chicken-and-egg problem).

### 61. Approach #29: Remove deferred clear entirely — FAILED

Removed the deferred INT1-5 clear completely to let the PMU handler read ONKEY naturally.

**Result**: FAILED. Without the deferred clear, the CPU was stuck with CPSR=0x...01D3 (I=1, F=1, A=1). The kernel's idle loop disabled both IRQ and FIQ (finding #59), blocking all timer delivery. All 40 wake assist pulses were consumed (each found I=1, F=1 and had to re-enable), but the kernel immediately re-disabled FIQ. The scheduler never ran.

### 62. Approach #30: Deferred clear + delayed ONKEY re-injection — IN PROGRESS

**Strategy**: Separate "wake the CPU" from "deliver the ONKEY event":
1. Deferred clear fires at 200ms: clears INT1-5 → de-asserts PMU nIRQ → kernel idle loop uses F=0 path → timer FIQs fire → scheduler runs
2. Separate timer fires at +1 second: re-injects ONKEY event via `pcf50633_set_onkey(true)` then `pcf50633_set_onkey(false)` → fresh PMU interrupt delivered when scheduler is already running → workqueue handler reads INT1 successfully

**Implementation**:
- Added `pmu_onkey_reinject_callback()` in `ipod_touch_sysic.c`
- Modified `pmu_reassert_callback()` to track `had_onkey` and schedule re-injection
- Added `pmu_onkey_reinject_timer` to SYSIC state
- Changed `PMU_REASSERT_DELAY_NS` back to 200ms (fast clear is now desirable)

**Test results (iteration 1)**: The re-injection fired but created an **infinite loop** — each re-injected ONKEY triggered a new GPIO interrupt → kernel acked INTSTAT → new deferred clear scheduled → new re-inject scheduled → ad infinitum. The re-injection and deferred clear kept scheduling each other endlessly.

**Fix**: Added a `pmu_wake_clear_active` flag to SYSIC state:
- Set to `true` in the wake handler (Step 5) before ONKEY fires
- Checked in INTSTAT write handler — only schedules deferred clear when flag is true
- Checked in `pmu_reassert_callback()` — only schedules re-inject when flag is true
- Set to `false` in `pmu_onkey_reinject_callback()` — stops the cycle after one shot

**Test results (iteration 2)**: The one-shot guard worked — exactly one re-injection fired. The kernel processed the GPIO interrupt (INTSTAT read/write confirmed). But **no I2C reads of PMU INT1 occurred**. The PMU driver's workqueue handler never ran.

### 63. Finding #60: byte[1] re-sticks after re-injection

After the re-injected ONKEY event was processed, the PMU sub-IRQ entry at 0xE0248AA0 showed: `00 01 01 01` — byte[1] ("handling") stuck at 1 AGAIN. The initial wake code (Step 1b) cleared byte[1] to 0, but the GPIO ISR sets it back to 1 when dispatching the sub-handler. Since the handler uses deferred/threaded work and never clears byte[1] itself, the flag stays stuck after every dispatch.

**Implication**: Every time we re-inject an ONKEY event, the GPIO ISR processes it and sets byte[1]=1. But the deferred workqueue handler (which should clear byte[1] after completing its work) never runs because it needs the PMU driver's threaded context, which appears to be blocked or not scheduled.

**Fix (implemented but not yet tested)**: Added byte[1] clear in `pmu_onkey_reinject_callback()` right before firing the re-injected ONKEY. This ensures the GPIO ISR can dispatch to the PMU sub-handler when it processes the re-injected event.

### Current status of approach #30

The fundamental problem remains: even though the GPIO ISR acknowledges the PMU interrupt and the sub-handler byte[1] flag is managed, **the PMU driver's deferred work (workqueue) never executes an I2C read of INT1**. The workqueue handler appears to be blocked by something deeper in the kernel's power management framework — possibly because the kernel doesn't believe the device should be awake, or because the power management state machine is in a state where it ignores new PMU events.

**Root cause hypothesis**: The kernel's power management state machine (IOKit PM) tracks sleep/wake state independently of the PMU interrupts. During sleep, the kernel sets internal flags (possibly in IOPMRootDomain or similar) that suppress power event processing. Even if we deliver a perfect ONKEY interrupt chain, the PM framework may discard it because the state machine says "we're sleeping, don't process power events."

This would explain why:
1. Before sleep: clicks/touch work normally (PM state = "awake")
2. After forced wake: display shows content but clicks don't work (PM state still = "sleeping")
3. No I2C reads occur: the workqueue handler checks PM state first and bails out

**Potential next directions**:
1. Find and patch the kernel's PM state variable directly (requires Ghidra analysis)
2. Trace the workqueue handler to understand why it doesn't read INT1
3. Inject a fake "wake complete" signal into IOKit's PM framework
4. Accept cosmetic-only wake and focus on touch event delivery through a different mechanism

### 64. Finding #64: Stale multitouch interrupt in INTSTAT[4] after wake

After wake, there's a stale multitouch interrupt in INTSTAT[4] bit 27 from pre-sleep touch activity. Combined with the level-triggered PMU nIRQ (INT1=0xc0), this keeps the kernel in CPSID IF idle. VIC0 IRQ 2 = S5L8900_GPIO_G4_IRQ = GPIO group 4 = multitouch interrupt.

### 65. Finding #65: PM "sleeping" state controls CPSID IF independently of interrupts

The kernel's PM "sleeping" state DIRECTLY controls the CPSID IF idle path, independently of interrupt status. Even with all interrupts clean (INTSTAT=0, INTLEVEL=0, VIC IRQSTATUS=0), the PM state prevents FIQ delivery, blocking the scheduler. The PMU workqueue thread is frozen during sleep — creating an unbreakable deadlock where: sleep requires ONKEY → ONKEY requires workqueue → workqueue requires scheduler → scheduler requires FIQ → FIQ blocked by PM sleep state.

### 66. Finding #66: Stale VIC in-service interrupt + VECTADDR side-effect

After PM suspend interrupted an active IRQ handler (GPIO G4 / multitouch, VIC IRQ 2), the VIC has a stale in-service interrupt (VECTADDR was read to ack but EOI write never happened). The VIC's priority stack blocks all same/lower priority IRQ delivery. VIC0 VECTADDR = 0x80000002 confirmed. Additionally, reading VIC VECTADDR from the QEMU monitor is SIDE-EFFECTING — it calls pl192_irq_ack() which pushes onto the priority stack, making debugging actively harmful.

### 67. Finding #67 (in progress): VIC IRQ output already HIGH blocks new edge

After VIC priority stack reset, the VIC's IRQ output to the CPU may already be HIGH from a stale assertion. When pl192_update() calls pl192_raise(), it's a NOP because the level is already high. The CPU never sees a new edge and never takes the IRQ exception. Fix: force-lower the VIC outputs before pl192_update() so the raise creates a real LOW→HIGH transition.

### 68. Approach #31: INT1 shadow register + stale INTSTAT[4] clear

INT1 shadow register + stale INTSTAT[4] clear — When SYSIC acknowledges the PMU's GPIO_INTSTAT group during wake, immediately save INT1 to a shadow register and clear INT1-5 to de-assert nIRQ. This breaks the CPSID IF chicken-and-egg problem where INT1 must stay set for the workqueue but causes level-triggered nIRQ. Also clear stale INTSTAT[4] bit 27 (multitouch from pre-sleep). Result: all interrupt state clean (INTSTAT=0, INTLEVEL=0, VIC=0), but CPU still at I=1 F=1 because PM "sleeping" state controls CPSID IF independently (finding #65).

### 69. Approach #32: Pre-patch sleep function on OOCSHDWN write

Pre-patch sleep function on OOCSHDWN write — When PMU receives OOCSHDWN register write (sleep trigger), patch the sleep function BEFORE the CPU reaches it. Replaces DSB + B. (infinite loop at PA 0x0805A6D0) with CPSIE IF + POP {R4,R7,PC} so the function returns immediately. This avoids the frozen-workqueue deadlock (finding #65). Result: CPU stays in normal idle (CPSR I=1 F=0, NOT deep sleep), but multitouch SPI commands stop — the kernel's PM suspend disables device drivers, and the patched return means the VIC has stale priority state (finding #66).

### 70. Approach #33 (current): Post-sleep VIC priority stack cleanup

Post-sleep VIC priority stack cleanup — After OOCSHDWN triggers the sleep pre-patch, schedule a 500ms timer to clean up VIC state. The timer callback: (1) directly resets VIC0/VIC1 priority stacks via pl192_reset_priority() which restores base priority level 0x10, clears current/current_highest to NO_IRQ, and force-lowers IRQ/FIQ outputs before calling pl192_update() to create a fresh edge; (2) re-pulses any pending SYSIC GPIO IRQs. Testing in progress — the force-lower fix addresses finding #67 where the VIC output was already HIGH making the re-raise a NOP.

## Wake Fix Tracker — What's Solved vs What's Stuck Post-Wake

### Fixes that SOLVED the wake itself (CPU exits sleep, display returns)

These fixes are confirmed working and required. Without any of them, the device stays asleep.

| Fix | What it solved | Finding/Approach |
|-----|---------------|-----------------|
| Sleep function patch (`b .` → CPSIE+POP) | CPU exits infinite sleep loop at 0xC005A6D0 | Approach #20 |
| CPSR I-bit clear | IRQs re-enabled after sleep (kernel idle loop re-disables) | Approach #22, Step 4 |
| CPSR F-bit clear | Timer FIQs can fire (VIC INTSELECT routes timer to FIQ) | **Finding #58** |
| Wake assist timer (40×50ms) | Keeps re-enabling IRQs+FIQs against kernel idle loop | Approach #27, Step 5 |
| Framebuffer snapshot restore | Display shows previous content (3 buffers at 0x0FE/0x0F4/0x0F496) | Approach #22, Step 2 |
| UART character injection | Breaks UART serial poll that blocks CPU | Approach #22, Step 3 |
| Deferred INT1-5 clear (200ms) | De-asserts PMU nIRQ → kernel idle uses F=0 path → timer FIQs fire | **Finding #59** |
| Stuck byte[1] clear at 0xE0248AA1 | Unblocks GPIO ISR dispatch to PMU sub-handler | Finding #54, Approach #27 |

**Result of all wake fixes combined**: CPU exits sleep, CPSR I=0 F=0, timer FIQs firing (272+ counted), kernel scheduler running, display shows restored content. **Wake is fully solved.**

### Attempts that work for wake but FAIL at post-wake responsiveness

These all achieve wake successfully but clicking/touch still doesn't work afterward.

| Attempt | What was tried | Result | Why it failed |
|---------|---------------|--------|--------------|
| Approach #28: 5s deferred delay | Increased INT1 clear delay to 5s to let handler read ONKEY | Wake OK, touch broken | Workqueue handler never ran in 5s; chicken-and-egg with timer FIQs |
| Approach #29: No deferred clear | Removed INT1 clear to let handler read ONKEY naturally | Wake BROKEN (no timer FIQs) | Finding #59: kernel disables FIQs when nIRQ pending |
| Approach #30 iter1: ONKEY re-inject | Clear INT1 at 200ms, re-inject ONKEY at +1s | Infinite loop (wake OK but stuck) | Re-inject → GPIO ack → deferred clear → re-inject (loop) |
| Approach #30 iter2: One-shot re-inject | Added `pmu_wake_clear_active` flag for one-shot | Wake OK, one re-inject fired, touch broken | Kernel processed GPIO IRQ but never read INT1 via I2C |
| Finding #60: byte[1] re-stuck | Discovered byte[1] sticks again after re-inject dispatch | — | GPIO ISR sets byte[1]=1 on every dispatch; workqueue never clears it |

**Common thread**: All post-wake failures share the same root cause — the kernel's PMU workqueue handler **never reads INT1 via I2C**. The interrupt delivery chain works perfectly (PMU→GPIO→SYSIC→VIC→ISR→INTSTAT ack all confirmed), but the deferred work that should follow up with I2C reads never executes.

## Strategic Analysis — Comparison of All Approaches

### Progress Assessment (after 30 approaches)

| Phase | Approaches | Achievement |
|-------|-----------|-------------|
| Phase 1: Interrupt routing (#1-14) | Fix button storms, add PMU, fix VIC | GPIO ISR is generic acknowledge-only (finding #31) |
| Phase 2: Direct kernel manipulation (#15-21) | Reset, stack unwind, patches, direct call | Can return from sleep, but kernel re-sleeps or enters UART poll |
| Phase 3: Combined approach (#22) | FB restore + UART inject + sleep patch | **First visible wake** — framebuffer restored, CPU active |
| Phase 4: PMU dispatch + FIQ (#27, #58-59) | byte[1] clear, F-bit clear, deferred INT1 clear | **Wake fully solved** — timer FIQs fire, scheduler runs |
| Phase 5: Post-wake responsiveness (#28-30) | Extended delay, remove clear, ONKEY re-inject | **Stuck** — kernel never reads INT1 via I2C |
| Phase 6-11: PM bypass + kernel patches (#31-41) | VIC cleanup, sleep return, timer fix, get_ticks, secmod/KDP bypass | No panics. **Stuck** in debugger protocol loop (finding #90) |

**Key milestones:**
- Approach #5: First CPU wake from sleep ✅
- Approach #11: Clean sleep detection (I&&F) ✅
- Approach #17: Storm prevention ✅
- Approach #20: Sleep function patched, CPU exits `b .` ✅
- **Approach #22: First visible wake — framebuffer restored** ✅
- **Finding #58: Timer FIQ fix — scheduler runs after wake** ✅
- **Finding #59: Deferred clear required for clean idle** ✅
- **Approach #30: ONKEY re-injection — one-shot works but no I2C read** ❌
- **Approach #41: Security Modules + KDP patches — no panic but stuck in debugger loop** ❌ (finding #90)

### What's genuinely NEW from recent work

1. **Framebuffer triple-buffering discovered** — explains why single-address reads missed content
2. **Display DOES work** — the kernel renders a mostly-black lock screen with dark gray dock
3. **Snapshot + restore mechanism works** — can capture and restore visible content across sleep
4. **UART injection works** — can break the serial poll (temporarily)
5. **New code path 0xc0061650** — after approach #22, CPU visits this address (not seen before)

### What's a confirmed DEAD END (DO NOT RETRY)

1. **Interrupt-based PMU event delivery** — GPIO ISR is generic acknowledge-only (finding #31). 14 approaches tried, none worked.
2. **PMU re-assertion timing** — synchronous, deferred, edge, level — ALL cause storms or have no effect
3. **INTLEVEL tracking** — OS never reads it
4. **LCD render register patching** — already ON, not the issue
5. **Single state variable patch** — multiple sleep paths exist
6. **Kernel reset handler jump** — dead `b .` trap
7. **Stack-unwind alone** — insufficient without wake signal
8. **Direct wake function call (0xc0019790)** — it's just a scheduler hint, not display wake
9. **CPSR I-bit clearing alone** — kernel re-disables interrupts immediately in its idle loop
10. **Patching only the `b .` loop** — kernel enters UART poll with interrupts disabled
11. **Removing deferred INT1 clear** — finding #59: kernel idle loop uses CPSID IF (disabling FIQs), blocking timer entirely
12. **Extended deferred delay (5s)** — approach #28: workqueue handler never runs before the delay expires; chicken-and-egg with timer FIQs
13. **ONKEY re-injection without byte[1] clear** — finding #60: GPIO ISR sets byte[1]=1 during dispatch, preventing subsequent re-dispatch
14. **ONKEY re-injection alone** — even with correct interrupt delivery, the PMU driver's workqueue handler never reads INT1 via I2C; the kernel's PM state machine appears to suppress event processing during sleep

### The remaining unsolved problems

**Problem A: The kernel's PM workqueue handler never reads INT1.**
- The ONKEY interrupt chain works: PMU→GPIO→SYSIC→VIC→kernel ISR→INTSTAT ack all confirmed
- byte[1] clear enables ISR dispatch to PMU sub-handler
- But the sub-handler's deferred work (workqueue) never executes I2C reads of INT1
- The workqueue handler appears blocked by kernel PM state machine
- **This is the FUNDAMENTAL blocker.** The interrupt delivery is correct; the event processing is suppressed.

**Problem B (SOLVED): The kernel re-disables interrupts in its idle loop.**
- Finding #58: Timer uses FIQ (INTSELECT), F-bit must be cleared → SOLVED
- Finding #59: Deferred INT1 clear needed for clean idle loop (F=0 path) → SOLVED
- Wake assist timer (40 pulses) keeps re-enabling IRQs+FIQs → SOLVED

**Problem C: Touch/click not responsive after wake.**
- Before sleep, touch works normally. After wake, clicking on app icons has no effect
- Display shows restored framebuffer (cosmetic wake) but is not interactive
- Likely related to Problem A: kernel PM state says "sleeping" so touch events are suppressed
- May also require touch controller re-initialization after wake

### What could actually work (ranked by feasibility)

**1. Identify and fix the polling hardware register (CURRENT BLOCKER)**
The kernel polls ioremap'd VA 0xe0000010 bit 0 via a "Security Modules v6.6" vtable. Need to:
- Walk the ARM page table to find the physical address behind VA 0xe0000000
- Check which QEMU device this maps to (clock, power controller, security engine?)
- Add the correct "ready" response to unblock the PM resume path

**2. Force-skip the polling loop via kernel memory patch**
Instead of fixing the hardware register, patch the polling caller at VA 0xc0160a20 to skip the check and proceed directly to vtable[5] (the function at [struct+20]). This bypasses the hardware dependency.

**3. Direct CPSR + PM state manipulation**
In the post-sleep callback: clear the I bit in CPSR (enable IRQs), find and clear the PM "sleeping" state variable in kernel memory, force thaw_processes() by modifying process freeze flags.

**4. Prevent sleep entirely (fallback)**
Patch the kernel to never enter the sleep state. Find the function that decides to sleep (the caller of 0xC005A6D0) and NOP the sleep call. This avoids all wake complexity but means the screen never turns off.

**5. Cosmetic wake + touch passthrough (last resort)**
Accept that the kernel doesn't truly wake. Instead:
- Keep the current framebuffer restore (visual wake)
- Bypass the kernel's touch event path and directly inject touch coordinates
- This means clicks wouldn't use iOS gesture recognition but would simulate app launches

## What we've been repeating (AVOID THESE)
- Variations of CPSR I-bit clearing — **SOLVED** (I=1&&F=1 detect + P-only)
- Variations of PMU GPIO assertion — **SOLVED** (level-triggered nIRQ works)
- Variations of auto-lower timer — **DEAD END** (edge for buttons works; PMU re-assertion always causes storm because ISR never reads INT1)
- Variations of which buttons trigger what — **SOLVED** (P=wake+PMU, H=GPIO only)
- VIC0 INTENABLE bit 31 — **INVESTIGATED** (stays enabled, not the issue)
- **ANY variation of SYSIC→PMU re-assertion** — **DEAD END** (synchronous=#12 storm, timer=#13 storm, never=#8 no effect, auto-clear=#17 no dispatch). The ISR never dispatches to PMU handler regardless of timing.
- **INTLEVEL tracking** — **DEAD END** (OS never reads INTLEVEL at runtime)
- **Jumping to kernel reset handler (0xc005fff4)** — **DEAD END** (it's `b .`, a dead trap)
- **Stack-unwind alone** — **INSUFFICIENT** (kernel goes back to sleep without wake signal)
- **Unmasking FIQ (F bit) ALONE** — Was listed as dangerous, but finding #58 showed F-bit clear is REQUIRED (timer uses FIQ). The danger was from doing it without the sleep patch; with the sleep patch + CPSR override, F=0 is safe and necessary.
- **LCD render register** — **NOT THE ISSUE** (already 0x1 during sleep; screen black because FB content is black)
- **Single state variable patch** — **INSUFFICIENT** (multiple sleep paths exist; patching one guard doesn't block others)
- **Patching only `b .` loop** — **INSUFFICIENT** (sleep function returns, but kernel enters UART poll with interrupts disabled; the idle loop re-disables interrupts before checking for work)
- **Direct wake function call (0xc0019790)** — **INSUFFICIENT** (function is a scheduler hint, not display wake; kernel returns to idle loop even after it runs)
- **Direct jump to wake path (0xc005a8e0)** — **INSUFFICIENT** (same result as calling 0xc0019790 directly; the wake path doesn't turn on the display)

## Testing Technique: QEMU Monitor Socket for Programmatic Key Injection

To send keys to QEMU programmatically (useful when osascript/xdotool are unavailable):

1. Launch QEMU with a monitor Unix socket:
```bash
./arm-softmmu/qemu-system-arm ... -monitor unix:/tmp/qemu-monitor.sock,server,nowait 2>/tmp/qemu_debug.log &
```

2. Send keys via Python:
```python
import socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/tmp/qemu-monitor.sock')
time.sleep(0.3)
s.recv(4096)  # consume banner
s.sendall(b'sendkey p\n')
time.sleep(0.5)
resp = s.recv(4096)
print('Response:', resp.decode())
s.close()
```

3. Check results: `grep "BTN.*Power\|DEBUG DUMP" /tmp/qemu_debug.log`

This is the reliable way to trigger power button presses during automated testing.

## Next Steps (genuinely new investigation directions)

### Completed:
1. ~~**Investigate the sleep function at 0xc0061168**~~ → DONE (finding #32: it's a D-cache flush)
2. ~~**Look for warm reset / resume vector mechanism**~~ → DONE (finding #33: reset handler is dead `b .`)
3. ~~**Search iDroid/Linux source for S5L8900 sleep/wake**~~ → DONE (confirmed PMU-triggered hardware reset)
4. ~~**Approach #15: warm-reset to kernel reset handler**~~ → FAILED (handler is `b .` dead trap)
5. ~~**Approach #16: stack-unwind from sleep function**~~ → PARTIAL (works but kernel re-sleeps)
6. ~~**Approach #17: ONKEY with auto-clear storm prevention**~~ → PARTIAL (no storm! but ISR still doesn't dispatch)
7. ~~**Approach #18: LCD force-wake**~~ → NOT THE ISSUE (render already ON; FB is black)
8. ~~**Approach #19: state variable patch**~~ → FAILED (multiple sleep paths)
9. ~~**Approach #20: Patch `b .` in guest memory**~~ → PARTIAL (sleep returns, but kernel enters UART poll)
10. ~~**Approach #21: Direct wake function call**~~ → FAILED (wake function runs, IRQ delivered, but kernel returns to UART poll — wake function is just a scheduler hint, not display wake)
11. ~~**Approach #22: FB restore + UART inject + sleep patch**~~ → **PARTIAL SUCCESS** (first visible wake! Framebuffer restored, CPU active, but kernel doesn't truly wake)

### Current status (after approach #30 — deferred clear + ONKEY re-injection):
- **Sleep function patched** — `b .` replaced with CPSIE I + POP, returns immediately ✅
- **Storm is eliminated** — auto-clear in SYSIC prevents infinite re-assertion ✅
- **LCD is ON** — render=0x1, display pipeline works ✅
- **Framebuffer snapshot captured and restored** — all 3 buffers, dock area visible ✅
- **UART injection works** — breaks serial poll temporarily ✅
- **FIQ enabled** — finding #58 fix: both I and F bits cleared in CPSR ✅
- **Timer FIQs firing** — 272+ timer interrupts counted after wake ✅
- **Kernel scheduler running** — CPU in normal idle loop with F=0 ✅
- **Deferred INT1 clear** — fires at 200ms, de-asserts nIRQ for clean idle ✅
- **ONKEY re-injection** — fires at +1s when scheduler is running (approach #30) ⏳ TESTING
- **Touch/click responsiveness** — not yet working after wake ❌

### Remaining directions (prioritized):
1. **Approach #23: Polish cosmetic wake + toggle behavior** — Approach #22 gives a visual wake. Now add: (a) sleep/wake toggle tracking so P toggles between sleep and wake states, (b) prevent kernel from blanking framebuffer after restore, (c) capture a better snapshot (e.g., during iBoot Apple logo for a brighter image).
2. **Approach #24: Patch kernel idle loop IRQ disable** — Find and NOP the CPSID I instruction in the kernel's idle loop to keep interrupts enabled. This would let the ONKEY interrupt be processed. Requires finding the specific instruction in the idle path.
3. **Approach #25: Continuous UART injection + monitor serial responses** — Set up a timer to keep feeding serial characters and observe what the kernel's serial console outputs. This might reveal the kernel state and available commands.
4. **Approach #26: Kernel binary analysis with Ghidra** — Reverse-engineer the idle loop, GPIO ISR, and power management state machine to understand the exact wake requirements.
5. **Approach #27: Patch GPIO ISR in guest memory** — Add a jump at the end of the ISR that checks PMU INT1 and calls the PMU driver's handler. This would fix the root cause (finding #31) at the kernel binary level.

---

## Phase 6: PM Suspend Bypass + VIC Cleanup (Approaches #31–33, Findings #64–69)

### Finding #64: Stale multitouch interrupt blocks wake
After wake, there's a stale multitouch interrupt in INTSTAT[4] bit 27 from pre-sleep touch activity. VIC0 IRQ 2 = S5L8900_GPIO_G4_IRQ = GPIO group 4 = multitouch. Combined with the level-triggered PMU nIRQ (INT1=0xc0), this keeps the kernel in CPSID IF idle.

### Finding #65: PM "sleeping" state creates unbreakable deadlock
The kernel's PM "sleeping" state DIRECTLY controls the CPSID IF idle path, independently of interrupt status. Even with all interrupts clean (INTSTAT=0, INTLEVEL=0, VIC=0), the PM state prevents FIQ delivery, blocking the scheduler. The PMU workqueue thread is frozen during sleep — creating an unbreakable deadlock: sleep requires ONKEY → ONKEY requires workqueue → workqueue requires scheduler → scheduler requires FIQ → FIQ blocked by PM sleep state.

### Finding #66: VIC priority stack blocks IRQ delivery
After PM suspend interrupts an active IRQ handler (GPIO G4 / multitouch, VIC IRQ 2), the VIC has a stale in-service interrupt (VECTADDR was read to ack but EOI write never happened). The VIC's priority stack blocks all same/lower priority IRQ delivery. VIC0 VECTADDR = 0x80000002 confirmed.

**CRITICAL**: Reading VIC VECTADDR from the QEMU monitor via `xp` is SIDE-EFFECTING — it calls `pl192_irq_ack()` which pushes onto the priority stack. Do not use `xp` on VECTADDR for debugging.

### Finding #67: VIC IRQ output already HIGH prevents re-raise
After VIC priority stack reset, the VIC's IRQ output to the CPU may already be HIGH from a stale assertion. `pl192_raise()` is a NOP because `qemu_irq_raise()` on an already-HIGH line does nothing. Fix: force-lower VIC outputs before `pl192_update()` so the raise creates a real LOW→HIGH edge.

### Finding #68: CPU CPSR I=1 blocks all IRQs
After approach #32's patched sleep returns, CPSR = 0x20000093 (I=1, F=0). The kernel's idle loop runs with IRQs disabled. VIC correctly raises IRQ 2 to the CPU (confirmed with extensive logging) but the CPU ignores it because I=1.

### Finding #69: Zero timer FIQs after sleep — scheduler dead
292 timer FIQ events before sleep, ZERO after. Timer hardware was suspended during PM suspend and never resumed. Without the timer FIQ, the scheduler never runs, IRQs are never re-enabled, and the system is effectively dead despite the CPU being in a normal idle loop.

**Root cause of findings #68-69**: The patched sleep function (approach #32) used `CPSIE IF` as the first replacement instruction. This was wrong — the PM caller checks R0 for the return value. The `bl 0x8061168` call before the sleep loop leaves a non-zero value in R0. The PM caller interprets this as an error and skips `device_resume()` + `thaw_processes()`, leaving the timer suspended and all processes frozen.

**Fix**: Replace `CPSIE IF` with `MOV R0, #0` (0xe3a00000) so the sleep function returns success. The PM caller should then execute the full resume path: device_resume() → thaw_processes().

### Finding #70: Replacing bl 0x8061168 kills the timer
Approach #34's first attempt replaced `bl 0x8061168` with `MOV R0,#0`. This killed the timer entirely — VIC0 RAWINTR showed bit 7 (timer) NOT set. The function at 0x8061168 is a cache flush function that must run for the timer to work properly. **Lesson**: preserve the bl call, patch only after it.

### Finding #71: Sleep function disables FIQ — CPSIE IF needed
The bl 0x8061168 (cache flush) or the PM framework before it disables FIQ (F=1). Without CPSIE IF after the bl, the CPU returns with I=1 F=1, blocking all interrupts. **Solution**: Write 3 instructions at 0x6cc-0x6d4: CPSIE IF, MOV R0 #0, POP.

### Finding #72: Timer IRQ stuck HIGH after sleep — edge fix required
When timer tick #2 fires synchronously during a timer_mod (inside the FIQ handler's STATE write), the IRQ line is still HIGH from tick #1 (IRQLATCH not yet cleared). `qemu_irq_raise()` on an already-HIGH line is a NOP. **Fix**: Added `qemu_irq_lower()` before `qemu_irq_raise()` in the tick callback to force a LOW→HIGH edge.

### Finding #73: Kernel FIQ handler doesn't reprogram timer after sleep
With the edge fix (#72), the timer fires once, the FIQ handler clears IRQLATCH but does NOT reprogram the next tick. The kernel's FIQ handler checks PM state "sleeping" and refuses to schedule next tick — timer dies after one tick. **Fix**: Use auto-recurring mode (TIMER_STATE_START without MANUALUPDATE) in the post-sleep timer restart.

### Finding #74: Sleep function is a one-way trip — NO context save
The function at 0x806155c is NOT a setjmp/context save — it's a timer read function (hardware counter with retry loop, returns 64-bit time). The function at 0x8061168 is a cache flush (iterates cache sets/ways with MCR instructions). The sleep function just: reads current time, stores it, flushes cache, spins forever. There is NO setjmp/longjmp mechanism. The wake path on real hardware is entirely external. Making the sleep function return is NOT how real wake works; it was only a temporary diagnostic strategy before the retained-RAM power-cycle work.

### Finding #75: CPU state at PM suspend (OOCSHDWN write)
CPU register dump at OOCSHDWN I2C write:
```
PC=0xc048a088 (I2C send function)  LR=0xc0489f28
R4=0xc0b16400 (PMU driver struct)  R9=0xc0824b34
SP=0xc0003ebc  CPSR=0x600000d3 (SVC, I=1, F=1)
```
Stack trace (frame pointer chain): 0xc0489c68 → 0xc0489bc4 → 0xc01589d8 (Thumb PM framework). The I2C write path goes through virtual method dispatch (vtable+0x384 at 0x8489c64, vtable+0x380 at 0x8489bc0).

### Finding #76: Kernel stuck polling hardware "ready" register after sleep return
After sleep function returns (approach #34), the CPU does NOT stay in the sleep loop. It returns to the PM framework and continues executing. However, the kernel gets stuck in a polling loop at VA 0xc01604d4 — a function that reads hardware register at ioremap'd VA 0xe0000010, masks bit 0, and returns. The caller (at 0xc0160a1c) calls this via a "Security Modules v6.6" vtable (at 0xc01bf150). The register currently returns 0x6 (bit 0 = 0, not ready). Until bit 0 becomes 1, the PM resume cannot proceed to thaw_processes(). This is the current blocker.
```
VA 0xe0000000:  0x00000003 0x00000405 0x00000001 0x00000001
VA 0xe0000010:  0x00000006 0x00000000 0x00000000 0x00000000
```
The physical device behind this ioremap'd address is unknown — it doesn't match the clock controllers (0x38100000/0x3c500000), SYSIC (0x39a00000), or any known device's register pattern.

### Approach #34: CPSIE IF + MOV R0,#0 + POP with auto-recurring timer
Pre-patch sleep function with 3 instructions at PA 0x0805a6cc-0x0805a6d4 (after bl 0x8061168):
- 0x0805a6cc: CPSIE IF (0xf10800c0) — re-enable IRQ+FIQ
- 0x0805a6d0: MOV R0, #0 (0xe3a00000) — PM "success" return
- 0x0805a6d4: POP {R4,R7,PC} (0xe8bd8090) — return to caller
Combined with: VIC priority stack cleanup (500ms), auto-recurring timer restart, SYSIC GPIO re-pulse.
**Result**: Timer FIQs fire (50k+ ticks), IRQLATCH clears work. But kernel stuck polling hardware register (finding #76). 0 VECTADDR reads, 0 SPI commands. Processes remain frozen.

### Approach #35: CPU register tracing + hardware register identification
Added CPU register dump at OOCSHDWN write time (finding #75). Traced PM suspend call chain through 4 stack frames. Discovered kernel is stuck polling a "Security Modules" hardware register (finding #76). **In progress**: identifying the physical device and making it return "ready."

### Approach #31: INT1 shadow register + stale INTSTAT clear
When SYSIC acknowledges the PMU's GPIO_INTSTAT group during wake, immediately save INT1 to a shadow register and clear INT1-5 to de-assert nIRQ. Also clear stale INTSTAT[4] bit 27 (multitouch from pre-sleep). **Result**: All interrupt state clean, but CPU still at I=1 F=1 because PM "sleeping" state controls CPSID IF independently (→ finding #65).

### Approach #32: Pre-patch sleep function on OOCSHDWN write
When PMU receives OOCSHDWN register write (sleep trigger), patch the sleep function at PA 0x0805A6D0. Originally replaced DSB + B. with CPSIE IF + POP {R4,R7,PC}. **Result**: CPU stays in normal idle (not deep sleep), but timer FIQs stop and multitouch SPI commands stop — PM resume path not running (→ findings #66-69). **Fixed**: Changed CPSIE IF to MOV R0, #0 so PM caller sees success return.

### Approach #33: Post-sleep VIC priority stack cleanup
After OOCSHDWN triggers the sleep pre-patch, schedule a 500ms timer. Timer callback: (1) resets VIC0/VIC1 priority stacks via `pl192_reset_priority()` — restores base priority, clears current/highest to NO_IRQ, force-lowers IRQ/FIQ outputs before `pl192_update()` to create fresh edge; (2) re-pulses any pending SYSIC GPIO IRQs. Addresses finding #66/#67.

### Key IRQ Mappings Discovered
```
VIC0 IRQ 2  = S5L8900_GPIO_G4_IRQ = GPIO group 4 = multitouch
VIC0 IRQ 7  = Timer (routed to FIQ via INTSELECT bit 7)
VIC0 IRQ 22 = I2C/PMU related
VIC0 IRQ 31 = GPIO group 0
GPIO_IRQS[7] = { 0x21, 0x20, 0x1F, 0x03, 0x02, 0x01, 0x00 }
```

### Sub-IRQ Dispatch Table
```
Base: VA 0xE0248000, entry size 0x20 bytes
Entry format: [in-service(1), handling(1), re-run(1), enabled(1), dev_ptr(4), ..., handler_addr(4), ...]
PMU:        entry 85  at 0xE0248AA0 — handler 0xC014047B (Thumb)
Multitouch: entry 155 at 0xE0249360 — handler 0xC014046B (Thumb)
```

### Finding #79: Delay function at 0xc04bc460 — combined timeout + UART serial poll
The delay function has this structure (ARM code):
```
0xc04bc460: PUSH {R4,R5,R6,R7,R8,LR}
0xc04bc474: BL get_ticks               ; R0 = current_time
0xc04bc478-484: target = R0 + delay*1000  ; compute timeout
0xc04bc488: BL get_ticks               ; LOOP START: R0 = now
0xc04bc48c: RSB R0, R4, R0            ; R0 = now - target
0xc04bc490: CMP R0, #0
0xc04bc494: BGE exit                   ; timeout reached → exit
0xc04bc498: LDR R3, [PC+64]           ; R3 = [0xc04bc4e0] = 0xc01603ef
0xc04bc49c: BLX R3                    ; call UART poll (Security Modules wrapper)
0xc04bc4a0: CMP R0, #0
0xc04bc4a4: BLT 0xc04bc488            ; no data → retry
0xc04bc4d4: ... exit epilogue ...
```
Function pointers: [0xc04bc4e0] = 0xc01603ef (UART poll), [0xc04bc458] = 0xc00536d1 (get_ticks inner wrapper).

### Finding #80: Inner wrapper 0xc00536d0 is shared — cannot be patched
get_ticks (0xc04bc410) calls 0xc00536d0 to read the timer. Patching 0xc00536d0 to return -1 causes get_ticks to always return -1, making the delay function's timeout computation broken (target = -1 + delay*1000, now = -1 always → elapsed = always negative, never reaches timeout). **This approach is a dead end.**

### Finding #81: get_ticks stuck in big_function — frozen timebase structure
After patching the delay function's BGE to unconditional B (approach #39), the CPU is still stuck at VA 0xc0061650 (timer_read function). The timer_read function itself is NOT stuck (TICKSHIGH is stable at 0x00000001, consistency check passes). The CALLER loops.

Call chain: `delay_func → get_ticks → 0xc00536d0 → big_function(0xc0062462) → timer_wrapper(0xc0062458) → timer_read(0xc0061650)`.

The big_function at 0xc0062462 has a seqlock-style consistency check reading a timebase structure at VA 0xc01ca5f4+0x14:
```
[0xc01ca608] = 0x47b934e8  (ticks_low — frozen at pre-sleep value)
[0xc01ca60c] = 0x00000000  (ticks_high — frozen)
[0xc01ca610] = 0x000000c8  (additional field)
[0xc01ca614] = 0x0008717c  (additional field)
```
Structure at 0xc01ca5f4: frequency=6MHz (0x005b8d80), ns_factor=1e9 (0x3b9aca00).

The hardware timer has advanced past the software values (TICKSHIGH=0x01, TICKSLOW=0x57xxxxxx → ~5.9 billion ticks vs software's ~1.2 billion). The big_function is stuck in an internal loop because the FIQ handler that should update the timebase structure is NOT updating it after wake.

VIC state at this point: FIQSTATUS=0 (no pending FIQ), RAWINTR=0x11002000 (bits 13,24,28 — no timer bit 7). Timer restart succeeded but the FIQ handler's timebase update path is apparently broken.

### Approach #38: Patch UART poll wrapper to return -1
Patched Thumb wrapper at VA 0xc01603ee (PA 0x081603ee) with `MOVS R0,#1; NEGS R0,R0; BX LR` (returns -1 = "no serial input"). Made the delay function's UART poll return immediately.
**Result**: First delay loop exited via timeout. 8 VECTADDR reads after sleep (power button IRQ delivered!). Timer FIQs working. PMU ONKEY events processed. LCD framebuffer switching observed. But CPU entered a different timer-reading loop at 0xc04bc428 (get_ticks epilogue) — the delay function was still running, spending most time in get_ticks.
**Iteration 2**: Also patched 0xc00536d0 → BROKE get_ticks (finding #80). Reverted.

### Approach #39: Patch delay function BGE → B unconditional
Patched VA 0xc04bc494 (PA 0x084bc494): ARM BGE instruction (0x5A00000E) → B unconditional (0xEA00000E). Makes the delay function exit on first loop iteration without calling UART poll at all.
**Result**: Delay function exits immediately, but get_ticks (called by delay function's first BL at 0xc04bc474) is stuck in the big_function at 0xc0062462 (finding #81). get_ticks never returns, so the delay function never even reaches the loop.

### Finding #82 (corrected): type-4 ends at address zero; target semantics unresolved

**Critical discovery.** Disassembly of the bootrom (`bootrom_s5l8900`, 64KB at PA 0x20000000) and iBoot (`iboot_204_n45ap.bin`, 136KB at PA 0x18000000) reveals:

**Bootrom:**
- Has NO I2C access (no references to I2C base 0x3C600000 or 0x3C900000)
- Cannot read any PMU registers, including GPMEM0 or the warm boot flag
- Always runs the same cold boot path: init hardware → load iBoot from NOR via SPI → verify signature → jump to iBoot
- The bootrom is completely oblivious to warm vs cold boot

**iBoot warm boot detection (VA 0x18009734):**
- Reads PMU register 0x76 via I2C (slave 0xE7), extracts bit 5
- PMU register addressing: iBoot adds 0x67 to abstract index, so index 0x0F → register 0x76
- If bit 5 = 1 → warm boot detected → calls resume handler at 0x18004004
- Cached flag stored at 0x180237AC

**iBoot resume handler (VA 0x18004004):**
- Checks DRAM at PA 0x08000080 for magic `"MOSX"` (0x4D4F5358)
- Checks DRAM at PA 0x08000084 for magic `"SUSP"` (0x53555350)
- Validates 16-byte `iBootSleepValid` token at PA 0x08000090 (AES-encrypted or plaintext)
- Clears both markers (prevents re-detection)
- Calls `boot(type=4, addr=0, args=0)` at VA 0x18003FE0

**The boot function (VA 0x18003FE0) for type=4:**
1. `pre_boot(4)` — brief display flash
2. `task_shutdown()` — stop iBoot tasks
3. `prepare_for_boot(4)` — DMA shutdown, clock gates, GPIO interrupt disable
4. `disable_caches()` — D-cache and I-cache off
5. `disable_mmu()` — clear MMU enable bit
6. `BLX R6` where R6=0 → transfers control to address `0x00000000` after
   caches and the MMU have been disabled.

**Original conclusion, now withdrawn:** this was initially interpreted as an
unconditional jump to the bootrom and therefore proof of a clean reboot with no
kernel resume. That interpretation did not establish what physical address
zero maps to at this point in the S5L8900 boot sequence. A low-memory alias or
memory-remap operation can make `BLX 0` a retained-context handoff instead.

The current QEMU machine cannot answer this dynamically because it has no
mapping at physical address zero and its reset callback jumps directly to
`IBOOT_BASE`, bypassing the real bootrom/NOR/LLB path. Therefore the proven
facts are limited to the marker/token validation, the type-4 call, and the
final transfer to address zero. Whether that transfer resumes retained kernel
state remains an open reverse-engineering and emulation task.

**Verified in QEMU:** The kernel DOES write the sleep markers before OOCSHDWN:
- PA 0x08000080 = 0x4D4F5358 ("MOSX") ✓
- PA 0x08000084 = 0x53555350 ("SUSP") ✓
- PA 0x08000090 = AES-encrypted iBootSleepValid token ✓

### Finding #83: QEMU timer TICKSLOW stale value bug

The QEMU S5L8900 timer implementation (`hw/arm/ipod_touch_timer.c`) had a bug where reading TICKSLOW returned a stale value. The `s5l8900_timer1_read()` function only recalculated ticks (via `clock_ns_to_ticks()`) when TICKSHIGH was read. If TICKSLOW was read first (or alone, as in our approach #40 patch), it returned whatever value was left from the previous TICKSHIGH read.

The original code even had a comment acknowledging this: `"needs to be fixed so that read from low first works as well."`

**Fix:** Modified `s5l8900_timer1_read()` to recalculate ticks on EITHER TICKSHIGH or TICKSLOW read:
```c
case TIMER_TICKSHIGH:
case TIMER_TICKSLOW:
    // Recalculate on EITHER read so read-low-first also works
    elapsed_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 2;
    ticks = clock_ns_to_ticks(s->sysclk, elapsed_ns);
    s->ticks_high = (ticks >> 32);
    s->ticks_low = (ticks & 0xFFFFFFFF);
    return (addr == TIMER_TICKSHIGH) ? s->ticks_high : s->ticks_low;
```

This fix is important even outside the sleep/wake context — any code that reads TICKSLOW without first reading TICKSHIGH would get stale data.

### Finding #84: PMU register 0x76 bit 5 is warm boot flag (not GPMEM0)

iBoot-204 does NOT use GPMEM0 (register 0x67) for warm boot detection. It reads register 0x76 and checks bit 5. The GPMEM0=0x01 value we observed is written by the kernel but not used by iBoot for the warm boot decision. The PMU register map:
- 0x67 = GPMEM0 (kernel sets to 0x01 before sleep)
- 0x68 = GPMEM1 (kernel sets to 0xFF)
- 0x69 = GPMEM2 (kernel sets to 0x00)
- 0x6A = GPMEM3 (kernel sets to 0x00)
- 0x76 = Warm boot flag (iBoot checks bit 5)

### Approach #40: Rewrite get_ticks to read hardware timer directly

Bypassed the frozen timebase structure problem (finding #81) by rewriting `get_ticks` at PA 0x084bc410 (VA 0xc04bc410) with 4 ARM instructions that read the hardware timer TICKSLOW register directly:

```arm
LDR R3, [PC, #4]       @ load timer base address
LDR R0, [R3, #0x84]    @ read TICKSLOW from hardware
BX LR                   @ return
.word 0xe0099000        @ timer base address constant
```

Combined with the TICKSLOW fix (finding #83), this unblocked `get_ticks()` — the CPU now progresses past the delay function. However, the PM resume path then enters the Security Modules serial console check at VA 0xc000f094, which loops indefinitely calling `delay_func(delay=3)` + UART poll.

**Current CPU state:** Cycling between 0xc01604d4 (UART poll) and 0xc04bc460/0xc04bc4a0 (delay function). Power button IRQ delivered and processed (IRQ 31, ISR at 0xc04888c4, EOI written), but ISR is generic acknowledge-only (finding #31) — doesn't dispatch to PMU driver.

### Approach #41: Patch Security Modules + KDP + debugger wait loop

**Goal:** Bypass the Security Modules serial console check, KDP debugger loop, and debugger wait loop that block PM resume.

**Patches applied at OOCSHDWN time (6 total in this approach):**

1. **Security Modules entry** at PA 0x0800f094 → `MOVS R0,#0; BX LR` (Thumb)
2. **Security Modules caller** at PA 0x080203c4 → `MOVS R0,#0; BX LR` (Thumb)
3. **KDP BL NOP** at PA 0x0800e396 → `NOP; NOP` (2x Thumb NOP, 4 bytes)
4. **Outer loop BL NOP** at PA 0x0800fc3a → `NOP; NOP` + **BEQ NOP** at PA 0x0800fc42 → `NOP`
5. **Debugger wait exit** at PA 0x08010066 → `B 0xc001006a` (BEQ→B unconditional)

**Iteration history:**
- Iteration 1-5 (previous session): Tried various combinations of entry/caller/return-point patches. Entry+caller alone caused fallthrough into KDP. Adding return-point + outer loop NOPs caused panic.
- Iteration 6: Entry+caller+KDP BL NOP only → outer loop still infinite (flag never set)
- Iteration 7: Added outer loop NOPs back → CPU stuck in debugger wait loop (memcpy cycle)
- **Iteration 8: Added debugger wait exit → SUCCESS!** Sleep/wake completes, CPU returns to idle.

**Iterations 1-8:** Various combinations of inner-loop patches (KDP BL NOP, outer loop NOPs, BEQ→B at 0x10066). All failed — the debugger protocol state machine at 0xfc10-0x10100 has 4+ loop-back branches (finding #90).

**Iteration 9 — FIXED (finding #91):** Replaced inner-loop patches (3-6) with a single patch at the debugger handler's **function entry** at PA 0x0800faf4. The PUSH {R4-R7, LR} prologue is replaced with MOVS R0,#0 + BX LR, making the function return 0 immediately. This catches ALL call paths via vtable dispatch, regardless of which caller enters the function.

**Result: SLEEP/WAKE WORKS!** CPU returns to normal idle (0xc0061650) after each power press. Verified 4 consecutive power presses without panic or loop. Timer FIQs, VIC, scheduler all working post-sleep. Display stays black (finding #89 — separate issue).

### Finding #87: KDP debugger loop blocks PM resume after Security Modules bypass

After patching the Security Modules entry and caller to return immediately, the PM resume code falls through into the KDP (Kernel Debugger Protocol) subsystem. The exception handler at VA 0xc000e368 contains a `BL` at VA 0xc000e396 that calls the KDP packet handler at ~0xc0020306.

With no debugger connected, KDP enters an infinite packet processing loop, continuously printing:
```
"kdp_packet bad len pkt %lu hdr %d\n"
"Waiting for remote debugger connection.\n"
```

The CPU was observed actively outputting characters via the UART putchar function at VA 0xc01609a0 — it was NOT stuck waiting for TX ready (UTRSTAT=0x6, both TX bits set). R5 values sampled over time showed changing characters (0x30='0', 0x74='t', 0x6b='k', etc.).

**Key insight:** The return value of the KDP BL is discarded — R5=0 unconditionally at VA 0xc000e39a. Therefore, replacing the 4-byte Thumb32 BL with 2x NOP is safe.

### Finding #88: Debugger connection wait loop at PM resume 0xc001004c

Even with KDP BL NOPed and Security Modules outer loop NOPed, the PM resume path contains a higher-level debugger wait loop:

```
0xc001004c: BL 0xc000e368        ; call exception handler (with memcpy)
0xc0010050: CMP R0, #0
0xc0010052: BEQ 0xc001005c       ; check result
...
0xc001005c: LDRB R3, [R5+offset] ; load received byte
0xc001005e: AND R3, R3, R6       ; mask
0xc0010060: CMP R3, #0x12        ; check for KDP disconnect (type 0x12)
0xc0010066: BEQ 0xc001006a       ; exit if disconnect received
0xc0010068: B 0xc000fc76         ; LOOP BACK if not disconnect
```

KDP packet type 0x12 = reboot/disconnect. Since no debugger is connected, 0x12 is never received, creating an infinite loop. The CPU spends most time in memcpy (0xc0061700-0xc0061750, ARM LDMIA/STMIA 64-byte blocks) called from the exception handler.

**Attempted fix:** Change BEQ at PA 0x08010066 from `0xd000` (BEQ) to `0xe000` (B unconditional), forcing the "debugger done" exit path to always be taken. Same branch target, just always taken.

**PROBLEM (finding #90):** This fix is INSUFFICIENT. The "exit" code at 0x1006a processes the disconnect and then branches BACK to 0xfc76 at instruction 0x10080 (`B 0xfc76`). The exit path is not a true exit — it's just a different iteration of the same loop. See finding #90 for full analysis.

### Finding #90: Debugger protocol loop has multiple loop-backs — BEQ→B patch insufficient

**Regression discovered:** On fresh QEMU restart, the approach #41 patches do NOT result in clean idle. The previous session's reported "success" was likely a false positive due to sampling timing.

**Root cause:** The BEQ→B patch at 0x10066 forces the "disconnect handler" path at 0x1006a. But this path does NOT exit the loop — it performs bookkeeping and then branches BACK to 0xfc76:

```
0xc001006a: LDR R3, [PC, #0x150]     ; load constant
0xc001006c: MOV R4, R8
0xc001006e: STR R4, [R3, #0]          ; store R4
0xc0010070: MOVS R3, #0x7E
0xc0010072: BICS R2, R3
0xc0010074: ADDS R3, R2, #0
0xc0010076: MOVS R2, #1
0xc0010078: ORRS R3, R2
0xc001007a: STRB R3, [R1, #0]
0xc001007c: LDR R1, [PC, #0x128]
0xc001007e: STRB R4, [R1, #0]
0xc0010080: B 0xc000fc76              ; ← LOOPS BACK!
```

The full loop structure (all branch-back points discovered):

```
0xc000fc10: Main loop start
  ...
0xc000fc3a: BL secmod (NOPed)         → falls through
0xc000fc42: BEQ loop_start (NOPed)    → falls through
0xc000fc44: Process data (loads [R6+0x5F0], calls 0x080613cc)
0xc000fc54: Classify packet byte:
  - byte & 0x7F == 0x13 → B 0xc00100e8 (exit path A?)
  - byte & 0x7F == 0x12 → B 0xc0010028 (exit path B?)
0xc000fc76: Reset state, check [R5+8]
0xc000fc84: BEQ 0xc000fc10            ; LOOP BACK #1 (if [R5+8]==0)
  ...
0xc001004c: BL 0xc000e368             ; call exception handler (memcpy)
0xc0010050: CMP R0, #0
0xc0010066: B 0xc001006a (our patch)  ; was BEQ
0xc0010068: B 0xc000fc76              ; LOOP BACK #2
  ...
0xc0010080: B 0xc000fc76              ; LOOP BACK #3 (disconnect handler)
  ...
0xc001008c: B 0xc000fc36              ; LOOP BACK #4 (post-handler)
```

**4 separate branch-back points** have been identified, all feeding back into the main debugger protocol loop at fc10 or fc76 or fc36. Patching one (at 0x10066) is insufficient because the code flows through the disconnect handler and hits the loop-back at 0x10080.

**The entire region 0xc000fc10-0xc0010100 is a complex debugger packet processing state machine** that handles KDP protocol communication. It receives packets, classifies them by type (0x12=disconnect, 0x13=reboot, etc.), processes them, and loops back. Without actual debugger interaction, no packet type matches the exit conditions and the loop runs forever.

**Possible fixes:**
1. Patch the BL at PA 0x0801004c (call to exception handler) → skip the entire handler + replace R0 with non-zero to avoid the byte-check path
2. Patch ALL loop-back branches (0x10068, 0x10080, 0x1008c, fc84) to NOP
3. Find and patch the CALLER of this entire debugger function from the PM resume path
4. Set [R5+8] to non-zero to satisfy the loop exit condition at fc84

Option 3 (patching the caller) is likely cleanest — find who calls into the 0xfc10 area from the PM resume path and NOP that call.

### Finding #91: Function entry patch fixes the debugger loop (approach #41 SUCCESS)

**Solution:** Patch the debugger protocol handler's function entry at PA 0x0800faf4 (VA 0xc000faf4) with MOVS R0,#0 + BX LR. The function's PUSH {R4-R7, LR} prologue (0xb5f0) is replaced.

This approach is superior to patching individual loop branches because:
1. It catches ALL call paths — multiple callers dispatch via vtable (BLX R5 at 0xc015764a, BLX R3 at 0xc014d6d8, etc.)
2. R0=0 is the expected "success" return value (callers check R0==0)
3. A single 4-byte patch replaces 6+ individual branch patches

**Callers discovered on the stack:**
```
0xc015764a: BLX R5 (vtable dispatch, first call from PM resume)
0xc014d6d8: BLX R3 (vtable dispatch, higher-level caller)
0xc014bcb0: function epilogue (returns to even higher caller)
```

**Verified:** 4 consecutive power presses, CPU returns to normal idle (0xc0061650) each time. No panics, no loops. Timer FIQs, VIC scheduling, and UART output all working normally.

**Final OOCSHDWN patch list (simplified to 5 patches):**
1. get_ticks rewrite at PA 0x084bc410 (direct hardware timer read)
2. Security Modules entry at PA 0x0800f094 (MOVS R0,#0 + BX LR)
3. Security Modules caller at PA 0x080203c4 (MOVS R0,#0 + BX LR)
4. **Debugger handler entry at PA 0x0800faf4 (MOVS R0,#0 + BX LR)**
5. Sleep function at PA 0x0805a6cc-d4 (CPSIE IF + MOV R0,#0 + POP)
6. Post-sleep VIC cleanup timer (500ms delay)

### Finding #89: Display stays black after successful sleep/wake — SOLVED by finding #92

After sleep/wake cycle completes, the screen remained black because the kernel switches to a blank framebuffer (0x0f496000) as the last step of the "screen off" animation before sleep. After wake, the LCD controller continues reading from this blank buffer. Solved by finding #92 below.

### Finding #92: Framebuffer snapshot restore fixes display after wake

**Root cause:** The kernel draws the lock screen, then switches to a blank framebuffer as the final step of the sleep animation. After our patched wake (sleep function returns R0=0), the PM resume path doesn't trigger the display-on sequence. The LCD controller (w1_framebuffer_base register at offset 0x60) continues pointing to the blank buffer (0x0f496000).

**Pre-sleep framebuffer state:**
- 0x0fe00000: Lock screen content (icons, slide-to-unlock bar, dock)
- 0x0f400000: Home screen content (rich colors, full UI)
- 0x0f496000: Blank (just dark gray status bar at row 10)

**Solution:** The LCD code continuously captures a framebuffer snapshot (fb_snapshot in IPodTouchLCDState) whenever non-black content is detected. In the post-sleep VIC cleanup timer (500ms after OOCSHDWN), restore this snapshot to the current w1_framebuffer_base address.

**Implementation:**
1. Added `void *lcd` pointer to Pcf50633State (PMU → LCD reference)
2. Wired in ipod_touch.c machine init
3. Moved FB_WIDTH/FB_HEIGHT/FB_BPP/FB_SIZE defines from lcd.c to lcd.h
4. In pmu_post_sleep_vic_cleanup(), write fb_snapshot to w1_framebuffer_base

**Result:** After wake, the display shows the pre-sleep home screen content. Verified across 3 consecutive sleep/wake cycles. Icons, dock, and main content area all visible. Status bar (rows 0-10) may be black since the snapshot was captured from a buffer where the status bar had been cleared.

**Final OOCSHDWN patch list (7 items):**
1. get_ticks rewrite at PA 0x084bc410 (direct hardware timer read)
2. Security Modules entry at PA 0x0800f094 (MOVS R0,#0 + BX LR)
3. Security Modules caller at PA 0x080203c4 (MOVS R0,#0 + BX LR)
4. Debugger handler entry at PA 0x0800faf4 (MOVS R0,#0 + BX LR)
5. Sleep function at PA 0x0805a6cc-d4 (CPSIE IF + MOV R0,#0 + POP)
6. Post-sleep VIC cleanup timer (500ms delay)
7. **Framebuffer snapshot restore to w1_framebuffer_base**

### Finding #93: IOPMrootDomain wake transition patches — REVERTED (regression)

**Goal:** Force IOPMrootDomain to call `changePowerStateToPriv(ON_STATE=3)` after sleep, which triggers `kIOMessageSystemHasPoweredOn` to all clients and unblocks user processes waiting on driver I/O.

**Background research:** XNU 9.x (xnu-1228, Darwin 9) does NOT have an explicit process freeze/thaw mechanism. There is no `tasks_system_suspend()`, no `freeze_processes()`/`thaw_processes()`. Processes stop implicitly when the CPU halts and resume when it restarts. Thread suspension primitives exist (`task_hold`/`task_release`) but are per-task for debugging, NOT used system-wide during sleep.

**Root cause analysis:** After our patched sleep returns (R0=0), `powerChangeDone()` at VA 0xC015D5E0 (Thumb) checks flags at `[self+0x8C]`:
1. `[self+0x58] != 0` (IOPMrootDomain marker)
2. `[self+0x8C] & 5 == 4` (bit 2 = needs wake transition)
3. `[self+0x8C] & 0x40` (bit 6 = was sleeping)
4. If bit 3 set → `changePowerStateToPriv(SLEEP=2)` (wrong!)
5. If bit 3 clear → `changePowerStateToPriv(ON=3)` (correct!)

The `setPowerState` at VA 0xC015E744 sets `[self+0x8C]` with `MOVS R3, #8` (only bit 3), missing bit 2 and bit 6.

**Patches attempted:**
1. PA 0x0815E782: `MOVS R3, #8` → `MOVS R3, #0x44` (sets bits 2+6, clears bit 3)
2. PA 0x0815D5F4: `BEQ +12` → `B +12` (unconditional branch in powerChangeDone)

**REGRESSION:** Both patches modify instructions that affect ALL power state transitions, not just SLEEP→ON. The `BEQ → B` in powerChangeDone forces the wake path during ANY transition (ON→IDLE, etc.), corrupting the PM state machine. Power/home buttons stopped working entirely.

**REGRESSION:** Both patches modify instructions that affect ALL power state transitions, not just SLEEP→ON. The `BEQ → B` in powerChangeDone forces the wake path during ANY transition (ON→IDLE, etc.), corrupting the PM state machine. Power/home buttons stopped working entirely — GPIO IRQs were received but the kernel never processed them into a PM sleep request.

**Timing problem (also discovered):** The OOCSHDWN handler fires DURING the sleep path, AFTER `setPowerState` has already executed. So patching the `MOVS R3,#8` instruction only affects *future* calls — the first sleep/wake cycle still has the original (wrong) flags. Even if the patch values were correct, the first cycle would fail.

**Reverted.** The 7-patch approach (#41 + findings #91-92) remains the working baseline.

**Lessons learned:**
- IOKit PM functions are called for ALL power state transitions, not just sleep/wake
- Patching PM instructions globally causes cascading state corruption
- Need a targeted approach: write flags directly to IOPMrootDomain instance memory at the right moment, or inject a deferred callback only after sleep→wake
- Instruction patches applied at OOCSHDWN time are too late for the current cycle's `setPowerState` (already ran) and affect all future calls (not just sleep)

---

## Phase 8: Auto-Lock Display Recovery (Findings #94–95)

### Finding #94 (corrected): auto-lock is the lead-in to OOCSHDWN

The original observation stopped too early and incorrectly classified timed
auto-lock as a separate terminal state. A complete untouched run confirms this
sequence:

1. SpringBoard performs its fade/lock transition and leaves a mostly black
   framebuffer; the status bar can remain in the last scanout buffer.
2. IOKit powers down LCD, multitouch, USB, SDIO, GPU, and other clients.
3. The kernel masks IRQ and FIQ and writes `OOCSHDWN=0x02`.
4. CPU execution reaches the terminal loop at `0xc005a6d0`, where real
   hardware expects application-processor power to disappear.

The leaked status bar in the QEMU window was not a distinct guest sleep mode.
It was an LCD device-model bug: QEMU kept scanning out framebuffer memory after
the PMU had powered the panel off. OOCSHDWN now marks the emulated panel off,
and a pre-wake screenshot is uniformly black.

**Earlier incomplete evidence:**
```
[BTN] keycode=25  PC=0xc0061650  I=1 F=1  power=1   ← first P press, already dark
[PMU] ONKEY pressed  int1=0x80
...
(no OOCSHDWN message in the observation window)
```

That trace captured the IOKit transition before its final PMU write. Later
controlled runs observed `AppleMultitouchZ2SPI: disabled power`, `pmu go hib`,
the OOCSHDWN write, and PC `0xc005a6d0`. Manual P also reaches OOCSHDWN after
the guest consumes the ONKEY event; it is slower than the former host blank
because iPod OS performs its real shutdown sequence.

### Finding #95: Framebuffer snapshot overwritten by fade-to-black animation

**Bug found:** The LCD refresh callback continuously captures a framebuffer snapshot whenever non-black content is detected in any of the 3 framebuffer addresses. During the auto-lock fade-to-black animation:

1. Home screen captured to snapshot ✓ (bright, colorful content)
2. Fade frame 1 captured — slightly darker, overwrites snapshot
3. Fade frame 2 captured — darker still, overwrites again
4. ...
5. Final frame — nearly all black but 1-2 sample pixels still non-zero → captured
6. Snapshot now contains a nearly-black frame (useless for display restore)

**Root cause:** The capture code at `lcd_refresh()` always overwrites the snapshot when `has_visible` is true, with no brightness threshold. A single non-zero pixel among the 6 sample points is enough to trigger overwrite.

**Fix applied:** Snapshot capture now requires at least 4 of 6 representative
pixels to remain visible for 20 consecutive LCD refreshes (two seconds). It
then locks the first stable frame. This avoids both the fade-to-black frames
and the earlier SpringBoard boot overlay (Apple logo with dimmed icons).

### Approach #42: Framebuffer restore on power button press — superseded

**Historical goal:** Make the display show content when P is pressed after the
apparent auto-lock state. This was a visual workaround, not device wake.

**Implementation:** In `ipod_touch_key_event()`, when P is pressed (keycode=25):
1. Check if `fb_snapshot_valid` is true
2. Read first 16 pixels from current `w1_framebuffer_base`
3. If all black (RGB channels all zero) → restore snapshot to ALL 3 framebuffer addresses
4. Write 614KB (320×480×4) to each of 0x0FE00000, 0x0F400000, 0x0F496000

**Iteration history:**

| Iter | Change | Result | Problem |
|------|--------|--------|---------|
| 1 | Restore to current `w1_framebuffer_base` only | Snapshot was all black | Finding #95: snapshot overwritten by fade animation |
| 2 | Fixed snapshot capture (4/6 threshold) + restore to all 3 buffers | Partial | Captured SpringBoard's Apple-logo boot overlay too early |
| 3 | Require 20 consecutive visible frames; reconnect 40-pulse wake assist | **PASS** | Full bright home screen restored and remains visible after the pulse window |

**Known limitations:**
- Frame restoration is still an emulator workaround rather than the missing
  hardware/iBoot display-on sequence.
- HMP-injected touch is processed and acknowledged after wake, but launching a
  specific icon with macOS GUI automation was not confirmed.
- Repeated end-to-end GUI cycles still need deliberate soak testing.

**Final disposition:** rejected. It can paint a bright screen while the guest
multitouch driver remains powered off. Approach #45 uses the actual
OOCSHDWN→SoC reset→pristine iBoot path instead.

### Approach #42 iter 1 — FAILED: Snapshot capture bug

Restored framebuffer snapshot to `0x0f496000` (current `w1_framebuffer_base`). Log showed:
```
[WAKE] Restored framebuffer snapshot to 0x0f496000 on power button press (finding #94)
```
But all 3 framebuffers remained all black (`0xFF000000`). The snapshot itself was black because the fade-to-black animation overwrote it before the screen went fully dark (finding #95).

### Dead Ends Confirmed This Session

| Approach | What | Why Dead |
|----------|------|----------|
| Patching `setPowerState` instruction at PA 0x0815E782 | Change MOVS R3,#8 to set wake flags | Affects ALL PM transitions, not just sleep; causes PM state corruption |
| Patching `powerChangeDone` BEQ→B at PA 0x0815D5F4 | Force unconditional wake path | Same — affects ALL PM transitions; system becomes unresponsive |
| Combined setPowerState + powerChangeDone patches | Two-pronged wake transition fix | Regression: power/home buttons completely stop working |
| OOCSHDWN-time instruction patches for PM functions | Patch at sleep trigger time | Too late for current cycle (setPowerState already ran), too broad for future cycles |
| ~~`sendkey` via QEMU monitor socket for testing~~ **STALE — see below** | Automate P/H presses from terminal | ~~Events never reach `ipod_touch_key_event`~~ **This is no longer true and must not be relied on.** QMP `send-key` reaches `ipod_touch_key_event()` today, on a running *and* on a parked (`RUN_STATE_SUSPENDED`) machine — proven by `IT_KEY_TRACE=1` and by the iPod's and 1.0's parked wakes. Believing this row is part of what sent finding #96 down the wrong path. |

### Discovered Code Identity Corrections

During disassembly analysis this session, two previously-labeled patches were re-identified:

1. **PA 0x080203C4** — Previously labeled "Security Modules caller." Actually a **console output/putchar function**: takes a byte, calls `putchar` at VA 0xC01609A0, stores to log buffer, optionally calls a function pointer. Not PM-related.

2. **PA 0x0800FAF4** — Previously labeled "debugger handler entry." Confirmed as the **KDP debugger protocol handler** entry point. Called via vtable dispatch from multiple PM resume callers (0xC015764A, 0xC014D6D8). Our MOVS R0,#0 + BX LR patch makes it return success immediately, bypassing the entire debugger packet processing loop (finding #90-91).

---

## Phase 9: Genuine OOCSHDWN Power Cycle (Approach #45)

### Trigger for revisiting the accepted result

The host-suspend build appeared to fix P but timed sleep still showed the last
status bar on a black framebuffer and could not wake with P or H. This proved
that approach #44 had only hidden one entry path and was not device sleep.

### Reproduction and corrected model

An untouched controlled run eventually logged the complete sequence:

```
AppleMultitouchZ2SPI: disabled power
pmu go hib
[LCD] PMU powered panel off
[PMU] OOCSHDWN=0x02
PC=0xc005a6d0, CPSR I=1 F=1
```

This corrected finding #94: the status-bar-only frame is an intermediate
scanout during the shutdown sequence, and timed auto-lock does ultimately
reach OOCSHDWN.

### Reset experiment and iBoot heap failure

Issuing a QEMU system reset from the terminal loop preserved SDRAM and reached
`iBoot start`, but iBoot panicked with `heap error: free`. The old reset
handler jumped into the already-used writable iBoot region. Real hardware
reloads iBoot after application-processor power loss.

The reset handler now clears/reloads the 4 MiB iBoot region, clears 64 KiB of
volatile SRAM, preserves main SDRAM/sleep markers, and resets the CPU to the
emulator's existing iBoot entry. This removed the heap panic and booted the
kernel/SpringBoard reliably.

### LCD and input power-domain fixes

- OOCSHDWN marks the panel powered off; refresh emits a fully black surface.
- Reset clears the three volatile scanout buffers so retained pixels cannot
  falsely mark SpringBoard ready.
- The SPI/multitouch controller resets command buffers, frame counters,
  timers, touch state, and its GPIO frame IRQ.
- Idle zero clocks and unknown commands no longer call `hw_error()`.
- Host touch is ignored until a bright framebuffer has remained stable for
  two seconds after reset.

### P/H transition race

P or H can arrive after the visible buffer has become status-bar-only but
before the guest writes OOCSHDWN. Checking all triple buffers was wrong because
inactive buffers retain old bright frames. The wake detector now samples the
active `w1_framebuffer_base`, records `wake_reset_pending`, lets the guest
finish device shutdown, and resets immediately after OOCSHDWN. Both buttons
use ONKEYF as the current PMU model's generic boot-visible wake latch.

### Final direct validation

| Sequence | Result |
|----------|--------|
| Normal boot → Safari tap | Safari opens |
| P → guest ONKEY read → OOCSHDWN | Guest owns shutdown; no host suspension |
| OOCSHDWN screenshot | Uniform black; no status bar |
| OOCSHDWN → P → iBoot → Safari tap | Safari opens; multitouch reinitialized |
| Status-bar transition → H | H queued, guest completes OOCSHDWN, reset starts |
| Queued H → iBoot → stable-frame marker → Safari tap | Safari opens; no crash |
| Tap during Apple-logo interval | Ignored until driver/display startup is stable |

## Files Modified

| File | Changes |
|------|---------|
| `include/hw/arm/ipod_touch_sysic.h` | Added `QEMUTimer`, `GPIOIRQLowerInfo` for auto-lower timers; `Pcf50633State *pmu` pointer; `pmu_reassert_timer`; `pmu_onkey_reinject_timer` (approach #30); `PMU_REASSERT_DELAY_NS` constant (200ms) |
| `hw/arm/ipod_touch_sysic.c` | Added `gpio_irq_auto_lower()` callback + timer init; GPIO_INTSTAT write handler with auto-clear of PMU INT1-5 (approach #17); deferred `pmu_reassert_callback()` with ONKEY tracking; `pmu_onkey_reinject_callback()` for delayed re-injection (approach #30); debug logging for group 2 MMIO ops |
| `hw/arm/ipod_touch.c` | Guest-owned P handling; active-scanout P/H transition queue; retained-SDRAM SoC reset; pristine iBoot/SRAM reload; volatile scanout clearing; boot-input gating state |
| `include/hw/arm/ipod_touch_multitouch.h` | Added `CPUState *cpu`, `Pcf50633State *pmu`, `IPodTouchLCDState *lcd` (forward decl), `wake_unwind_active` fields |
| `include/hw/arm/ipod_touch_pcf50633_pmu.h` | PMU interrupt/register model, OOCSHDWN state, retained-RAM reset queue, historical trampoline/timer state retained for investigation |
| `hw/arm/ipod_touch_pcf50633_pmu.c` | PMU interrupt model plus OOCSHDWN panel-off and queued SoC-reset handoff; historical return-trampoline path remains documented but is not used by normal P/H wake |
| `hw/arm/ipod_touch_timer.c` | Timer tick logging; IRQLATCH logging; IRQ edge fix (lower+raise in tick callback, finding #72); Timer write MMIO logging for TIMER_4; **TICKSLOW stale value fix** — recalculate ticks on either TICKSHIGH or TICKSLOW read (finding #83) |
| `include/hw/arm/ipod_touch_lcd.h` | Shared framebuffer dimensions, stable-startup capture state, and PMU panel-power state |
| `hw/arm/ipod_touch_lcd.c` | Panel-off black output, active-scanout transition detection, stable-startup capture, and boot-time touch suppression |
| `hw/arm/ipod_touch_multitouch.c` | Volatile device reset, safe idle/unknown SPI handling, timer/frame/IRQ cleanup across SoC power cycles |
| `hw/intc/pl192.c` | `pl192_reset_priority()` API for post-sleep cleanup; removed VECTADDR hot-path tracing |

---

## Phase 10: Retained iBoot Type-4 Resume and Reset-Domain Completion

Phase 9 correctly made OOCSHDWN terminal, but its reset still behaved like a
normal boot: it reloaded iBoot without exposing retained LPDDR at physical
address zero. It also cleared scanout memory and allowed historical
OOCSHDWN-time kernel patches to remain in the retained image. Those details
explained the long wake, lost foreground application, and misleading visual
results. This phase supersedes those parts of Phase 9.

### Proven type-4 handoff

The wake reset now maps the first 128 MiB of retained LPDDR at physical address
zero for iBoot's MMU-off type-4 branch. A complete wake trace shows:

```text
[WAKE] Retained LPDDR CRC32C before AP reset: 0xf8ed1fbc
[WAKE] Retained LPDDR CRC32C at reset: 0xf8ed1fbc (stable)
[PMU] RESUME_STATUS read -> 0xa0 (armed + wake)
[PMU] RESUME_STATUS write <- 0x80 (armed)
[PMU] RESUME_STATUS write <- 0x40
pmu wake events:
System Wake
AppleMultitouchZ2SPI: downloaded 49128 bytes of firmware data
```

There is no second `Darwin Kernel Version` line and no SpringBoard launch in
that wake interval. iBoot consumes the retained token and returns to the
existing kernel session. The full 128 MiB checksum is identical immediately
before and at the AP reset boundary.

The SYSIC power registers were also corrected. `POWER_STATE` represents
domains that remain off: `POWER_OFFCTRL` sets bits and `POWER_ONCTRL` clears
them. The retained kernel had previously polled an incorrectly latched bit 2
forever.

### No retained-kernel patching

OOCSHDWN no longer rewrites `get_ticks`, Security Modules, KDP, or the terminal
sleep function. It marks the panel off, records the terminal state, and waits
for application-processor power loss. Wake resets CPU/volatile boot state,
reloads pristine iBoot, resets multitouch protocol state, and retains LPDDR.

This is not the rejected host-side `vm_stop()`/`vm_start()` suspension and it
does not return from the kernel's terminal `b .` loop.

### Exact cause of the leaked status bar

The shutdown compositor uses two OS VRAM buffers at `0x0f400000` and
`0x0f496000`. During the fade, one can contain only the status bar while the
other still contains the complete foreground surface. The older QEMU model
kept scanning the active status-bar buffer after PMU panel-off, which leaked
the bar while the real panel should have been black.

Panel-off now produces a uniformly black host surface. On wake, another reset
domain mattered: iBoot temporarily points the CLCD scanout register at its own
`0x0fe00000` framebuffer. The type-4 kernel retains OS VRAM but does not
rewrite that emulated register soon enough. Selecting iBoot's buffer produced
a white screen; selecting the shutdown buffer reproduced the leaked bar.

The emulator now records likely OS scanout buffers before sleep and restores
the CLCD scanout register when iBoot consumes the type-4 token (`0x40`). Pixels
are not copied and LPDDR is not modified. The iBoot framebuffer is deliberately
excluded because iBoot overwrites it on every wake. This is still an
investigatory approximation: it can avoid the white iBoot buffer and the
status-bar-only shutdown buffer, but it has not reliably restored the actual
foreground application (Safari in the current test). The missing work is in
the retained display/power-domain resume, not in preserving framebuffer bytes.

### Manual and timed validation

| Acceptance check | Observed result |
|---|---|
| Guest-owned sleep | `System Sleep`, driver shutdown, `OOCSHDWN=0x02` |
| Panel while asleep | Uniform black; no retained status bar |
| Timed sleep then P | Same OOCSHDWN/reset/type-4 path as manual sleep |
| Retained memory | Full 128 MiB CRC32C stable across every tested AP reset |
| Boot identity | `System Wake`; no new Darwin kernel or SpringBoard launch |
| Foreground scanout | **Unresolved:** retained kernel/process memory survives, but the visible buffer can fall back to a stale SpringBoard home frame instead of Safari |
| Touch after wake | Multitouch firmware reload and host `mouse DOWN` / `mouse UP` callbacks observed; reliable guest-side cancellation of the next idle transition remains unresolved |
| Crash/root regression | Cold boot mounts root and reaches SpringBoard after restoring committed SPI FIFO behavior |

The wake still traverses the available iBoot image and is slower than real
hardware, but it is now a retained-kernel wake rather than a normal kernel
load.

### VROM/NOR/LLB investigation and current boundary

The address-zero VROM alias, SRAM0/SRAM1 layout, and missing 8900 service shims
were modeled far enough for the dumped VROM to execute and issue its SPI NOR
sequence. It read the supplied NOR catalog's `IMG2` header. The supplied
artifacts do not, however, contain the 8900-wrapped LLB that this VROM expects;
the catalog contains image records and VROM falls back toward DFU. There is no
separate compatible LLB in the repository.

Experimental generic SPI FIFO changes made while tracing VROM also prevented
the cold kernel from mounting its NAND root. They were rejected and the
committed SPI controller behavior was restored. Production wake therefore
starts from a pristine reloaded iBoot image, with the real type-4 retained
handoff after that point. Starting at VROM/NOR remains feasible only when a
compatible user-supplied LLB/firmware set is available.

## Phase 11: Correct PMU Wake-Status Map and Type-4 Cause Handoff

The retained kernel originally printed an empty wake reason and soon entered
sleep again even though iBoot had completed a type-4 handoff. The decisive bug
was not the Power-key edge or the I2C byte transport: QEMU had assigned the
five read-clear PMU interrupt-status registers to `0x13..0x17`. Disassembly of
the ApplePCF50635 driver proves that its wake decoder reads `INT1..INT5` from
`0x02..0x06`. The code at `0xc047754c` decodes the returned bytes as follows:

| Status byte | Bits | Printed wake reason |
|---|---:|---|
| `INT1` | `0x40`, `0x10`, `0x04`, `0x01` | RTC, accessory, USB, FireWire |
| `INT2` | `0x03` | buttons |
| `INT2` | `0x04`, `0x10`, `0x80` | EXTOn1 buttons, EXTOn2 baseband, EXTOn3 accessory |

The apparently convincing reads at `0x13..0x17` were a false lead. A saved
guest return address identified their caller as `0xc0475f34`, an unrelated
read/modify/write routine, not the wake decoder. QEMU was delivering correct
bytes to the wrong guest register block. Correcting the status addresses to
`0x02..0x06` also made the normal Power interrupt path read and clear
`INT1=0xc0` directly.

### Dead ends rejected during this phase

- Holding `INT1` readable for two arbitrary reads failed: iBoot performed only
  one relevant read in that experiment, the kernel still printed an empty
  cause, and the device slept again.
- Moving wake bits inside the old `0x13..0x17` block reached the Apple I2C
  state machine but could never affect the real decoder.
- Treating the stack destination as a broken I2C copy was incorrect. Byte-level
  tracing proved the guest stored the returned bytes in order; the stack
  buffer belonged to the unrelated `0x13` register-maintenance call.
- Globally changing I2C interrupt-level behavior broke cold boot/root
  publication and was reverted. The focused I2C reset-domain cleanup remains.

### Wake-cause lifetime across iBoot

With the register map fixed, the next boundary became visible. iBoot reads and
clears `INT2=0x03` while selecting retained resume, before the kernel decoder
runs. QEMU now keeps a separate retained PMU wake-cause latch and re-exposes it
when iBoot writes `RESUME_STATUS=0x40`, the observed commit point for its
type-4 branch. This is tied to the boot protocol rather than returning a value
for an arbitrary number of reads.

A validated trace now contains:

```text
[PMU] RESUME_STATUS read -> 0xa0 (armed)
[PMU] INT2 read -> 0x03 (cleared)        # iBoot consumer
[PMU] RESUME_STATUS write <- 0x40
[PMU] INT2 read -> 0x03 (cleared)        # retained kernel consumer
pmu wake events: buttons
System Wake
```

The full LPDDR CRC remains stable and no second Darwin kernel or SpringBoard
launch occurs. This closes the blank wake-reason bug. It does **not** yet close
the full wake acceptance gate: the current test still returned to OOCSHDWN
after roughly 20 seconds, and an automated host click did not prove that a
guest touch event cancelled that idle transition. Foreground scanout survival
and immediately usable touch therefore remain blockers before performance
optimization resumes.

### Post-wake touch boundary (still unresolved)

QMP exact input removed macOS pointer scaling from the test. A held center
touch produces `(0.500, 0.500)`. On cold boot the guest fetches every generated
SPI frame. Immediately after retained wake, the same test reaches a different
boundary:

1. AppleMultitouch reloads its 49,128-byte firmware successfully.
2. The multitouch sub-IRQ entry at `0xE0249360` is enabled and clean
   (`in-service=0`, `handling=0`, `re-run=0`).
3. The SYSIC frame status and an explicit low-to-high GPIO edge are generated.
4. VIC0 does contain stale retained-resume state (`stack_i=1`, current IRQ 22);
   resetting its priority/in-service stack is correct but not sufficient.
5. The edge reaches the CPU as pending IRQ `0x2`, proving the emulated
   touchscreen, SYSIC, and VIC route are connected.
6. The retained kernel is still executing with both IRQ and FIQ masked
   (`CPSR I=1 F=1`) in its timer/resume path, never services the pending touch,
   and returns to OOCSHDWN.

Further rejected experiments:

- Forcing the scheduler timer into auto-recurring mode did not help; the timer
  was already active (`status=0x3`) and the guest still fetched no frame.
- Modeling multitouch ATN as a held GPIO level instead of a latched edge did
  not change dispatch.
- Clearing the multitouch software sub-IRQ latch was unnecessary because its
  retained bytes were already clean.

The remaining bug is therefore above the peripheral models: the retained
kernel context is not completing the exact interrupt-mask/return portion of
the real type-4 resume. Forcing CPSR bits from the host was tried in earlier
phases and caused crashes, so it is not an acceptable fix. The next correctness
work must reconstruct the retained resume trampoline/context restoration (and
its VIC/CPU mask ordering), not add another input bypass.

### Accuracy verdict

| Area | Current implementation |
|---|---|
| Sleep entry | Guest-owned driver shutdown and PMU OOCSHDWN |
| AP power loss | Functional reset domain; CPU and volatile boot/protocol state reset |
| Main LPDDR | Retained and checksum-verified |
| Wake cause | Correct `0x02..0x06` status map; button cause retained across iBoot read-clear and re-exposed at the type-4 handoff |
| Boot chain | Reloaded iBoot, not yet VROM → NOR → LLB |
| Resume | Genuine iBoot type-4 handoff to retained kernel |
| Display | Panel-off black is correct; retained CLCD register restoration is partial and foreground-app recovery is unresolved |
| Touch | Controller reset and guest firmware reload work; immediately usable post-wake input is not yet proven reliable |

The honest description is: **guest-driven sleep with a functional S5L8900 AP
power cycle and genuine retained-kernel type-4 resume, but not cycle-accurate
boot-ROM or complete peripheral-domain emulation.** The CLCD fix models a
missing reset-domain register restoration; it is not host suspension or pixel
repainting.

## Phase 12: N45 Wake-Button Latch and Resumed Z2 Transactions

This phase supersedes two conclusions in Phase 11. First, the PCF50633 Power
edges are not `INT1=0xc0`: upstream PCF50633 definitions and the guest driver
agree that `ONKEYR/ONKEYF` are `INT2` bits `0x01/0x02`; `INT1` bits
`0x40/0x80` are RTC alarm/second. Second, the retained kernel is not stuck
because QEMU failed to restore CPSR. The address-zero type-4 trampoline at
physical `0x080607b8` deliberately masks IRQ and FIQ while it restores the
MMU and retained context. Forcing CPSR from the host was therefore both wrong
and the cause of earlier critical-section crashes.

The missing wake condition was the N45 board-level wake-button latch. The
retained device tree contains `button-wake,n45`; its `button_status` platform
function asks ApplePCF50635 for selector `0x100`. Disassembly shows that this
selector reads cached `INT2` bit `0x04`, printed by the wake decoder as
`exton1(buttons)`. Reporting only the generic Power edge (`INT2=0x03`) let
iBoot resume but did not tell `AppleM68WakeButton` that the wake button was
active. QEMU now retains `EXTON1R`, models the physical ONKEY state in
`OOCSTAT`, and re-exposes the complete cause after iBoot consumes the first
read-clear copy.

A successful trace now reaches the retained kernel's real power-on path:

```text
[WAKE] Retained LPDDR CRC32C before AP reset: 0x...
[WAKE] Retained LPDDR CRC32C at reset: 0x... (stable)
[PMU] INT2 read -> 0x07 (cleared)
pmu wake events: buttons exton1(buttons)
System Wake
AppleMultitouchZ2SPI: enabled power, scheduled bootloading
AppleMerlotLCD::_lcdEnable: enable: 1
```

This crosses the old masked-interrupt boundary without rewriting CPSR and is
the strongest evidence so far that the AP reset/type-4 model is following the
guest's intended lifecycle.

### Multitouch crash and protocol findings

Once the kernel completed `System Wake`, the old simplified multitouch model
became the next failure. The resumed driver uses command `0xeb` as a 16-byte
read-interrupt transaction, then performs a separate packet read. Modern Linux
rounds that read to four bytes, but live disassembly/debugging of the 2007
Apple driver proves that this guest reads exactly 59 bytes. Treating every byte
of the command as a new opcode desynchronized the protocol (`0xeb`, `0x01`,
`0xec`, and similar bogus commands) and eventually turned a payload byte into
report ID `0xbf`; the emulator aborted in `hw_error()`.

The model now:

- implements the 16-byte `0xeb` length reply and the guest's 59-byte packet;
- keeps the original direct `0xea` frame path used after cold boot;
- gives the two reply formats independent markers and checksums;
- treats optional/unsupported report selectors as guest errors instead of
  terminating QEMU;
- makes an empty legacy frame poll return an empty response rather than
  dereferencing `NULL`; and
- clears the SPI FIFO IRQ level and multitouch transaction state in the AP
  reset domain.

The host crash is fixed. Direct cold-boot touch remains functional (an exact
QMP tap launches the Music app), and after retained wake the driver reloads
firmware, enables GPIO group 4 bit 27, requests the `0xeb` packet for every
generated movement frame, and QEMU remains alive.

### Dead ends and present acceptance boundary

- Preserving an `0xeb` frame for a hypothetical following `0xea` read was
  wrong for this resumed driver. It repeatedly reread the same frame and
  starved normal UI timing. `0xeb` frames are now consumed once.
- Returning `0xe1` versus native `0xea` as the packet marker was A/B
  tested. Neither marker alone made the lock slider accept the gesture; the
  stored cold-boot frame must remain all-`0xea`, while only the `0xeb` length
  reply is synthesized as `0xe1`.
- Adding a padding byte to match the modern Linux driver's 60-byte aligned
  read was wrong for this firmware. At `0xc0441620`, the guest computed
  checksum `0x0655` but read `0x5500` because it treated bytes 57-58 as the
  final checksum. Removing the padding produces the exact
  `5 + 52 + 2 = 59` byte layout and reaches the checksum-success branch at
  `0xc0441650`.
- Preserving the pre-sleep frame counter and timestamp across the AP reset did
  not change lock-screen behavior and was reverted.
- Several apparent slider failures were also contaminated by QMP's inverted Y
  axis. Exact captures corrected the test to the center of the control, but
  the resumed lock UI still did not complete the drag.
- A partial/cropped lock-screen transition can appear just before the guest
  sleeps again. This is not proof of foreground restoration; the decisive
  result remains that the lock screen does not unlock and the device returns
  to `OOCSHDWN`.
- One cold boot produced a kernel panic during this work but was not
  reproducible on subsequent launches with the same binary; it is recorded as
  a transient observation, not attributed to the wake patch.

Current honest boundary: manual and timed sleep use the same guest-owned
`OOCSHDWN`/AP-reset/type-4 path; LPDDR checksums are stable; the wake cause is
accepted; `System Wake`, LCD enable, and crash-free post-wake Z2 packet reads
all occur. The 59-byte frame now passes both kernel checksum validators, enters
the Z2 virtual handler at `0xc043b46c`, reaches the registered user-client
callback, and its shared-queue enqueue returns success (`r0=1` at
`0xc043d6e8`). SpringBoard's lock UI nevertheless does not move or cancel the
next idle transition. The remaining boundary is therefore after successful
kernel queue delivery: retained user-space notification/consumption or HID
plugin state, rather than PMU, IRQ routing, SPI transport, or frame checksum.
Foreground-app survival is not yet accepted, and performance work remains
blocked on that result.

## Phase 13: Manual Wake Retest and Display-Sequencing Follow-up

A manual retest of the packaged `6d0f0241fc` build materially changes the
acceptance boundary from Phase 12. Slide to unlock works nearly every time
after the resumed lock screen has had enough host time to become responsive.
Post-wake touch is therefore no longer considered the active blocker. It stays
on the later reliability checklist because the long delay can still produce
false negatives and the kernel/user queue boundary has not yet been observed
end to end.

The retest exposed three display/power-sequencing issues to investigate after
the first performance pass:

1. Manual Power sleep can leave a status-bar-only frame visible for several
   host seconds before the panel becomes uniformly black. Sleeping directly
   from the lock screen goes black immediately. The leading hypothesis is a
   real guest fade/compositor interval stretched by slow emulation, not the old
   post-`OOCSHDWN` scanout leak; a timestamped trace must prove whether the bar
   disappears before or only after the PMU shutdown write.
2. Power or Home wake displays iBoot's empty-battery artwork for a noticeable
   interval. The PCF50633 model currently synthesizes only a small subset of
   charger and ADC status, including conditional USB presence during retained
   wake. Trace every battery/charger register read and the selected iBoot
   framebuffer to determine whether iBoot deliberately chooses the artwork or
   a stale scanout buffer merely exposes it.
3. Wake can briefly show SpringBoard before the lock screen. Trace CLCD base,
   render, panel-power, and retained-scanout restoration at every transition.
   Restore scanout only at the hardware-equivalent type-4/LCD-enable boundary
   once the correct foreground-versus-lock-screen ownership is known.

Timed sleep often appears to return to slide to unlock immediately, while the
slider itself remains slow to become active. This observation needs serial and
PMU timestamps after the speed pass to distinguish lock-only behavior, a
queued wake, and a complete `OOCSHDWN`/type-4 cycle.

Display-sequencing acceptance is: no empty-battery artwork on a valid retained
wake, no stale SpringBoard flash, uniform black after `OOCSHDWN`, and the first
visible lock-screen frame accepting touch without an unexplained delay.

### Optimized release manual validation (2026-07-17)

Manual testing of the installed release build at `3797660aff` confirms that
the corrected CLCD interrupt model, 60 Hz cadence, dirty-only redraw, ARM1176
default, quiet launcher, and O3/LTO build produce a visibly much more responsive
device. This closes the subjective responsiveness gate for the first display
optimization pass. It does **not** close the sleep/wake work.

The same build still reproduces the following retained-wake sequence:

1. iBoot's empty-battery image is visible for a noticeable interval;
2. a SpringBoard frame flashes briefly; and
3. the slide-to-unlock screen finally replaces it.

Treat this exact ordering as one unresolved display/power-sequencing symptom,
not three independent UI bugs. The empty-battery frame may be an iBoot decision
caused by incomplete PCF50633 charger/ADC state, while the SpringBoard flash may
be a retained or newly selected CLCD buffer; neither explanation is proven yet.
Trace PMU reads, framebuffer writes, CLCD base selection, and panel enable on a
single timestamped wake before changing either model.

Sleep and wake are also still far too slow. Sleep entry can take ten host
seconds or more before the panel becomes black, and the resumed lock screen can
remain visible but unresponsive for a long interval. Measure these boundaries
separately:

- Power event to the guest's first shutdown/fade action;
- shutdown start to `OOCSHDWN=0x02` and panel black;
- wake event to iBoot's type-4 decision and `System Wake`;
- `System Wake` to Z2 firmware/input readiness; and
- first complete lock-screen frame to the first accepted touch.

The leading hypothesis is that normal guest shutdown, iBoot, and resumed-driver
work are being stretched by slow emulation. A host-side early blank, skipped
fade, or delayed synthetic input would hide the latency without fixing it and
must not be used as the solution.

### Platform integration backlog

These tasks are separate from sleep/wake accuracy but are required for a useful
device demo:

1. **Synchronize guest time with the host.** The PCF50633 RTC read path already
   synthesizes BCD fields from `localtime()`, yet the displayed time does not
   match the host. Trace `RTCSC` through `RTCYR` during cold boot and retained
   wake, then distinguish an RTC-value bug from guest timezone configuration,
   cached wall-clock state, or incomplete RTC write/alarm behavior. Acceptance:
   host and guest date/time agree after cold boot and retained wake without a
   manual correction, while monotonic guest timers remain unaffected.
2. **Provide host-backed Wi-Fi networking.** The current S5L8900 SDIO device is
   only a register stub and is not connected to a QEMU network backend; the
   documented development and release configurations also disable libslirp.
   A generic emulated Ethernet NIC will not help unless this iPod OS build has
   its driver. Implement enough of the expected SDIO Wi-Fi function and firmware
   protocol to satisfy the native guest driver, then connect packet transport to
   a QEMU net backend. Start with user-mode/NAT networking for a portable proof,
   followed by an optional macOS `vmnet`/TAP bridge when LAN-level host bridging
   is required. Acceptance: the guest discovers the interface, obtains an
   address, resolves DNS, and loads a page in Safari through the host connection.

## Wi-Fi Feasibility and Implementation Path

### Verdict

Functional Wi-Fi is feasible, but it is a substantial device-emulation task.
The target is not cycle-accurate radio-frequency simulation. It is a behavioral
model that presents the SDIO/full-MAC device expected by iPod OS, synthesizes
scan and association results, and exchanges ordinary Ethernet frames with a
QEMU host network backend.

There is strong precedent, but no drop-in implementation has been identified:

- The archived [iDroid S5L8900 kernel](https://github.com/iDroid-Project/iDroid-kernel)
  is a direct hardware-reference project for this Apple generation.
- Linux's maintained
  [Marvell Libertas SDIO driver](https://github.com/torvalds/linux/blob/master/drivers/net/wireless/marvell/libertas/if_sdio.c)
  implements the SD8686-family enumeration, helper/main firmware download,
  interrupt, command, event, and packet flows. Its
  [host protocol definitions](https://github.com/torvalds/linux/blob/master/drivers/net/wireless/marvell/libertas/host.h)
  describe commands such as hardware-spec query, scan, authenticate, associate,
  key material, power save, and the Tx/Rx packet descriptors.
- QEMU explicitly separates a guest-visible device frontend from a host
  backend. Its [network model](https://www.qemu.org/docs/master/system/devices/net.html)
  already supports user-mode NAT and host network backends, and this source
  tree's `mv88w8618_eth` device demonstrates the required `NICConf`, receive
  callback, and `qemu_send_packet()` plumbing.

The Linux driver is a protocol oracle, not guaranteed proof of Apple's exact
driver behavior. The N45AP firmware contains `arm-io/sdio`, `wifiaddr`, AirPort,
and Marvell strings, but the precise card revision, CIS identity, Apple firmware
revision, and vendor extensions must be established from the running guest.
The largest uncertainty is therefore the Apple driver/firmware contract, not
QEMU's ability to carry IP packets.

### Why a host bridge alone cannot work

A QEMU network backend receives and sends Ethernet frames. iPod OS first needs
to discover a real-looking SDIO card, load or recognize its firmware, scan for
an access point, authenticate, associate, configure encryption, and receive
link events. None of those operations reach a backend with the current
`ipodtouch.sdio` stub.

The host Mac's Wi-Fi adapter also cannot simply be exposed as the guest's radio.
For a deterministic demo, QEMU should present a synthetic access point (for
example `iPod Emulator Network`) and translate its post-association traffic to
Ethernet. The backend can then use NAT or a host bridge. Thus the frontend makes
iPod OS believe it has joined Wi-Fi; the backend decides where the resulting
packets go.

### Recommended implementation stages

#### 0. Identify the exact guest contract

Add opt-in, rate-limited tracing around the existing SDIO MMIO block and boot
with Wi-Fi both disabled and enabled in Settings. Record register offsets,
command arguments, response polling, transfer sizes, GPIO/interrupt routing,
DMA addresses, power GPIOs, and reset behavior. Correlate the trace with the
kernel's AirPort/SDIO strings and disassemble only the exercised routines.

This stage should answer before implementation:

- Which S5L8900 interrupt and DMA channels serve SDIO?
- Does the Apple driver use programmed I/O, DMA, or both?
- Which CMD5/CMD3/CMD7/CMD52/CMD53 sequence and CIS IDs does it require?
- Is Marvell helper/main firmware uploaded by the guest, already running, or
  embedded in the kernel image?
- Which firmware command IDs and packet headers differ from Linux Libertas?

Do not guess these values globally. Capture them behind an opt-in trace and
remove hot-path logging from release builds once the protocol is known.

#### 1. Complete the S5L8900 SDIO host controller

Replace the current eight-register readiness stub with the minimum host
controller behavior observed in stage 0:

- command launch, completion, response types, timeout, and error status;
- block size/count, data FIFO and/or guest-memory DMA;
- interrupt status, mask, acknowledgement, and the correct SYSIC/VIC route;
- clock, bus width, card-select, and reset/power state; and
- AP-reset and sleep-domain behavior consistent with the retained-wake model.

Keep the host controller separate from the card. This prevents Apple-specific
MMIO behavior and Marvell card behavior from becoming one untestable state
machine.

#### 2. Enumerate a Marvell-compatible SDIO card

Implement the card-side state needed by the guest:

- I/O OCR and ready response (`CMD5`), RCA/select state (`CMD3`/`CMD7`);
- direct and extended I/O (`CMD52`/`CMD53`);
- CCCR, function-basic registers, CIS tuples, function enable/ready bits;
- function-one block size and card interrupt assertion; and
- stable vendor/device/revision values taken from the guest trace.

The first milestone is not Internet access: it is AirPort attaching without a
timeout and the Wi-Fi control appearing enabled in Settings.

#### 3. Model firmware boot and the full-MAC mailbox

Prefer a behavioral firmware model over emulating the proprietary Marvell
embedded processor. Accept and validate the guest's helper/main download
sequence if it uploads firmware, transition to firmware-ready state, and then
implement only commands the Apple driver actually issues. Use Linux Libertas
for structure and ordering, but make the guest trace authoritative.

Likely initial commands include hardware specification/MAC address, radio
control, channel/rate setup, scan, authenticate, associate, key material,
RSSI, multicast filter, power save, and deauthenticate. Responses must preserve
sequence numbers and generate the same command-complete/link events and SDIO
interrupt timing expected by the guest.

User-supplied Apple/Marvell firmware may be inspected or accepted at runtime,
but it must not be added to this repository or bundled without redistribution
rights. Executing its internal CPU is unnecessary unless the behavioral command
surface proves too large or fundamentally incompatible.

#### 4. Add a deterministic virtual access point

Return one configurable synthetic BSS from scan first. Implement an open
network before security, then add only the WEP/WPA command path observed from
the target iPod OS version. Association establishes a link between the emulated
full-MAC device and QEMU's Ethernet backend; actual RF modulation, beacon timing,
and host Wi-Fi password extraction are out of scope.

Transmit path:

`guest Ethernet payload -> Apple/Marvell Tx wrapper -> SDIO -> QEMU NIC queue`

Receive path:

`QEMU NIC queue -> Marvell Rx wrapper/event -> SDIO function interrupt -> guest`

#### 5. Connect the host backend

Use user-mode NAT as the first backend because it is portable and does not need
privileged host network configuration. Rebuild with libslirp enabled, add a NIC
property/netdev link to the iPod machine, and expose an opt-in launcher setting.
QEMU's user backend supplies DHCP and DNS, but the guest still runs its own
normal DHCP client over the emulated Wi-Fi link.

The current QEMU 6.2-derived tree has no `vmnet` backend. Upstream QEMU documents
`vmnet-shared` and `vmnet-bridged` as available
[since QEMU 7.1](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html).
True macOS bridging therefore requires either a carefully isolated vmnet
backport, a later-QEMU migration, or a separately installed TAP-style backend.
Do that only after NAT works; it changes host integration, not SDIO correctness.

#### 6. Integrate power management and regressions

Wi-Fi adds another device to the power lifecycle that caused the sleep bug.
Model radio power-off, SDIO interrupt quiescence, volatile firmware/card state,
AP-domain reset, and the guest's resume/reload sequence. Manual and timed sleep
must remain identical, and a disconnected backend must never block the vCPU or
delay OOCSHDWN.

### Milestones and acceptance

| Milestone | Observable acceptance |
|---|---|
| A. Host controller | No unknown SDIO MMIO; commands complete; IRQs acknowledge without storms |
| B. Card enumeration | Native AirPort driver attaches and Settings can power Wi-Fi on/off |
| C. Firmware mailbox | MAC address query succeeds; no firmware-download or command timeout |
| D. Scan | Synthetic SSID appears repeatedly with stable signal/channel metadata |
| E. Association | Guest shows the Wi-Fi icon and remains associated |
| F. Network transport | Guest receives DHCP, resolves DNS, and reaches a host-local HTTP test page |
| G. Internet demo | Safari loads compatible HTTP content; legacy TLS/certificate limitations are reported separately from networking |
| H. Power lifecycle | Wi-Fi works after cold boot and retained wake; sleep latency and RAM-retention checks do not regress |

### Main risks and explicit non-goals

- **Apple protocol divergence:** vendor commands or packet layouts may differ
  from upstream Libertas. Mitigation: trace the guest and implement incrementally.
- **Firmware/legal constraints:** binary firmware may be proprietary. Keep it
  user-supplied and avoid depending on redistribution.
- **Old web compatibility:** successful IP networking does not make 2007 Safari
  understand modern TLS, certificates, JavaScript, or websites. Test a simple
  host-local HTTP page first.
- **Bridge complexity:** macOS bridging is a later backend choice, not a shortcut
  to card emulation.
- **Non-goals:** RF/baseband accuracy, host SSID passthrough, monitor mode, and
  executing the Marvell firmware CPU are not required for the iPod OS demo.

## Performance Optimization Plan

The original two visible performance problems had different causes and must
continue to be measured separately:

- Display refresh was explicitly capped at **10 Hz** by
  `LCD_REFRESH_RATE_FREQUENCY` in `include/hw/arm/ipod_touch_lcd.h`. The CLCD
  interrupt mask/status model has now been corrected and the cadence raised to
  60 Hz; manual release testing confirms a large visible responsiveness gain.
  This was an emulator limitation, not an M2 hardware limit.
- CPU/device speed is dominated by single-vCPU TCG translation and emulated
  device polling. The current development build also enables assertions,
  diagnostic logging, and a debug-oriented configuration. One emulated CPU
  mainly uses one host core, so additional host cores do not directly improve
  guest execution.

Optimization must not hide correctness bugs. Every stage below keeps manual
and timed sleep on the same guest-owned OOCSHDWN/type-4 path and reruns the
sleep/wake acceptance checks.

**Prerequisite-zero update:** the packaged build now passes manual slide to
unlock nearly every time once its very slow resumed UI becomes responsive.
That is sufficient to begin priorities 1-2 because performance is itself
obscuring wake validation. Repeat-cycle reliability, foreground ownership,
and the Phase 13 display artifacts remain regression gates rather than reasons
to postpone the first measured display-speed pass.

### Recommended order

| Priority | Change | Why this order | Measurement / acceptance |
|---|---|---|---|
| 1 | Correct CLCD interrupt cadence/acknowledgement, then raise 10 Hz to the hardware's 59.977 Hz (**implemented at 60 Hz; boot/display/retained-wake pass, manual touch regression pending**) | Removes the artificial UI cap without creating an interrupt storm | Scrolling/animation can present up to 60 frames/s; no `unexpected CLCD interrupt`, kernel panic, accelerated guest timers, or input regression |
| 2 | Redraw only on dirty framebuffer/display state (**implemented; cold boot and retained-wake smoke test pass**) | A blind high-rate full redraw would waste the same host core needed by TCG | Idle display avoids full-frame conversion; changed regions appear on the next presentation tick; no stale frames |
| 3 | Use the real `arm1176` CPU model as the default performance baseline (**implemented; cold boot and release benchmark pass; `max` A/B still pending**) | `-cpu max` overrides the board default with a heavier and less representative execution target; the device used an ARM11-class S5L8900 | Cold boot, launch, scrolling, and sleep/wake pass with `arm1176`; compare guest-time/host-time ratio against `max` |
| 4 | Produce a release build and remove hot-path diagnostics (**implemented and measured**) | Assertions and `-d unimp`/MMIO/IRQ/frame logging distort timing and add I/O overhead | Build with optimization (target O3/LTO if supported), no `-d unimp` in the normal launcher, and no repetitive hot-path prints; retain an opt-in trace build |
| 5 | Stop executing the terminal `b .` after OOCSHDWN | The sleeping CPU currently burns one host core even though real AP power is off | Near-zero QEMU CPU use while asleep; P/H still initiates the retained AP reset and type-4 handoff; RAM CRC stays stable |
| 6 | Profile an awake workload and fix the largest emulated-device polling loops | Overall slowness cannot be attributed safely without sampling a representative boot/UI trace | Record boot-to-SpringBoard time, app-launch latency, scrolling frame rate, vCPU samples, and top MMIO addresses before each change; improve one identified hotspot at a time |
| 7 | Evaluate a newer QEMU/TCG base and safe translation settings | This is higher-risk and should follow local hot-path fixes so behavior changes remain attributable | Same firmware and acceptance suite, with repeatable speedup and no boot, NAND, touch, display, or resume regression |
| 8 | Reassess host hardware only after the software baseline is optimized | Buying a faster Mac cannot remove the 10 Hz cap or pathological polling | Run the same release benchmark on M2 and candidate Macs; use single-core improvement, not total core count, as the primary predictor |

### Benchmark protocol

Use one reproducible release configuration and record at least three runs of
each result:

1. Host seconds from QEMU start to usable SpringBoard.
2. Host seconds to open Safari and display its first complete frame.
3. Presented frames per second during a fixed scroll/animation gesture.
4. QEMU CPU percentage while idle awake, actively scrolling, and fully asleep.
5. Host-to-guest time ratio over a fixed 60-second guest interval.
6. Manual and timed sleep results: black panel, stable retained-RAM checksum,
   type-4 `System Wake`, foreground application survival, and immediately
   working touch.

### First optimization experiment: CLCD rate versus dirty presentation

The first A/B test separated two behaviors that the original plan treated as
one:

- iBoot reports the physical panel target as `fps set to: 59.977`, confirming
  that 10 Hz is not hardware-accurate.
- Directly changing `LCD_REFRESH_RATE_FREQUENCY` from 10 to 60 also changes the
  guest interrupt cadence. The first clean-NAND 60 Hz boot panicked at caller
  `0xc012d73f` during early driver configuration. This single result is
  provisional rather than a fully reproduced root cause, but it blocks
  shipping the direct constant change.
- The guest prints `unexpected CLCD interrupt: 00000001` even at 10 Hz. The
  next rate experiment must trace render writes, IRQ raise/lower/acknowledge,
  and the guest handler before attempting 60 Hz again.
- Removing the unconditional `lcd->invalidate = 1` makes QEMU use RAM dirty
  tracking instead of converting and presenting all 320x480 pixels on every
  display callback. A 10 Hz build with this change booted through SpringBoard,
  produced a correct complete framebuffer, reached timed `OOCSHDWN`, resumed
  through the retained LPDDR/type-4 path, logged `System Wake`, and reloaded
  the multitouch firmware without a kernel or host crash.

The immediate safe checkpoint is therefore dirty-only redraw at the existing
guest IRQ cadence. The next display-speed step is correcting CLCD interrupt
semantics, not forcing another rate value.

### CLCD protocol trace and corrected interrupt model (2026-07-17)

The next experiment instrumented the low CLCD registers and timer IRQ while
leaving the rate at 10 Hz. The guest produced this stable sequence once
`AppleH1CLCD` and its framebuffer user client started:

```text
W +0x018 <- 0x00000001
W +0x014 <- 0x00003f01
tick: IRQ raised
R +0x018 -> 0x00000001
R +0x018 -> 0x00000001
W +0x018 <- 0x00000001
```

The sequence repeats once per frame. Later the guest disables bit zero:

```text
W +0x014 <- 0x00003f00
tick: QEMU still raised IRQ
R +0x018 -> 0x00000001
unexpected CLCD interrupt: 00000001
W +0x018 <- 0x000000ff
```

This identifies register `0x14` as the interrupt mask/enable register and
`0x18` as interrupt status with write-one-to-clear behavior. The old model had
named them `unknown1` and `render`, stored status acknowledgements as a
persistent render state, and raised on every timer tick whenever that stored
value was one. It did not consult the mask. The final interrupt after
`0x3f01 -> 0x3f00` therefore came from QEMU, not the guest, and increasing the
timer to 60 Hz multiplied the invalid delivery rate.

The corrected 10 Hz model now:

1. latches frame status bit zero on the timer tick;
2. asserts the IRQ only when a latched status bit is enabled by the mask;
3. clears selected status bits when the guest writes ones to `0x18`; and
4. recomputes the level immediately after mask or status changes.

A boot using the corrected model reached the Darwin kernel, downloaded the Z2
firmware, and configured SpringBoard without an `unexpected CLCD interrupt`
or kernel panic. This is the first checkpoint; the rate deliberately remains
10 Hz until a separate 60 Hz boot and sleep/wake regression test passes.

**Path taken:** trace the actual iPod OS register protocol, infer semantics
from ordering and IRQ response, correct the register model at the old rate,
then retest 60 Hz independently.

**Paths not taken:**

- The trace-only instrumentation was removed after collecting the sequence;
  retaining a branch and formatted logging in CLCD MMIO would work against the
  performance goal.
- The previous direct `10 -> 60` constant change remains rejected because it
  changed interrupt frequency before the interrupt protocol was correct.
- Host-only SDL repainting cannot make the guest render more often when its
  frame-completion interrupt is still capped, so presentation polling alone
  is not treated as the fix.
- [openiBoot's S5L8900 LCD source](https://github.com/iDroid-Project/openiBoot/blob/master/plat-s5l8900/lcd.c)
  confirms the `0x38900000` controller layout and 59.977 Hz timing setup but
  does not implement the iPod OS CLCD interrupt handler or name `0x14/0x18`;
  it was useful corroboration, not the basis for inventing register semantics.

### 60 Hz validation after the interrupt fix (2026-07-17)

Changing `LCD_REFRESH_RATE_FREQUENCY` from 10 to 60 was retried as its own
step after committing the interrupt model. Unlike the earlier direct-rate
experiment, a fresh-NAND boot passed the former early-driver panic location,
loaded the multitouch firmware, and configured SpringBoard. The serial log
contained neither `unexpected CLCD interrupt` nor a kernel panic. A visible
SDL run showed the complete SpringBoard framebuffer rather than a black or
partially scanned-out frame.

The same SDL process then exercised retained sleep/wake at 60 Hz:

1. A monitor-injected Power key entered `System Sleep` and reached
   `OOCSHDWN=0x02`.
2. Because QEMU `sendkey p` injects press and release as one short pulse, the
   release was queued during the sleep transition and caused an immediate
   retained wake. This is an input-injection artifact already distinct from a
   physical/manual held button, not a reason to change the power model.
3. The guest subsequently entered timed sleep and remained at terminal
   OOCSHDWN with the panel completely black.
4. An injected Home key started the retained AP reboot. LPDDR CRC32C was
   identical before and at reset, iBoot consumed the type-4 token, the guest
   logged `System Wake`, Z2 firmware reloaded, and the lock screen became
   visible.

The core 60 Hz gate therefore passes: boot, scanout, interrupt masking,
terminal sleep, retained memory, Home wake, and resumed display are intact.

**Still pending:** automated slide-to-unlock was not accepted as evidence.
The resumed lock-screen pixels become visible before the slow retained
HID/SpringBoard consumer is necessarily ready. The test drag produced no
emulated absolute-touch callback, and the device then started its next timed
sleep: input is deliberately blocked for several seconds during that sleep
transition while the display fades. The attempted drag therefore landed in an
invalid timing window and says nothing conclusive about touch at 60 Hz.
Additionally, QEMU HMP `mouse_move` only queues relative axes and cannot drive
this absolute touchscreen. Manual drag testing in the packaged release build,
after waiting for resumed input to become active but before the next sleep
transition, remains required before declaring the complete input regression
gate closed.

**Paths not taken:**

- Do not weaken the CLCD mask/status model merely to make 60 Hz boot; the
  corrected model already passes at the target cadence.
- Do not treat the monitor Power pulse's immediate wake as a timed-sleep
  failure; stable timed OOCSHDWN was tested separately in the same process.
- Do not add a relative-to-absolute coordinate workaround or change sleep
  input blocking for test automation. QMP absolute input with a readiness
  signal, or a correctly timed manual SDL drag, is the proper follow-up;
  production input and power semantics should not be changed for the harness.

### Release-build benchmark (2026-07-17)

The development and release binaries were built from revision `81d477c5d4`.
The existing development configuration is `-O2` with debug information, no
LTO, and assertions enabled. The release configuration is `-O3`, LTO enabled,
debug information disabled, and assertions still enabled. QEMU 6.2 explicitly
rejects compiling this tree with `NDEBUG`, so disabling assertions is not a
valid optimization without first auditing and changing that upstream design
constraint. Binary size fell from 15 MB to 13 MB.

The test alternated debug and release boots. Each run used the machine's
ARM1176 default, `-display none`, serial output captured to a file, and a fresh
APFS copy-on-write clone of the same 521 MB NAND tree. Clone time was excluded.
The process was stopped when the serial log reached SpringBoard's
`Configuring SpringBoard for N45AP` line. This isolates boot and emulated-device
execution from SDL presentation; it does not measure visible frame rate.

| Milestone | Debug runs (s) | Release runs (s) | Debug mean / median | Release mean / median | Mean / median improvement |
|---|---:|---:|---:|---:|---:|
| Darwin kernel banner | 4.219, 7.005, 5.924 | 4.228, 5.932, 5.011 | 5.716 / 5.924 | 5.057 / 5.011 | 11.5% / 15.4% |
| Multitouch firmware downloaded | 5.601, 8.983, 8.101 | 5.572, 7.521, 6.403 | 7.562 / 8.101 | 6.499 / 6.403 | 14.1% / 21.0% |
| SpringBoard configured | 10.090, 16.778, 16.194 | 11.481, 15.620, 11.623 | 14.354 / 16.194 | 12.908 / 11.623 | 10.1% / 28.2% |

The release build is faster overall, but three trials are not enough to claim
that the best 28.2% median figure will hold for every launch. Pairwise
SpringBoard results ranged from release being 13.8% slower to being 28.2%
faster, showing meaningful host/filesystem scheduling noise. The conservative
result is approximately **10-15% lower mean boot time at the early milestones**,
with a larger possible gain later in boot. Future comparisons should retain
at least three alternating runs and report both mean, median, and raw values.

The same build also cold-booted through SpringBoard with the machine's default
ARM1176 model after removing the launcher's explicit `-cpu max`. This validates
the accurate CPU as a functional baseline but does not yet prove it faster;
matched host-time benchmarks remain required. The normal packaged launcher
can safely omit `-d unimp` and route serial output to `null`, with the old
verbose behavior retained behind `IPOD_TOUCH_DEBUG=1`.

The current recommendation is therefore to keep the M2 as the development
baseline. It should be capable of a much better result than the current build;
the explicit refresh cap, debug overhead, sleeping busy-loop, and any awake
polling hotspots must be removed or measured before declaring host hardware the
limiting factor. A faster single-core Mac may improve TCG throughput, but it
cannot by itself make the emulation accurate or guarantee original-hardware
speed.

## iPhone OS 1.0 / Original iPhone Feasibility

An original-iPhone (M68AP) variant is feasible as a follow-on machine because
it shares the S5L8900 generation and much of the current PMU, LCD, SPI,
multitouch, NAND, and retained-resume work. It is not a firmware-only rename of
the N45AP iPod touch machine. A useful demo requires:

1. A separate `iPhone-2G`/M68AP machine definition and device-tree identity.
2. User-supplied matching boot ROM, NOR/LLB/iBoot, kernel cache, and root
   filesystem artifacts.
3. M68-specific GPIO/button, camera, USB, and baseband-facing stubs sufficient
   for iPhone OS 1.0 to finish booting.
4. Validation of its PMU wake token and type-4 path against the retained-resume
   model proven here.

Telephony does not need to be fully emulated for a UI/demo target; a controlled
"no service" baseband stub is a reasonable first milestone. The largest
current blocker to an exact first-stage boot remains the matching LLB/artifact
set, not the retained-kernel mechanism.

## Phase 16: Remove wake-only display artifacts and diagnostic latency (2026-07-17)

Manual testing of the optimized release exposed a repeatable retained-wake
sequence: an empty-battery screen, a very brief old SpringBoard frame, and
finally the correct slide-to-unlock screen. Sleep entry could also take ten or
more host seconds. These were not three renderings of one guest state; they
came from three different layers of the emulation.

### Root causes

1. During type-4 wake, the PMU returned `MBCS1=0x01`. PCF50633 bit zero means
   `USBPRES`, while bit one means `USBOK`. Reporting present without OK told
   iBoot that a USB source existed but was not valid. The serial trace
   confirmed `power supply type usb host`, battery-artwork resource loading,
   and a 3800 mV ADC result.
2. At iBoot's `RESUME_STATUS=0x40` handoff, QEMU explicitly called
   `ipod_touch_lcd_resume_scanout()`. That helper replaced iBoot's CLCD base
   with the brightest pre-sleep OS triple buffer and immediately powered the
   panel on. The brief SpringBoard image was therefore an emulator-created
   stale frame, not SpringBoard resuming and changing its mind.
3. The wake key synchronously computed CRC32C over all 128 MiB of LPDDR, then
   the reset callback computed the same full CRC again. A macOS sample caught
   `ipod_touch_cpu_reset()` in that scan. This validation code was on every
   normal wake and materially extended wake latency.
4. Supporting the stale-frame helper required scanning two OS framebuffers on
   a coarse brightness grid every 60 Hz display refresh. Those repeated
   `cpu_physical_memory_read()` calls competed with the single TCG vCPU and
   slowed guest-driven display fade and driver shutdown.

The PCF50633 bit interpretation is corroborated by the historical Linux
driver definitions: `USBPRES=0x01` and `USBOK=0x02`. The emulator now reports
the coherent combination during iBoot's retained verification phase rather
than synthesizing an invalid source.

### Implemented power/display behavior

- OOCSHDWN still leaves the PMU-owned LCD panel rail off.
- The retained AP reset reloads volatile boot memory but no longer turns that
  panel rail on. iBoot's own log says `displayEnabled: 0`, so its temporary
  `0x0fe00000` scanout remains correctly invisible.
- QEMU no longer chooses or restores an old OS framebuffer. The panel becomes
  visible only after the retained kernel programs one of its OS CLCD bases
  (`0x0f400000` or `0x0f496000`). The buffer contents and lock-screen decision
  therefore remain guest-owned.
- A dedicated `retained_wake_pending` flag now carries reset semantics. The
  full 128 MiB before/after CRC is available only when launching with
  `IPOD_TOUCH_VALIDATE_RETAINED_RAM=1`; it is deliberately not part of normal
  wake timing.
- The continuous brightest-buffer scan was removed. The short startup
  visibility check used to gate premature touch input remains separate.

This is closer to the physical lifecycle than the prior workaround: the panel
rail stays off across the bootloader phase and the retained kernel, not QEMU,
selects the first visible OS framebuffer. It does not yet model the Merlot
panel command stream in enough detail to derive rail state from the exact SPI
sleep-out/display-on command, so recognizing the resumed kernel's CLCD base is
still a device-level approximation.

### Validation and measured effect

A release build booted from the existing disposable NAND and exercised both
manual and timed OOCSHDWN. Manual Power reached terminal OOCSHDWN within the
next five-second observation interval, versus the earlier roughly fifteen
seconds. Home requested the AP reset immediately without a checksum pause.
iBoot consumed the type-4 token, the retained kernel logged `System Wake`,
the Z2 firmware reloaded, and CLCD returned through an OS base.

Two screen captures bracketed the second, timed-sleep wake:

1. immediately after Home: completely black;
2. after the retained kernel CLCD write: the complete slide-to-unlock screen.

No battery artwork or old SpringBoard framebuffer was visible in either
phase. The guest subsequently returned to timed OOCSHDWN through the same PMU
path. Manual touchscreen dragging still requires validation in the packaged
SDL app because HMP only exposes relative mouse motion, but no touch protocol
or readiness gating was changed in this phase.

### Paths rejected or retained only for diagnostics

- Restoring the brightest retained framebuffer was removed, not retimed. It
  caused the SpringBoard flash and overrode the guest's buffer ownership.
- Copying a saved screenshot into all triple buffers remains a historical
  abandoned approach; it is not used by the current AP-reset/type-4 path.
- Turning the panel on at AP reset would merely reveal iBoot's temporary
  battery/logo scanout again and contradict `displayEnabled: 0`.
- Removing the retained-RAM checksum entirely would lose a useful regression
  tool. Making it explicit keeps the strong validation while keeping normal
  wake latency honest.
- Returning `MBCS1=0` was not chosen because earlier type-4 experiments found
  that iBoot needs a valid power-on source while checking the retained image.
  The present-and-OK state corrects the inconsistent model without changing
  that proven boot precondition.

## Phase 17: Whole-machine workaround retirement and cleanup plan (2026-07-17)

The sleep investigation accumulated code that was useful evidence at the time
but is no longer on the retained-reset path. This matters outside sleep too:
forced interrupt delivery, guest-memory patching, periodic framebuffer reads,
and leaked command buffers can distort timing and make unrelated device models
look slower or less reliable than they are.

Git archaeology gives the following ownership and rationale. The purpose of
this table is to make deletion reviewable rather than treating old experiments
as unexplained dead code.

| Code family | Introduced by / original rationale | Current status | Retirement gate |
|---|---|---|---|
| Sleep-function patch at `0x0805a6cc` plus delayed VIC/timer cleanup | `bbe67d01de` (`Fix reliable display sleep and input wake`): make the terminal `B .` return, forcibly reopen IRQ/FIQ delivery, restart the tick timer, and restore a framebuffer | Superseded by `d1f48701c0` and `01f12e434a`, which model AP power loss and start iBoot's retained type-4 path; returning from the kernel sleep function is now specifically incorrect | Remove together with its timer and PMU state, then prove OOCSHDWN still leads to retained reset rather than stack unwind |
| Forty wake-assist IRQ/FIQ pulses and guest PMU sub-IRQ descriptor writes at `0xe0248aa0` | `bbe67d01de`: break the IRQ-masked-idle deadlock created by returning from sleep without a hardware power cycle | No longer called by the normal terminal or transition wake paths; it can corrupt guest critical sections if accidentally re-entered | Remove and verify manual/timed wake, scheduler progress, and touch without modifying CPSR or guest kernel memory |
| SYSIC deferred PMU clear/reinject timers and `int2_shadow` | `bbe67d01de`: clear a stuck level IRQ, then synthesize ONKEY later so the old resumed kernel workqueue could run | The activating helper has no current caller. `3f558b79f9` and `738bae5cb8` instead retain the real PMU event byte across AP reset and re-expose it to the retained kernel | Remove as one family; retain `retained_int2_wake` and validate the ApplePCF50635 event seen after type-4 handoff |
| Full framebuffer snapshot copied into all three scanout buffers | `bbe67d01de`: make the screen visible after the incorrect stack-unwind resume; `d1f48701c0` later stopped using it for retained reset but kept the snapshot as a touch-readiness gate | Restoring it is obsolete and caused stale-image ownership problems. The remaining 600 KiB copy is serving only as an indirect “first useful frame seen” flag | Replace with a small explicit input-readiness state; do not write guest framebuffers from QEMU |
| Dead multitouch wake flags (`display_sleep_requested`, `alternate_wake_via_power`, `swallow_wake_touch`, `wake_unwind_active`) | `bbe67d01de`/`d1f48701c0`: coordinate host blanking, synthetic Home-as-ONKEY, and the old stack unwind | Never armed by the current retained-reset path | Remove after confirming Power/Home release suppression remains intact |
| Disabled multi-page kernel/stack/LCD debug dump in the button handler | `bbe67d01de`: document the reverse-engineering state at the terminal sleep loop | The findings belong in this document; thousands of disabled source lines are not a maintainable diagnostic interface | Delete; recreate future targeted probes behind small named opt-in diagnostics if needed |

The later commits did not make the earlier work pointless. The false-positive
wake implementations established several facts used by the current design:
OOCSHDWN is terminal, IRQ injection cannot wake a powered-off AP, the PMU event
must survive reset, volatile iBoot state must not survive, and framebuffer
visibility is not evidence that touchscreen state resumed correctly. The
cleanup removes the experimental mechanism while retaining those conclusions.

### Cleanup order

1. Remove the superseded sleep trampoline, CPSR forcing, wake-assist timer,
   SYSIC reinjection timers, and disabled debug dump as one behaviorally dead
   family.
2. Replace the framebuffer snapshot with an explicit lightweight input-ready
   gate, preserving the cold-boot delay without copying or restoring pixels.
3. Remove unreachable wake-era state fields and audit every remaining host
   write to guest CPU registers or RAM.
4. Fix low-risk, whole-machine correctness issues already exposed by the
   audit: multitouch command-buffer ownership, the oversized `MTFrame`
   allocation, ADM transfer allocations, AES error-path ownership, and the
   8900 image decoder's assignment-in-condition/magic-check defects.
5. Establish measurements before behavior-changing optimization: touch report
   cadence, timer/clock ratios, NAND page-file I/O, and main-thread stalls.
6. Only then change the 10 Hz touch movement cadence, timer model, or NAND
   representation. Each can affect guest timeouts or persistent data and needs
   its own measured commit and rollback point.

### Whole-machine audit register

Not every shortcut is removable debugging code. The SDIO register stub, USB
FIFO gaps, GPIO skeleton, accelerometer identity stub, unconditional NAND ECC
success, minimal LCD-panel identity, TV-out/MBX magic values, direct iBoot/LLB
loading, and hard-coded image keys are deliberate incomplete device models.
They should be replaced by implemented hardware semantics, not simply deleted.

The highest-value non-sleep cleanup targets found so far are:

- multitouch movement frames are emitted at a hard-coded 10 Hz, independently
  of the now-60 Hz CLCD presentation cadence;
- normal multitouch commands replace heap buffers without consistently freeing
  the previous command, and `MTFrame` is overallocated by pointer size;
- ADM allocates temporary scalar/page buffers for every NAND transfer without
  freeing them;
- the 8900 decoder validates magic bytes with `&&` instead of rejecting any
  mismatch and assigns `0x04` in an encryption test instead of comparing it;
- the NAND backend performs synchronous per-page filesystem I/O and writes
  `_new.page` files without a complete overlay/readback contract;
- timer frequencies, minimum counts, and latch values contain functional
  constants that can change perceived device speed and sleep duration.

The first four are bounded cleanup work. NAND and timer changes are separate
design projects because they can alter storage or guest-visible time. This
register is the baseline for documenting paths taken and deliberately untaken
as cleanup proceeds.

### First cleanup tranche and validation

The first tranche removed the complete superseded stack-unwind family:

- the guest sleep-function instruction patch and its delayed VIC/timer repair;
- forty forced CPSR IRQ/FIQ pulses and direct writes into the guest PMU
  sub-interrupt descriptor;
- SYSIC PMU clear/reinject timers and the synthetic `int2_shadow` path;
- the disabled kernel/page-table/stack/framebuffer dump in the key handler;
- dead wake-unwind multitouch flags; and
- the framebuffer snapshot/restore buffer.

The former snapshot's only live consumer was an early-touch guard. It is now a
small `input_ready` state that becomes true after a useful OS framebuffer has
remained visible for two seconds. It samples only until ready and never copies
or writes guest framebuffer memory. The retained path keeps this already-proven
driver readiness across AP reset, matching the behavior before cleanup.

The same tranche corrected two independent ownership/correctness findings:
normal multitouch commands now release the previous command buffers before
allocating replacements, `MTFrame` allocates its actual object size, and the
8900 decoder now rejects any bad magic byte, compares rather than assigns the
encryption marker, leaves plaintext payloads alone, validates AES block length,
and frees every temporary buffer. These changes removed all of the decoder's
non-deprecation compiler warnings.

An optimized release build completed successfully. A headless native run then
validated cold boot, manual Power sleep, and Home wake:

1. iPod OS disabled LCD, multitouch, USB, SDIO, and other drivers and wrote
   `OOCSHDWN=0x02`;
2. QEMU left the application processor at terminal power loss;
3. Home requested the retained-RAM SoC reset;
4. iBoot consumed the type-4 state and handed off to retained memory;
5. the kernel logged `System Wake`;
6. the retained kernel enabled CLCD at `0x0f496000`; and
7. Z2 re-enabled, downloaded calibration, and reloaded 49,128 bytes of
   firmware.

No deleted mechanism was needed for this sequence. VIC priority reset remains
because it is part of the modeled volatile AP interrupt-controller domain, and
the retained PMU event byte remains because the always-on PMU must preserve the
wake cause across reset.

Two paths were deliberately not changed in this tranche. Sleep entry was still
slow in the headless run, so timer/clock ratios and blocking device shutdown
remain measurement targets rather than being papered over with a host shortcut.
The NAND backend was also left intact: attempting an additional disposable
page-directory copy exhausted available filesystem resources, reinforcing the
packed-base/overlay priority, but storage semantics must be redesigned in a
separate commit rather than mixed into wake cleanup.

### Second cleanup tranche: ADM and AES ownership

Git blame traced the ADM allocations to the original 2022 NAND bring-up
(`7ebda9567e`, `19014a5ea2`, `55b2ddb796`, `d9380bddc9`, and
`0b241ee0b6`). They were temporary transfer buffers, not required persistent
device state. Every ADM startup or NAND command leaked several allocations;
multi-page reads additionally leaked one 12-byte buffer per page and copied
eleven uninitialized bytes from each buffer into guest completion records.

The ADM path now uses bounded stack scalars and arrays, named native/big-endian
read helpers, and one initialized completion record. It retains the byte order
established by the working driver path, bounds guest page counts against the
512-entry NAND queues, and removes all per-command host allocations. This does
not change the page-per-file NAND backend or its persistence semantics.

The AES GID-not-implemented path previously allocated its full input buffer and
returned without freeing it. Key type validation now occurs before allocation,
including rejection of unknown key selectors. Supported UID/custom operations
retain their existing buffer lifecycle and crypto behavior. Migrating from the
deprecated low-level OpenSSL AES API remains separate modernization work.

An optimized release build completed with only the known OpenSSL 3 deprecation
warnings. One headless run exercised two retained cycles:

1. timed sleep followed by Power wake; and
2. manual Power sleep followed by Home wake.

Both cycles logged `System Wake`, restored the retained kernel's CLCD scanout,
re-enabled Z2, loaded built-in calibration, and downloaded the 49,128-byte
firmware without a panic or data abort. This closes the bounded allocation
cleanup item. The next work is measurement of sleep-entry stalls, touch report
cadence, and timer/clock ratios before changing guest-visible timing.

## Phase 18: Model the Merlot panel sleep boundary (2026-07-17)

The remaining status-bar flash during sleep was not caused by a slow PMU
power-off or by the retained-reset code. It was a missing state transition in
the SPI LCD-panel peripheral.

### Measurement path and traps

A macOS sample taken during the slow transition showed the main QEMU thread
almost entirely blocked in `select`, while the TCG vCPU spent nearly every
sample in one guest timer-reading loop. This rules out SDL redraw and host
main-thread saturation as the direct cause. It also does not prove that the
S5L8900 timer runs at the wrong rate: git history traced the counter's `/ 2`
to upstream commit `0b62d22a3c` (`Fixed kernel timer`), whose explicit purpose
was to expose the kernel's half-clock timer. Removing it would make all guest
time run twice as fast and was rejected.

Three timing attempts produced useful harness corrections:

1. A fixed 25-second button press landed during boot and was queued as a
   shutdown-transition wake. It did not measure normal Power sleep.
2. SpringBoard's serial `Activated` message preceded the visible OS CLCD
   buffer by up to ten seconds on the page-file NAND. Serial readiness is not
   display readiness.
3. HMP's default synthetic key press lasted only about 11 ms. With an explicit
   200 ms hold, the guest followed the same prompt Power path seen with a
   physical SDL key.

The final harness waited until CLCD had scanned a bright OS framebuffer for
two seconds, held Power for 200 ms, sampled the active framebuffer through
QMP, and timestamped guest driver and PMU messages. The main content became
black after 1.013 seconds, but status-bar pixels remained in the active RAM
buffer until terminal shutdown. `OOCSHDWN` arrived 21.272 seconds after the
button. That long tail is guest-owned driver shutdown, not the time the
physical display should remain visible.

An opt-in, temporary trace of SPI1 then captured the decisive sequence:

```text
+0.000 s  Power press
+0.417 s  Merlot register writes
+0.510 s  SPI command 0x10
+21.272 s OOCSHDWN
```

`0x10` is the MIPI DCS **Sleep In** command. The historical panel stub
recognized only the ID/status reads `0x95` and `0xDA`-`0xDC`; it silently
discarded Sleep In. QEMU therefore kept scanning RAM for another twenty
seconds even though the guest had already told the physical panel to stop.
That is the exact cause of the leaked status bar.

### Implemented behavior

SPI1 now retains its Merlot peripheral instance, and the board connects that
peripheral to the CLCD display state. When the guest sends an unambiguous
command-byte `0x10`, the panel becomes non-visible and invalidates the host
surface immediately. This does not suspend QEMU, alter the framebuffer, skip
driver shutdown, accelerate guest time, or trigger the AP reset. The PMU still
owns the later `OOCSHDWN` power loss.

Wake continues to use the existing retained lifecycle. iBoot itself sent
another `0x10` while quiescing the display, `System Wake` followed, and only
the retained kernel's OS CLCD setup made the panel visible again. A full
manual cycle measured:

| Milestone | Host time relative to Power |
|---|---:|
| Merlot Sleep In / completely black output | 0.596 s |
| PMU `OOCSHDWN` | 21.065 s |
| Home wake requested (one second later) | 22.104 s |
| Retained `System Wake` | 33.659 s |
| Kernel OS scanout visible | 34.338 s |
| Z2 49,128-byte firmware reload complete | 34.442 s |

An untouched timer-sleep run used the identical boundary: Merlot Sleep In at
73.370 seconds, then OOCSHDWN 20.500 seconds later. Thus manual and timed
sleep now go fully black at the guest's panel command while retaining their
shared, guest-owned PMU shutdown path.

The temporary byte-by-byte panel trace was removed after identifying the
command. The normal build keeps only one state-change message, avoiding a new
high-volume diagnostic path.

### Next performance work

This result narrows the remaining performance plan:

1. Do not optimize away the roughly twenty-second post-panel shutdown tail;
   it is no longer visible and is part of the OS power sequence.
2. Measure and then raise the hard-coded 10 Hz multitouch motion cadence,
   correcting the Y-delta calculation in the same isolated change.
3. Replace synchronous per-page NAND filesystem access with a packed,
   read-only base plus a small writable overlay. Cold-boot tests varied from
   roughly twelve seconds to more than a minute depending on page-directory
   state, making NAND the strongest remaining whole-machine latency source.
4. Revisit timer and clock ratios only with guest-time acceptance tests. The
   existing half-clock counter has upstream rationale and must not be used as
   a global speed knob.

## Phase 19: Raise touch motion cadence without losing protocol frames (2026-07-17)

The perceived 10 Hz touch response was literal: the original Z2 model created
one `TOUCH_MOVED` frame every 100 ms, independently of the 60 Hz CLCD. Simply
changing the divisor would have made an existing ownership bug six times more
likely. Every timer tick assigned a new allocation to `next_frame`, even when
the guest had not consumed the previous frame or had already received its EB
length header.

The queue now distinguishes the frame currently advertised to the guest from
one deferred frame. Repeated motion is coalesced to the newest coordinates,
but it cannot replace a packet whose length transaction has begun or replace
a touch-release boundary. A frame is consumed as soon as its bytes have been
copied into the SPI response buffer; any deferred frame is then promoted and
generates a fresh ATN edge. `TOUCH_FULL_END` is scheduled only after the guest
consumes `TOUCH_ENDED`, preventing a slow guest from losing the release frame.
Reset frees both queue positions.

Motion reports now run at 60 Hz. The same change corrects two velocity defects
from the 2022 scrolling implementation: Y velocity now subtracts the previous
Y coordinate instead of X, and multiplication occurs before millisecond
division so normal velocities do not truncate to zero. Zero-time deltas are
bounded and results are clamped to the signed 16-bit wire fields. Emitting a
frame advances the previous coordinate, so stationary periodic reports have
zero velocity.

### Validation

The optimized release build completed successfully. An SDL/QMP harness waited
for the normal two-second input-ready gate, then drove a bottom-screen absolute
drag from `(0.180, 0.140)` to `(0.860, 0.140)` over 0.705 seconds at 60 Hz.
The press and release reached the Z2 model with the correct coordinates, no
unknown command, panic, or data abort occurred, and a screenshot showed an
intact SpringBoard.

The higher-risk retained test also passed:

1. Power sent Merlot Sleep In and the guest reached OOCSHDWN.
2. Home requested the retained reset and the kernel logged `System Wake`.
3. Z2 reloaded its 49,128-byte firmware.
4. One second later, the same 60 Hz drag completed in 0.702 seconds with the
   correct down/up coordinates.
5. The post-drag screenshot again showed an intact, responsive SpringBoard.

This validates cold-boot and post-wake input transport. Manual slider feel in
the packaged app remains the user-facing acceptance test; the automation
proves frame cadence, coordinate delivery, release preservation, and retained
driver recovery rather than inferring unlock state from a screenshot.

## Phase 20: Stop applying the host timezone twice (2026-07-17)

Manual testing found that date and minutes matched the host, but selecting the
Paris timezone left the displayed hour exactly two hours ahead during summer.
The PMU RTC implementation called `localtime()` and exposed the host's CEST
wall clock. iPod OS correctly treated the hardware RTC as UTC and then applied
its selected `Europe/Paris` offset, adding the same two hours a second time.

The PCF50633 read path now uses `qemu_get_timedate(&tm, 0)`. QEMU defaults its
RTC base to UTC, while still honoring explicit `-rtc base=...` and clock
options. The guest remains solely responsible for timezone and daylight-saving
presentation.

The automated post-wake screenshot provided a cross-check without changing
guest settings: at 15:50 CEST (13:50 UTC), the disposable NAND's default
Cupertino timezone displayed 6:50 AM, exactly UTC minus seven hours. Under the
old local-time feed it would have displayed 8:50 AM. A Paris-configured guest
should correspondingly display 15:50 rather than 17:50; final confirmation is
left to the installed app because timezone preferences live in its NAND.

## Phase 21: Pack the immutable NAND base (2026-07-17)

The remaining cold-boot storage path opened and closed one file for every NAND
page cache miss. The installed image contains 132,894 base pages spread over
eight directories. On APFS they occupy approximately 523 MiB despite holding
about 268 MiB of page and spare payload. Metadata lookup, `stat`, `fopen`, and
`fclose` therefore competed with ARM translation on QEMU's main thread and made
otherwise identical boots unusually variable.

The native backend now optionally recognizes `nand.pack`. Its small sorted
index maps `(page, bank)` to an immutable 2,112-byte page record, and QEMU maps
the pack read-only. A binary search plus two memory copies replaces the
per-page filesystem operation. If the pack is absent or does not contain a
requested page, the original directory backend remains available. The
converter writes a temporary file, flushes it, and atomically installs the
finished pack; it never changes or deletes the source page tree.

### Rejected shortcut: treating `_new.page` as a valid overlay

The page tree also contained 953 `_new.page` files. Git history shows that the
2022 backend added them as outputs from the main-data write path, but no read
path or complete copy-on-write contract was ever added. A tempting first fix
was to read `_new.page` before the matching base page and write replacements
atomically. This compiled, but a disposable-clone boot initialized the NAND and
FTL before failing to mount root: iBoot read an HFS signature of `0x0000` and
entered recovery.

That result is not evidence of corrupt source NAND. The files capture only the
currently buffered 2,048-byte program payload plus whatever 64-byte spare data
happened to have been loaded. The model does not persist a complete NAND
program/erase transaction, updated spare/FTL metadata, or block lifecycle.
Consequently the historical files do not form a self-consistent image across
relaunch. Overlay precedence was reverted before packaging. The current pack
deliberately includes only canonical `N.page` files and preserves the existing
runtime behavior of `_new.page` as incomplete diagnostic output.

A real writable overlay remains feasible, but it must begin by modeling the
guest's data and spare transfers, program/erase commands, bad-block rules, and
read-after-program behavior. Only then can a journal or copy-on-write pack be
replayed safely. Simply renaming the existing captures is an explicitly closed
path.

### Controlled cold-boot benchmark

Three alternating pairs used the same optimized release binary and APFS clones
of one disposable NAND. Each process stopped at serial's `Configuring
SpringBoard for N45AP` marker.

| Backend | SpringBoard trials (s) | Mean (s) | Median (s) |
|---|---:|---:|---:|
| Legacy page files | 9.292, 8.307, 9.915 | 9.171 | 9.292 |
| Read-only pack | 5.628, 5.687, 5.891 | 5.735 | 5.687 |

The packed median is 38.8% faster and its range is 0.263 seconds rather than
1.608 seconds. All six boots reached the Darwin kernel, loaded the 49,128-byte
Z2 firmware, and configured SpringBoard. This isolates a genuine host I/O
improvement; it does not change guest clocks, CPU frequency, LCD cadence, or
the sleep/wake state machine.

The signed packaged app then passed the storage-sensitive retained path:

1. the packed base cold-booted and configured SpringBoard;
2. Power drove Merlot panel sleep and the guest's normal OOCSHDWN;
3. Home initiated the retained-RAM reset and the kernel logged `System Wake`;
4. the CLCD scanout and 49,128-byte Z2 firmware returned; and
5. post-wake taps plus a 60 Hz drag reached the multitouch model with correct
   press and release coordinates and no panic, data abort, or process crash.

The retained guest nevertheless reused its prior inactivity deadline and soon
entered timed sleep again, even while host touch events were reaching the
emulated controller. This predates the pack and reproduces with the identical
guest-owned panel/PMU path. It remains a separate power-management/input-
consumer issue: transport delivery is proven, but SpringBoard does not always
convert early post-wake HID traffic into activity assertions. The next
performance work should profile awake MMIO/polling hot spots rather than using
timer ratios as a global speed control; the next sleep investigation should
trace that retained inactivity deadline and the first acknowledged Z2 frame.

## Phase 22: Gate retained input on the real Z2 reload boundary (2026-07-17)

The apparent immediate re-sleep initially suggested that SpringBoard retained
an expired inactivity deadline or failed to turn consumed touch frames into a
power assertion. That interpretation was based on a run contaminated by manual
interaction. A second attempt also produced no evidence because its harness
watched guest serial while the required `[LCD]` marker was on QEMU's stderr;
it never sent Power and eventually observed an ordinary timed sleep. Neither
run is used as acceptance evidence.

A corrected marker-driven harness combined host and guest logs and waited for
each hardware boundary before acting. On the unmodified readiness behavior it
recorded:

- cold input ready at 10.140 seconds;
- OOCSHDWN at 30.807 seconds;
- retained `System Wake` at 42.326 seconds;
- Z2's 49,128-byte firmware reload at 43.118 seconds; and
- guest consumption of touch start/end at 44.331/44.357 seconds.

The guest stayed awake for the following 25 seconds. This disproved the claim
that a correctly timed, consumed touch inherently fails to reset inactivity.
The trace did reveal a real emulator error: `input_ready` survived the
application-processor reset, so QEMU accepted and queued host touches during
iBoot and before the reset Z2 controller or retained driver could consume
them. Input arriving after OOCSHDWN similarly accumulated in a powered-off
controller queue. Host enqueue logs from those intervals were therefore false
evidence of guest HID activity.

The readiness boundary now follows emulated hardware state:

1. Merlot's guest-issued MIPI DCS Sleep In immediately closes host touch input.
2. Every SoC reset clears display input readiness and Z2 firmware-loaded state.
3. The large HBPP firmware transaction arms readiness, and the following small
   calibration transaction completes it. Requiring the sequence prevents an
   intermediate large-packet boundary from opening input prematurely.
4. Retained input opens only when the kernel has reclaimed one of its known OS
   scanout buffers, the panel is powered, and the Z2 firmware is loaded.
5. Cold boot keeps the existing stable-visible-frame gate.

The first implementation treated completion of the large transaction alone as
ready. A validation run showed that marker before the guest's firmware message;
the harness waited in the opposite order, missed the marker, and allowed the
guest to sleep before injecting touch. That exposed a genuinely premature
boundary rather than just a harness ordering problem, so the large-then-small
sequence above replaced it.

In the final release regression, `System Wake` occurred at 87.627 seconds, the
firmware log at 88.469, and retained input became ready at 88.988. A controlled
60 Hz drag was accepted at 89.949 and the device remained awake for 25 seconds.
The unusually slow absolute cold boot in that run reflected host load; the
wake-relative sequence remained about 2.3 seconds and no guest clock changed.
An earlier paired no-touch control also remained awake for 25 seconds,
confirming that the contaminated immediate re-sleep must not be “fixed” by
altering guest timers or injecting a synthetic activity assertion.

Manual packaged testing remains important for a human beginning to drag during
the wake animation. Movement events should be ignored while the controller is
off and begin naturally once scanout and firmware are both ready; no frame is
fabricated and no guest power-management deadline is modified.

## Phase 23: Profile awake timer and TCG cost (2026-07-17)

With retained touch gated at the real controller boundary, work returned to
performance priority 6. A macOS `sample` capture showed that the UI thread is
normally blocked in `g_poll`, SDL/Metal contributes little, and the vCPU thread
spends its time in translated guest code. Idle-awake SpringBoard consumes about
one host core, so the M2 GPU and total core count are not the present limit.

PC sampling repeatedly found the kernel's ARMv6 idle sequence near
`0xc005a9c4`: drain the write buffer, execute WFI, then run Apple's 1,200-
iteration post-WFI delay. The delay is guest code, not an emulator throttle.
Removing it or skipping WFI would change guest timing without explaining why
the CPU wakes, so neither was attempted.

An opt-in timer counter build then measured the S5L8900 Timer 4 interface. The
guest programs one-shot deadlines (`START | MANUALUPDATE`), normally produces
hundreds rather than 10,000 interrupts per virtual second, and clears the latch
once per delivered tick. The old 1,000-count minimum therefore does not create
a fixed 10 kHz interrupt storm during the measured awake workload. By contrast,
the free-running `TICKSLOW`/`TICKSHIGH` pair is read tens of thousands of times
per virtual second and substantially more during early boot. The temporary
counters were removed after the measurement.

Two profiling paths were deliberately rejected:

- QEMU's `hotpages,io=on` TCG plugin instrumented every memory access and was
  so intrusive that the guest did not reach SpringBoard within 120 seconds.
  Its partial-boot MMIO ranking (Timer 1, NAND, DMA, ECC, then VIC) is useful
  only for orientation, not as an optimization verdict. The bundled plugin
  Makefile also passes Linux's `-soname` to macOS `ld`; a manually built
  `.dylib` was required for this experiment.
- Replacing `clock_ns_to_ticks()` on every free-running counter read with an
  exact constant-frequency calculation appeared promising, because our prior
  read-order fix recalculates on both high and low reads. A controlled headless
  A/B measured 89.0% of one core for the installed baseline and 89.6% for the
  candidate. The change produced no speedup and was reverted.

The next optimization must therefore sample translated blocks and MMIO with
lower overhead over a clearly delimited, fully booted workload. It should test
whether repeated WFI returns are driven by another interrupt source and compare
the `arm1176` baseline against `-cpu max`; it must not alter guest timer ratios,
idle loops, or sleep deadlines without a measured hardware-model error.

A code-only `hotblocks,inline=true` run was light enough to reach SpringBoard
in 12.059 seconds and then sample another 15 seconds. Its hottest kernel blocks
were decoded rather than guessed:

- `0xc00570ba`/`0xc00570c8` walk a linked list and compare address ranges. This
  is ordinary guest virtual-memory lookup work, not an emulated-device poll.
- `0xc00611c4` cleans successive 32-byte cache lines with ARM1176 DCCMVAC.
  QEMU already registers DCCMVAC as `ARM_CP_NOP`, so the translated loop does
  not call a device helper or end its translation block. The remaining loop
  instructions are genuine guest work; a PC-specific loop skip would be a
  brittle benchmark hack and was rejected.
- `0xc005a9cc` is the already identified 1,200-iteration post-WFI delay.

The planned CPU-model comparison also showed identical idle-awake host use:
89.0% for the board's `arm1176` and 89.0% for `-cpu max`. The broader `max`
model is therefore neither faster in this workload nor hardware-faithful, so
the default remains `arm1176`. Forcing `-accel tcg,thread=single` reduced host
CPU only by making the guest slower: SpringBoard took 22.544 seconds instead
of the usual roughly 12 seconds. Single-thread TCG is not an optimization for
this build and will not be added to the launcher.

These results move the likely high-value work to priority 7: compare a modern
QEMU/TCG AArch64 backend using the same firmware and benchmark, while keeping
the current tree as the correctness oracle. Before a forward port, a second
profile should delimit an interactive scroll rather than idle SpringBoard so
display/cache-maintenance costs can be separated from normal VM and scheduler
work.

## Phase 24: QEMU 11 forward port reaches SpringBoard (2026-07-17)

Priority 7 began in an isolated QEMU 11.0.2 source and build tree. The
known-good QEMU 6.2 app and repository branch remained untouched while the
iPod machine and its custom devices were adapted to the modern header layout,
reset API, input API, Meson device lists, and ARM machine QOM interfaces. The
port is native arm64, uses TCG with LTO and SDL, and executes the existing
VROM, NOR, LLB, iBoot, NAND, and kernel artifacts rather than substituting a
host-side boot path.

The first linked binary exposed the `iPod-Touch` machine and progressed through
`FIL_Init`, `BUF_Init`, `VFL_Init`, and `FTL_Init`, but never printed
`VFL_Open`. Timer instrumentation ruled out the initial timing theory: Timer 4
was programmed with a count of 120,000, fired about every 12 ms, and was
acknowledged by the guest. ADM was not yet touched. iBoot PC samples alternated
between its stable high-low-high free-running timer read and UART/event polling,
which showed that it was waiting for an asynchronous task rather than trapped
in a bad instruction.

A marker-triggered QMP harness then stopped the old and new engines immediately
after the identical `FTL_Init [OK]` serial boundary. Guest code, scheduler
nodes, timer state, and most VIC state matched. One decisive difference
remained: the current engine had VIC0 source 16 asserted (`0x00010080` raw
status), while QEMU 11 had only Timer 4 source 7 (`0x00000080`). Source 16 is
DMAC0. Its channel 0 was waiting to copy 512 bytes from the NAND FIFO at
`0x38a00080` into iBoot RAM with peripheral-to-memory flow control and source
request ID 2.

This was a compatibility defect with a clear history. The old fork had
commented out PL080 request checks for both peripheral flow directions.
Upstream QEMU 11 enforces those checks, but this PL080 model exposes no request
input that the iPod NAND stub can drive. Consequently the modern controller
waited forever for an event that the emulated topology could not produce. The
port does not restore the old global bypass. It adds a configurable permanent
request mask and sets only DMAC0 request 2 for the iPod's always-ready NAND
FIFO stub. The transfer then completes through modern PL080 semantics and
raises the real DMAC interrupt.

After that fix the same unmodified firmware passed `VFL_Open`, `FTL_Open`,
mounted HFS, loaded the Darwin kernel, initialized the 49,128-byte Z2 firmware,
and reached `Configuring SpringBoard for N45AP`. All timer, VIC, ADM, and DMA
diagnostics were removed before measurement. The clean binary was verified not
to contain any `[PORT ...]` marker and reached the SpringBoard marker in 6.087
seconds using the installed packed NAND. This is within, and at the fast end
of, the current release engine's usual 6–8-second range. Idle-awake sampling
still lands overwhelmingly in the guest kernel's post-WFI delay, so the port
did not introduce a new busy host-device loop.

### What is and is not complete

The forward port now compiles, links, follows the real boot chain, and boots
SpringBoard. It is not yet the packaged engine. Before promotion it must pass
the same visible and retained correctness matrix as the current build:

1. SDL scanout, Home/Power key input, taps, and a 60 Hz drag after cold boot;
2. guest-driven manual sleep to OOCSHDWN and retained Power/Home wake;
3. Z2 firmware reload, immediate post-wake touch, and foreground retention;
4. guest timed sleep using the identical power-cycle path;
5. repeated sleep/wake cycles without panic, data abort, or stale scanout;
6. app-bundle dependency relocation, signing, and launch from `/Applications`.

The old engine remains the correctness oracle and `/Applications/iPod
Touch.app` must not be replaced until those checks pass. The next repository
step is to establish a QEMU 11.0.2-based port branch, import the already
validated device/API changes as reviewable commits, and perform GUI plus
sleep/wake validation from that branch.

## Phase 25: The 10-second wake stall is iBoot's charging wait (2026-07-18)

Wake latency was the last slow feature: 11.5 seconds from Power/Home to
`System Wake` and 12.4 seconds to retained touch readiness on the promoted
QEMU 11 engine. A marker harness that timestamps every serial and stderr line
and samples the guest PC over QMP during the wake window localized all of it
to one place.

### Where the time went

- 93.6% of wake-window PC samples landed in iBoot, alternating between the
  64-bit free-running timer read (`0x18002bb6`, reading `0x3E200080/84`
  high-low-high) and the serial console poll task (`0x1800349c`) — the idle
  signature of a blocked task scheduler, not real work.
- The serial timeline bracketed the stall precisely: after iBoot's second
  `power supply type usb host` line (1.2 s) nothing happened until 11.3 s,
  when iBoot itself read PMU INT1–5 at the standard addresses 0x02–0x06,
  wrote `0x76 <- 0x80` then `0x76 <- 0x40`, ran `merlot_quiesce()`, and
  handed off to the retained kernel.
- MMIO tracing proved the guest touched no PMU, SYSIC, or GPIO register at
  all during the stall. Nothing the models could assert would end it.

### What iBoot-204 actually does on the warm path

Static analysis of `iboot_204_n45ap.bin` (all addresses in-image):

- The boot task (`0x18004c76`) calls `0x18009734`, which reads resume
  register 0x76 through a cached GPMEM helper (`0x180095cc`, register
  `0x67 + 0x0f`) and extracts **bit 5** — the flag this project names
  `PMU_RESUME_WAKE`. When set, the boot task runs a charging dispatcher
  (`0x180099f4`) before the type-4 resume.
- The dispatcher's charging loop (`0x18009878`) sleeps 5,000,000 µs per
  iteration (`task_sleep`, pool literal at `0x18009980`), reads INT1–5 once
  per iteration, and exits when an elapsed budget derived from the data word
  at `0x18021458` (10,000,000 µs) is spent. That is the observed 10.08 s.
- Bit 7 of register 0x76 only selects the standby type in the
  `need battery charge, no power source` shutdown routine (`0x180099b8`).

### Disproved alternatives (do not retry)

| Attempt | Result |
|---|---|
| Re-assert PMU nIRQ level after the SoC reset | Correct level-triggered modeling, kept, but no effect: iBoot never arms the PMU GPIO interrupt on this path |
| Report a full 4.20 V battery | Stall unchanged; the budget is a constant, not voltage-sized |
| Report no USB source (battery wake) | `need battery charge, no power source` → guest OOCSHDWN shutdown, at 3.80 V and at 4.08 V |
| Clear bit 5 of 0x76 | Charging skipped but the resume gate is lost: iBoot cold-boots the kernel cache and foreground state is gone |
| Clear bit 7, keep bit 5 | Still shuts down on battery; bit 7 is not the charge trigger |

The warm path hard-requires a valid external source and always spends the
fixed charge budget. There is no PMU register state that both resumes and
skips the wait.

### Fix

`ipod_touch_cpu_reset()` already reloads a pristine volatile iBoot image on
every retained wake. `ipod_touch_patch_iboot_charge_wait()` now verifies and
rewrites the two charge-timing words in that RAM copy only (never the file):
the 10,000,000 µs budget at `+0x21458` becomes 5,000 µs and the 5,000,000 µs
poll sleep at `+0x9980` becomes 100,000 µs. A mismatched word (any other
iBoot build) is left untouched and logged. An env-gated `IPOD_TRACE_PMU`
diagnostic that logs every PMU I2C access remains available.

Result: Power/Home to `System Wake` fell from 11.5 s to 1.74 s and retained
touch readiness from 12.4 s to 2.5 s. Cold boot, manual sleep/wake with
post-wake drag, timed sleep, and two consecutive cycles all passed with no
panic or data abort.

## Phase 26: Pre-warmed wake parks before the type-4 handoff (2026-07-18)

Since wake is an application-processor power cycle whose boot work is
identical whenever the button is pressed, the wake boot now runs at sleep
entry instead of at wake time.

1. When an untouched `OOCSHDWN=0x02` completes (manual or timed sleep, no
   queued button), the PMU immediately requests the same retained-RAM SoC
   reboot a button would, arms resume bit 5, but supplies no wake cause.
2. iBoot runs its whole wake path with the panel off and input closed.
3. At the type-4 commit (`0x76 <- 0x40`) with no wake requested, a bottom
   half parks the machine with `vm_stop(RUN_STATE_SUSPENDED)`. QEMU's input
   layer still delivers key events in that runstate.
4. Power or Home then records the true hardware wake cause (OOCSTAT/ONKEY
   edges, `EXTON1R`, retained INT2 re-expose) and calls `vm_start()`. Only
   the kernel resume remains: RTC resync and the wake-cause read execute
   after the real button press, so wall-clock time and the reported wake
   reason stay correct no matter how long the park lasted.
5. A press that lands while the pre-warm boot is still running skips the
   park and completes as a normal full wake; the bottom-half also resolves
   the race where the press arrives between the commit write and the park.

One hard-won constraint: the park must not be issued from a
`QEMU_CLOCK_VIRTUAL` timer callback. `vm_stop()` disables the virtual clock
whose timer list is being dispatched while the vCPU waits on the BQL inside
an MMIO access — a permanent deadlock, observed and symbolized. A bottom
half (the same context as the monitor's `stop`) is safe.

Measured on the M2 host: parked wake is 1.09–1.13 s from button press to
retained touch readiness (`System Wake` at 0.36 s); a press immediately
after sleep entry (pass-through) is 2.4–2.5 s. Validated: manual and timed
sleep, two consecutive cycles with 60 Hz drags, and a 25-second park.

## Phase 27: Buttons must work during the lock phase (2026-07-18)

Manual testing of the Phase 25/26 engine immediately found what every
harness had missed: pressing Power and then Home a few seconds later did
nothing, and the eventual wake still looked slow. The harnesses only ever
pressed Home after `OOCSHDWN`; a human presses it during the lock phase,
when the guest is still fully awake for about 20 seconds with the panel
dark before committing to deep sleep. The dark-framebuffer wake-queue
heuristic swallowed those presses, so the OS never saw them, and the
queued reboot fired only when the guest deep-slept on its own much later.

Three defects were fixed, each confirmed against the real SDL build:

1. **Queue gate.** Power/Home are now delivered normally unless the kernel
   has armed the resume token (`0x76 <- 0x80`), which uniquely marks the
   sub-second final commit window before OOCSHDWN; iBoot rewrites the
   register to 0x40 on every wake. `INT1M == 0xB0` was tried first and is
   not usable — it is also the post-resume runtime mask value, and it
   swallowed the second cycle's Power press in the two-cycle regression.
2. **Merlot Sleep Out.** The OS answers a lock-phase press with
   `AppleMerlotLCD::_lcdEnable: enable: 1`, but the panel model only
   understood DCS Sleep In (0x10): the guest relit while the host surface
   stayed black. DCS Sleep Out (0x11) now powers the panel back on, and a
   panel that slept while interactive reopens touch input immediately;
   boot overlays keep the two-second visibility gate.
3. **Racing press.** A press within three seconds of OOCSHDWN becomes an
   immediate wake instead of a silent pre-warm park.

The parked machine's SDL title now reads `[Sleeping]` instead of the
default `[Stopped]`, which read like a fault.

Validated in one scripted run against the SDL build: lock-phase Home
relights the display with instant touch and no reboot; a press 18.5
seconds after Power still ends awake; an untouched sleep parks and wakes
in 1.0 second. The two-cycle and timed-sleep regressions pass unchanged.
The lesson for future acceptance work: always exercise the lock-phase
press path, not only the post-OOCSHDWN one.

## Acceptance harness

`scripts/ipod-acceptance-test.py` runs the complete user-visible sleep/wake
matrix — including the manual lock-phase steps that Phase 27 showed the
older harnesses missed — against the installed application engine with no
setup: it clones a disposable NAND from the application resources, boots
one SDL engine, and drives everything over QMP.

```bash
python3 scripts/ipod-acceptance-test.py            # main matrix, ~2 min
python3 scripts/ipod-acceptance-test.py --timed    # adds untouched idle sleep
IPOD_QEMU=/path/to/qemu-system-arm \
    python3 scripts/ipod-acceptance-test.py        # a development build
```

Checks, in one boot: cold boot to a lit home screen and a consumed 60 Hz
drag; Power then Home two seconds later relights the locked device with
working touch and no reboot or queued wake; Home ~18.5 s after Power (near
the sleep commit) still ends awake; two untouched sleep → pre-warm park →
one-second Home wake cycles, each with a post-wake drag; and a panic scan
with the known `Panic Fail Count`/IOPanicPlatform/sdio-crc false positives
filtered. `--timed` boots a second engine and lets the guest idle-sleep on
its own before the parked wake. Exit status is non-zero on any failing
check, and every serial/stderr log and screendump is kept in a printed
`/private/tmp/ipod-acceptance-*` directory.

Run this before promoting any engine change; it replaces the ad-hoc
`/private/tmp` harnesses used during Phases 22–27.

---

## Finding #96: `vm_stop()` from a virtual-clock timer callback deadlocks the whole main loop (2026-07-27)

**Symptom.** iPhone OS 1.0/1A543a on `-M iPhone-2G` parked on sleep as designed
(`88e73d8cec`), but Power/Home did nothing at all: no serial growth, no `[WAKE]`
line, no reaction of any kind. Only a restart brought the device back. The iPod
(N45AP) and 1.1.x were unaffected.

**Root cause.** The 25 s "no type-4 commit" deadline added in `88e73d8cec` is a
`QEMU_CLOCK_VIRTUAL` timer, and its callback called `vm_stop()` **inline**:

```
qemu_main_loop → main_loop_wait → qemu_clock_run_all_timers
  → timerlist_run_timers            (running a QEMU_CLOCK_VIRTUAL timer)
    → pcf50633_prewarm_deadline
      → vm_stop → do_vm_stop → pause_all_vcpus
        → qemu_clock_enable(QEMU_CLOCK_VIRTUAL, false)
          → qemu_event_wait(&tl->timers_done_ev)   ← never returns
```

`pause_all_vcpus()` disables the virtual clock. `qemu_clock_enable()` waits for
every timerlist on that clock to finish running its callbacks — and we are
inside one of those callbacks, so `timers_done_ev` can never be set. The main
loop is dead from that instant: no BHs, no timers, no chardev I/O, no monitor.

QEMU documents this exactly, in the comment above `qemu_clock_enable()` in
`util/qemu-timer.c`: the function "should not be used from the callback of a
timer that is based on @clock. Doing so would cause a deadlock."

**Why the iPod never hit it.** The type-4 commit path parks from a **bottom
half** (`prewarm_park_bh`), and BHs run outside `timerlist_run_timers`.
iBoot-204 always writes the type-4 commit, so N45AP and 1.1.x never reach the
deadline path. Only iBoot-159 (1.0/1.0.x), which writes `RESUME_STATUS` zero
times, does — which is why the defect looked firmware-specific when it was
purely a host-side threading bug.

**Fix** (`hw/arm/ipod_touch_pcf50633_pmu.c`, `pcf50633_prewarm_deadline`): set
`prewarm_no_park`, then `qemu_bh_schedule(s->prewarm_park_bh)` instead of
calling `vm_stop()`. The BH performs the stop and retains the
wake-arrived-before-the-park race handling it already had. Both park paths now
stop the VM from a BH; neither does it from a timer.

**Verification, 1.0/1A543a:**

| | before | after |
|---|---|---|
| deadline line at ~162 s | printed | printed |
| `[WAKE] Pre-warmed wake parked; awaiting Power/Home` | **never** (loop already dead) | **printed** |
| QMP greeting on connect while parked | **never sent** | `{"status": "suspended"}` |
| `send-key h` → `ipod_touch_key_event()` | **not called** | `[KEYTRACE] keycode=35 … active=1 parked=1 no_park=1` |
| wake | none | `[WAKE] Home starting retained-RAM wake boot`, RESUME + guest RESET |
| serial | 144 151 B, frozen | 217 480 B |
| framebuffer (`pmemsave`, 3 bases) | — | **45.6 % non-black** |

Regression gate `scripts/lock-unlock-probe.py --board n45ap --cycles 4` stays
4/4, "first failing cycle: none" — the iPod path is untouched.

### The false path this session took, and why each true premise misled

The investigation was framed as "which early `return` in
`ipod_touch_key_event()` swallows the key?" Every stated premise was correct and
the conclusion was still wrong:

| Premise | True? | Why it misled |
|---|---|---|
| "Keys reach a suspended VM" | **Yes** — the iPod's park wakes on the identical QMP `send-key` | Says nothing about whether *this* process can receive anything at all |
| "`prewarm_active` and `prewarm_parked` are both set" | **Yes** | State was fine; the code reading it was never reached |
| "No log output ⇒ an earlier `return` fired" | **No** | The unexamined leap. The handler was never *entered* |
| "`sendkey` never reaches `ipod_touch_key_event`" (stale row in the dead-ends table above) | **No longer true** | An out-of-date doc claim that made "keys are being lost" look plausible; now corrected in place |

**What actually resolved it, in two cheap steps:**

1. Instrument the **entry** of `ipod_touch_key_event()` — not a suspected branch.
   `IT_KEY_TRACE=1` (committed) prints keycode plus
   `prewarm_active/parked/no_park` and the suppress flags on every button event.
   **Zero lines** is a qualitatively different signal from "wrong branch taken",
   and it points away from the device model entirely.
2. Notice that a QMP client `connect()`s while parked but **never receives the
   greeting** — `connect()` succeeds off the listen backlog, so a successful
   connect proves nothing. Nothing was servicing the monitor. `sample <pid>`
   then produced the deadlock stack immediately.

### Rules

- **Never call `vm_stop()` from a `QEMU_CLOCK_VIRTUAL` timer callback.** Park
  from a BH. The same applies to anything else that funnels into
  `pause_all_vcpus()`.
- **A parked/suspended VM that ignores QMP *entirely* is a deadlocked QEMU**, not
  a guest bug and not a key-routing bug. Check the monitor greeting first, then
  `sample <pid>`, before touching the device model.
- **Do not sample the framebuffer with QMP `stop`/`cont` while parked** —
  `cont` un-parks the device and invalidates the test. Use `pmemsave` on the
  running machine after the wake (unchanged by this fix).

Board-side write-up and the 1.0 open-issue list:
[`IPHONE_OS_1X_VERSIONS.md`](IPHONE_OS_1X_VERSIONS.md) and
[`IPHONE_2G_BRINGUP_HANDOFF.md`](IPHONE_2G_BRINGUP_HANDOFF.md) § 2026-07-27.


# AUTO-SLEEP WAKE LOOP: touch dies after the FIRST auto-sleep (2026-08-01, iPhone boards)

**User-reported, and REPRODUCED in the emulator's own Cocoa window.** After the
device auto-sleeps from idle, waking with H leaves touch dead: slide-to-unlock
does nothing. Reported on 1.0 AND 1.1.4; the iPod appears immune. Waking from a
MANUAL Power-button sleep is fine.

## It does NOT reproduce through the VNC harness -- five configurations passed

`scripts/autosleep-touch-probe.py` (new) drives boot -> idle -> auto-sleep ->
H -> slide over VNC. On 1.1.4 it PASSES in all of: wake at +5-10 s (shallow),
wake at +30 s (the user's timing), wake at +55 s (after the deep-sleep commit),
a manual P-cycle before the auto-sleep, and a 5-minute host SIGSTOP freeze.
Every one woke to the slider and unlocked, `[TOUCH] mouse DOWN/UP` delivered.

So the harness cannot see this bug. It reproduces in the REAL WINDOW, which is
how the user hits it. Any future "auto-sleep wake is fine" claim from that
probe alone is worth nothing.

## What actually happens (measured, one Cocoa session)

Session totals: **3 pre-warm parks, 5 touches REFUSED, 1 touch ever accepted**
(the first-launch modal's Dismiss, before any sleep).

```
[KEYTRACE] keycode=35  active=1 parked=1        <- H resumes the parked pre-warm
[WAKE] Home completed pre-warmed wake
[KEYTRACE] keycode=163 ... sup_home=1           <- the RELEASE is SUPPRESSED
[LCD] Retained kernel enabled scanout at 0x0f496000
[LCD] Touch input restored after wake (device was already interactive)
[LCD] Merlot panel entered sleep                <- goes straight back to sleep
[PMU] RESUME_STATUS write <- 0x80 (armed)
[PMU] Application processor awaiting power loss
[WAKE] Pre-warming retained-RAM wake after OOCSHDWN
[WAKE] Pre-warmed wake parked; awaiting Power/Home     <- RE-PARKED
[TOUCH] Ignoring input until display/driver startup is stable   <- every touch
```

The chain, each link measured:

1. **The wake press is consumed as a wake CAUSE, not delivered as input.**
   `ipod_touch_key_event()`'s pre-warm branch sets `EXTON1R` and
   `suppress_home_release = true`, so the guest never sees HOME as a button.
   Its display-idle timer is therefore never reset.
2. **The guest resumes and immediately finishes the sleep it was parked inside**
   -- panel sleep, OOCSHDWN, pre-warm, park again. A wake -> re-sleep -> re-park
   LOOP. The window title flips `QEMU` -> `QEMU [Sleeping]` within seconds.
3. **Each park is a machine reset, and reset clears `input_ready`**
   (`s5l8900_lcd_realize`, `input_ready = false`).
4. **While parked the gate cannot re-arm**: both fast paths in
   `lcd_update_input_ready()` require `!panel_off`, and the generic path needs
   120 consecutive visible frames, which a stopped vCPU never produces. So
   every touch hits `[TOUCH] Ignoring input until display/driver startup is
   stable` -- **the MODEL refuses them; the guest never sees them.**

A SECOND H press, with the machine already running, IS delivered normally
(`sup_home=0`, a real `[BTN]` line) -- and does not rescue it: the device
re-parks anyway.

## Why this is a model bug, not iPhone OS behaviour

Real hardware has no "park". The pre-warm/park machinery exists only because
iBoot-204 has no resume entry point (finding #82), and it assumes ONE wake per
park. When the resumed kernel simply continues into the sleep it was already
committing, the model parks again -- and its touch gate, designed for boot
overlays, is cleared by the reset and can never re-arm while parked.

## Next steps, in order

1. **Why does the resumed guest re-sleep at once?** Park happens at the type-4
   commit, i.e. while the kernel is mid-sleep. If the park point is too late,
   resuming can only finish the sleep. Instrument the resumed kernel's PC to
   see whether it is completing the old sleep or starting a new one.
2. **Deliver the wake press as INPUT too**, so the OS sees user activity and
   restarts its idle timer, instead of consuming it purely as a PMU wake cause.
   That is the smallest change that could break the loop -- and it needs an
   A/B, because the suppression exists for a measured reason.
3. **Make the touch gate survivable**: `input_ever_ready` is already persisted
   in vmstate, but reset clears `input_ready` and the re-arm paths all require
   a lit panel. A device that has ever been interactive should not need 120
   visible frames again after a park.
4. Re-test the iPod to confirm it is genuinely immune rather than merely
   running an older engine -- the iPod bundle was NOT updated this session
   (its binary md5 differs from both iPhone bundles).

## Fix attempt 1: deliver the wake press as INPUT (2026-08-01) -- partial, and it corrects a wrong hypothesis

### What was built

`ipod_touch_wake_activity()` in `hw/arm/ipod_touch.c`. After a parked pre-warm
wake resumes the machine, a virtual timer (3 s, then a 150 ms hold) injects a
full Home press+release through the ordinary GPIO path, so the OS sees user
activity and restarts its display-idle timer. `IT_WAKE_ACTIVITY=0` disables it.

### Measured: it fixes the re-park loop and the touch refusals

Same window, same board (1.1.4), same sequence -- idle to a park, press H,
settle 45 s, tap:

| | parks | touches REFUSED by the model | touch accepted |
|---|---|---|---|
| before | 3 (every wake re-parked) | **5** | only the one before the first sleep |
| after | **1** (no re-park) | **0** | yes, `[TOUCH] mouse DOWN` logged |

So the wake -> re-sleep -> re-park loop is broken and
`[TOUCH] Ignoring input until display/driver startup is stable` no longer
fires. That part of the diagnosis held.

### NOT fixed: the panel still does not light on the park path

After the wake the log still shows `Merlot panel entered sleep` and never
`Merlot panel woke from sleep`; the window stays black even though the guest
is running and touch is now accepted. So the user-visible symptom on the deep
path remains. The next question is why the resumed retained kernel leaves the
panel asleep, which is a DISPLAY question, not an input one -- separate from
everything above.

### A hypothesis this KILLED, and a probe trap it exposed

The rationale for the fix was "the wake press is consumed as a PMU wake cause
and never delivered as input". That is true **only on the parked path**
(`sup_home=1`). Measured on the shallow path, the same press arrives
completely normally:

```
[LCD] Merlot panel entered sleep                     <- panel sleep, no OOCSHDWN
[KEYTRACE] keycode=35  ... sup_home=0                <- delivered NORMALLY
[BTN] keycode=35 / keycode=163                       <- both edges
[LCD] Merlot panel woke from sleep                   <- panel wakes correctly
[LCD] Merlot panel entered sleep                     <- re-sleeps 45 s later
```

And the re-sleep there is almost certainly CORRECT: a real iPhone that wakes
to the lock screen and is then ignored turns the screen off again after ~15-20
seconds. **So `--wake-settle 45`, added to make the probe "see the bug", makes
it report a FAIL for ordinary auto-lock behaviour on the shallow path.** It
found a genuine failure on the deep path and a false one on the shallow path,
which is worse than useless if read carelessly.

Rules that follow, for anyone touching this probe:

* The unambiguous MODEL-side bug signals are `Ignoring input until
  display/driver startup is stable` (the model refusing a touch) and a park
  count that INCREASES after the wake. Both are counted and printed. A pixel
  verdict alone cannot tell "the model broke" from "the device correctly went
  back to sleep while you stared at it".
* The deep path (auto-sleep -> OOCSHDWN -> pre-warm park) and the shallow path
  (panel sleep only) are DIFFERENT BUGS' territory and behave differently.
  Which one a run exercises depends on how long the device idles first; check
  the park count rather than assuming.
* The failure the user reports reproduces in the REAL WINDOW. Five VNC
  configurations passed a build that was visibly broken by hand.

### A/B against the PRE-SESSION engine: this session did not cause it

Suspicion worth taking seriously, since the day had touched SYSIC `INTLEVEL`
and the whole MBX register block: did today's work break the wake? Restored
the pre-session 1.1.4 binary (md5 `32c092a1...`, saved before any of it) and
ran the identical probe:

| engine | parks | refused | slide | verdict |
|---|---|---|---|---|
| today's build (with the wake fix) | 0 -> 0 | 0 | 0.00% | FAIL |
| **pre-session `32c092a1`** | 0 -> 0 | 0 | 0.00% | **FAIL, identical** |

So the shallow-path behaviour is **PRE-EXISTING** and none of this session's
changes caused it. Both bundles now carry the current build (`4640dbe0...`),
which is strictly better on the deep path (refusals 5 -> 0, no re-park loop)
and indistinguishable on the shallow one.

### What is still genuinely unknown

Whether the shallow-path result is a BUG AT ALL. The device wakes to the lock
screen, the model delivers both touch edges, the guest ignores them, and the
panel is asleep again by ~16 s after the wake. A real iPhone woken to the lock
screen and then ignored also turns its screen off after ~15-20 s, and a slept
guest ignoring touch is correct. Distinguishing "correct auto-lock" from "the
lock screen sleeps far too fast" needs a reference the emulator cannot
provide: the iPod bundle (which the user reports as immune, and which still
runs a DIFFERENT binary), or hardware behaviour. Until then this probe's
shallow-path FAIL should be read as "the device was asleep when the slide
happened", not as a proven defect.

The DEEP path is not ambiguous: a model that refuses input with "Ignoring
input until display/driver startup is stable" is broken by construction, and
that is the part the fix addresses.

### Fix attempt 1 is now OFF BY DEFAULT -- the user reported it made things WORSE

Shipped enabled on the strength of ONE probe run (refusals 5 -> 0, re-park
loop gone). In real interactive use the user immediately reported worse
behaviour than before: **the device sleeps with SpringBoard displayed, H
blanks the screen, SpringBoard comes back, and there is no slide-to-unlock at
all.** Reverted to opt-in (`IT_WAKE_ACTIVITY=1`) the moment that was
reported; both bundles reinstalled with it off (`bceeec6e...`).

Two lessons, and the second is the general one:

* Injecting a synthetic Home press into a guest that is still mid-resume
  perturbs more than it repairs. The measured wins were real but narrow --
  they were counted from the model's own log, and the model's log cannot see
  "the OS is now in a state the user finds broken".
* **One green probe run is not grounds for enabling something by default.**
  The probe measures two model-side counters; the user measures the device.
  Anything that changes guest-visible timing needs the second kind of
  evidence before it becomes the default, not after.

### And the reproduction still does not match the user's symptom

The user sees the slider ON SCREEN with touch dead. Every probe run here had
`panel_off` already set when the touch arrived -- and `panel_off` blanks the
host surface (`lcd_refresh`), so those runs were black-screen failures, a
DIFFERENT bug from the one being reported. Stated plainly so nobody reads the
probe's FAIL as the user's bug.

**Leading hypothesis for the real one, untested.** `Merlot panel entered
sleep` (MIPI DCS 0x10, `ipod_touch_lcd_panel.c`) unconditionally does:

```c
s->lcd->panel_off = true;
s->lcd->input_ready = false;        /* the touch gate slams shut */
s->lcd->input_ready_frames = 0;
```

Sleep Out (0x11) restores both, gated on `relight_input_fast ||
input_ever_ready`. But `panel_off` is ALSO cleared elsewhere without touching
`input_ready` -- `ipod_touch_lcd.c:221`, the framebuffer-base path. Any
relight that goes through THAT path leaves the panel lit and the gate shut:
slider visible, every touch silently refused. That is exactly the reported
symptom, it explains why manual P-sleep differs (a different relight path),
and it is a one-line asymmetry. Verify by logging `input_ready` alongside
`panel_off` at every transition before changing anything.
