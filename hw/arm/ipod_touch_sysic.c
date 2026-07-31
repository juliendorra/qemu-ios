#include "hw/arm/ipod_touch_sysic.h"
#include "migration/vmstate.h"
#include "hw/arm/ipod_touch_pcf50633_pmu.h"

static void gpio_irq_auto_lower(void *opaque)
{
    GPIOIRQLowerInfo *info = (GPIOIRQLowerInfo *)opaque;
    qemu_irq_lower(info->sysic->gpio_irqs[info->group]);
}

/*
 * IT_SYSIC_TRACE=1: the GPIO interrupt-controller conversation.
 *
 * The GPIO trace in ipod_touch_gpio.c shows pin-level MMIO, which cannot
 * answer the question that matters for a button: was the interrupt RAISED,
 * did the guest READ the status, and did it ACK. Buttons are raised from
 * ipod_touch_key_event(); everything else happens here.
 *
 * Repeats collapse per (direction, group): the first 24 print, then every
 * 4096th, so a polling guest cannot bury a single button edge.
 */
static void sysic_trace(const char *what, uint8_t group, uint32_t value)
{
    static int enabled = -1;
    static uint32_t counts[8][GPIO_NUMINTGROUPS];

    if (enabled < 0) {
        enabled = getenv("IT_SYSIC_TRACE") != NULL;
    }
    if (!enabled || group >= GPIO_NUMINTGROUPS) {
        return;
    }
    uint32_t slot = (uint32_t)(what[0] + what[1]) & 7;
    uint32_t n = ++counts[slot][group];
    if (n > 24 && (n & 0xFFF) != 0) {
        return;
    }
    int64_t now = qemu_clock_get_us(QEMU_CLOCK_VIRTUAL);
    fprintf(stderr, "[SYSIC] t=%lld.%06lld %s group %u = 0x%08x (n=%u)\n",
            now / 1000000LL, now % 1000000LL, what, group, value, n);
}

static uint64_t ipod_touch_sysic_read(void *opaque, hwaddr addr, unsigned size)
{
    IPodTouchSYSICState *s = (IPodTouchSYSICState *) opaque;

    //fprintf(stderr, "%s: offset = 0x%08x\n", __func__, addr);

    switch (addr) {
        case POWER_ID:
            /* Board-specific epoch: N45AP=2, M68AP=3 (see power_epoch). A
             * zero value means the field was never initialised, so fall back
             * to the historical N45AP default rather than reporting epoch 0. */
            return ((s->power_epoch ? s->power_epoch : 2) << 0x18);
        case POWER_SETSTATE:
        case POWER_STATE:
            return s->power_state;
        case 0x7a:
        case 0x7c:
            return 1;
        case GPIO_INTLEVEL ... (GPIO_INTLEVEL + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTLEVEL) / 4;
            /* Readback of the guest's own INTLEVEL writes (they were
             * silently DISCARDED until 2026-07-31, so this always read 0 --
             * "released" -- whatever the guest configured). */
            sysic_trace("rd INTLEVEL", group, s->gpio_int_level[group]);
            return s->gpio_int_level[group];
        }
        case GPIO_INTSTAT ... (GPIO_INTSTAT + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTSTAT) / 4;
            sysic_trace("rd INTSTAT", group, s->gpio_int_status[group]);
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
            uint8_t group = (addr - GPIO_INTLEVEL) / 4;
            /* Stored so the trace shows the polarity the guest asked for;
             * reads still return the stored word (see the read comment). */
            sysic_trace("wr INTLEVEL", group, (uint32_t)val);
            s->gpio_int_level[group] = val;
            break;
        }
        case GPIO_INTSTAT ... (GPIO_INTSTAT + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTSTAT) / 4;

            sysic_trace("ACK INTSTAT", group, (uint32_t)val);
            // acknowledge the interrupts and clear the corresponding bits
            s->gpio_int_status[group] = s->gpio_int_status[group] & ~val;

            qemu_irq_lower(s->gpio_irqs[group]);

            break;
        }
        case GPIO_INTEN ... (GPIO_INTEN + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTEN) / 4;
            sysic_trace("wr INTEN", group, (uint32_t)val);
            s->gpio_int_enabled[group] = val;
            break;
        }
        case GPIO_INTTYPE ... (GPIO_INTTYPE + GPIO_NUMINTGROUPS * 4):
        {
            uint8_t group = (addr - GPIO_INTTYPE) / 4;
            /* Stored and never consulted: the model delivers the same edge
             * whatever trigger type the guest asked for. */
            sysic_trace("wr INTTYPE", group, (uint32_t)val);
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
}

/*
 * Migration. SYSIC owns the GPIO interrupt block, which is the path the
 * multitouch ATN edge takes to reach the guest: the controller sets a bit in
 * gpio_int_status and pulses gpio_irqs[group].
 *
 * Restored at RESET, the enable/type masks are gone, so a frame the model
 * queues raises an edge the guest has no reason to look at -- the tap is
 * delivered, logged, and then simply never collected.
 *
 * The auto-lower timers are NOT migrated: they exist to drop an edge-triggered
 * pulse shortly after it is raised, so the worst a fresh one does is leave a
 * line high that the next pulse re-lowers. Their INFO struct is migrated,
 * because it says which group a pending lower belongs to.
 */
static const VMStateDescription vmstate_ipod_touch_sysic = {
    .name = "ipod-touch-sysic",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(power_state, IPodTouchSYSICState),
        VMSTATE_UINT32(power_epoch, IPodTouchSYSICState),
        VMSTATE_UINT32_ARRAY(gpio_int_level, IPodTouchSYSICState,
                             GPIO_NUMINTGROUPS),
        VMSTATE_UINT32_ARRAY(gpio_int_status, IPodTouchSYSICState,
                             GPIO_NUMINTGROUPS),
        VMSTATE_UINT32_ARRAY(gpio_int_enabled, IPodTouchSYSICState,
                             GPIO_NUMINTGROUPS),
        VMSTATE_UINT32_ARRAY(gpio_int_type, IPodTouchSYSICState,
                             GPIO_NUMINTGROUPS),
        VMSTATE_END_OF_LIST()
    },
};

static void ipod_touch_sysic_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->vmsd = &vmstate_ipod_touch_sysic;
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
