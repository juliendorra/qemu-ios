/*
 * S5L8900 SPI Emulation
 *
 * This code is based on:
 * - https://github.com/TrungNguyen1909/qemu-t8030/blob/master/hw/ssi/apple_spi.c (by TrungNguyen1909)
 * - https://github.com/danzatt/QEMU-s5l89xx-port/blob/master/hw/s5l8900_spi.c (by cmw)
 */

#include "hw/arm/ipod_touch_spi.h"
#include "migration/vmstate.h"
#include "hw/core/hw-error.h"

static int apple_spi_word_size(S5L8900SPIState *s)
{
    switch (R_CFG_WORD_SIZE(REG(s, R_CFG))) {
    case R_CFG_WORD_SIZE_8B:
        return 1;
    case R_CFG_WORD_SIZE_16B:
        return 2;
    case R_CFG_WORD_SIZE_32B:
        return 4;
    default:
        break;
    }
    g_assert_not_reached();
}

static void apple_spi_update_xfer_tx(S5L8900SPIState *s)
{
    if (fifo8_is_empty(&s->tx_fifo)) {
        REG(s, R_STATUS) |= R_STATUS_TXEMPTY;
    }
}

static void apple_spi_update_xfer_rx(S5L8900SPIState *s)
{
    if (!fifo8_is_empty(&s->rx_fifo)) {
        REG(s, R_STATUS) |= R_STATUS_RXREADY;
    }
}

static void apple_spi_update_irq(S5L8900SPIState *s)
{
    uint32_t irq = 0;
    uint32_t mask = 0;

    if (REG(s, R_CFG) & R_CFG_IE_RXREADY) {
        mask |= R_STATUS_RXREADY;
    }
    if (REG(s, R_CFG) & R_CFG_IE_TXEMPTY) {
        mask |= R_STATUS_TXEMPTY;
    }
    if (REG(s, R_CFG) & R_CFG_IE_COMPLETE) {
        mask |= R_STATUS_COMPLETE;
    }

    if (REG(s, R_STATUS) & mask) {
        irq = 1;
    }
    if (irq != s->last_irq) {
        s->last_irq = irq;
        qemu_set_irq(s->irq, irq);
    }
}

static void apple_spi_update_cs(S5L8900SPIState *s)
{
    BusState *b = BUS(s->spi);
    BusChild *kid = QTAILQ_FIRST(&b->children);

    /* IT_SPI_CS_TRACE=1: does this guest drive chip-select at all? The whole
     * transaction-framing workaround (R_RXCNT reaching 0) exists only because
     * the answer was measured to be "no". Re-measure before trusting it. */
    if (getenv("IT_SPI_CS_TRACE")) {
        static uint32_t n;
        fprintf(stderr, "[SPI%d] CS write #%u -> %s (R_PIN=0x%08x)\n",
                s->base, ++n, (REG(s, R_PIN) & R_PIN_CS) ? "high" : "low",
                REG(s, R_PIN));
    }
    if (kid) {
        /* Not forwarded to the peripheral. Measured 2026-07-27: the guest
         * writes R_PIN once per transaction but ONLY ever 0x00000000 -- it
         * asserts and never deasserts -- and it re-asserts between the two
         * halves of a split 0xEA frame read. So forwarding CS would mark the
         * same boundaries the R_RXCNT framing in apple_spi_run() already
         * marks; it is fidelity, not a fix. See ipod_touch_multitouch.c. */
        //qemu_set_irq(qdev_get_gpio_in_named(kid->child, SSI_GPIO_CS, 0), (REG(s, R_PIN) & R_PIN_CS) != 0);
    }
}

static void apple_spi_cs_set(void *opaque, int pin, int level)
{
    S5L8900SPIState *s = S5L8900SPI(opaque);
    if (level) {
        REG(s, R_PIN) |= R_PIN_CS;
    } else {
        REG(s, R_PIN) &= ~R_PIN_CS;
    }
    apple_spi_update_cs(s);
}

