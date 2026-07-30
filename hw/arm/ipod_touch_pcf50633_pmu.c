#include "hw/arm/ipod_touch_pcf50633_pmu.h"
#include "migration/vmstate.h"
#include "hw/arm/ipod_touch_sysic.h"
#include "hw/arm/ipod_touch_lcd.h"
#include "hw/intc/pl192.h"
#include "qemu/main-loop.h"
#include "system/runstate.h"
#include "qemu/timer.h"
#include "system/rtc.h"

// Check if any interrupt is pending and update the nIRQ line.
// nIRQ is active-low and level-triggered: stays asserted as long as
// any INTx register has unread bits (ONKEY bypasses masks).
void pcf50633_update_irq(Pcf50633State *s)
{
    if (!s->sysic) return;

    bool onkey_pending = s->int2 & (PMU_INT2_ONKEYF | PMU_INT2_ONKEYR);
    bool other_pending = (s->int1 & ~s->int1m) ||
                         (s->int2 & ~s->int2m &
                          ~(PMU_INT2_ONKEYF | PMU_INT2_ONKEYR)) ||
                         (s->int3 & ~s->int3m) ||
                         (s->int4 & ~s->int4m) ||
                         (s->int5 & ~s->int5m);

    if (onkey_pending || other_pending) {
        // Assert nIRQ: set GPIO status + level bits and raise IRQ line
        s->sysic->gpio_int_status[PMU_INT_GPIO_GROUP] |= (1 << PMU_INT_GPIO_BIT);
        s->sysic->gpio_int_level[PMU_INT_GPIO_GROUP] |= (1 << PMU_INT_GPIO_BIT);
        qemu_irq_raise(s->sysic->gpio_irqs[PMU_INT_GPIO_GROUP]);
        fprintf(stderr, "[PMU] nIRQ assert: int1=0x%02x int2=0x%02x "
                "masks=%02x/%02x\n",
                s->int1, s->int2, s->int1m, s->int2m);
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

void pcf50633_set_onkey(Pcf50633State *s, bool pressed)
{
    if (pressed) {
        s->regs[PMU_OOCSTAT] &= ~PMU_OOCSTAT_ONKEY;
        s->int2 |= PMU_INT2_ONKEYF;
    } else {
        s->regs[PMU_OOCSTAT] |= PMU_OOCSTAT_ONKEY;
        s->int2 |= PMU_INT2_ONKEYR;
    }
    fprintf(stderr, "[PMU] ONKEY %s  int2=0x%02x oocstat=0x%02x\n",
            pressed ? "pressed" : "released", s->int2,
            s->regs[PMU_OOCSTAT]);

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

static bool pcf50633_trace_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        enabled = g_getenv("IPOD_TRACE_PMU") != NULL;
    }
    return enabled;
}

static void pcf50633_write_reg(Pcf50633State *s, uint8_t reg, uint8_t val)
{
    if (pcf50633_trace_enabled()) {
        fprintf(stderr, "[PMUTRACE] write 0x%02x <- 0x%02x\n", reg, val);
    }
    // Store all writes in register file for debug inspection
    s->regs[reg] = val;
    switch (reg) {
        case PMU_RESUME_STATUS:
            fprintf(stderr, "[PMU] RESUME_STATUS write <- 0x%02x%s\n",
                    val, (val & PMU_RESUME_ARMED) ? " (armed)" : "");
            if (val == 0x40) {
                if (s->prewarm_active && !s->prewarm_wake_requested) {
                    /* Pre-warmed boot reached the type-4 commit with no wake
                     * button pressed yet. Park the whole machine here; the
                     * kernel resume (RTC resync, wake-cause read) must not
                     * run until a real Power/Home press. vm_stop() cannot be
                     * called from the vCPU thread, so park from a bottom
                     * half. */
                    fprintf(stderr, "[WAKE] Pre-warm reached type-4 commit; "
                            "parking\n");
                    timer_del(s->prewarm_deadline);
                    qemu_bh_schedule(s->prewarm_park_bh);
                    break;
                }
                /* iBoot has consumed the read-clear interrupt status and is
                 * committing its type-4 branch. Re-expose the retained PMU
                 * wake cause for the kernel's resume decoder. */
                s->int2 |= s->retained_int2_wake;
                s->retained_int2_reexposed = true;
                s->prewarm_active = false;
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
                    "INT1=%02x/%02x INT2=%02x/%02x RESUME=0x%02x\n",
                    val, s->regs[PMU_OOCWAKE], s->int1, s->int1m,
                    s->int2, s->int2m,
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
                s->int2 |= PMU_INT2_EXTON1R;
                s->retained_int2_wake |= PMU_INT2_EXTON1R;
                s->retained_int2_reexposed = false;
                fprintf(stderr, "[WAKE] Completing queued retained-RAM "
                        "SoC reboot after OOCSHDWN\n");
                ipod_touch_prepare_retained_wake();
                qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
            } else if (val == 0x02) {
                int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
                bool recent_press = s->last_button_press_ns &&
                    now - s->last_button_press_ns <
                        3 * NANOSECONDS_PER_SECOND;

                s->regs[PMU_RESUME_STATUS] |= PMU_RESUME_WAKE;
                s->retained_int2_reexposed = false;
                if (recent_press) {
                    /* A Power/Home press raced the sleep commit: the user
                     * wanted the device awake. Wake immediately instead of
                     * silently pre-warming and parking. */
                    s->int2 |= PMU_INT2_EXTON1R;
                    s->retained_int2_wake |= PMU_INT2_EXTON1R;
                    fprintf(stderr, "[WAKE] Press raced OOCSHDWN; waking "
                            "immediately\n");
                } else {
                    /* Untouched sleep. Pre-run the retained wake boot now,
                     * with the panel off and input closed, and park just
                     * before the type-4 handoff (see PMU_RESUME_STATUS
                     * above). A later Power/Home press then only pays for
                     * the kernel resume. */
                    if (s->prewarm_no_park) {
                        /* This firmware never signals the type-4 commit, so a
                         * pre-warm would just boot all the way through and
                         * idle out again. Stay asleep instead; the Power/Home
                         * handler starts the wake boot on demand. */
                        fprintf(stderr, "[WAKE] Sleeping (this firmware cannot "
                                "be pre-warmed); press Power or Home to wake\n");
                        /* prewarm_active gates both the park BH and the
                         * Power/Home wake branch, so it must be set even
                         * though no pre-warm boot is being run. */
                        s->prewarm_active = true;
                        s->prewarm_wake_requested = false;
                        qemu_bh_schedule(s->prewarm_park_bh);
                        break;
                    }
                    s->prewarm_active = true;
                    s->prewarm_parked = false;
                    s->prewarm_wake_requested = false;
                    timer_mod(s->prewarm_deadline,
                              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                              25 * NANOSECONDS_PER_SECOND);
                    fprintf(stderr, "[WAKE] Pre-warming retained-RAM wake "
                            "after OOCSHDWN\n");
                }
                ipod_touch_prepare_retained_wake();
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

    struct tm tm;

    /* The PCF50633 RTC stores an absolute UTC wall clock. iPod OS applies
     * the user-selected timezone itself. Returning host local time here made
     * Paris daylight time get added twice (+2 hours in summer). Use QEMU's
     * RTC clock so the standard -rtc base/clock options also remain valid. */
    qemu_get_timedate(&tm, 0);

    int res = 0;

    switch(s->cmd) {
        case PMU_INT1:
            res = s->int1;
            s->int1 = 0;
            fprintf(stderr, "[PMU] INT1 read -> 0x%02x (cleared)\n", res);
            pcf50633_update_irq(s);  // may de-assert nIRQ
            break;
        case PMU_INT2:
            res = s->int2;
            s->int2 = 0;
            if (s->retained_int2_reexposed &&
                (res & s->retained_int2_wake)) {
                /* iBoot and the retained-resume prologue have both touched
                 * the reset VIC domain. Complete that domain reset before
                 * deferred kernel resume work starts using GPIO IRQs. */
                if (s->vic0) {
                    pl192_reset_priority((PL192State *)s->vic0);
                }
                if (s->vic1) {
                    pl192_reset_priority((PL192State *)s->vic1);
                }
                s->retained_int2_wake = 0;
                s->retained_int2_reexposed = false;
            }
            fprintf(stderr, "[PMU] INT2 read -> 0x%02x (cleared)\n", res);
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
             * verifies the retained image. USBPRES without USBOK describes
             * an invalid/weak source and selects the low-battery UI. Report
             * a coherent present-and-valid USB source during that phase. */
            res = (s->regs[PMU_RESUME_STATUS] & PMU_RESUME_WAKE) ?
                PMU_MBCS1_USBPRES | PMU_MBCS1_USBOK : 0;
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

    if (pcf50633_trace_enabled()) {
        fprintf(stderr, "[PMUTRACE] read 0x%02x -> 0x%02x\n", s->cmd, res);
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

static void pcf50633_prewarm_deadline(void *opaque)
{
    Pcf50633State *s = opaque;

    if (!s->prewarm_active || s->prewarm_parked) {
        return;
    }
    /* No type-4 commit arrived: this bootloader does not announce its handoff
     * (iBoot-159 never writes RESUME_STATUS). Park the pre-warmed boot where
     * it stands and stop pre-warming for the rest of the session. */
    s->prewarm_no_park = true;
    fprintf(stderr, "[WAKE] No type-4 commit within the deadline; this "
            "firmware cannot be pre-warmed -- parking here and sleeping\n");
    /* vm_stop() must NOT be called from a QEMU_CLOCK_VIRTUAL timer callback:
     * pause_all_vcpus() disables that very clock, which waits for this
     * timerlist to finish running -- a deadlock that freezes the whole main
     * loop, so QMP is never serviced and no wake key can ever arrive. Park
     * from the bottom half, exactly like the type-4 commit path does. */
    qemu_bh_schedule(s->prewarm_park_bh);
}

static void pcf50633_prewarm_park(void *opaque)
{
    Pcf50633State *s = opaque;

    if (!s->prewarm_active) {
        return;
    }
    if (s->prewarm_wake_requested) {
        /* A wake button arrived between the type-4 commit and this timer.
         * Complete the pass-through that the commit write skipped. */
        s->int2 |= s->retained_int2_wake;
        s->retained_int2_reexposed = true;
        s->prewarm_active = false;
        return;
    }
    s->prewarm_parked = true;
    vm_stop(RUN_STATE_SUSPENDED);
    fprintf(stderr, "[WAKE] Pre-warmed wake parked; awaiting Power/Home\n");
}

static void pcf50633_init(Object *obj)
{
    Pcf50633State *s = PCF50633(obj);
    s->int1m = 0xFF;
    s->int2m = 0xFF;
    s->int3m = 0xFF;
    s->int4m = 0xFF;
    s->int5m = 0xFF;
    s->regs[PMU_OOCSTAT] = PMU_OOCSTAT_ONKEY;
    s->prewarm_park_bh = qemu_bh_new(pcf50633_prewarm_park, s);
    s->prewarm_deadline = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                       pcf50633_prewarm_deadline, s);
}

/*
 * Migration. The PMU is the third device made migratable, and it was found by
 * measurement rather than by working down a list: a snapshot restored in the
 * browser loaded, painted the home screen, and then went to RunState 12 --
 * RUN_STATE_SUSPENDED -- within a second. There is exactly one caller of
 * vm_stop(RUN_STATE_SUSPENDED) in the tree, and it is this device's pre-warm
 * park.
 *
 * With no VMStateDescription the PMU came back at RESET while the guest resumed
 * mid-flight, so the sleep/wake state machine restarted from the wrong place and
 * parked a machine that should have been running. Note the shape of the bug: not
 * a lost register value, but a lost position in a STATE MACHINE that stops the
 * whole VM.
 *
 * NOT migrated: the i2c parent (handled by its own vmstate), the vic0/vic1/lcd/
 * sysic back-pointers (wired at machine init, before the incoming state loads),
 * and prewarm_park_bh (a bottom half, recreated by instance_init).
 *
 * The pre-warm deadline timer IS migrated. It is what distinguishes "this
 * firmware can be pre-warmed" from "park where we are"; restoring without it
 * would re-arm nothing and leave prewarm_active set for ever.
 */
static const VMStateDescription vmstate_pcf50633 = {
    .name = "pcf50633-pmu",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_I2C_SLAVE(i2c, Pcf50633State),
        VMSTATE_UINT32(cmd, Pcf50633State),
        VMSTATE_BOOL(has_reg_addr, Pcf50633State),
        VMSTATE_UINT8(int1, Pcf50633State),
        VMSTATE_UINT8(int2, Pcf50633State),
        VMSTATE_UINT8(int3, Pcf50633State),
        VMSTATE_UINT8(int4, Pcf50633State),
        VMSTATE_UINT8(int5, Pcf50633State),
        VMSTATE_UINT8(retained_int2_wake, Pcf50633State),
        VMSTATE_BOOL(retained_int2_reexposed, Pcf50633State),
        VMSTATE_UINT8(int1m, Pcf50633State),
        VMSTATE_UINT8(int2m, Pcf50633State),
        VMSTATE_UINT8(int3m, Pcf50633State),
        VMSTATE_UINT8(int4m, Pcf50633State),
        VMSTATE_UINT8(int5m, Pcf50633State),
        VMSTATE_UINT8_ARRAY(regs, Pcf50633State, 256),
        VMSTATE_BOOL(oocshdwn_fired, Pcf50633State),
        VMSTATE_BOOL(wake_reset_pending, Pcf50633State),
        VMSTATE_BOOL(prewarm_active, Pcf50633State),
        VMSTATE_BOOL(prewarm_parked, Pcf50633State),
        VMSTATE_BOOL(prewarm_wake_requested, Pcf50633State),
        VMSTATE_BOOL(prewarm_no_park, Pcf50633State),
        VMSTATE_TIMER_PTR(prewarm_deadline, Pcf50633State),
        VMSTATE_INT64(last_button_press_ns, Pcf50633State),
        VMSTATE_END_OF_LIST()
    },
};

static void pcf50633_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);

    k->event = pcf50633_event;
    k->recv = pcf50633_recv;
    k->send = pcf50633_send;
    dc->vmsd = &vmstate_pcf50633;
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
