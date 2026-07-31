/*
 * WebAssembly display and input backend: a shared-memory seam to the page.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * The emulator runs on a pthread (-sPROXY_TO_PTHREAD) while the canvas and the
 * pointer/keyboard events live on the main thread, so this backend deliberately
 * does NOT draw and is NOT called from JS directly. Everything crosses the
 * thread boundary through plain structures in the wasm heap:
 *
 *   - the display publishes geometry, the surface address and a seqlock
 *     counter into WasmDisplayInfo; the page reads the pixels straight out of
 *     HEAPU8 and paints on its own animation frame;
 *   - input is pushed by the page into a single-producer/single-consumer ring
 *     and drained on the emulator thread by a QEMU timer, so QEMU's input
 *     queue is only ever touched by the thread that owns it.
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
 * WHY NOT EM_JS: an EM_JS body executed here runs on the emulator's *worker*,
 * where `Module` is that worker's own module object. Anything assigned to it is
 * invisible to the page. Exported functions, by contrast, are callable from any
 * thread holding the instance and all of them address the same shared memory.
 * An earlier revision published `Module.qemuDisplay` from EM_JS; it could never
 * have worked under PROXY_TO_PTHREAD.
 *
 * The guest panel is 320x480 at 32bpp, so a whole frame is 614,400 bytes and
 * repainting all of it is cheap. Damage rectangles are still tracked and
 * published, so a future consumer can repaint only what changed.
 */

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "ui/console.h"
#include "ui/input.h"
#include "ui/surface.h"
#include "qapi/error.h"
#include "qemu/error-report.h"
#include "system/runstate.h"
#include "exec/cpu-common.h"
#include "hw/arm/ipod_touch_nand.h"
#include "hw/arm/ipod_touch_lcd.h"
#include <emscripten.h>

/*
 * Published to the page. Every field is a 32-bit word so JS can read the whole
 * thing out of HEAPU32 without any layout guesswork; the surface address is
 * split into two words because a wasm64 pointer does not fit in one.
 *
 * `seq` is a seqlock: odd while the writer is mid-update, even when stable.
 * A reader takes seq, reads the fields, takes seq again and retries if it moved
 * or was odd. Without it a reader can catch a half-written pointer, which under
 * a raw HEAPU8 view means reading arbitrary memory rather than a torn frame.
 * `seq` doubles as the change counter: it advances by 2 per update.
 */
typedef struct WasmDisplayInfo {
    uint32_t seq;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t pixels_lo;
    uint32_t pixels_hi;
    uint32_t damage_x;
    uint32_t damage_y;
    uint32_t damage_w;
    uint32_t damage_h;
    /*
     * Written by the PAGE, read here: the seq it last painted. Everything else
     * in this struct goes the other way.
     *
     * Without it the damage rectangle is useless. Updates that arrive between
     * two paints have to accumulate into one rectangle, so something has to say
     * when accumulation may restart -- and with no ack the union only ever
     * grows, reaching full-screen within a second and staying there. The page
     * currently repaints in full and ignores damage, so this costs it one store
     * per frame and buys a partial-blit consumer the option of existing.
     */
    uint32_t ack;
    /*
     * QEMU_CLOCK_VIRTUAL in milliseconds — the guest's own idea of how much
     * time has passed. Deliberately OUTSIDE the seqlock: it is a single 32-bit
     * store, so it cannot tear, and putting it inside would bump `seq` on every
     * poll and force a full repaint at the poll rate rather than at the panel's.
     *
     * The page divides its delta by the wall-clock delta to get guest seconds
     * per wall second. That ratio is the only honest answer to "is this
     * real-time" -- with -icount, boot DURATION and guest SPEED are different
     * quantities, and a boot time alone cannot distinguish a fast engine from a
     * throttled tab.
     *
     * It also sizes input. The multitouch model reports motion at
     * MT_MOTION_REPORT_HZ (60) in GUEST time, so how long a press must be held
     * in WALL time is entirely a function of this ratio.
     *
     * u32 milliseconds wraps after ~49 days of guest time, which no session
     * approaches; a delta across the wrap is discarded by the reader.
     */
    uint32_t guest_ms;
} WasmDisplayInfo;

static WasmDisplayInfo wasm_display_info;