static void apple_spi_run(S5L8900SPIState *s)
{
    uint32_t tx;
    uint32_t rx;
    uint32_t rxcnt_in;
    unsigned moved = 0;

    if (!(REG(s, R_CTRL) & R_CTRL_RUN)) {
        return;
    }
    rxcnt_in = REG(s, R_RXCNT);

    while (!fifo8_is_empty(&s->tx_fifo)) {
        tx = (uint32_t)fifo8_pop(&s->tx_fifo);
        rx = ssi_transfer(s->spi, tx);
        moved++;
        apple_spi_update_xfer_tx(s);
        if (REG(s, R_RXCNT) > 0) {
            if (fifo8_is_full(&s->rx_fifo)) {
                qemu_log_mask(LOG_GUEST_ERROR, "%s: rx overflow\n", __func__);
                REG(s, R_STATUS) |= R_STATUS_RXOVERFLOW;
            } else {
                fifo8_push(&s->rx_fifo, (uint8_t)rx);
                REG(s, R_RXCNT)--;
                apple_spi_update_xfer_rx(s);
            }
        }
    }

    // fetch the remaining bytes by sending sentinel bytes.
    while (!fifo8_is_full(&s->rx_fifo) && (REG(s, R_RXCNT) > 0) && (REG(s, R_CFG) & R_CFG_AGD)) {
        rx = ssi_transfer(s->spi, 0xff);
        if (fifo8_is_full(&s->rx_fifo)) {
            qemu_log_mask(LOG_GUEST_ERROR, "%s: rx overflow\n", __func__);
            REG(s, R_STATUS) |= R_STATUS_RXOVERFLOW;
            break;
        } else {
            fifo8_push(&s->rx_fifo, (uint8_t)rx);
            REG(s, R_RXCNT)--;
            apple_spi_update_xfer_rx(s);
        }
    }
    if (REG(s, R_RXCNT) == 0 && fifo8_is_empty(&s->tx_fifo)) {
        REG(s, R_STATUS) |= R_STATUS_COMPLETE;
        REG(s, R_CTRL) &= ~R_CTRL_RUN;
    }
    /*
     * SPI transaction framing. ON by default; IT_SPI_FRAMING=0 disables it.
     *
     * R_RXCNT is the number of bytes the driver asked for, so reaching 0 ends
     * the transfer and any half-consumed command must be dropped. Without
     * this the multitouch model has NO framing at all: a driver that asks for
     * fewer bytes than our reply is long -- a short status poll, or a 0xEB
     * frame poll abandoned once the length reads zero -- leaves the device
     * stuck mid-command forever, and slide-to-unlock works exactly once per
     * boot (T7).
     *
     * History worth keeping: this was briefly disabled because the first
     * attempt appeared to regress the packaged app. The harness that had
     * "passed" it was headless, where QEMU never calls gfx_update, so it was
     * not testing what the app runs. With a display client attached
     * (scripts/lock-unlock-probe.py, default) the measurements are
     * unambiguous, at both a 0.7 s and a 4 s gesture:
     *     framing off -> cycle 1 unlocks, cycles 2+ fail with ZERO frames
     *                    consumed by the guest
     *     framing on  -> 3/3 cycles unlock, 46-233 frames consumed each
     * The env switch exists so the app can be A/B tested in place:
     *     IT_SPI_FRAMING=0 "/Applications/iPod Touch.app/Contents/MacOS/iPod Touch"
     */
    if (rxcnt_in > 0 && REG(s, R_RXCNT) == 0) {
        const char *off = getenv("IT_SPI_FRAMING");
        if (!off || strcmp(off, "0") != 0) {
            ipod_touch_multitouch_transaction_end(s->mt);
        }
    }

    /* IT_SPI_BURST_TRACE=1: is one "run" one SPI transaction? R_RXCNT is the
     * length the driver asked for, so RXCNT reaching 0 is a candidate
     * transaction boundary -- the framing this model lacks (T7). Logged with
     * the peripheral index so multitouch (spi2) can be told apart. */
    if (getenv("IT_SPI_BURST_TRACE")) {
        static unsigned n;
        if (++n <= 400) {
            fprintf(stderr, "[SPI%d] run: tx=%u rxcnt %u -> %u%s\n",
                    s->base, moved, rxcnt_in, REG(s, R_RXCNT),
                    REG(s, R_RXCNT) == 0 ? "  (complete)" : "");
        }
    }

}

