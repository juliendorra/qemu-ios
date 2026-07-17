#ifndef IPOD_TOUCH_LCD_PANEL_H
#define IPOD_TOUCH_LCD_PANEL_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/ssi/ssi.h"

#define TYPE_IPOD_TOUCH_LCD_PANEL                "ipodtouch.lcdpanel"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchLCDPanelState, IPOD_TOUCH_LCD_PANEL)

typedef struct IPodTouchLCDPanelState {
    SSIPeripheral ssidev;
    uint32_t cur_cmd;
    struct IPodTouchLCDState *lcd;
} IPodTouchLCDPanelState;

#endif
