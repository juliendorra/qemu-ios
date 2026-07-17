#ifndef IPOD_TOUCH_SYSIC_H
#define IPOD_TOUCH_SYSIC_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/sysbus.h"
#include "hw/irq.h"

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

// Delay (in nanoseconds) before auto-lowering a GPIO IRQ line.
// This creates edge-triggered behavior: the IRQ pulses HIGH for this duration
// then returns LOW, preventing interrupt storms from stuck-high lines.
#define GPIO_IRQ_PULSE_NS 100000000  // 100 ms (long pulse for wake reliability)

typedef struct GPIOIRQLowerInfo {
    struct IPodTouchSYSICState *sysic;
    int group;
} GPIOIRQLowerInfo;

typedef struct Pcf50633State Pcf50633State;

typedef struct IPodTouchSYSICState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq gpio_irqs[GPIO_NUMINTGROUPS];
    uint32_t power_state;
    Pcf50633State *pmu;

    // GPIO
    uint32_t gpio_int_level[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_status[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_enabled[GPIO_NUMINTGROUPS];
    uint32_t gpio_int_type[GPIO_NUMINTGROUPS];

    // GPIO IRQ auto-lower timers (edge-triggered pulse behavior)
    QEMUTimer *gpio_irq_lower_timers[GPIO_NUMINTGROUPS];
    GPIOIRQLowerInfo gpio_irq_lower_info[GPIO_NUMINTGROUPS];

} IPodTouchSYSICState;

#endif
