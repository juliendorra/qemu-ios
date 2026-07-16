#include "hw/arm/ipod_touch_lcd.h"
#include "ui/pixel_ops.h"
#include "ui/console.h"
#include "hw/display/framebuffer.h"
#include "exec/address-spaces.h"

static uint64_t s5l8900_lcd_read(void *opaque, hwaddr addr, unsigned size)
{
    //fprintf(stderr, "%s: read from location 0x%08x\n", __func__, addr);

    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;
    switch(addr)
    {
        case 0x4:
            return s->lcd_con;
        case 0x8:
            return s->lcd_con2;

        case 0x14:
            return s->unknown1;
        case 0x18:
            return s->render;

        case 0x20:
            return s->wnd_con;

        case 0x200:
            return s->vid_con0;
        case 0x204:
            return s->vid_con1;

        case 0x20C:
            return s->vidt_con0;
        case 0x210:
            return s->vidt_con1;
        case 0x214:
            return s->vidt_con2;
        case 0x218:
            return s->vidt_con3;

        case 0x58:
            return s->w1_hspan;
        case 0x5c:
            return s->w1_display_depth_info;
        case 0x60:
            return s->w1_framebuffer_base;
        case 0x64:
            return s->w1_display_resolution_info;
        case 0x68:
            return s->w1_qlen;

        case 0x70:
            return s->w2_hspan;
        case 0x74:
            return s->w2_display_depth_info;
        case 0x78:
            return s->w2_framebuffer_base;
        case 0x7c:
            return s->w2_display_resolution_info;
        case 0x80:
            return s->w2_qlen;
        default:
            // hw_error("%s: read invalid location 0x%08x.\n", __func__, addr);
            break;
    }
    return 0;
}

static void s5l8900_lcd_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;
    //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, (uint32_t)val, addr);

    switch(addr) {
        case 0x4:
            s->lcd_con = val;
            break;
        case 0x8:
            s->lcd_con2 = val;
            break;

        case 0x14:
            s->unknown1 = val;
            break;
        case 0x18:
            s->render = val;
            qemu_irq_lower(s->irq);
            break;

        case 0x20:
            s->wnd_con = val;
            break;

        case 0x200:
            s->vid_con0 = val;
            break;
        case 0x204:
            s->vid_con1 = val;
            break;

        case 0x20C:
            s->vidt_con0 = val;
            break;
        case 0x210:
            s->vidt_con1 = val;
            break;
        case 0x214:
            s->vidt_con2 = val;
            break;
        case 0x218:
            s->vidt_con3 = val;
            break;

        case 0x58:
            s->w1_hspan = val;
            break;
        case 0x5c:
            s->w1_display_depth_info = val;
            break;
        case 0x60:
            s->w1_framebuffer_base = val;
            break;
        case 0x64:
            s->w1_display_resolution_info = val;
            break;
        case 0x68:
            s->w1_qlen = val;
            break;

        case 0x70:
            s->w2_hspan = val;
            break;
        case 0x74:
            s->w2_display_depth_info = val;
            break;
        case 0x78:
            s->w2_framebuffer_base = val;
            break;
        case 0x7c:
            s->w2_display_resolution_info = val;
            break;
        case 0x80:
            s->w2_qlen = val;
            break;
    }
}

static void lcd_invalidate(void *opaque)
{
    IPodTouchLCDState *s = opaque;
    s->invalidate = 1;
}

static void draw_line32_32(void *opaque, uint8_t *d, const uint8_t *s, int width, int deststep)
{
    uint8_t r, g, b;

    do {
        //v = lduw_le_p((void *) s);
        //printf("V: %d\n", *s);
        b = s[0];
        g = s[1];
        r = s[2];
        //printf("R: %d, G: %d, B: %d\n", r, g, b);
        ((uint32_t *) d)[0] = rgb_to_pixel32(r, g, b);
        s += 4;
        d += 4;
    } while (-- width != 0);
}