static uint64_t s5l8900_spi_read(void *opaque, hwaddr addr, unsigned size)
{
    S5L8900SPIState *s = S5L8900SPI(opaque);
    //fprintf(stderr, "%s (base %d): read from location 0x%08x\n", __func__, s->base, addr);

    uint32_t r;
    bool run = false;

    r = s->regs[addr >> 2];
    switch (addr) {
    case R_RXDATA: {
        const uint8_t *buf = NULL;
        int word_size = apple_spi_word_size(s);
        uint32_t num = 0;
        if (fifo8_is_empty(&s->rx_fifo)) {
            qemu_log_mask(LOG_GUEST_ERROR, "%s: rx underflow\n", __func__);
            r = 0;
            break;
        }
        buf = fifo8_pop_bufptr(&s->rx_fifo, word_size, &num);
        memcpy(&r, buf, num);
        if (fifo8_is_empty(&s->rx_fifo)) {
            run = true;
        }
        break;
    }
    case R_STATUS: {
        int val = 0;
        val |= fifo8_num_used(&s->tx_fifo) << R_STATUS_TXFIFO_SHIFT;
        val |= fifo8_num_used(&s->rx_fifo) << R_STATUS_RXFIFO_SHIFT;
        val &= (R_STATUS_TXFIFO_MASK | R_STATUS_RXFIFO_MASK);
        r &= ~(R_STATUS_TXFIFO_MASK | R_STATUS_RXFIFO_MASK);
        r |= val;
        break;
    }
    default:
        break;
    }

    if (run) {
        apple_spi_run(s);
    }
    apple_spi_update_irq(s);
    return r;
}

static void s5l8900_spi_write(void *opaque, hwaddr addr, uint64_t data, unsigned size)
{
    S5L8900SPIState *s = S5L8900SPI(opaque);
    //fprintf(stderr, "%s (base %d): writing 0x%08x to 0x%08x\n", __func__, s->base, data, addr);

    uint32_t r = data;
    uint32_t *mmio = &REG(s, addr);
    uint32_t old = *mmio;
    bool cs_flg = false;
    bool run = false;

    switch (addr) {
    case R_CTRL:
        if (r & R_CTRL_TX_RESET) {
            fifo8_reset(&s->tx_fifo);
        }
        if (r & R_CTRL_RX_RESET) {
            fifo8_reset(&s->rx_fifo);
        }
        if (r & R_CTRL_RUN && !fifo8_is_empty(&s->tx_fifo)) {
            run = true;
        }
        break;
    case R_STATUS:
        r = old & (~r);
        run = true;
        break;
    case R_PIN:
        cs_flg = true;
        break;
    case R_TXDATA ... R_TXDATA + 3: {
        int word_size = apple_spi_word_size(s);
        if ((fifo8_is_full(&s->tx_fifo))
            || (fifo8_num_free(&s->tx_fifo) < word_size)) {
            hw_error("OVERFLOW: %d\n", fifo8_num_free(&s->tx_fifo));
            qemu_log_mask(LOG_GUEST_ERROR, "%s: tx overflow\n", __func__);
            r = 0;
            break;
        }
        fifo8_push_all(&s->tx_fifo, (uint8_t *)&r, word_size);
        break;
    case R_CFG:
        run = true;
        break;
    }
    default:
        break;
    }

    *mmio = r;
    if (cs_flg) {
        apple_spi_update_cs(s);
    }
    if (run) {
        apple_spi_run(s);
    }
    apple_spi_update_irq(s);
}

