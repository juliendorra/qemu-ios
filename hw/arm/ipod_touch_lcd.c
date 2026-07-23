#include "hw/arm/ipod_touch_lcd.h"
#include "ui/pixel_ops.h"
#include "ui/console.h"
#include "hw/display/framebuffer.h"
#include "exec/cpu-common.h"
#include "system/address-spaces.h"

/* Diagnostic: how much of a 320x480x4 frame at `base` is non-black, sampled
 * straight from guest RAM (independent of what the LCD scans out). Set
 * IT_LCD_TRACE=1 to log every window-base program the guest performs. */
static int lcd_visible_sample_count(uint32_t base);

static void it_lcd_trace_base(const char *win, uint32_t base)
{
    if (!getenv("IT_LCD_TRACE")) {
        return;
    }
    fprintf(stderr, "[LCD] %s base <- 0x%08x (visible %d/6 at that base)\n",
            win, base, lcd_visible_sample_count(base));
}

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
            return s->int_mask;
        case 0x18:
            return s->int_status;

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

static void s5l8900_lcd_update_irq(IPodTouchLCDState *s)
{
    qemu_set_irq(s->irq, (s->int_status & s->int_mask) != 0);
}

static void s5l8900_lcd_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;
    uint32_t old_framebuffer_base = s->w1_framebuffer_base;
    //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, (uint32_t)val, addr);

    switch(addr) {
        case 0x4:
            s->lcd_con = val;
            break;
        case 0x8:
            s->lcd_con2 = val;
            break;

        case 0x14:
            s->int_mask = val;
            s5l8900_lcd_update_irq(s);
            break;
        case 0x18:
            s->int_status &= ~val;
            s5l8900_lcd_update_irq(s);
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
            if (s->w1_framebuffer_base != old_framebuffer_base) {
                s->invalidate = 1;
                it_lcd_trace_base("w1", (uint32_t)val);
            }
            if (s->retained_resume &&
                (val == 0x0f400000 || val == 0x0f496000)) {
                /* The retained kernel, rather than iBoot's temporary
                 * 0x0fe00000 scanout, now owns the display again. */
                s->retained_resume = false;
                s->panel_off = false;
                s->invalidate = 1;
                fprintf(stderr,
                        "[LCD] Retained kernel enabled scanout at 0x%08x\n",
                        (uint32_t)val);
            }
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
            if (val != s->w2_framebuffer_base) {
                it_lcd_trace_base("w2", (uint32_t)val);
            }
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
        if (lcd->invalidate) {
            memset(surface_data(surface), 0,
                   surface_stride(surface) * surface_height(surface));
            dpy_gfx_update(lcd->con, 0, 0,
                           surface_width(surface), surface_height(surface));
            lcd->invalidate = 0;
        }
        return;
    }

    dest_width = 4;
    draw_line = draw_line32_32;

    /* Resolution */
    first = last = 0;
    width = 320;
    height = 480;
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

    /* Do not accept touch during the early boot overlays. A useful OS frame
     * must remain visible for two seconds before input becomes ready. This is
     * deliberately a boolean readiness gate: QEMU never copies or restores
     * guest-owned framebuffer contents. */
    if (!lcd->input_ready) {
        static const uint32_t known_bases[] = {
            /* 0x0fe00000 belongs to iBoot and is overwritten on every wake. */
            0x0f400000, 0x0f496000
        };
        int best_visible_count = 0;

        if (lcd->retained_input_wait && lcd->mt->firmware_loaded &&
            !lcd->panel_off &&
            (lcd->w1_framebuffer_base == 0x0f400000 ||
             lcd->w1_framebuffer_base == 0x0f496000)) {
            lcd->input_ready = true;
            lcd->retained_input_wait = false;
            fprintf(stderr,
                    "[LCD] Retained touch input ready after Z2 reload\n");
            return;
        }

        if (lcd->retained_input_wait) {
            return;
        }

        for (int b = 0; b < ARRAY_SIZE(known_bases); b++) {
            uint32_t base = known_bases[b];
            int visible_count = lcd_visible_sample_count(base);

            if (visible_count > best_visible_count) {
                best_visible_count = visible_count;
            }
        }

        if (best_visible_count >= 4) {
            lcd->input_ready_frames++;
        } else {
            lcd->input_ready_frames = 0;
        }

        if (lcd->input_ready_frames >=
            2 * LCD_REFRESH_RATE_FREQUENCY) {
            lcd->input_ready = true;
            fprintf(stderr, "[LCD] Touch input ready "
                    "(%d/6 visible after %d frames)\n",
                    best_visible_count, lcd->input_ready_frames);
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
        if (!lcd->input_ready) {
            fprintf(stderr, "[TOUCH] Ignoring input until display/driver "
                    "startup is stable\n");
            return;
        }

        fprintf(stderr, "[TOUCH] mouse DOWN at (%.3f, %.3f)\n", fx, fy);
        ipod_touch_multitouch_on_touch(lcd->mt);
    }
    else if(!buttons_state && lcd->mt->touch_down) {
        fprintf(stderr, "[TOUCH] mouse UP at (%.3f, %.3f)\n", fx, fy);
        ipod_touch_multitouch_on_release(lcd->mt);
    }
}

static void refresh_timer_tick(void *opaque)
{
    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;

    s->int_status |= 1;
    s5l8900_lcd_update_irq(s);

    timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + NANOSECONDS_PER_SECOND / LCD_REFRESH_RATE_FREQUENCY);
}

static void s5l8900_lcd_realize(DeviceState *dev, Error **errp)
{
    IPodTouchLCDState *s = IPOD_TOUCH_LCD(dev);
    s->con = graphic_console_init(dev, 0, &s5l8900_gfx_ops, s);
    qemu_console_resize(s->con, FB_WIDTH, FB_HEIGHT);

    s->input_ready = false;
    s->input_ready_frames = 0;
    s->retained_input_wait = false;
    s->retained_resume = false;
    s->invalidate = 1;

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

static void s5l8900_lcd_class_init(ObjectClass *klass, const void *data)
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
