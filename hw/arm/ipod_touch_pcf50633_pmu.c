#include "hw/arm/ipod_touch_pcf50633_pmu.h"
#include "hw/arm/ipod_touch_sysic.h"
#include "hw/arm/ipod_touch_timer.h"
#include "hw/arm/ipod_touch_lcd.h"
#include "hw/intc/pl192.h"
#include "exec/cpu-common.h"    // cpu_physical_memory_read/write
#include "hw/core/cpu.h"        // current_cpu
#include "target/arm/cpu.h"     // ARM_CPU, CPUARMState, cpsr_read
#include "exec/exec-all.h"     // tb_flush
#include "sysemu/runstate.h"

// Delay after OOCSHDWN write before cleaning up VIC state.
// The patched sleep function returns immediately; this delay ensures
// the kernel has had time to execute it and enter the resume path.
#define POST_SLEEP_VIC_CLEANUP_NS  500000000LL  // 500ms

#define SLEEP_FUNC_DSB_PA   0x0805a6cc
#define SLEEP_FUNC_LOOP_PA  0x0805a6d0
#define SLEEP_FUNC_NEXT_PA  0x0805a6d4

// Finding #66: After the patched sleep function returns, the VIC may
// have a stale in-service interrupt (VECTADDR was read/acked by a GPIO
// handler but PM suspend prevented the EOI write).  This blocks all
// same/lower priority IRQ delivery.
//
// This timer callback directly resets VIC0/VIC1 priority stacks and
// re-pulses any pending SYSIC GPIO IRQs.
static void pmu_post_sleep_vic_cleanup(void *opaque)
{
    Pcf50633State *s = (Pcf50633State *)opaque;

    // Directly reset VIC priority stacks via public API
    if (s->vic0) {
        pl192_reset_priority((PL192State *)s->vic0);
    }
    if (s->vic1) {
        pl192_reset_priority((PL192State *)s->vic1);
    }
    fprintf(stderr, "[PMU] Post-sleep VIC cleanup: reset priority stacks\n");

    // Re-pulse any pending SYSIC GPIO IRQs so the VIC delivers them
    if (s->sysic) {
        for (int grp = 0; grp < GPIO_NUMINTGROUPS; grp++) {
            if (s->sysic->gpio_int_status[grp]) {
                qemu_irq_lower(s->sysic->gpio_irqs[grp]);
                qemu_irq_raise(s->sysic->gpio_irqs[grp]);
                fprintf(stderr, "[PMU] Re-pulsed GPIO IRQ group %d "
                        "(INTSTAT=0x%08x)\n", grp,
                        s->sysic->gpio_int_status[grp]);
            }
        }
    }

    // Approach #37: Force-enable IRQs by clearing CPSR I bit.
    // After sleep function returns, the kernel PM code keeps I=1
    // (IRQ disabled). Without IRQs, VIC cannot deliver interrupts
    // and processes remain frozen. Use first_cpu (timer callbacks
    // have current_cpu=NULL).
    {
        CPUState *cpu = first_cpu;
        if (cpu) {
            CPUARMState *env = &ARM_CPU(cpu)->env;
            uint32_t cpsr = cpsr_read(env);
            fprintf(stderr, "[PMU] Post-sleep: CPSR=0x%08x "
                    "(I=%d F=%d mode=0x%02x) PC=0x%08x\n",
                    cpsr, (cpsr >> 7) & 1, (cpsr >> 6) & 1,
                    cpsr & 0x1f, env->regs[15]);
            if (cpsr & CPSR_I) {
                uint32_t new_cpsr = cpsr & ~(CPSR_I | CPSR_F);
                cpsr_write(env, new_cpsr, CPSR_I | CPSR_F,
                           CPSRWriteByInstr);
                fprintf(stderr, "[PMU] Post-sleep: FORCE-ENABLED IRQ+FIQ "
                        "(CPSR 0x%08x → 0x%08x)\n", cpsr, new_cpsr);
            }
        }
    }

    // Findings #71-73: PM suspend stops the timer. The abort-suspend
    // path (R0 != 0) doesn't restart it, and the kernel's FIQ handler
    // clears the timer IRQ but refuses to reprogram the next tick
    // (PM state check fails).  Force-restart unconditionally.
    if (s->timer) {
        IPodTouchTimerState *t = (IPodTouchTimerState *)s->timer;
        uint32_t count = t->bcreload ? t->bcreload : 100000;
        t->bcount1 = count;
        t->bcreload = count;
        t->base_time = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        // Use START without MANUALUPDATE — auto-recurring mode.
        // The kernel's FIQ handler clears IRQLATCH but doesn't reprogram
        // (finding #73), so MANUALUPDATE would stop after one tick.
        // Auto-recurring keeps the timer alive until kernel takes over.
        t->status = TIMER_STATE_START;
        t->freq_out = 1000000000 / 100;  // 10 MHz
        t->tick_interval = muldiv64(
            (t->bcount1 < 1000) ? 1000 : t->bcount1,
            NANOSECONDS_PER_SECOND, t->freq_out);
        t->next_planned_tick = t->tick_interval;
        qemu_irq_lower(t->irq);  // Ensure clean edge
        timer_mod(t->st_timer,
                  t->base_time + t->tick_interval);
        fprintf(stderr, "[PMU] Post-sleep: force-restarted timer "
                "(bcount1=%u, interval=%llu ns, status was 0x%x)\n",
                count, (unsigned long long)t->tick_interval, t->status);
    }

    // Finding #92: Restore framebuffer snapshot after wake.
    // The kernel switches to a blank framebuffer as the last step of the
    // "screen off" animation before sleep. After our patched wake-up
    // (sleep function returns R0=0), the PM resume path doesn't trigger
    // the display-on sequence. The LCD controller continues reading from
    // the blank buffer, showing a black screen.
    //
    // Fix: Write the pre-sleep framebuffer snapshot to the current
    // w1_framebuffer_base. The LCD code captures this snapshot
    // continuously whenever non-black content is visible.
    if (s->lcd) {
        IPodTouchLCDState *lcd = (IPodTouchLCDState *)s->lcd;
        if (lcd->fb_snapshot && lcd->fb_snapshot_valid) {
            // Write to ALL 3 known framebuffer addresses so the display
            // shows content regardless of which buffer the LCD points to.
            static const uint32_t fb_addrs[] = {
                0x0fe00000, 0x0f400000, 0x0f496000
            };
            for (int i = 0; i < 3; i++) {
                cpu_physical_memory_write(fb_addrs[i],
                                          lcd->fb_snapshot,
                                          FB_WIDTH * FB_HEIGHT * FB_BPP);
            }
            fprintf(stderr, "[PMU] Post-sleep: restored framebuffer "
                    "snapshot to all 3 buffers (%dx%d, %d bytes)\n",
                    FB_WIDTH, FB_HEIGHT,
                    FB_WIDTH * FB_HEIGHT * FB_BPP);
        } else {
            fprintf(stderr, "[PMU] Post-sleep: no valid framebuffer "
                    "snapshot to restore (lcd=%p, snapshot=%p, valid=%d)\n",
                    lcd, lcd ? lcd->fb_snapshot : NULL,
                    lcd ? lcd->fb_snapshot_valid : 0);
        }
    }

    // The wake trampoline temporarily replaces the sleep function's DSB/B .
    // sequence and the first instruction of the following function. Restore
    // all three once the resume path is safely past them. Leaving the patch in
    // place corrupts the adjacent function and makes later sleep cycles return
    // immediately instead of waiting for another power-button event.
    if (s->sleep_func_patched) {
        static const uint32_t original_dsb = 0xee073f9a;
        static const uint32_t original_loop = 0xeafffffe;
        static const uint32_t original_next = 0xe92d4090;
        CPUState *cpu = first_cpu;

        cpu_physical_memory_write(SLEEP_FUNC_DSB_PA,
                                  &original_dsb, sizeof(original_dsb));
        cpu_physical_memory_write(SLEEP_FUNC_LOOP_PA,
                                  &original_loop, sizeof(original_loop));
        cpu_physical_memory_write(SLEEP_FUNC_NEXT_PA,
                                  &original_next, sizeof(original_next));
        if (cpu) {
            tb_flush(cpu);
        }
        s->sleep_func_patched = false;
        s->oocshdwn_fired = false;
        fprintf(stderr, "[PMU] Restored sleep function for the next cycle\n");
    }
}

