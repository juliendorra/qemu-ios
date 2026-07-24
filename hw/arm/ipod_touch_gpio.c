#include "hw/arm/ipod_touch_gpio.h"
#include "cpu.h"

/*
 * IT_GPIO_TRACE=<path|stderr> logs every GPIO MMIO access with guest time,
 * value and PC, to attribute polling to a driver (e.g. does AppleBaseband
 * spin on a radio GPIO while uart1 stays mute -- see
 * IPHONE_2G_BRINGUP_HANDOFF.md). A poll loop would swamp the file, so per
 * (dir,addr,pc) key the first 16 hits log verbatim, then only every
 * 4096th with the running count.
 */
static FILE *gpio_trace_fp;
static GHashTable *gpio_trace_counts;

static void gpio_trace(char dir, hwaddr addr, uint64_t value)
{
    static bool checked;

    if (!checked) {
        checked = true;
        const char *path = getenv("IT_GPIO_TRACE");
        if (path && *path) {
            if (!strcmp(path, "1") || !strcmp(path, "stderr")) {
                gpio_trace_fp = stderr;
            } else {
                gpio_trace_fp = fopen(path, "a");
            }
            gpio_trace_counts = g_hash_table_new(NULL, NULL);
        }
    }
    if (!gpio_trace_fp) {
        return;
    }
    uint32_t pc = current_cpu ? ARM_CPU(current_cpu)->env.regs[15] : 0;
    gpointer key = (gpointer)(((uint64_t)pc << 21) | (addr << 1) |
                              (dir == 'W'));
    uint64_t n = (uint64_t)g_hash_table_lookup(gpio_trace_counts, key) + 1;
    g_hash_table_insert(gpio_trace_counts, key, (gpointer)n);
    if (n > 16 && n % 4096 != 0) {
        return;
    }
    int64_t now = qemu_clock_get_us(QEMU_CLOCK_VIRTUAL);
    fprintf(gpio_trace_fp, "[%3lld.%06lld] %c 0x%03x = 0x%08x pc=%08x n=%llu\n",
            now / 1000000LL, now % 1000000LL, dir, (uint32_t)addr,
            (uint32_t)value, pc, (unsigned long long)n);
    fflush(gpio_trace_fp);
}

static void s5l8900_gpio_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, value, addr);
    IPodTouchGPIOState *s = (struct IPodTouchGPIOState *) opaque;

    gpio_trace('W', addr, value);
    switch(addr) {
      default:
        break;
    }
}

static uint64_t s5l8900_gpio_read(void *opaque, hwaddr addr, unsigned size)
{
    //fprintf(stderr, "%s: read from location 0x%08x\n", __func__, addr);
    IPodTouchGPIOState *s = (struct IPodTouchGPIOState *) opaque;
    uint64_t ret = 0;

    switch(addr) {
        case 0x2c4:
            ret = s->gpio_state;
            break;
        default:
            break;
    }

    gpio_trace('R', addr, ret);
    return ret;
}

static const MemoryRegionOps gpio_ops = {
    .read = s5l8900_gpio_read,
    .write = s5l8900_gpio_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void s5l8900_gpio_init(Object *obj)
{
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    DeviceState *dev = DEVICE(sbd);
    IPodTouchGPIOState *s = IPOD_TOUCH_GPIO(dev);

    memory_region_init_io(&s->iomem, obj, &gpio_ops, s, "gpio", 0x10000);
}

static void s5l8900_gpio_class_init(ObjectClass *klass, const void *data)
{

}

static const TypeInfo ipod_touch_gpio_info = {
    .name          = TYPE_IPOD_TOUCH_GPIO,
    .parent        = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchGPIOState),
    .instance_init = s5l8900_gpio_init,
    .class_init    = s5l8900_gpio_class_init,
};

static void ipod_touch_machine_types(void)
{
    type_register_static(&ipod_touch_gpio_info);
}

type_init(ipod_touch_machine_types)