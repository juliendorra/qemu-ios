/*
 * WebAssembly display backend: publish the guest framebuffer to JavaScript.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * The emulator runs on a pthread (PROXY_TO_PTHREAD) while the canvas lives on
 * the main thread, so this backend deliberately does NOT draw. It publishes
 * the surface's geometry and its address inside the wasm heap, and bumps a
 * generation counter on every damage event. The page then reads those pixels
 * straight out of HEAPU8 and paints, on its own animation frame.
 *
 * That split is the point:
 *
 *   - wasm memory is SharedArrayBuffer-backed, so the main thread can read the
 *     surface the emulator thread wrote without any copy or postMessage;
 *   - no cross-thread messaging is needed per frame, which is what makes a
 *     10 Hz panel affordable;
 *   - the page decides how to present (scale, rotate, colour-convert), which
 *     keeps policy out of the emulator.
 *
 * The guest panel is 320x480 at 32bpp, so a whole frame is 614,400 bytes and
 * repainting all of it is cheap. Damage rectangles are still tracked and
 * published, so a future consumer can repaint only what changed.
 */

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "ui/console.h"
#include "ui/surface.h"
#include "qapi/error.h"
#include "qemu/error-report.h"
#include <emscripten.h>

/*
 * Publish geometry. `pixels` is a wasm heap address; under -sMEMORY64 it
 * arrives in JS as a BigInt, hence bigintToI53Checked. Number() would silently
 * lose precision above 2^53, which cannot happen for our heap but is the kind
 * of thing that bites once memories grow.
 */
EM_JS(void, wasm_display_publish, (int width, int height, int stride,
                                   void *pixels), {
    Module.qemuDisplay = {
        width: width,
        height: height,
        stride: stride,
        ptr: bigintToI53Checked(pixels),
        generation: 0,
        damage: { x: 0, y: 0, w: width, h: height },
    };
});

EM_JS(void, wasm_display_damage, (int x, int y, int w, int h), {
    const d = Module.qemuDisplay;
    if (!d) {
        return;
    }
    /* Union with any damage the page has not consumed yet. */
    if (d.generation === d.consumed) {
        d.damage = { x: x, y: y, w: w, h: h };
    } else {
        const x0 = Math.min(d.damage.x, x);
        const y0 = Math.min(d.damage.y, y);
        const x1 = Math.max(d.damage.x + d.damage.w, x + w);
        const y1 = Math.max(d.damage.y + d.damage.h, y + h);
        d.damage = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }
    d.generation++;
});

static void wasm_gfx_switch(DisplayChangeListener *dcl,
                            DisplaySurface *surface)
{
    if (surface == NULL) {
        return;
    }
    wasm_display_publish(surface_width(surface), surface_height(surface),
                         surface_stride(surface), surface_data(surface));
}

static void wasm_gfx_update(DisplayChangeListener *dcl,
                            int x, int y, int w, int h)
{
    wasm_display_damage(x, y, w, h);
}

static void wasm_refresh(DisplayChangeListener *dcl)
{
    /*
     * Drives the device's own refresh path. The S5L8900 LCD re-scans at 10 Hz
     * and calls dpy_gfx_update() for the lines it touched, so damage arrives
     * without this backend polling pixels itself.
     */
    graphic_hw_update(dcl->con);
}

static const DisplayChangeListenerOps wasm_dcl_ops = {
    .dpy_name       = "wasm",
    .dpy_gfx_switch = wasm_gfx_switch,
    .dpy_gfx_update = wasm_gfx_update,
    .dpy_refresh    = wasm_refresh,
};

static DisplayChangeListener wasm_dcl = {
    .ops = &wasm_dcl_ops,
};

static void wasm_display_init(DisplayState *ds, DisplayOptions *opts)
{
    QemuConsole *con = qemu_console_lookup_by_index(0);

    if (con == NULL) {
        error_report("wasm display: the machine has no graphic console");
        exit(1);
    }
    wasm_dcl.con = con;
    register_displaychangelistener(&wasm_dcl);
}

static QemuDisplay qemu_display_wasm = {
    .type = DISPLAY_TYPE_WASM,
    .init = wasm_display_init,
};

static void register_wasm_display(void)
{
    qemu_display_register(&qemu_display_wasm);
}

type_init(register_wasm_display);