// Check if any interrupt is pending and update the nIRQ line.
// nIRQ is active-low and level-triggered: stays asserted as long as
// any INTx register has unread bits (ONKEY bypasses masks).
void pcf50633_update_irq(Pcf50633State *s)
{
    if (!s->sysic) return;

    bool onkey_pending = s->int1 & (PMU_INT1_ONKEYF | PMU_INT1_ONKEYR);
    bool other_pending = (s->int1 & ~s->int1m & ~(PMU_INT1_ONKEYF | PMU_INT1_ONKEYR)) ||
                         (s->int2 & ~s->int2m) ||
                         (s->int3 & ~s->int3m) ||
                         (s->int4 & ~s->int4m) ||
                         (s->int5 & ~s->int5m);

    if (onkey_pending || other_pending) {
        // Assert nIRQ: set GPIO status + level bits and raise IRQ line
        s->sysic->gpio_int_status[PMU_INT_GPIO_GROUP] |= (1 << PMU_INT_GPIO_BIT);
        s->sysic->gpio_int_level[PMU_INT_GPIO_GROUP] |= (1 << PMU_INT_GPIO_BIT);
        qemu_irq_raise(s->sysic->gpio_irqs[PMU_INT_GPIO_GROUP]);
        fprintf(stderr, "[PMU] nIRQ assert: int1=0x%02x mask=0x%02x\n",
                s->int1, s->int1m);
    } else {
        // De-assert nIRQ: clear GPIO status + level bits and lower IRQ line
        s->sysic->gpio_int_level[PMU_INT_GPIO_GROUP] &= ~(1 << PMU_INT_GPIO_BIT);
        if (s->sysic->gpio_int_status[PMU_INT_GPIO_GROUP] & (1 << PMU_INT_GPIO_BIT)) {
            s->sysic->gpio_int_status[PMU_INT_GPIO_GROUP] &= ~(1 << PMU_INT_GPIO_BIT);
            qemu_irq_lower(s->sysic->gpio_irqs[PMU_INT_GPIO_GROUP]);
            fprintf(stderr, "[PMU] nIRQ de-assert\n");
        }
    }
}

