#include "hw/arm/ipod_touch_sdio.h"
#include "hw/core/irq.h"
#include "system/dma.h"

/* Opt-in stage-0 protocol tracing (SLEEP_WAKE_INVESTIGATION.md, Wi-Fi plan).
 * Enabled with IPOD_SDIO_TRACE=1; rate-limited so an access storm cannot
 * flood the host log. Not compiled out: the check is one cached branch. */
#define SDIO_TRACE_LINE_LIMIT 60000

static ssize_t ipod_touch_sdio_receive(NetClientState *nc,
                                       const uint8_t *buf, size_t size)
{
    IPodTouchSDIOState *s = qemu_get_nic_opaque(nc);

    mv8686_receive_frame(&s->card, buf, size);
    return size;
}

static NetClientInfo ipod_touch_sdio_net_info = {
    .type = NET_CLIENT_DRIVER_NIC,
    .size = sizeof(NICState),
    .receive = ipod_touch_sdio_receive,
};

static void ipod_touch_sdio_send_frame(void *opaque, const uint8_t *buf,
                                       size_t size)
{
    IPodTouchSDIOState *s = opaque;

    if (s->nic) {
        qemu_send_packet(qemu_get_queue(s->nic), buf, size);
    }
}

static bool sdio_trace_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        const char *env = getenv("IPOD_SDIO_TRACE");
        enabled = (env && env[0] && strcmp(env, "0") != 0) ? 1 : 0;
    }
    return enabled;
}

static void G_GNUC_PRINTF(1, 2) sdio_trace(const char *fmt, ...)
{
    static unsigned lines;
    va_list ap;

    if (!sdio_trace_enabled()) {
        return;
    }
    if (lines >= SDIO_TRACE_LINE_LIMIT) {
        if (lines == SDIO_TRACE_LINE_LIMIT) {
            fprintf(stderr, "[sdio] trace line limit reached; suppressing\n");
            lines++;
        }
        return;
    }
    lines++;
    fprintf(stderr, "[sdio] ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n");
}

static void sdio_update_irq(IPodTouchSDIOState *s)
{
    qemu_set_irq(s->irq, (s->irq_reg & s->irq_mask) != 0);
}

static void sdio_card_irq(void *opaque, int level)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(opaque);

    if (level) {
        s->irq_reg |= SDIO_IRQ_CARD_INT;
    }
    /* deassert only through the guest's write-one-to-clear */
    sdio_update_irq(s);
}

static void sdio_exec_cmd53(IPodTouchSDIOState *s)
{
    uint32_t arg = s->arg;
    bool write = (arg >> 31) & 1;
    uint8_t fn = (arg >> 28) & 0x7;
    bool block_mode = (arg >> 27) & 1;
    uint32_t addr = (arg >> 9) & 0x1ffff;
    uint32_t count = arg & 0x1ff;
    uint32_t len;
    g_autofree uint8_t *buf = NULL;

    if (block_mode) {
        if (count == 0) {
            count = s->numblk;
        }
        len = count * s->blklen;
    } else {
        len = count ? count : 512;
    }
    if (len == 0 || len > 0x10000) {
        sdio_trace("CMD53 with unusable length %u", len);
        return;
    }

    buf = g_malloc0(len);
    if (write) {
        dma_memory_read(&address_space_memory, s->baddr, buf, len,
                        MEMTXATTRS_UNSPECIFIED);
        mv8686_io_rw_extended(&s->card, true, fn, addr, buf, len);
    } else {
        mv8686_io_rw_extended(&s->card, false, fn, addr, buf, len);
        dma_memory_write(&address_space_memory, s->baddr, buf, len,
                         MEMTXATTRS_UNSPECIFIED);
    }

    sdio_trace("CMD53 %s fn=%u addr=0x%05x len=%u dma=0x%08x",
               write ? "write" : "read", fn, addr, len, s->baddr);

    /* the transfer completes immediately: data-done interrupt */
    s->irq_reg |= SDIO_IRQ_DATA_DONE;
    sdio_update_irq(s);
}

static void sdio_exec_cmd(IPodTouchSDIOState *s)
{
    uint8_t idx = s->cmd & 0x3f;

    if (idx == 53) {
        s->resp0 = 0x00001000; /* R5: no errors */
        sdio_exec_cmd53(s);
    } else {
        s->resp0 = mv8686_exec_cmd(&s->card, idx, s->arg);
    }
    s->resp1 = 0;
    s->resp2 = 0;
    s->resp3 = 0;
    s->dsta |= SDIO_DSTA_READY | SDIO_DSTA_CMD_COMPLETE;

    if (idx != 53) {
        sdio_trace("CMD%u arg=0x%08x -> resp 0x%08x (cmd52 fn=%u reg=0x%05x %s)",
                   idx, s->arg, s->resp0,
                   (s->arg >> 28) & 0x7, (s->arg >> 9) & 0x1ffff,
                   (s->arg & (1u << 31)) ? "write" : "read");
    }
}

