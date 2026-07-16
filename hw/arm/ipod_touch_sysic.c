#include "hw/arm/ipod_touch_sysic.h"
#include "hw/arm/ipod_touch_pcf50633_pmu.h"
#include "hw/core/cpu.h"       // first_cpu
extern int cpu_memory_rw_debug(CPUState *cpu, vaddr addr,
                               void *ptr, size_t len, bool is_write);

static void gpio_irq_auto_lower(void *opaque)
{
    GPIOIRQLowerInfo *info = (GPIOIRQLowerInfo *)opaque;
    qemu_irq_lower(info->sysic->gpio_irqs[info->group]);
}

// Deferred PMU nIRQ re-assertion callback.
// Called after a short delay when the OS acknowledges GPIO_INTSTAT for the
// PMU's group.  By this point, the GPIO ISR has dispatched to the PMU
// sub-IRQ handler (approach #27 cleared the stuck byte[1] flag).
//
// The handler uses deferred/threaded processing: it sets byte[1]=1 (requesting
// a workqueue callback) but doesn't read INT1-5 inline.  The workqueue item
// needs the scheduler to run, which requires the CPU's idle loop to not
// immediately re-disable interrupts.
//
// To prevent the re-assertion storm: if INT1-5 still have pending bits after
// the handler has had its chance, clear them here.  The handler already queued
// the workqueue item during its inline execution, so the deferred processing
// will still happen — we just prevent the PMU from re-asserting nIRQ in a
// tight loop that starves the scheduler.
// Deferred PMU nIRQ cleanup + ONKEY re-injection.
//
// Finding #59: without this clear, the kernel's idle loop runs with F=1
// (FIQ disabled), blocking timer FIQs and preventing the scheduler.
// The clear restores the hardware to a clean state so the kernel uses
// its normal idle path (F=0, allowing timer FIQ).
//
// But the clear wipes INT1 (ONKEY) before the PMU driver can read it.
// Solution: clear INT1 now to fix the idle loop, then re-inject ONKEY
// after a 1-second delay when the scheduler is running and the workqueue
// can process the event.
static void pmu_onkey_reinject_callback(void *opaque)
{
    IPodTouchSYSICState *s = (IPodTouchSYSICState *)opaque;
    if (s->pmu && s->pmu_wake_clear_active) {
        s->pmu_wake_clear_active = false;  // one-shot complete: no more deferred clears

        // Clear stuck byte[1] at PMU sub-IRQ entry before re-injecting.
        // The initial wake cleared it, but the GPIO ISR sets it back to 1
        // when processing the first ONKEY. Without clearing it again,
        // the ISR won't dispatch to the PMU driver for the re-injected event.
        #define PMU_SUB_IRQ_ENTRY_VA 0xE0248AA0
        CPUState *cs = first_cpu;
        if (cs) {
            uint8_t entry_bytes[4];
            if (cpu_memory_rw_debug(cs, PMU_SUB_IRQ_ENTRY_VA,
                                     entry_bytes, 4, 0) == 0) {
                if (entry_bytes[1] != 0) {
                    entry_bytes[1] = 0;  // clear "handling" flag
                    entry_bytes[2] = 0;  // clear "re-run" flag
                    cpu_memory_rw_debug(cs, PMU_SUB_IRQ_ENTRY_VA,
                                         entry_bytes, 4, 1);
                    fprintf(stderr, "[SYSIC] Cleared stuck byte[1] before "
                            "ONKEY re-inject\n");
                }
            }
        }
        #undef PMU_SUB_IRQ_ENTRY_VA

        fprintf(stderr, "[SYSIC] Re-injecting ONKEY event (delayed, one-shot)\n");
        pcf50633_set_onkey(s->pmu, true);   // press
        pcf50633_set_onkey(s->pmu, false);  // release

        // Schedule a final cleanup clear to de-assert nIRQ.
        // The re-injection sets INT1=0xc0, which keeps nIRQ asserted.
        // Finding #59: with nIRQ asserted, the kernel's idle loop uses
        // CPSID IF (disabling FIQs), blocking timer + all other interrupts.
        // This cleanup restores the normal idle path (F=0).
        // Since pmu_wake_clear_active is now false, pmu_reassert_callback
        // will clear INT1 but NOT schedule another re-injection (no loop).
        timer_mod(s->pmu_reassert_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                  + PMU_REASSERT_DELAY_NS);
        fprintf(stderr, "[SYSIC] Cleanup INT1 clear scheduled in 200ms\n");
    }
}

static void pmu_reassert_callback(void *opaque)
{
    IPodTouchSYSICState *s = (IPodTouchSYSICState *)opaque;
    if (s->pmu) {
        bool had_onkey = s->pmu->int2 & (PMU_INT2_ONKEYF | PMU_INT2_ONKEYR);

        // Clear pending INT bits so the kernel's idle loop uses F=0 path.
        if (s->pmu->int1 || s->pmu->int2 || s->pmu->int3 ||
            s->pmu->int4 || s->pmu->int5) {
            fprintf(stderr, "[SYSIC] Deferred clear: PMU INT1=0x%02x "
                    "→ 0x00 (clearing for clean idle)\n", s->pmu->int1);
            s->pmu->int1 = 0;
            s->pmu->int2 = 0;
            s->pmu->int3 = 0;
            s->pmu->int4 = 0;
            s->pmu->int5 = 0;
        }
        pcf50633_update_irq(s->pmu);

        // Schedule ONKEY re-injection after scheduler is running.
        // By then timer FIQs are active and the workqueue can process it.
        if (had_onkey && s->pmu_wake_clear_active) {
            timer_mod(s->pmu_onkey_reinject_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                      + 1000000000LL);  // +1 second
            fprintf(stderr, "[SYSIC] ONKEY re-inject scheduled in 1s\n");
        }
    }
}