/*
 * The page needs the address of the block above. Returning it as a uint32_t
 * keeps it a plain JS Number: static data lives at the bottom of linear memory,
 * far below 4 GiB, even in a full wasm64 build.
 */
EMSCRIPTEN_KEEPALIVE uint32_t wasm_display_info_addr(void)
{
    return (uint32_t)(uintptr_t)&wasm_display_info;
}

static void wasm_display_seq_begin(void)
{
    qatomic_store_release(&wasm_display_info.seq, wasm_display_info.seq + 1);
}

static void wasm_display_seq_end(void)
{
    qatomic_store_release(&wasm_display_info.seq, wasm_display_info.seq + 1);
}

/*
 * ZERO-COPY scanout: publish the GUEST framebuffer itself, not a console
 * surface.
 *
 * The first revision registered a DisplayChangeListener and let the console
 * machinery drive the LCD model's gfx_update. That path costs three things,
 * and the page needs none of them:
 *
 *   - framebuffer_update_memory_section() enables DIRTY_MEMORY_VGA logging on
 *     the framebuffer pages, which forces every guest STORE to them off the
 *     TCG fast path -- the single most expensive consequence, invisible in
 *     any display-side profile because it is paid inside generated code;
 *   - framebuffer_update_display() syncs and walks the dirty bitmap every
 *     refresh tick;
 *   - draw_line32_32() converts BGRX to a surface the page never reads (its
 *     bytes are identical to the source anyway) -- the page does its own
 *     swizzle straight out of HEAPU32.
 *
 * So: no DCL, no console surface, no dirty tracking. The LCD model exports
 * the scanout base (it_lcd_scanout_pa); the drain timer below maps it once
 * per base change and republishes at WASM_SCANOUT_HZ. The page reads the
 * pixels from shared memory exactly as before -- same struct, same seqlock,
 * same swizzle -- it cannot tell the difference except by speed.
 *
 * Tearing: the page can catch a frame mid-composite. The real panel's DMA
 * races the CPU identically; nothing downstream cares.
 *
 * Behaviour change, deliberate: while the panel is OFF the old path blanked
 * the surface; this one stops publishing, so the page keeps the last frame.
 * The auto-lock blank therefore no longer reaches the canvas. If that matters
 * to the page it can watch guest_ms stalls or a future panel flag; blanking
 * from here would mean writing 600 KiB into guest-visible memory, which
 * zero-copy exists to avoid.
 */
/*
 * A new frame is OFFERED to the page (a seq bump; no pixels move) on EVERY
 * drain tick, ~66 Hz. The real panel is 60 Hz and the page accepts on
 * requestAnimationFrame -- the browser's own vsync -- so the canvas sees at
 * most the display's rate whatever is offered here. Do NOT rate-limit here
 * "to 60": the 15 ms tick grid aliases a 16.7 ms limit down to ~33 Hz.
 *
 * History: 10 Hz first (invisible only because a busy guest at ~6% of real
 * time makes ~4 frames a wall second), then 30. Publishing is free by
 * design; the page pays one sub-ms 153k-pixel swizzle per ACCEPTED frame,
 * and that is the entire cost of offering at full rate.
 */

static uint32_t wasm_scanout_pa;       /* currently mapped guest PA, 0 = none */
static void *wasm_scanout_ptr;
static hwaddr wasm_scanout_len;