static void ipod_touch_sdio_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    IPodTouchSDIOState *s = (struct IPodTouchSDIOState *) opaque;

    sdio_trace("W off=0x%03x size=%u val=0x%08" PRIx64, (uint32_t)addr, size, value);

    switch(addr) {
        case SDIO_CTRL:
            s->ctrl = value;
            break;
        case SDIO_DCTRL:
            s->dctrl = value;
            break;
        case SDIO_CMD:
            s->cmd = value;
            if(value & (1 << 31)) {
                sdio_exec_cmd(s);
            }
            break;
        case SDIO_ARGU:
            s->arg = value;
            break;
        case SDIO_STAC:
            /* the guest writes back the DSTA bits it consumed */
            s->dsta &= ~value;
            break;
        case SDIO_CLKDIV:
            s->clkdiv = value;
            break;
        case SDIO_CSR:
            s->csr = value;
            break;
        case SDIO_IRQ:
            /* write-one-to-clear */
            s->irq_reg &= ~value;
            sdio_update_irq(s);
            break;
        case SDIO_IRQMASK:
            s->irq_mask = value;
            sdio_update_irq(s);
            break;
        case SDIO_BADDR:
            s->baddr = value;
            break;
        case SDIO_BLKLEN:
            s->blklen = value;
            break;
        case SDIO_NUMBLK:
            s->numblk = value;
            break;
        default:
            if (addr / 4 < ARRAY_SIZE(s->unknown_regs)) {
                s->unknown_regs[addr / 4] = value;
            }
            break;
    }
}

static uint64_t ipod_touch_sdio_read(void *opaque, hwaddr addr, unsigned size)
{
    IPodTouchSDIOState *s = (struct IPodTouchSDIOState *) opaque;
    uint64_t ret = 0;

    switch (addr) {
        case SDIO_CTRL:
            ret = s->ctrl;
            break;
        case SDIO_DCTRL:
            ret = s->dctrl;
            break;
        case SDIO_CMD:
            ret = s->cmd;
            break;
        case SDIO_ARGU:
            ret = s->arg;
            break;
        case SDIO_DSTA:
            /* the controller is always ready for the next command */
            ret = s->dsta | SDIO_DSTA_READY;
            break;
        case SDIO_RESP0:
            ret = s->resp0;
            break;
        case SDIO_RESP1:
            ret = s->resp1;
            break;
        case SDIO_RESP2:
            ret = s->resp2;
            break;
        case SDIO_RESP3:
            ret = s->resp3;
            break;
        case SDIO_CLKDIV:
            ret = s->clkdiv;
            break;
        case SDIO_CSR:
            ret = s->csr;
            break;
        case SDIO_IRQ:
            ret = s->irq_reg;
            break;
        case SDIO_IRQMASK:
            ret = s->irq_mask;
            break;
        case SDIO_BADDR:
            ret = s->baddr;
            break;
        case SDIO_BLKLEN:
            ret = s->blklen;
            break;
        case SDIO_NUMBLK:
            ret = s->numblk;
            break;
        case SDIO_REMBLK:
            ret = 0; /* every programmed block has been transferred */
            break;
        default:
            if (addr / 4 < ARRAY_SIZE(s->unknown_regs)) {
                ret = s->unknown_regs[addr / 4];
            }
            break;
    }

    sdio_trace("R off=0x%03x size=%u -> 0x%08" PRIx64, (uint32_t)addr, size, ret);
    return ret;
}

static const MemoryRegionOps ipod_touch_sdio_ops = {
    .read = ipod_touch_sdio_read,
    .write = ipod_touch_sdio_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_sdio_reset(DeviceState *dev)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    memset(&s->ctrl, 0,
           offsetof(IPodTouchSDIOState, card) -
           offsetof(IPodTouchSDIOState, ctrl));
    mv8686_reset(&s->card);
    memcpy(s->card.mac, s->conf.macaddr.a, sizeof(s->card.mac));
}

static void ipod_touch_sdio_init(Object *obj)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_sdio_ops, s, TYPE_IPOD_TOUCH_SDIO, 4096);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    s->card.set_card_irq = sdio_card_irq;
    s->card.irq_opaque = s;
    s->card.send_frame = ipod_touch_sdio_send_frame;
    s->card.net_opaque = s;
    mv8686_reset(&s->card);
}

static void ipod_touch_sdio_realize(DeviceState *dev, Error **errp)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    qemu_macaddr_default_if_unset(&s->conf.macaddr);
    memcpy(s->card.mac, s->conf.macaddr.a, sizeof(s->card.mac));
    s->nic = qemu_new_nic(&ipod_touch_sdio_net_info, &s->conf,
                          object_get_typename(OBJECT(dev)), dev->id,
                          &dev->mem_reentrancy_guard, s);
    qemu_format_nic_info_str(qemu_get_queue(s->nic), s->conf.macaddr.a);
}

static void ipod_touch_sdio_unrealize(DeviceState *dev)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    if (s->nic) {
        qemu_del_nic(s->nic);
        s->nic = NULL;
    }
    mv8686_cleanup(&s->card);
}

static const Property ipod_touch_sdio_properties[] = {
    DEFINE_NIC_PROPERTIES(IPodTouchSDIOState, conf),
};

static void ipod_touch_sdio_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = ipod_touch_sdio_realize;
    dc->unrealize = ipod_touch_sdio_unrealize;
    device_class_set_props(dc, ipod_touch_sdio_properties);
    device_class_set_legacy_reset(dc, ipod_touch_sdio_reset);
}

static const TypeInfo ipod_touch_sdio_type_info = {
    .name = TYPE_IPOD_TOUCH_SDIO,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchSDIOState),
    .instance_init = ipod_touch_sdio_init,
    .class_init = ipod_touch_sdio_class_init,
};

static void ipod_touch_sdio_register_types(void)
{
    type_register_static(&ipod_touch_sdio_type_info);
}

type_init(ipod_touch_sdio_register_types)
