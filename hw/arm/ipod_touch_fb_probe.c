/*
 * Framebuffer probe for the browser: "did the guest render a home screen?"
 *
 * SpringBoard never announces itself on serial, so every home-screen check in
 * this repo is a PIXEL measurement -- scripts/fb-snapshot.py reads the three
 * candidate framebuffer bases out of guest memory and reports how much of each
 * is non-black. This is that tool, inside the emulator, for a browser run.
 *
 * Why not use the display backend (-display wasm) for this: it costs real
 * time. Measured on an otherwise identical chunked boot, kernel banner at
 * 276 s with it against 118 s without, and launchd had still not arrived
 * 1,000 s in. Verification must not change what it measures, so the probe
 * reads guest memory directly and the boot runs with -display none.
 *
 * Sampling happens on a QEMU timer, i.e. on the emulator's own thread:
 * cpu_physical_memory_read() from the page's thread would be an unlocked
 * access from outside QEMU's world. The page only ever reads the published
 * results, through a seqlock, exactly as ui/wasm.c publishes its surface.
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 2 of the License, or (at your option)
 * any later version.
 */

#include "qemu/osdep.h"

#ifdef EMSCRIPTEN

#include <emscripten.h>
#include "qemu/timer.h"
#include "system/runstate.h"
#include "system/system.h"
#include "exec/cpu-common.h"

/* The bases fb-snapshot.py knows about: iBoot's logo, and the two the kernel,
 * SpringBoard and CoreSurface render into. */
static const uint32_t fb_bases[] = { 0x0fe00000, 0x0f400000, 0x0f496000 };
#define FB_PROBE_BASES ARRAY_SIZE(fb_bases)

#define FB_WIDTH  320
#define FB_HEIGHT 480
#define FB_BYTES  (FB_WIDTH * FB_HEIGHT * 4)

#define FB_PROBE_INTERVAL_MS 2000

/*
 * Published into the wasm heap for the page. All u32, all read under `seq`
 * (odd while being written), same discipline as ui/wasm.c: a torn read here
 * would be a wrong measurement reported as a fact.
 */
static struct {
    uint32_t seq;
    uint32_t samples;
    uint32_t nonblack_permille[FB_PROBE_BASES];
    uint32_t guest_ms;          /* virtual time, to tell slow from stopped */
} fb_probe;

static QEMUTimer *fb_probe_timer;

EMSCRIPTEN_KEEPALIVE uint32_t it_fb_probe_addr(void)
{
    return (uint32_t)(uintptr_t)&fb_probe;
}

static uint32_t fb_probe_one(uint32_t base, uint8_t *buffer)
{
    uint64_t nonblack = 0;

    cpu_physical_memory_read(base, buffer, FB_BYTES);
    for (size_t offset = 0; offset < FB_BYTES; offset += 4) {
        if (ldl_le_p(buffer + offset) & 0x00ffffff) {
            nonblack++;
        }
    }
    return (uint32_t)(nonblack * 1000 / (FB_WIDTH * FB_HEIGHT));
}

static void fb_probe_sample(void *opaque)
{
    /* 600 KiB, allocated once: a per-sample allocation of this size would be
     * a fresh page fault every two seconds for the whole boot. */
    static uint8_t *buffer;
    uint32_t results[FB_PROBE_BASES];

    if (buffer == NULL) {
        buffer = g_malloc(FB_BYTES);
    }
    for (unsigned i = 0; i < FB_PROBE_BASES; i++) {
        results[i] = fb_probe_one(fb_bases[i], buffer);
    }

    qatomic_set(&fb_probe.seq, fb_probe.seq + 1);      /* odd: writing */
    smp_wmb();
    for (unsigned i = 0; i < FB_PROBE_BASES; i++) {
        fb_probe.nonblack_permille[i] = results[i];
    }
    fb_probe.guest_ms =
        (uint32_t)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / SCALE_MS);
    fb_probe.samples++;
    smp_wmb();
    qatomic_set(&fb_probe.seq, fb_probe.seq + 1);      /* even: stable */

    timer_mod(fb_probe_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + FB_PROBE_INTERVAL_MS);
}

static void fb_probe_start(Notifier *notifier, void *data)
{
    /* REALTIME, not VIRTUAL: this measures how a run is progressing in the
     * wall-clock world the user is watching, and under -icount a virtual-clock
     * timer would fire at a rate that depends on the very thing being
     * measured. */
    fb_probe_timer = timer_new_ms(QEMU_CLOCK_REALTIME, fb_probe_sample, NULL);
    timer_mod(fb_probe_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + FB_PROBE_INTERVAL_MS);
}

static Notifier fb_probe_notifier = { .notify = fb_probe_start };

static void fb_probe_register(void)
{
    qemu_add_machine_init_done_notifier(&fb_probe_notifier);
}

type_init(fb_probe_register);

#endif /* EMSCRIPTEN */
