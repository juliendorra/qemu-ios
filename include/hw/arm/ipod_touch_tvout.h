#ifndef IPOD_TOUCH_TVOUT_H
#define IPOD_TOUCH_TVOUT_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"

#define SDO_IRQ 0x280

/* Is the SDO field-interrupt model active? (IT_TVOUT_SDO, default on.)
 * The swap-device zero-window in ipod_touch.c is only placed when this is
 * OFF -- they are alternative answers to the same missing completion. */
bool ipod_touch_tvout_sdo_modelled(void);

#define TYPE_IPOD_TOUCH_TVOUT                "ipodtouch.tvout"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchTVOutState, IPOD_TOUCH_TVOUT)

typedef struct IPodTouchTVOutState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t index;

    /* IT_TVOUT_SDO=1: a modelled SDO field interrupt (T1). The real SDO
     * block raises an interrupt per video FIELD; the AppleH1TVOut swap path
     * completes queued swaps from exactly that ISR (MBX_HANDOFF.md,
     * 2026-07-31). The engine lives on instance 3 (the DT's tv-out node,
     * 0x39300000) and toggles the field-parity bit on instance 2 via
     * `peer`. Off by default until verified on both boards. */
    QEMUTimer *frame_timer;
    bool frame_timer_running;
    bool irq_high;
    uint32_t field_parity;
    struct IPodTouchTVOutState *peer;   /* instance 3 -> instance 2 */

    uint32_t data[4096];
} IPodTouchTVOutState;

#endif