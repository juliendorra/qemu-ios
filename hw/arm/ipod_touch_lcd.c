#include "hw/arm/ipod_touch_lcd.h"
#include "target/arm/cpu.h"
#include "hw/core/cpu.h"
#include "ui/pixel_ops.h"
#include "ui/console.h"
#include "hw/display/framebuffer.h"
#include "migration/vmstate.h"
#include "exec/cpu-common.h"
#include "system/address-spaces.h"

/* Diagnostic: how much of a 320x480x4 frame at `base` is non-black, sampled
 * straight from guest RAM (independent of what the LCD scans out). Set
 * IT_LCD_TRACE=1 to log every window-base program the guest performs. */
static int lcd_visible_sample_count(uint32_t base);

static void it_lcd_trace_base(const char *win, uint32_t base)
{
    uint32_t pc = 0, lr = 0;

    if (!getenv("IT_LCD_TRACE")) {
        return;
    }
    /*
     * WHO programmed the base. iPhone OS 1.1.4 re-points the window 182 times
     * in a run and 1.0 only twice, which is why 1.0 never shows the home screen
     * again after an app is dismissed (it repaints into a backing store and the
     * display is never aimed at it). Naming the writer is the way to find what
     * 1.0 is missing, and the guest pc is right here for the taking -- the same
     * idiom the [BTN] trace uses.
     */
    if (current_cpu) {
        CPUARMState *env = &ARM_CPU(current_cpu)->env;
        pc = env->regs[15];
        lr = env->regs[14];
    }
    /* Virtual timestamp goes LAST: app-button-probe's LCD_BASE_RE matches
     * the head of this line literally. Cadence of the flips is the dismissal
     * latency question -- a line without time cannot answer it. */
    int64_t now = qemu_clock_get_us(QEMU_CLOCK_VIRTUAL);
    fprintf(stderr, "[LCD] %s base <- 0x%08x (visible %d/6 at that base) "
            "pc=0x%08x lr=0x%08x t=%lld.%06lld\n",
            win, base, lcd_visible_sample_count(base), pc, lr,
            now / 1000000LL, now % 1000000LL);
}

/* IT_FB_TRACE=1: log every LCD MMIO access. The render investigation needs
 * the whole display-controller conversation, not just window-base programs:
 * a driver that never writes vidcon/wndcon, or polls a status register
 * forever, is invisible to IT_LCD_TRACE. Per-register throttling keeps the
 * 60 Hz vblank-ack chatter bounded: the first 8 accesses of each register
 * print, then every 4096th with its running count. */
static bool it_fb_trace_enabled(void)
{
    static int cached = -1;
    if (cached < 0) {
        cached = getenv("IT_FB_TRACE") != NULL;
    }
    return cached;
}

static void it_fb_trace_mmio(const char *dir, hwaddr addr, uint64_t val)
{
    static uint32_t counts[0x240 / 4];
    uint32_t idx = addr / 4;

    if (!it_fb_trace_enabled()) {
        return;
    }
    if (idx < ARRAY_SIZE(counts)) {
        uint32_t n = ++counts[idx];
        if (n > 8 && (n & 0xFFF) != 0) {
            return;
        }
        fprintf(stderr, "[FB] %s 0x%03x = 0x%08x (n=%u)\n",
                dir, (uint32_t)addr, (uint32_t)val, n);
    } else {
        /* an out-of-map register is exactly what we want to see */
        fprintf(stderr, "[FB] %s 0x%03x = 0x%08x (unmapped)\n",
                dir, (uint32_t)addr, (uint32_t)val);
    }
}

static uint64_t s5l8900_lcd_read_internal(void *opaque, hwaddr addr, unsigned size);

static uint64_t s5l8900_lcd_read(void *opaque, hwaddr addr, unsigned size)
{
    uint64_t val = s5l8900_lcd_read_internal(opaque, addr, size);
    it_fb_trace_mmio("rd", addr, val);
    return val;
}

