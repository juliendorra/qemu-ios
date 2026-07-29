# Session A — make it visible and interactive

One of two parallel browser-port sessions. **Session B** (`BROWSER_WASM_SESSION_B.md`)
works on speed and assets at the same time; the two touch different files on
purpose.

Read first: [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md) for the ordered
plan and the traps, [`BROWSER_WASM_STATUS.md`](BROWSER_WASM_STATUS.md) for what
is proven and every dead end so far.

**Goal: a browser page showing the iPhone home screen that responds to touch.**
Not a fast one — that is Session B's job.

---

## What already works

- `build-wasm/qemu-system-arm.wasm` runs iPhone OS in the browser and boots to
  launchd. Build with `IT_WASM_MEMORY64_FULL=1 scripts/wasm/build-qemu.sh`.
- **`-display wasm` exists** (`ui/wasm.c`, committed). It does not draw: it
  publishes `Module.qemuDisplay = {width, height, stride, ptr, generation,
  damage}` where `ptr` is an address inside the wasm heap, and bumps
  `generation` on damage. Painting is the page's job.
- `web/public/jit-boot/` is a working boot page: stages firmware into MEMFS,
  polls a serial log, tracks landmarks. Start it with `scripts/wasm/serve.py`
  and open `/public/jit-boot/`.

## You own these files

```
ui/wasm.c                 the display backend
web/                      pages, canvas, input plumbing
migration/, savevm paths  only if you take A3
```

Do **not** touch `tcg/wasm64.c`, `hw/arm/ipod_touch_nand.c` or
`scripts/wasm/measure-pack.py` / `analyze-nand-trace.py` — Session B has them.

## Avoiding collisions with Session B

| | |
| --- | --- |
| Build directory | yours is the default `build-wasm/`; B uses `WASM_BUILD_DIR=build-wasm-b` |
| Dev server | yours is `serve.py` (8010); B uses `--port 8011` |
| Native build | `build-ipod11/` is the shared correctness oracle — keep it green |

---

## A1 — Paint the framebuffer (start here)

Add a canvas to the boot page and paint from `Module.qemuDisplay`.

```js
const d = Module.qemuDisplay;               // published by ui/wasm.c
if (d && d.generation !== lastGeneration) {
  lastGeneration = d.generation;
  const bytes = HEAPU8.subarray(d.ptr, d.ptr + d.stride * d.height);
  // → ImageData → ctx.putImageData
}
```

Things that will bite:

- **Pixel format.** The surface is 32bpp but almost certainly `x8r8g8b8`
  (BGRA in memory on a little-endian host), while `ImageData` wants RGBA.
  Check `surface_format()` and swap channels; if the panel comes out blue,
  this is why.
- **Run the loop on `requestAnimationFrame`**, not a timer. The panel refreshes
  at 10 Hz, so most frames have nothing new — compare `generation` first.
- **`d.ptr` can move.** `dpy_gfx_switch` republishes on every surface change,
  so re-read `Module.qemuDisplay` each frame rather than caching the pointer.
- The emulator runs on a pthread and wasm memory is SharedArrayBuffer-backed,
  so reading it from the main thread is safe and copy-free. That is the whole
  reason the backend publishes instead of drawing.

**Acceptance:** the boot page shows the Apple logo, then the home screen. The
native oracle renders 59.0% non-black for 1.0 — a good sanity check.

## A2 — Input

Two separate paths, both already present natively:

- **Touch** — `qemu_add_mouse_event_handler(ipod_touch_lcd_mouse_event, …, 1, …)`
  in `hw/arm/ipod_touch_lcd.c`. Absolute coordinates in the panel's 320×480
  space; `web/src/app/main.js` already normalises pointer events that way.
- **Home / Power** — `ipod_touch_input_event()` in `hw/arm/ipod_touch.c` maps
  `Q_KEY_CODE_H` / `Q_KEY_CODE_P` to guest keycodes. The web shell already
  emits `home`/`power` and binds the `H`/`P` keys.

The hard part is threading: input arrives on the main thread, QEMU's input
queue belongs to the emulator thread. Use a bottom half (`qemu_bh_schedule`) or
`emscripten_dispatch_to_thread` rather than calling QEMU input APIs directly
from JS. Export the entry points with `EMSCRIPTEN_KEEPALIVE`.

**Acceptance:** tapping an icon launches an app; Home returns to SpringBoard;
Power sleeps and wakes.

## A3 — Skip the boot (the big win)

A cold boot in the browser currently takes many minutes, and Session B may or
may not fix that. A **post-boot snapshot** sidesteps it: resume a machine that
is already at the home screen and startup becomes download-and-resume.

- QEMU 11.0.2 has `file:` migration, so `migrate file:/fw/state` and
  `-incoming file:/fw/state` are the likely mechanism — check what survives
  under Emscripten (threads, timers, the `-icount` clock).
- The snapshot embeds guest RAM (128 MiB) — compresses well, and is a natural
  fit for Session B's chunked delivery.
- Take the snapshot **natively** (`build-ipod11/`) and restore it in the
  browser if the format allows; that skips the slow boot entirely during
  development too.

**Acceptance:** the page reaches an interactive home screen in seconds rather
than minutes.

---

## Standing constraints (from the whole port)

- **Always `-icount shift=1`.** Anything slower makes the guest take timeout
  paths and panic. This is not a browser workaround — the same panic was
  reproduced natively (1 in 3 boots) and the launcher now passes it too.
- **The NAND needs its `bank0..bank7` directories** even when packed and
  read-only, or the first guest write calls `hw_error()` and kills the
  emulator.
- **Verify by framebuffer, not serial.** SpringBoard never announces itself,
  and iPhone OS 1.0's iBoot-159 prints nothing at all before the kernel.
- **Never run `meson`/`ninja` on the wasm build by hand** — outside the
  toolchain environment it re-detects host libraries and enables curl/zstd for
  a WebAssembly build. Always go through `scripts/wasm/build-qemu.sh`.