bool pcf50633_resume_from_sleep(Pcf50633State *s)
{
    // Approach #43: Deferred sleep patch — apply when input arrives and
    // the CPU is stuck in the sleep loop (B . at PA 0x0805a6d0).
    CPUState *cpu = first_cpu;
    uint32_t orig_loop;
    uint32_t cpsie_if = 0xf10800c0;   // CPSIE IF
    uint32_t mov_r0_0 = 0xe3a00000;   // MOV R0, #0
    uint32_t pop_ret  = 0xe8bd8090;    // POP {R4,R7,PC}

    if (!s->oocshdwn_fired || !cpu) {
        return false;
    }

    CPUARMState *env = &ARM_CPU(cpu)->env;
    uint32_t pc = env->regs[15];

    if (pc != 0xc005a6d0 && pc != 0x0005a6d0) {
        return false;
    }

    cpu_physical_memory_read(SLEEP_FUNC_LOOP_PA, &orig_loop, 4);
    if (orig_loop != 0xeafffffe) {
        return false;
    }

    // Patch: DSB → CPSIE IF, B . → MOV R0,#0, next → POP.
    cpu_physical_memory_write(SLEEP_FUNC_DSB_PA, &cpsie_if, 4);
    cpu_physical_memory_write(SLEEP_FUNC_LOOP_PA, &mov_r0_0, 4);
    cpu_physical_memory_write(SLEEP_FUNC_NEXT_PA, &pop_ret, 4);
    s->sleep_func_patched = true;
    env->regs[15] = 0xc005a6cc;
    tb_flush(cpu);
    fprintf(stderr, "[PMU] Installed sleep-resume trampoline; "
            "PC set to 0xc005a6cc\n");

    if (s->post_sleep_timer) {
        timer_mod(s->post_sleep_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                  + POST_SLEEP_VIC_CLEANUP_NS);
    }

    return true;
}

