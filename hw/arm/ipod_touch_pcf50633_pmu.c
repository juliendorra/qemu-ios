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
        case PMU_RESUME_STATUS:
            fprintf(stderr, "[PMU] RESUME_STATUS write <- 0x%02x%s\n",
                    val, (val & PMU_RESUME_ARMED) ? " (armed)" : "");
            if (val == 0x40 && s->lcd) {
                ipod_touch_lcd_resume_scanout(s->lcd);
            }
            break;
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

            fprintf(stderr, "[PMU] OOCSHDWN=0x%02x OOCWAKE=0x%02x "
                    "INT1M=0x%02x INT1=0x%02x RESUME=0x%02x\n",
                    val, s->regs[PMU_OOCWAKE], s->int1m, s->int1,
                    s->regs[PMU_RESUME_STATUS]);

            /*
             * OOCSHDWN is terminal for the powered application processor.
             * Do not patch or unwind the retained kernel: the wake reset and
             * iBoot type-4 handoff are responsible for resuming it.
             */
            s->oocshdwn_fired = true;
            fprintf(stderr, "[PMU] Application processor awaiting power loss\n");

            if (s->wake_reset_pending) {
                s->wake_reset_pending = false;
                s->regs[PMU_RESUME_STATUS] |= PMU_RESUME_WAKE;
                s->int1 |= PMU_INT1_ONKEYF | PMU_INT1_ONKEYR;
                pcf50633_update_irq(s);
                fprintf(stderr, "[WAKE] Completing queued retained-RAM "
                        "SoC reboot after OOCSHDWN\n");
                ipod_touch_record_retained_crc();
                qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
            }

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
            /* iBoot's warm path requires a valid power-on source while it
             * verifies the retained image. Model USB presence for that brief
             * boot phase; normal battery operation remains unchanged. */
            res = (s->regs[PMU_RESUME_STATUS] & PMU_RESUME_WAKE) ? 1 : 0;
            break;
        case PMU_ADCS1:
            /* 10-bit BATSNS result: 648 / 1023 * 6 V = 3.80 V. */
            res = 0xa2;
            break;
        case PMU_ADCS2:
            res = 0;
            break;
        case PMU_ADCS3:
            /* Conversion complete; BATSNS result low bits are zero. */
            res = 0x80;
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
        case PMU_RESUME_STATUS:
            res = s->regs[PMU_RESUME_STATUS];
            fprintf(stderr, "[PMU] RESUME_STATUS read -> 0x%02x%s\n",
                    res, (res & PMU_RESUME_ARMED) ? " (armed)" : "");
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
