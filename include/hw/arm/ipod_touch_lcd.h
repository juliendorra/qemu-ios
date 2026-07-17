#ifndef IPOD_TOUCH_LCD_H
#define IPOD_TOUCH_LCD_H

#include <math.h>
#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/sysbus.h"
#include "hw/irq.h"
#include "hw/arm/ipod_touch_multitouch.h"

#define TYPE_IPOD_TOUCH_LCD                "ipodtouch.lcd"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchLCDState, IPOD_TOUCH_LCD)

#define LCD_REFRESH_RATE_FREQUENCY 10

#define FB_WIDTH  320
#define FB_HEIGHT 480
#define FB_BPP    4
#define FB_SIZE   (FB_WIDTH * FB_HEIGHT * FB_BPP)

typedef struct IPodTouchLCDState
{
    SysBusDevice parent_obj;
    MemoryRegion *sysmem;
    MemoryRegion iomem;
    QemuConsole *con;
    IPodTouchMultitouchState *mt;
    int invalidate;
    MemoryRegionSection fbsection;
    qemu_irq irq;
    uint32_t lcd_con;
    uint32_t lcd_con2;
    uint32_t int_mask;
    uint32_t int_status;

    uint32_t wnd_con;

    uint32_t vid_con0;
    uint32_t vid_con1;

    uint32_t vidt_con0;
    uint32_t vidt_con1;
    uint32_t vidt_con2;
    uint32_t vidt_con3;

    uint32_t w1_hspan;
    uint32_t w1_framebuffer_base;
    uint32_t w1_display_resolution_info;
    uint32_t w1_display_depth_info;
    uint32_t w1_qlen;

    uint32_t w2_hspan;
    uint32_t w2_framebuffer_base;
    uint32_t w2_display_resolution_info;
    uint32_t w2_display_depth_info;
    uint32_t w2_qlen;

    QEMUTimer *refresh_timer;

    // Framebuffer snapshot for sleep/wake (approach #22)
    uint8_t *fb_snapshot;      // saved framebuffer content (320*480*4 bytes)
    bool fb_snapshot_valid;    // true if snapshot contains non-black content
    int snapshot_visible_frames;
    uint32_t retained_scanout_base;
    bool retained_scanout_valid;
    bool panel_off;            // PMU-powered LCD panel state
} IPodTouchLCDState;

bool ipod_touch_lcd_framebuffer_is_dark(IPodTouchLCDState *lcd);
void ipod_touch_lcd_restore_snapshot(IPodTouchLCDState *lcd);
void ipod_touch_lcd_resume_scanout(IPodTouchLCDState *lcd);

#endif
