#ifndef IPOD_TOUCH_SYSIC_H
#define IPOD_TOUCH_SYSIC_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"

#define TYPE_IPOD_TOUCH_SYSIC                "ipodtouch.sysic"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchSYSICState, IPOD_TOUCH_SYSIC)

#define POWER_ID 0x44
#define POWER_ONCTRL 0xC
#define POWER_OFFCTRL 0x10
#define POWER_SETSTATE 0x8
#define POWER_STATE 0x14 // seems to be toggled by writing a 1 to the right device ID - cleared to 0 when the device has started.

#define POWER_ID_ADM 0x10

// the GPIO IC is part of the system controller
#define GPIO_INTLEVEL 0x80
#define GPIO_INTSTAT  0xA0
#define GPIO_INTEN    0xC0
#define GPIO_INTTYPE  0xE0

#define GPIO_NUMINTGROUPS 7

typedef struct Pcf50633State Pcf50633State;

typedef struct IPodTouchSYSICState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq gpio_irqs[GPIO_NUMINTGROUPS];
    uint32_t power_state;
    /* Security/power epoch reported in POWER_ID bits [31:24]. N45AP (iPod
     * Touch 1G) iBoot expects 2; M68AP (iPhone 2G) iBoot's miu_init() panics
     * with "Epoch Mismatch" unless it reads 3. Set per-board at machine init. */
    uint32_t power_epoch;
    Pcf50633State *pmu;

    // GPIO
    uint32_t gpio_int_level[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_status[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_enabled[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_type[GPIO_NUMINTGROUPS];

} IPodTouchSYSICState;

#endif