static void wasm_publish_scanout(void)
{
    uint32_t pa = it_lcd_scanout_pa();
    uint64_t pixels;

    if (pa == 0) {
        return;                        /* panel off or not yet programmed */
    }

    if (pa != wasm_scanout_pa) {
        if (wasm_scanout_ptr != NULL) {
            cpu_physical_memory_unmap(wasm_scanout_ptr, wasm_scanout_len,
                                      false, 0);
            wasm_scanout_ptr = NULL;
        }
        wasm_scanout_len = FB_SIZE;
        wasm_scanout_ptr = cpu_physical_memory_map(pa, &wasm_scanout_len,
                                                   false);
        if (wasm_scanout_ptr == NULL || wasm_scanout_len < FB_SIZE) {
            /* Not plain RAM, or a partial mapping: refuse loudly once. */
            if (wasm_scanout_ptr != NULL) {
                cpu_physical_memory_unmap(wasm_scanout_ptr, wasm_scanout_len,
                                          false, 0);
                wasm_scanout_ptr = NULL;
            }
            fprintf(stderr, "[WASM] scanout base 0x%08x is not mappable RAM; "
                    "display frozen until the next base flip\n", pa);
            wasm_scanout_pa = 0;
            return;
        }
        wasm_scanout_pa = pa;
    }

    pixels = (uint64_t)(uintptr_t)wasm_scanout_ptr;

    wasm_display_seq_begin();
    wasm_display_info.width = FB_WIDTH;
    wasm_display_info.height = FB_HEIGHT;
    wasm_display_info.stride = FB_WIDTH * FB_BPP;
    wasm_display_info.pixels_lo = (uint32_t)pixels;
    wasm_display_info.pixels_hi = (uint32_t)(pixels >> 32);
    /* No dirty tracking by design: every publish is a full frame. */
    wasm_display_info.damage_x = 0;
    wasm_display_info.damage_y = 0;
    wasm_display_info.damage_w = FB_WIDTH;
    wasm_display_info.damage_h = FB_HEIGHT;
    wasm_display_seq_end();
}

/* ---------------------------------------------------------------- input --- */

/*
 * Single-producer (page, main thread) / single-consumer (emulator thread) ring.
 * The producer only ever advances `head`, the consumer only `tail`, so no lock
 * is needed -- just release/acquire ordering so the slot's contents are visible
 * before the index that publishes them.
 *
 * QEMU's input queue is not thread-safe and expects the BQL, which is why
 * nothing here calls into ui/input.c from the exported entry points: they only
 * write a slot. The drain timer below runs on the emulator thread, under the
 * BQL like every other QEMU timer, and does the actual dispatch.
 */
#define WASM_INPUT_RING_SIZE 256       /* power of two */
#define WASM_INPUT_RING_MASK (WASM_INPUT_RING_SIZE - 1)

enum {
    WASM_INPUT_NONE = 0,
    WASM_INPUT_TOUCH,                  /* a = x, b = y, c = down */
    WASM_INPUT_BUTTON,                 /* a = WasmButton, b = down */
};

/* The page speaks in buttons, not QKeyCodes, so the mapping stays in C. */
enum {
    WASM_BUTTON_HOME = 0,
    WASM_BUTTON_POWER = 1,
};

typedef struct WasmInputEvent {
    uint32_t type;
    int32_t a;
    int32_t b;
    int32_t c;
} WasmInputEvent;

static WasmInputEvent wasm_input_ring[WASM_INPUT_RING_SIZE];
static uint32_t wasm_input_head;
static uint32_t wasm_input_tail;
static QEMUTimer *wasm_input_timer;

/* How often the emulator thread looks at the ring. 15 ms is well inside the
 * touch controller's own sampling and cheap enough to leave running: the
 * display's own dpy_refresh would have been free, but QEMU throttles that
 * interval when a console looks idle, which would make input latency depend on
 * how much the guest happens to be drawing. */
#define WASM_INPUT_POLL_MS 15

static void wasm_input_push(uint32_t type, int32_t a, int32_t b, int32_t c)
{
    uint32_t head = qatomic_read(&wasm_input_head);
    uint32_t tail = qatomic_load_acquire(&wasm_input_tail);

    if (head - tail >= WASM_INPUT_RING_SIZE) {
        return;                        /* full: drop, the guest is wedged */
    }

    wasm_input_ring[head & WASM_INPUT_RING_MASK] = (WasmInputEvent){
        .type = type, .a = a, .b = b, .c = c,
    };
    qatomic_store_release(&wasm_input_head, head + 1);
}

/*
 * Panel coordinates, origin top-left, in the surface's own 320x480 space.
 * qemu_input_queue_abs rescales to QEMU's absolute range; the multitouch model
 * is what flips Y, exactly as it does for a native display.
 */
EMSCRIPTEN_KEEPALIVE void wasm_input_touch(int x, int y, int down)
{
    wasm_input_push(WASM_INPUT_TOUCH, x, y, down);
}

EMSCRIPTEN_KEEPALIVE void wasm_input_button(int button, int down)
{
    wasm_input_push(WASM_INPUT_BUTTON, button, down, 0);
}