void pcf50633_set_onkey(Pcf50633State *s, bool pressed)
{
    if (pressed) {
        s->int1 |= PMU_INT1_ONKEYF;
    } else {
        s->int1 |= PMU_INT1_ONKEYR;
    }
    fprintf(stderr, "[PMU] ONKEY %s  int1=0x%02x\n",
            pressed ? "pressed" : "released", s->int1);

    pcf50633_update_irq(s);
}

static int pcf50633_event(I2CSlave *i2c, enum i2c_event event)
{
    Pcf50633State *s = PCF50633(i2c);
    switch (event) {
    case I2C_START_SEND:
        s->has_reg_addr = false;
        break;
    case I2C_START_RECV:
    case I2C_FINISH:
    default:
        break;
    }
    return 0;
}

static int int_to_bcd(int value) {
    int shift = 0;
    int res = 0;
    while (value > 0) {
      res |= (value % 10) << (shift++ << 2);
      value /= 10;
   }
   return res;
}

static void pcf50633_write_reg(Pcf50633State *s, uint8_t reg, uint8_t val)
{
    // Store all writes in register file for debug inspection
    s->regs[reg] = val;
    switch (reg) {
        case PMU_INT1:
            s->int1 &= ~val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT2:
            s->int2 &= ~val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT3:
            s->int3 &= ~val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT4:
            s->int4 &= ~val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT5:
            s->int5 &= ~val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT1M:
            s->int1m = val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT2M:
            s->int2m = val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT3M:
            s->int3m = val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT4M:
            s->int4m = val;
            pcf50633_update_irq(s);
            break;
        case PMU_INT5M:
            s->int5m = val;
            pcf50633_update_irq(s);
            break;
        case PMU_OOCSHDWN:
        {
            if (s->lcd) {
                IPodTouchLCDState *lcd = s->lcd;
                lcd->panel_off = true;
                lcd->invalidate = 1;
                fprintf(stderr, "[LCD] PMU powered panel off\n");
            }

            // Finding #75: Log CPU state at OOCSHDWN write to trace PM caller.
            if (current_cpu) {
                CPUARMState *env = &ARM_CPU(current_cpu)->env;
                uint32_t cpsr = cpsr_read(env);
                fprintf(stderr, "[PMU] OOCSHDWN write: CPU state at PM suspend\n");
                fprintf(stderr, "[PMU]   R0=0x%08x R1=0x%08x R2=0x%08x R3=0x%08x\n",
                        env->regs[0], env->regs[1], env->regs[2], env->regs[3]);
                fprintf(stderr, "[PMU]   R4=0x%08x R5=0x%08x R6=0x%08x R7=0x%08x\n",
                        env->regs[4], env->regs[5], env->regs[6], env->regs[7]);
                fprintf(stderr, "[PMU]   R8=0x%08x R9=0x%08x R10=0x%08x R11=0x%08x\n",
                        env->regs[8], env->regs[9], env->regs[10], env->regs[11]);
                fprintf(stderr, "[PMU]   R12=0x%08x SP=0x%08x LR=0x%08x PC=0x%08x\n",
                        env->regs[12], env->regs[13], env->regs[14], env->regs[15]);
                fprintf(stderr, "[PMU]   CPSR=0x%08x (I=%d F=%d mode=%02x)\n",
                        cpsr, (cpsr >> 7) & 1, (cpsr >> 6) & 1, cpsr & 0x1f);
                // Dump stack to trace call chain (read 64 bytes from SP)
                uint32_t sp = env->regs[13];
                // Convert VA to PA: kernel VA 0xC0000000 -> PA 0x08000000
                uint32_t sp_pa = sp;
                if (sp >= 0xC0000000) {
                    sp_pa = sp - 0xC0000000 + 0x08000000;
                }
                fprintf(stderr, "[PMU]   Stack dump (SP=0x%08x, PA=0x%08x):\n", sp, sp_pa);
                for (int i = 0; i < 16; i++) {
                    uint32_t word;
                    cpu_physical_memory_read(sp_pa + i * 4, &word, 4);
                    fprintf(stderr, "[PMU]     [SP+0x%02x] = 0x%08x\n", i * 4, word);
                }
            }
            // Dump GPMEM and key PMU registers at sleep time
            fprintf(stderr, "[PMU] GPMEM at sleep: GPMEM0=0x%02x GPMEM1=0x%02x "
                    "GPMEM2=0x%02x GPMEM3=0x%02x\n",
                    s->regs[PMU_GPMEM0], s->regs[PMU_GPMEM1],
                    s->regs[PMU_GPMEM2], s->regs[PMU_GPMEM3]);
            fprintf(stderr, "[PMU] OOCSHDWN=0x%02x OOCWAKE=0x%02x "
                    "INT1M=0x%02x INT1=0x%02x\n",
                    val, s->regs[PMU_OOCWAKE], s->int1m, s->int1);
            // Check for resume address in well-known physical locations
            // (some Apple platforms store it at a fixed SRAM address)
            {
                uint32_t resume_candidates[4];
                // Check PA 0x0 area (SRAM / bootrom data)
                cpu_physical_memory_read(0x00000000, &resume_candidates[0], 4);
                cpu_physical_memory_read(0x00000004, &resume_candidates[1], 4);
                // Check PA 0x22000000 (SRAM on S5L8900)
                cpu_physical_memory_read(0x22000000, &resume_candidates[2], 4);
                cpu_physical_memory_read(0x22000004, &resume_candidates[3], 4);
                fprintf(stderr, "[PMU] Resume addr candidates: "
                        "[PA 0x0]=0x%08x [PA 0x4]=0x%08x "
                        "[PA 0x22000000]=0x%08x [PA 0x22000004]=0x%08x\n",
                        resume_candidates[0], resume_candidates[1],
                        resume_candidates[2], resume_candidates[3]);
            }
            // Approach #38-39: Patch delay function and get_ticks for PM resume.
            // Finding #79: The delay function at 0xc04bc460 calls a
            // Security Modules wrapper at VA 0xc01603ee (PA 0x081603ee)
            // to poll UART for serial debugger input. Even with the
            // "enabled" flag cleared (approach #36), this wrapper chains
            // into a nested timed-wait at 0xc0062462 that reads a data
            // structure never updated (workqueues frozen). The inner
            // timer-read loop at 0xc0061650 spins forever.
            //
            // Approach #39: Patch the delay function's loop exit branch
            // at VA 0xc04bc494 (PA 0x084bc494) from BGE (conditional)
            // to B (unconditional), making it exit on first iteration.
            //
            // The delay function at 0xc04bc460 is a combined timeout +
            // UART serial poll used by "Security Modules v6.6" during
            // PM resume. Its inner loop:
            //   0xc04bc488: BL get_ticks
            //   0xc04bc48c: RSB R0, R4, R0   (R0 = now - target)
            //   0xc04bc490: CMP R0, #0
            //   0xc04bc494: BGE exit          ← patch this to B (always)
            //   0xc04bc498: LDR R3, [UART poll ptr]
            //   0xc04bc49c: BLX R3            (call UART poll)
            //
            // Finding #80: Cannot patch the inner wrapper at 0xc00536d0
            // because get_ticks() also uses it — patching it breaks the
            // timer and causes the delay to never exit.
            //
            // This approach patches the branch itself: BGE (0x5A00000E)
            // becomes B (0xEA00000E). The delay function exits immediately
            // without calling the UART poll at all.
            // Approach #40: Rewrite get_ticks to read hardware timer directly.
            //
            // Finding #81: get_ticks → 0xc00536d0 → big_function(0xc0062462)
            // is stuck because the kernel timebase structure is frozen.
            // Instead of understanding the big_function's loop, bypass it
            // entirely by making get_ticks read TICKSLOW from the hardware
            // timer at VA 0xe0099000 + 0x84.
            //
            // Original get_ticks (0xc04bc410, ARM):
            //   PUSH {R7,LR}; SUB SP,#8; MOV R0,SP;
            //   LDR R3,[PC+0x30]; BLX R3; ...epilogue...
            //
            // Patched get_ticks (4 ARM instructions = 16 bytes):
            //   LDR R3, [PC, #4]     ; R3 = 0xe0099000 (timer base VA)
            //   LDR R0, [R3, #0x84]  ; R0 = TICKSLOW
            //   BX LR                ; return
            //   .word 0xe0099000     ; literal pool
            {
                static bool getticks_patched = false;
                if (!getticks_patched) {
                    uint8_t patch[] = {
                        0x04, 0x30, 0x9f, 0xe5,  // LDR R3, [PC, #4]
                        0x84, 0x00, 0x93, 0xe5,  // LDR R0, [R3, #0x84]
                        0x1e, 0xff, 0x2f, 0xe1,  // BX LR
                        0x00, 0x90, 0x09, 0xe0,  // .word 0xe0099000
                    };
                    cpu_physical_memory_write(0x084bc410, patch, sizeof(patch));
                    getticks_patched = true;
                    fprintf(stderr, "[PMU] Patched get_ticks at PA 0x084bc410: "
                            "direct hardware timer read (bypass frozen timebase)\n");
                }
            }
            // Approach #41: Patch Security Modules serial console check.
            //
            // Historical approach #41. Finding #82's claim that no retained
            // kernel resume exists was later withdrawn: the address-zero
            // type-4 handoff/remap semantics are still unresolved.
            // The PM resume path calls a "Security Modules v6.6" function
            // at VA 0xc000f094 (PA 0x0800f094, Thumb) that loops calling
            // delay_func + UART poll waiting for serial debugger input.
            //
            // The caller at VA 0xc000fc3a loops:
            //   BL 0xc000f094       ; call secmod check
            //   LDR R3, [R6, R4]    ; load flag at [0xc01c0f8c]
            //   CMP R3, #0
            //   BEQ loop_start      ; if flag==0, keep looping
            //
            // Finding #86: Setting [0xc01c0f8c]=1 exits the loop but
            // the post-loop code uses [R6+0x5F0] as a data pointer
            // (computed by the secmod function) → panic.
            //
            // Better approach: Patch the CALLER at 0xc00203c4 to skip
            // the entire Security Modules call. The function at 0xc00203c4
            // (Thumb, PUSH {R4, R7, LR}) calls into the secmod machinery.
            // Patch it to return immediately.
            {
                static bool secmod_patched = false;
                if (!secmod_patched) {
                    uint8_t bxlr_thumb[] = {
                        0x00, 0x20,  // MOVS R0, #0
                        0x70, 0x47,  // BX LR
                    };

                    // Patch 1: Security Modules function entry at 0xc000f094.
                    // This Thumb function loops calling delay_func + UART poll
                    // waiting for serial debugger input. Make it return 0.
                    cpu_physical_memory_write(0x0800f094, bxlr_thumb,
                                              sizeof(bxlr_thumb));

                    // Patch 2: Security Modules caller at 0xc00203c4.
                    // Higher-level function that calls into the secmod machinery.
                    cpu_physical_memory_write(0x080203c4, bxlr_thumb,
                                              sizeof(bxlr_thumb));

                    // Patch 3: Disable the debugger protocol handler function.
                    // Finding #90: The debugger protocol state machine at
                    // VA 0xc000faf4 (PA 0x0800faf4) is a complex function
                    // spanning ~0x450 bytes (fc10-10340) with 4+ loop-back
                    // branches. It processes KDP packets and never exits
                    // without actual debugger interaction.
                    //
                    // The function entry is PUSH {R4-R7, LR} at PA 0x0800faf4.
                    // Multiple callers dispatch to this function via vtable.
                    // Patching individual loop branches is whack-a-mole.
                    //
                    // Fix: Replace function entry with MOVS R0,#0 + BX LR.
                    // This catches ALL call paths. The function returns 0
                    // immediately, and callers handle R0=0 as success.
                    cpu_physical_memory_write(0x0800faf4, bxlr_thumb,
                                              sizeof(bxlr_thumb));

                    secmod_patched = true;
                    fprintf(stderr, "[PMU] Patched Security Modules: "
                            "entry@f094+caller@203c4=BX LR, "
                            "debugger handler@faf4=BX LR\n");
                }
            }
            // Approach #43 — Deferred sleep patch (on P press, not at OOCSHDWN).
            //
            // Previous approach (#34) pre-patched the sleep function at OOCSHDWN
            // time so it returned R0=0 immediately. This made the PM resume path
            // run BEFORE the power button press, so IOPMrootDomain never received
            // the ONKEY event and the display stayed OFF.
            //
            // New approach: let the CPU enter the B . sleep loop normally.
            // When the user presses P, patch the sleep function at that moment
            // so the ONKEY event and wake happen simultaneously. The kernel's
            // PMU interrupt handler then processes ONKEY, triggering the
            // IOPMrootDomain display-on transition.
            //
            // The sleep function patch is applied in pcf50633_set_onkey()
            // when it detects the CPU is in the sleep loop.
            //
            // Still mark that OOCSHDWN fired so the other patches (get_ticks,
            // SecMod, debugger) remain available for the PM resume path.
            s->oocshdwn_fired = true;
            fprintf(stderr, "[PMU] OOCSHDWN: sleep function NOT patched "
                    "(deferred to P press — approach #43)\n");

            if (s->wake_reset_pending) {
                s->wake_reset_pending = false;
                s->int1 |= PMU_INT1_ONKEYF;
                pcf50633_update_irq(s);
                fprintf(stderr, "[WAKE] Completing queued retained-RAM "
                        "SoC reboot after OOCSHDWN\n");
                qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
            }

            // Finding #93 (REVERTED): IOPMrootDomain wake transition patches.
            //
            // These patches modified setPowerState and powerChangeDone to
            // force the IOPMrootDomain wake transition. REVERTED because:
            //   - BEQ→B unconditional in powerChangeDone affects ALL power
            //     state transitions, not just SLEEP→ON, corrupting the PM
            //     state machine during normal pre-sleep transitions.
            //   - MOVS R3,#0x44 in setPowerState sets wake-related flags
            //     for ALL transitions, not just SLEEP.
            //   - This caused a regression: power/home buttons stopped
            //     working entirely after the patches were applied during
            //     the first sleep. The PM state machine entered an invalid
            //     state and could no longer process button events.
            //
            // TODO: Find a way to trigger changePowerStateToPriv(ON_STATE=3)
            // only AFTER the patched sleep returns, without affecting other
            // PM transitions. Possibilities:
            //   a) Write flags directly to the IOPMrootDomain instance
            //      memory (need to find instance address at runtime)
            //   b) Inject a deferred callback that calls the wake function
            //   c) Hook the powerChangeDone call specifically during the
            //      sleep→wake transition
            {
                // Patches disabled — keeping the block for future work
            }

            // Approach #43: VIC cleanup is now scheduled from pcf50633_set_onkey()
            // when the P press triggers the deferred sleep patch. Not needed here
            // since the CPU will be in the B . loop until then.
            break;
        }
        default:
            break;
    }
}

