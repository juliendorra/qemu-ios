#ifndef IPOD_TOUCH_TVOUT_H
#define IPOD_TOUCH_TVOUT_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"

#define SDO_IRQ 0x280

#define TYPE_IPOD_TOUCH_TVOUT                "ipodtouch.tvout"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchTVOutState, IPOD_TOUCH_TVOUT)

typedef struct IPodTouchTVOutState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t index;

    //uint32_t sdo_irq_reg;
    uint32_t data[4096];
} IPodTouchTVOutState;

#endif