static int lcd_visible_sample_count(uint32_t base)
{
    static const uint32_t check_offsets[] = {
        (240 * FB_WIDTH + 160) * FB_BPP,
        (100 * FB_WIDTH + 160) * FB_BPP,
        (400 * FB_WIDTH + 100) * FB_BPP,
        (450 * FB_WIDTH +  80) * FB_BPP,
        (450 * FB_WIDTH + 160) * FB_BPP,
        (460 * FB_WIDTH + 160) * FB_BPP,
    };
    int visible_count = 0;

    for (int i = 0; i < ARRAY_SIZE(check_offsets); i++) {
        uint8_t px[FB_BPP];

        cpu_physical_memory_read(base + check_offsets[i], px, sizeof(px));
        if (px[0] || px[1] || px[2]) {
            visible_count++;
        }
    }
    return visible_count;
}

static uint64_t lcd_brightness_score(uint32_t base)
{
    uint64_t score = 0;

    /* A coarse whole-screen grid distinguishes a complete foreground frame
     * from a white-content frame whose navigation and dock have already
     * faded to black. */
    for (int y = 20; y < FB_HEIGHT; y += 40) {
        for (int x = 20; x < FB_WIDTH; x += 40) {
            uint8_t px[FB_BPP];
            uint32_t offset = (y * FB_WIDTH + x) * FB_BPP;

            cpu_physical_memory_read(base + offset, px, sizeof(px));
            score += px[0] + px[1] + px[2];
        }
    }
    return score;
}

static void lcd_refresh(void *opaque)
{
    //fprintf(stderr, "%s: refreshing LCD screen\n", __func__);

    IPodTouchLCDState *lcd = (IPodTouchLCDState *) opaque;
    DisplaySurface *surface = qemu_console_surface(lcd->con);
    drawfn draw_line;
    int src_width, dest_width;
    int height, first, last;
    int width, linesize;

    if (!lcd || !lcd->con || !surface_bits_per_pixel(surface))
        return;

    if (lcd->panel_off) {
        memset(surface_data(surface), 0,
               surface_stride(surface) * surface_height(surface));
        dpy_gfx_update(lcd->con, 0, 0,
                       surface_width(surface), surface_height(surface));
        return;
    }

    dest_width = 4;
    draw_line = draw_line32_32;

    /* Resolution */
    first = last = 0;
    width = 320;
    height = 480;
    lcd->invalidate = 1;

    src_width =  4 * width;
    linesize = surface_stride(surface);

    if(lcd->invalidate) {
        framebuffer_update_memory_section(&lcd->fbsection, lcd->sysmem, lcd->w1_framebuffer_base, height, 4 * width);
    }

    framebuffer_update_display(surface, &lcd->fbsection,
                               width, height,
                               src_width,       /* Length of source line, in bytes.  */
                               linesize,        /* Bytes between adjacent horizontal output pixels.  */
                               dest_width,      /* Bytes between adjacent vertical output pixels.  */
                               lcd->invalidate,
                               draw_line, NULL,
                               &first, &last);
    if (first >= 0) {
        dpy_gfx_update(lcd->con, 0, first, width, last - first + 1);
    }
    lcd->invalidate = 0;

    /* Track the brightest complete OS buffer. During the sleep fade the
     * active buffer becomes status-bar-only or partially black, while an
     * inactive triple buffer still retains the foreground frame. */
    {
        static const uint32_t os_bases[] = { 0x0f400000, 0x0f496000 };
        uint32_t candidate = 0;
        int best_visible_count = 0;
        uint64_t best_brightness = 0;

        for (int i = 0; i < ARRAY_SIZE(os_bases); i++) {
            int visible_count = lcd_visible_sample_count(os_bases[i]);
            uint64_t brightness = lcd_brightness_score(os_bases[i]);

            if (visible_count >= 4 && brightness > best_brightness) {
                candidate = os_bases[i];
                best_visible_count = visible_count;
                best_brightness = brightness;
            }
        }
        if (candidate && best_visible_count >= 4) {
            lcd->retained_scanout_base = candidate;
            lcd->retained_scanout_valid = true;
        }
    }

    // Save a framebuffer snapshot once the home screen has remained visible
    // for two seconds. Capturing the first non-black frame locks in the
    // SpringBoard boot overlay (Apple logo with dimmed icons), while updating
    // forever lets the auto-lock fade overwrite a good image.
    if (lcd->fb_snapshot && !lcd->fb_snapshot_valid) {
        static const uint32_t known_bases[] = {
            /* 0x0fe00000 belongs to iBoot and is overwritten on every wake. */
            0x0f400000, 0x0f496000
        };
        uint32_t visible_base = 0;
        int best_visible_count = 0;

        for (int b = 0; b < ARRAY_SIZE(known_bases); b++) {
            uint32_t base = known_bases[b];
            int visible_count = lcd_visible_sample_count(base);

            if (visible_count > best_visible_count) {
                best_visible_count = visible_count;
                visible_base = base;
            }
        }

        if (best_visible_count >= 4) {
            lcd->snapshot_visible_frames++;
        } else {
            lcd->snapshot_visible_frames = 0;
        }

        if (lcd->snapshot_visible_frames >=
            2 * LCD_REFRESH_RATE_FREQUENCY) {
            cpu_physical_memory_read(visible_base, lcd->fb_snapshot, FB_SIZE);
            lcd->fb_snapshot_valid = true;
            lcd->retained_scanout_base = visible_base;
            lcd->retained_scanout_valid = true;
            fprintf(stderr, "[LCD] Captured stable framebuffer snapshot "
                    "(base=0x%08x, %d/6 visible after %d frames — locked)\n",
                    visible_base, best_visible_count,
                    lcd->snapshot_visible_frames);
        }
    }
}