static uint64_t s5l8900_lcd_read_internal(void *opaque, hwaddr addr, unsigned size)
{
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
    it_fb_trace_mmio("wr", addr, val);

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
                /* Window 2 can be the scanned-out window (see
                 * lcd_scanout_base), so a base change here must repaint. */
                s->invalidate = 1;
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

/* Which window the panel is actually scanning out.
 *
 * The controller has two window register blocks -- 0x58..0x68 ("w1") and
 * 0x70..0x80 ("w2") -- and real silicon composites both. iBoot and the OS do
 * not use the same one:
 *
 *   iBoot  programs ONLY w2, with its own framebuffer at 0x0fe00000. That is
 *          where it draws the Apple boot logo (and the battery/recovery
 *          images). Confirmed on both boards by IT_FB_TRACE: the only window
 *          writes before iBoot's banner are 0x070..0x080.
 *   kernel  programs w1, first adopting iBoot's 0x0fe00000 so the logo stays
 *          up, then flipping between its own buffers at 0x0f400000/0x0f496000.
 *
 * Scanning out w1 unconditionally therefore showed nothing at all for the
 * whole of iBoot: w1 is still zero then. On the iPod that was masked, because
 * its kernel reaches the adopt-0x0fe00000 step quickly and the logo appears to
 * have been there all along; on the iPhone the kernel takes far longer to get
 * there, so the screen stayed black for the entire boot and the logo only
 * flashed at the very end. Same defect, different exposure.
 *
 * w1 wins as soon as it has been programmed, so the OS-era behaviour is
 * exactly what it was; w2 is the fallback that covers the iBoot era.
 * WNDCON (0x20) cannot arbitrate: it is written once, by iBoot, and the
 * kernel never touches it.
 */
static uint32_t lcd_scanout_base(IPodTouchLCDState *lcd)
{
    return lcd->w1_framebuffer_base ? lcd->w1_framebuffer_base
                                    : lcd->w2_framebuffer_base;
}

/*
 * Zero-copy scanout for the WebAssembly display (ui/wasm.c).
 *
 * The wasm page reads guest RAM straight out of the shared heap and does its
 * own colour conversion, so going through the QEMU console surface would cost
 * three things for nothing: the per-refresh dirty-bitmap sync, a 320x480
 * conversion into a surface nobody reads, and -- the expensive one --
 * DIRTY_MEMORY_VGA logging on the framebuffer pages, which pushes every guest
 * STORE to them through the slow path. This accessor is the whole interface
 * instead: the current scanout base, or 0 while the panel is off.
 *
 * Compiled unconditionally (it is trivially small); only ui/wasm.c calls it.
 */
static IPodTouchLCDState *it_lcd_instance;

uint32_t it_lcd_scanout_pa(void)
{
    IPodTouchLCDState *s = it_lcd_instance;

    if (s == NULL || s->panel_off) {
        return 0;
    }
    return lcd_scanout_base(s);
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
        framebuffer_update_memory_section(&lcd->fbsection, lcd->sysmem, lcd_scanout_base(lcd), height, 4 * width);
    }

    framebuffer_update_display(surface, &lcd->fbsection,
                               width, height,
                               src_width,       /* Length of source line, in bytes.  */
                               linesize,        /* Bytes between adjacent horizontal output pixels.  */
                               dest_width,      /* Bytes between adjacent vertical output pixels.  */
                               lcd->invalidate,
                               draw_line, NULL,
                               &first, &last);
    /*
     * IT_FB_TRACE: what the presenter actually decided. The guest can be
     * painting perfectly while the HOST window stays frozen -- that is a
     * different failure from "the guest stopped drawing", and only this
     * distinguishes them. first < 0 means dirty tracking found nothing to
     * repaint, so no dpy_gfx_update is issued and the window keeps whatever
     * it last showed.
     */
    if (it_fb_trace_enabled()) {
        static uint32_t calls, silent;
        calls++;
        if (first < 0) {
            silent++;
        }
        if (calls % LCD_REFRESH_RATE_FREQUENCY == 0) {
            fprintf(stderr, "[FB] present t=%us base=0x%08x first=%d last=%d "
                    "silent=%u/%u\n", calls / LCD_REFRESH_RATE_FREQUENCY,
                    lcd_scanout_base(lcd), first, last, silent, calls);
        }
    }

    if (first >= 0) {
        dpy_gfx_update(lcd->con, 0, first, width, last - first + 1);
    }
    lcd->invalidate = 0;

}

/* Touch readiness is evaluated on the LCD's own refresh timer, NOT from
 * gfx_update. gfx_update only runs when a host display client exists, so
 * under `-display none` the gate never armed and every touch was refused --
 * which made headless input tests (scripts/lock-unlock-probe.py) impossible
 * and would equally affect any UI automation. The timer runs regardless. */