static uint64_t ipod_touch_sysic_read(void *opaque, hwaddr addr, unsigned size)
{
    IPodTouchSYSICState *s = (IPodTouchSYSICState *) opaque;

    //fprintf(stderr, "%s: offset = 0x%08x\n", __func__, addr);

    switch (addr) {
        case POWER_ID:
            //return (3 << 24); //for older iboots
            return (2 << 0x18);
        case POWER_SETSTATE:
        case POWER_STATE:
            return s->power_state;
        case 0x7a:
        case 0x7c:
            return 1;
        case GPIO_INTLEVEL ... (GPIO_INTLEVEL + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTLEVEL) / 4;
            return s->gpio_int_level[group];
        }
        case GPIO_INTSTAT ... (GPIO_INTSTAT + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTSTAT) / 4;
            return s->gpio_int_status[group];
        }
        case GPIO_INTEN ... (GPIO_INTEN + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTEN) / 4;
            return s->gpio_int_enabled[group];
        }
        case GPIO_INTTYPE ... (GPIO_INTTYPE + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTTYPE) / 4;
            return s->gpio_int_type[group];
        }
      default:
        break;
    }
    return 0;
}

static void ipod_touch_sysic_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    IPodTouchSYSICState *s = (IPodTouchSYSICState *) opaque;

    //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, val, addr);

    switch (addr) {
        case POWER_ONCTRL:
            /* POWER_STATE reports domains that are still off. Turning a
             * domain on completes immediately in this functional model, so
             * clear the requested bits instead of latching them forever. */
            s->power_state &= ~val;
            break;
        case POWER_OFFCTRL:
            s->power_state |= val;
            break;
        case GPIO_INTLEVEL ... (GPIO_INTLEVEL + GPIO_NUMINTGROUPS * 4):
        {
            break;
        }
        case GPIO_INTSTAT ... (GPIO_INTSTAT + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTSTAT) / 4;

            // acknowledge the interrupts and clear the corresponding bits
            s->gpio_int_status[group] = s->gpio_int_status[group] & ~val;

            qemu_irq_lower(s->gpio_irqs[group]);

            // Approach #31 — INT1 shadow clear:
            // When the kernel acks the PMU GPIO interrupt during wake,
            // IMMEDIATELY save INT1 to the shadow register and clear it.
            // This de-asserts nIRQ and INTLEVEL, breaking the CPSID IF
            // chicken-and-egg (finding #64).  The kernel's deferred PMU
            // workqueue will later read INT1 via I2C and get the shadow
            // value (which contains the ONKEY bits).
            if (group == PMU_INT_GPIO_GROUP && s->pmu
                && s->pmu_wake_clear_active) {
                s->pmu_wake_clear_active = false;  // one-shot
                fprintf(stderr, "[SYSIC] Wake shadow-clear: INT2=0x%02x saved to shadow, "
                        "clearing INT1-5 to de-assert nIRQ\n", s->pmu->int2);
                s->pmu->int2_shadow = s->pmu->int2;
                s->pmu->int1 = 0;
                s->pmu->int2 = 0;
                s->pmu->int3 = 0;
                s->pmu->int4 = 0;
                s->pmu->int5 = 0;
                pcf50633_update_irq(s->pmu);  // de-asserts nIRQ + INTLEVEL
            }

            break;
        }
        case GPIO_INTEN ... (GPIO_INTEN + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTEN) / 4;
            s->gpio_int_enabled[group] = val;
            break;
        }
        case GPIO_INTTYPE ... (GPIO_INTTYPE + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTTYPE) / 4;
            s->gpio_int_type[group] = val;
            break;
        }
        default:
            break;
    }
}

static const MemoryRegionOps ipod_touch_sysic_ops = {
    .read = ipod_touch_sysic_read,
    .write = ipod_touch_sysic_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_sysic_init(Object *obj)
{
    IPodTouchSYSICState *s = IPOD_TOUCH_SYSIC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_sysic_ops, s, TYPE_IPOD_TOUCH_SYSIC, 0x1000);
    sysbus_init_mmio(sbd, &s->iomem);
    for(int grp = 0; grp < GPIO_NUMINTGROUPS; grp++) {
        sysbus_init_irq(sbd, &s->gpio_irqs[grp]);
        s->gpio_irq_lower_info[grp].sysic = s;
        s->gpio_irq_lower_info[grp].group = grp;
        s->gpio_irq_lower_timers[grp] = timer_new_ns(QEMU_CLOCK_VIRTUAL,
            gpio_irq_auto_lower, &s->gpio_irq_lower_info[grp]);
    }
    s->pmu_reassert_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
        pmu_reassert_callback, s);
    s->pmu_onkey_reinject_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
        pmu_onkey_reinject_callback, s);
}

static void ipod_touch_sysic_class_init(ObjectClass *klass, void *data)
{
    
}

static const TypeInfo ipod_touch_sysic_type_info = {
    .name = TYPE_IPOD_TOUCH_SYSIC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchSYSICState),
    .instance_init = ipod_touch_sysic_init,
    .class_init = ipod_touch_sysic_class_init,
};

static void ipod_touch_sysic_register_types(void)
{
    type_register_static(&ipod_touch_sysic_type_info);
}

type_init(ipod_touch_sysic_register_types)