static const MemoryRegionOps lcd_ops = {
    .read = s5l8900_lcd_read,
    .write = s5l8900_lcd_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static const GraphicHwOps s5l8900_gfx_ops = {
    .invalidate  = lcd_invalidate,
    .gfx_update  = lcd_refresh,
};

bool ipod_touch_lcd_framebuffer_is_dark(IPodTouchLCDState *lcd)
{
    static const uint32_t check_offsets[] = {
        (240 * FB_WIDTH + 160) * FB_BPP,
        (100 * FB_WIDTH + 160) * FB_BPP,
        (400 * FB_WIDTH + 100) * FB_BPP,
        (450 * FB_WIDTH +  80) * FB_BPP,
        (450 * FB_WIDTH + 160) * FB_BPP,
        (460 * FB_WIDTH + 160) * FB_BPP,
    };
    uint32_t active_base = lcd->w1_framebuffer_base;

    if (!active_base) {
        return false;
    }

    /* Inspect the buffer that is actually scanned out. Inactive triple
     * buffers can retain a bright home screen after the visible buffer has
     * entered the status-bar-only OOCSHDWN transition. */
    for (int i = 0; i < ARRAY_SIZE(check_offsets); i++) {
        uint8_t pixel[FB_BPP];

        cpu_physical_memory_read(active_base + check_offsets[i],
                                 pixel, sizeof(pixel));
        if (pixel[0] || pixel[1] || pixel[2]) {
            return false;
        }
    }
    return true;
}

void ipod_touch_lcd_restore_snapshot(IPodTouchLCDState *lcd)
{
    static const uint32_t known_bases[] = {
        0x0fe00000, 0x0f400000, 0x0f496000,
    };

    if (!lcd || !lcd->fb_snapshot_valid) {
        return;
    }

    for (int i = 0; i < ARRAY_SIZE(known_bases); i++) {
        cpu_physical_memory_write(known_bases[i], lcd->fb_snapshot, FB_SIZE);
    }
    lcd->invalidate = 1;
    fprintf(stderr, "[WAKE] Restored stable framebuffer snapshot\n");
}

void ipod_touch_lcd_resume_scanout(IPodTouchLCDState *lcd)
{
    if (!lcd || !lcd->retained_scanout_valid) {
        return;
    }

    /*
     * LPDDR retains the foreground surface across OOCSHDWN, but the CLCD
     * scanout register is in the reset application-processor domain. iBoot
     * temporarily points it at its own buffer. Restore the retained surface
     * when iBoot consumes the type-4 token instead of copying framebuffer
     * contents or suspending the emulator.
     */
    lcd->w1_framebuffer_base = lcd->retained_scanout_base;
    lcd->panel_off = false;
    lcd->invalidate = 1;
    fprintf(stderr, "[WAKE] Restored retained CLCD scanout base 0x%08x\n",
            lcd->retained_scanout_base);
}

static void ipod_touch_lcd_mouse_event(void *opaque, int x, int y, int z, int buttons_state)
{
    // convert x and y to fractional numbers
    float fx = x / pow(2, 15);
    float fy = 1 - y / pow(2, 15);

    IPodTouchLCDState *lcd = (IPodTouchLCDState *) opaque;
    lcd->mt->prev_touch_x = lcd->mt->touch_x;
    lcd->mt->prev_touch_y = lcd->mt->touch_y;
    lcd->mt->touch_x = fx;
    lcd->mt->touch_y = fy;

    if(buttons_state && !lcd->mt->touch_down) {
        if (!lcd->fb_snapshot_valid) {
            fprintf(stderr, "[TOUCH] Ignoring input until display/driver "
                    "startup is stable\n");
            return;
        }

        fprintf(stderr, "[TOUCH] mouse DOWN at (%.3f, %.3f)\n", fx, fy);
        ipod_touch_multitouch_on_touch(lcd->mt);
    }
    else if(!buttons_state && lcd->mt->swallow_wake_touch) {
        if (lcd->mt->alternate_wake_via_power && lcd->mt->pmu) {
            pcf50633_set_onkey(lcd->mt->pmu, false);
            lcd->mt->alternate_wake_via_power = false;
        }
        lcd->mt->swallow_wake_touch = false;
        fprintf(stderr, "[TOUCH] wake click released; next click is input\n");
    }
    else if(!buttons_state && lcd->mt->touch_down) {
        if (lcd->mt->alternate_wake_via_power && lcd->mt->pmu) {
            pcf50633_set_onkey(lcd->mt->pmu, false);
            lcd->mt->alternate_wake_via_power = false;
        }
        fprintf(stderr, "[TOUCH] mouse UP at (%.3f, %.3f)\n", fx, fy);
        ipod_touch_multitouch_on_release(lcd->mt);
    }
}

static void refresh_timer_tick(void *opaque)
{
    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;

    if (s->render == 0x1)
        qemu_irq_raise(s->irq);
    else if (s->render == 0xFF)
        qemu_irq_lower(s->irq);

    timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + NANOSECONDS_PER_SECOND / LCD_REFRESH_RATE_FREQUENCY);
}