static void lcd_update_input_ready(IPodTouchLCDState *lcd)
{
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

        /* The gate exists to refuse touch during the boot overlays. Once the
         * device HAS been interactive, that has been proven for good: a later
         * wake must not depend on the touch controller's firmware being
         * re-uploaded, because the guest treats a retained-RAM resume as a
         * resume and may never re-upload it -- leaving input dead forever.
         * Measured symptom (scripts/lock-unlock-probe.py): power/home, then
         * slide-to-unlock is ignored, with "[TOUCH] Ignoring input until
         * display/driver startup is stable" on every touch. */
        if (lcd->retained_input_wait && lcd->input_ever_ready &&
            !lcd->panel_off &&
            (lcd->w1_framebuffer_base == 0x0f400000 ||
             lcd->w1_framebuffer_base == 0x0f496000)) {
            lcd->input_ready = true;
            lcd->retained_input_wait = false;
            fprintf(stderr, "[LCD] Touch input restored after wake "
                    "(device was already interactive)\n");
            return;
        }
        if (lcd->retained_input_wait && lcd->mt->firmware_loaded &&
            !lcd->panel_off &&
            (lcd->w1_framebuffer_base == 0x0f400000 ||
             lcd->w1_framebuffer_base == 0x0f496000)) {
            lcd->input_ready = true;
            lcd->input_ever_ready = true;
            lcd->retained_input_wait = false;
            fprintf(stderr,
                    "[LCD] Retained touch input ready after Z2 reload\n");
            return;
        }

        /* Do NOT bail out here. This used to `return` whenever a retained
         * wake was pending, which meant that if neither fast path above
         * matched -- the panel still off when the base was programmed, or a
         * framebuffer base other than the two listed -- the generic
         * stable-frame path below never ran and touch stayed dead for the
         * rest of the session. That is the intermittent "slide-to-unlock does
         * nothing after a sleep" seen on BOTH boards. Falling through costs
         * only the original two seconds of visible frames, and always
         * terminates. */

        for (int b = 0; b < ARRAY_SIZE(known_bases); b++) {
            uint32_t base = known_bases[b];
            int visible_count = lcd_visible_sample_count(base);

            if (visible_count > best_visible_count) {
                best_visible_count = visible_count;
            }
        }

        /*
         * IT_GATE_TRACE=1: why is touch refused RIGHT NOW?
         *
         * The user-reported failure is "the slide-to-unlock screen is
         * VISIBLE and touch does nothing", which this gate can cause in a way
         * no other trace shows: the fallback counts visible pixels only at
         * known_bases[] above, and 0x0fe00000 is deliberately excluded. A
         * lock screen scanned out from any other base leaves best_visible
         * stuck at 0, input_ready_frames never accumulates, and the gate stays
         * shut for the rest of the session WHILE THE PANEL IS LIT. Printed
         * once a second so a dead-touch window can be read off directly.
         */
        if (getenv("IT_GATE_TRACE")) {
            static int gate_ticks;
            if (++gate_ticks % LCD_REFRESH_RATE_FREQUENCY == 0) {
                fprintf(stderr,
                        "[GATE] REFUSING touch: panel_off=%d w1_base=0x%08x "
                        "visible=%d/6 frames=%d retained_wait=%d ever=%d "
                        "fast=%d\n",
                        lcd->panel_off, lcd->w1_framebuffer_base,
                        best_visible_count, lcd->input_ready_frames,
                        lcd->retained_input_wait, lcd->input_ever_ready,
                        lcd->relight_input_fast);
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
            lcd->input_ever_ready = true;
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

    /*
     * IT_FB_TRACE also reports the vsync state once a second. The MMIO trace
     * above suppresses a register after its 8th access, which hides exactly
     * the question that matters when the guest wedges: is the frame interrupt
     * still being RAISED, and is the guest still ACKing it? A stuck-high
     * int_status means the line never falls, so an edge-triggered consumer
     * gets no further interrupts and simply never wakes -- which is what an
     * idle-looping guest looks like from the outside.
     */
    if (it_fb_trace_enabled()) {
        static uint32_t ticks;
        if (++ticks % LCD_REFRESH_RATE_FREQUENCY == 0) {
            fprintf(stderr, "[FB] vsync t=%us status=0x%08x mask=0x%08x "
                    "irq=%d acked_since_last=%s\n",
                    ticks / LCD_REFRESH_RATE_FREQUENCY,
                    s->int_status, s->int_mask,
                    (s->int_status & s->int_mask) != 0,
                    s->int_status & 1 ? "NO (status still set)" : "yes");
        }
    }

    s->int_status |= 1;
    s5l8900_lcd_update_irq(s);
    lcd_update_input_ready(s);

    timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + NANOSECONDS_PER_SECOND / LCD_REFRESH_RATE_FREQUENCY);
}

static void s5l8900_lcd_realize(DeviceState *dev, Error **errp)
{
    IPodTouchLCDState *s = IPOD_TOUCH_LCD(dev);
    s->con = graphic_console_init(dev, 0, &s5l8900_gfx_ops, s);
    qemu_console_resize(s->con, FB_WIDTH, FB_HEIGHT);
    it_lcd_instance = s;

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

/*
 * Migration. This is the FIRST device in the machine to get a
 * VMStateDescription, and it is first for a measured reason: with none of the
 * 26 ipod_touch devices migratable, `migrate file:` restored guest RAM
 * byte-identically and the panel came back BLACK -- the rendered home screen was
 * still in RAM, but w1_framebuffer_base was zero, so the LCD scanned out
 * nothing. Scanout 45.4% -> 0.003% across a save/restore, with all three
 * framebuffers in RAM unchanged at 59.03%. See BROWSER_WASM_STATUS.md.
 *
 * What is deliberately NOT here:
 *
 *   sysmem, con, mt, irq   pointers into objects that realize() rebuilds; they
 *                          are valid before the incoming state is loaded.
 *   fbsection              a MemoryRegionSection cached from the LAST scanout
 *                          base. Migrating it would carry a stale mapping, so
 *                          post_load forces `invalidate` instead and the next
 *                          refresh recomputes it from w1/w2.
 *
 * The refresh timer IS migrated: it is what drives the panel, and a restored
 * machine whose refresh timer never fires again looks exactly like a dead
 * panel -- the same symptom this whole exercise is about.
 */
static int s5l8900_lcd_post_load(void *opaque, int version_id)
{
    IPodTouchLCDState *s = (IPodTouchLCDState *)opaque;

    /*
     * Recompute the framebuffer mapping and repaint everything. The incoming
     * scanout base may differ from whatever this freshly realized device had,
     * and dirty tracking cannot know that: it would report nothing to repaint
     * and the host window would keep its initial black frame.
     */
    s->invalidate = 1;
    return 0;
}

static const VMStateDescription vmstate_ipod_touch_lcd = {
    .name = "ipod-touch-lcd",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = s5l8900_lcd_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(lcd_con, IPodTouchLCDState),
        VMSTATE_UINT32(lcd_con2, IPodTouchLCDState),
        VMSTATE_UINT32(int_mask, IPodTouchLCDState),
        VMSTATE_UINT32(int_status, IPodTouchLCDState),
        VMSTATE_UINT32(wnd_con, IPodTouchLCDState),
        VMSTATE_UINT32(vid_con0, IPodTouchLCDState),
        VMSTATE_UINT32(vid_con1, IPodTouchLCDState),
        VMSTATE_UINT32(vidt_con0, IPodTouchLCDState),
        VMSTATE_UINT32(vidt_con1, IPodTouchLCDState),
        VMSTATE_UINT32(vidt_con2, IPodTouchLCDState),
        VMSTATE_UINT32(vidt_con3, IPodTouchLCDState),
        VMSTATE_UINT32(w1_hspan, IPodTouchLCDState),
        VMSTATE_UINT32(w1_framebuffer_base, IPodTouchLCDState),
        VMSTATE_UINT32(w1_display_resolution_info, IPodTouchLCDState),
        VMSTATE_UINT32(w1_display_depth_info, IPodTouchLCDState),
        VMSTATE_UINT32(w1_qlen, IPodTouchLCDState),
        VMSTATE_UINT32(w2_hspan, IPodTouchLCDState),
        VMSTATE_UINT32(w2_framebuffer_base, IPodTouchLCDState),
        VMSTATE_UINT32(w2_display_resolution_info, IPodTouchLCDState),
        VMSTATE_UINT32(w2_display_depth_info, IPodTouchLCDState),
        VMSTATE_UINT32(w2_qlen, IPodTouchLCDState),
        VMSTATE_TIMER_PTR(refresh_timer, IPodTouchLCDState),
        VMSTATE_BOOL(input_ready, IPodTouchLCDState),
        VMSTATE_BOOL(input_ever_ready, IPodTouchLCDState),
        VMSTATE_INT32(input_ready_frames, IPodTouchLCDState),
        VMSTATE_BOOL(retained_input_wait, IPodTouchLCDState),
        VMSTATE_BOOL(panel_off, IPodTouchLCDState),
        VMSTATE_BOOL(retained_resume, IPodTouchLCDState),
        VMSTATE_BOOL(relight_input_fast, IPodTouchLCDState),
        VMSTATE_END_OF_LIST()
    },
};

static void s5l8900_lcd_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = s5l8900_lcd_realize;
    dc->vmsd = &vmstate_ipod_touch_lcd;
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