static void wasm_input_dispatch(const WasmInputEvent *ev)
{
    switch (ev->type) {
    case WASM_INPUT_TOUCH:
        qemu_input_queue_abs(NULL, INPUT_AXIS_X, ev->a, 0,
                             wasm_display_info.width ?: 320);
        qemu_input_queue_abs(NULL, INPUT_AXIS_Y, ev->b, 0,
                             wasm_display_info.height ?: 480);
        qemu_input_queue_btn(NULL, INPUT_BUTTON_LEFT, ev->c != 0);
        qemu_input_event_sync();
        break;
    case WASM_INPUT_BUTTON:
        qemu_input_event_send_key_qcode(
            NULL, ev->a == WASM_BUTTON_POWER ? Q_KEY_CODE_P : Q_KEY_CODE_H,
            ev->b != 0);
        break;
    default:
        break;
    }
}

/*
 * Resuming a restored snapshot.
 *
 * `-incoming file:` leaves the machine PAUSED: QEMU records the source's
 * runstate in the migration stream, and process_incoming_migration_co() only
 * calls vm_start() when that says "running". A snapshot of a STOPPED guest
 * therefore always comes up paused -- and stopping the guest first is not
 * optional here, because a LIVE migration of this machine aborts on
 *
 *     assertion (block == qemu_get_ram_block(end - 1)) in
 *     tlb_reset_dirty_range_all
 *
 * The machine maps main RAM twice (RAM_MEM_BASE and its uncached alias at
 * RAM_MEM_BASE | UNCACHED_MEM_BIT), so a dirty range spans what that assertion
 * insists is a single block. Only stop-and-copy produces a usable stream.
 *
 * Natively the answer is one `cont` over QMP. The page has no monitor at all, so
 * it asks through the same ring the input events use, and the request is
 * serviced HERE -- on the emulator thread, under the BQL, which is what
 * vm_start() requires.
 *
 * The wait on RUN_STATE_PAUSED is the interesting part: while the incoming
 * stream is still loading the runstate is RUN_STATE_INMIGRATE, and it only
 * becomes PAUSED once the load has finished. So that transition is exactly the
 * "the snapshot is in, you may start now" signal, and polling for it avoids
 * having to guess when the load completed.
 */
static bool wasm_resume_requested;

EMSCRIPTEN_KEEPALIVE void wasm_request_resume(void)
{
    qatomic_set(&wasm_resume_requested, true);
}

/*
 * The RAW RunState index, not a hand-rolled summary.
 *
 * The first version returned 1/2/3 for running/paused/inmigrate and 0 for
 * "anything else" -- and the browser resume then sat at 0, which said only that
 * it was in none of the three states guessed at. A value that cannot name the
 * state it found is not a diagnostic. The page prints RunState_str() of this,
 * so a new state explains itself.
 */
EMSCRIPTEN_KEEPALIVE int wasm_run_state(void)
{
    return (int)runstate_get();
}

/* The name, so the page does not carry a copy of the enum that can drift. */
EMSCRIPTEN_KEEPALIVE const char *wasm_run_state_name(void)
{
    return RunState_str(runstate_get());
}

/*
 * W6: hand the NAND copy-on-write overlay to the page so it can persist it.
 *
 * The overlay is a live GHashTable owned by the emulator thread, so the page
 * cannot walk it. It asks here instead; the drain timer serializes it on the
 * emulator thread and publishes a pointer the page reads straight out of
 * HEAPU8, the same copy-free arrangement the framebuffer uses.
 *
 * Persistence is keyed to the NAND PACK, not to the engine's vmstate layout,
 * which is the reason it lives here and not in a VM snapshot: a snapshot is
 * invalidated by any device gaining a VMStateDescription, and would take a
 * user's saved state with it. It also would not work -- the overlay has no
 * vmstate, so a snapshot does not capture guest NAND writes at all.
 */
static bool wasm_overlay_requested;
static uint8_t *wasm_overlay_blob;
static uint32_t wasm_overlay_len;

EMSCRIPTEN_KEEPALIVE void wasm_request_overlay_save(void)
{
    qatomic_set(&wasm_overlay_requested, true);
}

/* 0 while the request is outstanding; the byte count once it is ready. */
EMSCRIPTEN_KEEPALIVE uint32_t wasm_overlay_size(void)
{
    return qatomic_read(&wasm_overlay_len);
}

