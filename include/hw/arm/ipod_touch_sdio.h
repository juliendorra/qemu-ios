#ifndef IPOD_TOUCH_SDIO_H
#define IPOD_TOUCH_SDIO_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/core/sysbus.h"
#include "hw/arm/ipod_touch_mv8686.h"

#define TYPE_IPOD_TOUCH_SDIO                "ipodtouch.sdio"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchSDIOState, IPOD_TOUCH_SDIO)

/* Register offsets observed from the guest (WIFI_SDIO_NOTES.md); the
 * remaining names follow the S5L8900 layout used by openiboot. */
#define SDIO_CTRL       0x0
#define SDIO_DCTRL      0x4
#define SDIO_CMD        0x8
#define SDIO_ARGU       0xC
#define SDIO_STATE      0x10
#define SDIO_STAC       0x14
#define SDIO_DSTA       0x18
#define SDIO_FSTA       0x1C
#define SDIO_RESP0      0x20
#define SDIO_RESP1      0x24
#define SDIO_RESP2      0x28
#define SDIO_RESP3      0x2C
#define SDIO_CLKDIV     0x30
#define SDIO_CSR        0x34
#define SDIO_IRQ        0x38
#define SDIO_IRQMASK    0x3C
#define SDIO_BADDR      0x44
#define SDIO_BLKLEN     0x48
#define SDIO_NUMBLK     0x4C
#define SDIO_REMBLK     0x50

/* SDIO_IRQ bits (iphone-linux iphone-sdio.c) */
#define SDIO_IRQ_DATA_DONE (1 << 0)
#define SDIO_IRQ_CARD_INT  (1 << 1)

/* DSTA bits the guest is known to poll and ack */
#define SDIO_DSTA_READY        (1 << 0)
#define SDIO_DSTA_CMD_COMPLETE (1 << 4)

typedef struct IPodTouchSDIOState
{
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq;

    uint32_t ctrl;
    uint32_t dctrl;
    uint32_t cmd;
    uint32_t arg;
    uint32_t dsta;
    uint32_t resp0;
    uint32_t resp1;
    uint32_t resp2;
    uint32_t resp3;
    uint32_t clkdiv;
    uint32_t csr;
    uint32_t irq_reg;
    uint32_t irq_mask;
    uint32_t baddr;
    uint32_t blklen;
    uint32_t numblk;
    /* raw storage for offsets without modeled behavior yet */
    uint32_t unknown_regs[0x1000 / 4];

    MV8686State card;
} IPodTouchSDIOState;

#endif