static const MemoryRegionOps spi_ops = {
    .read = s5l8900_spi_read,
    .write = s5l8900_spi_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void s5l8900_spi_reset(DeviceState *d)
{
    S5L8900SPIState *s = (S5L8900SPIState *)d;
	memset(s->regs, 0, sizeof(s->regs));
    fifo8_reset(&s->tx_fifo);
    fifo8_reset(&s->rx_fifo);
    s->last_irq = 0;
    qemu_irq_lower(s->irq);
}

static uint32_t base_addr = 0;

void set_spi_base(uint32_t base)
{
	base_addr = base;
}

static void s5l8900_spi_realize(DeviceState *dev, struct Error **errp)
{
    S5L8900SPIState *s = S5L8900SPI(dev);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    char bus_name[32] = { 0 };
    snprintf(bus_name, sizeof(bus_name), "%s.bus", dev->id);
    s->spi = ssi_create_bus(dev, (const char *)bus_name);

    sysbus_init_irq(sbd, &s->irq);
    sysbus_init_irq(sbd, &s->cs_line);
    qdev_init_gpio_in_named(dev, apple_spi_cs_set, SSI_GPIO_CS, 1);
    char name[5];
    snprintf(name, 5, "spi%d", base_addr);
    memory_region_init_io(&s->iomem, OBJECT(s), &spi_ops, s, name, 0x100);
    sysbus_init_mmio(sbd, &s->iomem);
    s->base = base_addr;

    fifo8_create(&s->tx_fifo, R_FIFO_DEPTH);
    fifo8_create(&s->rx_fifo, R_FIFO_DEPTH);

    // create the peripheral
    switch(s->base) {
        case 0:
            break;
        case 1:
        {
            DeviceState *dev = ssi_create_peripheral(s->spi,
                                                     TYPE_IPOD_TOUCH_LCD_PANEL);
            s->panel = IPOD_TOUCH_LCD_PANEL(dev);
            break;
        }
        case 2:
        {
            DeviceState *dev = ssi_create_peripheral(s->spi, TYPE_IPOD_TOUCH_MULTITOUCH);
            IPodTouchMultitouchState *mt = IPOD_TOUCH_MULTITOUCH(dev);
            s->mt = mt;
            break;
        }
    }
}

/*
 * Migration. The SPI controller is how the guest reads the touch controller, so
 * its register file and FIFOs have to come back: a restored machine whose SPI
 * block is at reset makes the driver re-enumerate the device it was already
 * talking to -- observed as a burst of Z1 get-report/report-info after a
 * restore, with touch frames queued by the model and never collected.
 */
static const VMStateDescription vmstate_s5l8900_spi = {
    .name = "s5l8900-spi",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(last_irq, S5L8900SPIState),
        VMSTATE_FIFO8(rx_fifo, S5L8900SPIState),
        VMSTATE_FIFO8(tx_fifo, S5L8900SPIState),
        VMSTATE_UINT32_ARRAY(regs, S5L8900SPIState, MMIO_SIZE >> 2),
        VMSTATE_UINT32(mmio_size, S5L8900SPIState),
        VMSTATE_UINT8(base, S5L8900SPIState),
        VMSTATE_END_OF_LIST()
    },
};

static void s5l8900_spi_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = s5l8900_spi_realize;
    device_class_set_legacy_reset(dc, s5l8900_spi_reset);
    DEVICE_CLASS(klass)->vmsd = &vmstate_s5l8900_spi;
}

static const TypeInfo s5l8900_spi_info = {
    .name          = TYPE_S5L8900SPI,
    .parent        = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(S5L8900SPIState),
    .class_init    = s5l8900_spi_class_init,
};

static void s5l8900_spi_register_types(void)
{
    type_register_static(&s5l8900_spi_info);
}

type_init(s5l8900_spi_register_types)