EMSCRIPTEN_KEEPALIVE uint32_t wasm_overlay_addr(void)
{
    return (uint32_t)(uintptr_t)qatomic_read(&wasm_overlay_blob);
}

/* The page calls this once it has copied the bytes out. */
EMSCRIPTEN_KEEPALIVE void wasm_overlay_release(void)
{
    uint8_t *blob = qatomic_xchg(&wasm_overlay_blob, NULL);

    qatomic_set(&wasm_overlay_len, 0);
    g_free(blob);
}

static void wasm_maybe_save_overlay(void)
{
    uint8_t *blob;
    uint32_t len = 0;

    if (!qatomic_read(&wasm_overlay_requested)) {
        return;
    }
    if (qatomic_read(&wasm_overlay_blob) != NULL) {
        return;                        /* the page has not collected the last */
    }
    qatomic_set(&wasm_overlay_requested, false);

    blob = it_nand_overlay_save(&len);
    if (blob == NULL) {
        return;                        /* no overlay: nothing to persist */
    }
    /* Publish the pointer LAST: the page polls the size, so a non-zero size
     * must imply a valid pointer. */
    qatomic_set(&wasm_overlay_blob, blob);
    qatomic_set(&wasm_overlay_len, len);
}

static void wasm_maybe_resume(void)
{
    static RunState last_reported = RUN_STATE__MAX;
    RunState now;

    if (!qatomic_read(&wasm_resume_requested)) {
        return;
    }

    now = runstate_get();
    if (now != last_reported) {
        /*
         * Every transition while a resume is pending, once each. Without this
         * a stuck resume is a silent black canvas: the page can poll the state
         * but cannot see the SEQUENCE, and the sequence is what says whether
         * the incoming stream ever finished loading.
         */
        last_reported = now;
        fprintf(stderr, "[WASM] resume pending; runstate=%s\n",
                RunState_str(now));
    }

    if (now == RUN_STATE_RUNNING) {
        qatomic_set(&wasm_resume_requested, false);
        fprintf(stderr, "[WASM] already running; nothing to resume\n");
        return;
    }
    if (now != RUN_STATE_PAUSED) {
        return;                        /* still loading incoming state */
    }
    qatomic_set(&wasm_resume_requested, false);
    fprintf(stderr, "[WASM] snapshot loaded; starting the vcpu\n");
    vm_start();
}

static void wasm_input_drain(void *opaque)
{
    uint32_t tail = qatomic_read(&wasm_input_tail);
    uint32_t head = qatomic_load_acquire(&wasm_input_head);

    wasm_maybe_resume();
    wasm_maybe_save_overlay();
    wasm_publish_scanout();

    while (tail != head) {
        wasm_input_dispatch(&wasm_input_ring[tail & WASM_INPUT_RING_MASK]);
        tail++;
    }
    qatomic_store_release(&wasm_input_tail, tail);

    /*
     * Published from here rather than from dpy_refresh: this timer runs on a
     * fixed 15 ms of REAL time whatever the guest is doing, whereas the display
     * refresh interval is throttled when a console looks idle -- which would
     * make the sampling rate depend on the very thing being measured.
     */
    qatomic_store_release(&wasm_display_info.guest_ms,
                          (uint32_t)qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL));

    timer_mod(wasm_input_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + WASM_INPUT_POLL_MS);
}

/* --------------------------------------------------------------- wiring --- */

static void wasm_display_init(DisplayState *ds, DisplayOptions *opts)
{
    /*
     * Deliberately NO DisplayChangeListener. Registering one starts the
     * console's GUI refresh timer, whose graphic_hw_update() drives the LCD
     * model's surface path and enables DIRTY_MEMORY_VGA logging on the
     * framebuffer -- the costs itemised above wasm_publish_scanout(). The
     * scanout is published from the drain timer instead, and input never
     * needed the console in the first place.
     */
    if (qemu_console_lookup_by_index(0) == NULL) {
        error_report("wasm display: the machine has no graphic console");
        exit(1);
    }

    wasm_input_timer = timer_new_ms(QEMU_CLOCK_REALTIME, wasm_input_drain,
                                    NULL);
    timer_mod(wasm_input_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + WASM_INPUT_POLL_MS);
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
