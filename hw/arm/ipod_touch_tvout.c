#include "hw/arm/ipod_touch_tvout.h"
#include "qapi/error.h"

/* Part of IT_FB_TRACE (see ipod_touch_lcd.c): the TVOut register traffic.
 * M68AP's SpringBoard attaches the AppleH1TVOut framebuffer and never
 * detaches it (N45AP's does within two log lines); what the driver programs
 * here before its silent wait identifies the completion it expects from
 * this otherwise RAM-backed stub. */
static bool it_tvout_trace_enabled(void)
{
    static int cached = -1;
    if (cached < 0) {
        cached = getenv("IT_FB_TRACE") != NULL;
    }
    return cached;
}

static void it_tvout_trace(IPodTouchTVOutState *s, const char *dir,
                           hwaddr offset, uint64_t value)
{
    static uint32_t n;

    if (!it_tvout_trace_enabled()) {
        return;
    }
    n++;
    if (n <= 256 || (n & 0x3FF) == 0) {
        fprintf(stderr, "[TVOUT%d] %s 0x%03x = 0x%08x (n=%u)\n",
                s->index, dir, (uint32_t)offset, (uint32_t)value, n);
    }
}

static uint64_t ipod_touch_tvout_read(void *opaque, hwaddr offset, unsigned size)
{
    IPodTouchTVOutState *s = (IPodTouchTVOutState *)opaque;

    it_tvout_trace(s, "rd", offset, s->data[offset]);
    return s->data[offset];
}

static void ipod_touch_tvout_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    IPodTouchTVOutState *s = (IPodTouchTVOutState *)opaque;

    it_tvout_trace(s, "wr", offset, value);

    //fprintf(stderr, "%s (%d): writing 0x%08x to 0x%08x\n", __func__, s->index, value, offset);
    s->data[offset] = value;
    // switch(offset) {
    //     case SDO_IRQ:
    //         s->sdo_irq_reg = value;
    //         break;
    //     default:
    //         break;
    // }
}

static const MemoryRegionOps ipod_touch_tvout_ops = {
    .read = ipod_touch_tvout_read,
    .write = ipod_touch_tvout_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_tvout_init(Object *obj)
{
    DeviceState *dev = DEVICE(obj);
    IPodTouchTVOutState *s = IPOD_TOUCH_TVOUT(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_tvout_ops, s, "tvout", 4096);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void ipod_touch_tvout_class_init(ObjectClass *klass, const void *data)
{

}

static const TypeInfo ipod_touch_tvout_type_info = {
    .name = TYPE_IPOD_TOUCH_TVOUT,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchTVOutState),
    .instance_init = ipod_touch_tvout_init,
    .class_init = ipod_touch_tvout_class_init,
};

static void ipod_touch_tvout_register_types(void)
{
    type_register_static(&ipod_touch_tvout_type_info);
}

type_init(ipod_touch_tvout_register_types)