static void s5l8900_lcd_realize(DeviceState *dev, Error **errp)
{
    IPodTouchLCDState *s = IPOD_TOUCH_LCD(dev);
    s->con = graphic_console_init(dev, 0, &s5l8900_gfx_ops, s);
    qemu_console_resize(s->con, FB_WIDTH, FB_HEIGHT);

    // Allocate framebuffer snapshot buffer for sleep/wake
    s->fb_snapshot = g_malloc0(FB_SIZE);
    s->fb_snapshot_valid = false;
    s->snapshot_visible_frames = 0;
    s->retained_scanout_base = 0;
    s->retained_scanout_valid = false;

    // add mouse handler
    qemu_add_mouse_event_handler(ipod_touch_lcd_mouse_event, s, 1, "iPod Touch Touchscreen");

    // initialize the refresh timer
    s->refresh_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, refresh_timer_tick, s);
    timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + NANOSECONDS_PER_SECOND / LCD_REFRESH_RATE_FREQUENCY);
}

static void s5l8900_lcd_init(Object *obj)
{
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    DeviceState *dev = DEVICE(sbd);
    IPodTouchLCDState *s = IPOD_TOUCH_LCD(dev);

    memory_region_init_io(&s->iomem, obj, &lcd_ops, s, "lcd", 0x10000);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void s5l8900_lcd_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = s5l8900_lcd_realize;
}

static const TypeInfo ipod_touch_lcd_info = {
    .name          = TYPE_IPOD_TOUCH_LCD,
    .parent        = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchLCDState),
    .instance_init = s5l8900_lcd_init,
    .class_init    = s5l8900_lcd_class_init,
};

static void ipod_touch_machine_types(void)
{
    type_register_static(&ipod_touch_lcd_info);
}

type_init(ipod_touch_machine_types)