static uint8_t pcf50633_recv(I2CSlave *i2c)
{
    Pcf50633State *s = PCF50633(i2c);

    time_t t = time(NULL);
    struct tm tm = *localtime(&t);

    int res = 0;

    switch(s->cmd) {
        case PMU_INT1:
            if (s->int1_shadow) {
                // Return shadow value saved during wake (INT1 was already
                // cleared to de-assert nIRQ; shadow preserves the ONKEY bits
                // for the kernel's deferred workqueue).
                res = s->int1_shadow;
                s->int1_shadow = 0;
                fprintf(stderr, "[PMU] INT1 read -> 0x%02x (from SHADOW, wake path)\n", res);
            } else {
                res = s->int1;
                s->int1 = 0;
                fprintf(stderr, "[PMU] INT1 read -> 0x%02x (cleared)\n", res);
            }
            pcf50633_update_irq(s);  // may de-assert nIRQ
            break;
        case PMU_INT2:
            res = s->int2;
            s->int2 = 0;
            pcf50633_update_irq(s);
            break;
        case PMU_INT3:
            res = s->int3;
            s->int3 = 0;
            pcf50633_update_irq(s);
            break;
        case PMU_INT4:
            res = s->int4;
            s->int4 = 0;
            pcf50633_update_irq(s);
            break;
        case PMU_INT5:
            res = s->int5;
            s->int5 = 0;
            pcf50633_update_irq(s);
            break;
        case PMU_INT1M:
            res = s->int1m;
            break;
        case PMU_INT2M:
            res = s->int2m;
            break;
        case PMU_INT3M:
            res = s->int3m;
            break;
        case PMU_INT4M:
            res = s->int4m;
            break;
        case PMU_INT5M:
            res = s->int5m;
            break;
        case PMU_MBCS1:
            res = 0;
            break;
        case PMU_ADCC1:
            res = 0;
            break;
        case PMU_RTCSC:
            res = int_to_bcd(tm.tm_sec);
            break;
        case PMU_RTCMN:
            res = int_to_bcd(tm.tm_min);
            break;
        case PMU_RTCHR:
            res = int_to_bcd(tm.tm_hour);
            break;
        case PMU_RTCDT:
            res = int_to_bcd(tm.tm_mday);
            break;
        case PMU_RTCMT:
            res = int_to_bcd(tm.tm_mon + 1);
            break;
        case PMU_RTCYR:
            res = int_to_bcd(tm.tm_year - 100);
            break;
        case 0x67:
            res = 1;
            break;
        case 0x69:
            res = 0;
            break;
        case 0x76:
            res = 0;
            break;
        default:
            res = 0;
    }

    s->cmd += 1;
    return res;
}

static int pcf50633_send(I2CSlave *i2c, uint8_t data)
{
    Pcf50633State *s = PCF50633(i2c);

    if (!s->has_reg_addr) {
        s->cmd = data;
        s->has_reg_addr = true;
    } else {
        pcf50633_write_reg(s, s->cmd, data);
        s->cmd += 1;
    }
    return 0;
}

static void pcf50633_init(Object *obj)
{
    Pcf50633State *s = PCF50633(obj);
    s->int1m = 0xFF;
    s->int2m = 0xFF;
    s->int3m = 0xFF;
    s->int4m = 0xFF;
    s->int5m = 0xFF;
    s->post_sleep_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
        pmu_post_sleep_vic_cleanup, s);
}

static void pcf50633_class_init(ObjectClass *klass, void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    k->event = pcf50633_event;
    k->recv = pcf50633_recv;
    k->send = pcf50633_send;
}

static const TypeInfo pcf50633_info = {
    .name          = TYPE_PCF50633,
    .parent        = TYPE_I2C_SLAVE,
    .instance_init = pcf50633_init,
    .instance_size = sizeof(Pcf50633State),
    .class_init    = pcf50633_class_init,
};

static void pcf50633_register_types(void)
{
    type_register_static(&pcf50633_info);
}

type_init(pcf50633_register_types)
