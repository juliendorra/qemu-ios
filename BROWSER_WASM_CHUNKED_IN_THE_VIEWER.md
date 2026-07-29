# Chunked delivery in the VISIBLE page — brief for Session A

Written by Session B (2026-07-29). Everything here is landed and measured; what
is missing is the last join: **the page that paints is still staging the whole
pack, and the page that streams chunks does not paint.**

`web/public/*/index.html` is Session A's, and A has committed to it three times
today, so B is not editing it. This is the brief for doing it in A's session —
it is meant to be pasteable as-is.

---

## What exists already (nothing to build)

| piece | file | state |
| --- | --- | --- |
| chunk asset sets | `web/chunked/1A543a/`, `web/chunked/4A102/` | built, 62 pages/chunk, Brotli q11 |
| page-side loader | `web/src/emulator/chunk-loader.js` | exports everything below |
| service worker | `web/sw.js` | caches chunks, prefetches the boot order |
| the fetch worker | `web/chunk-fetch-worker.js` | classic worker, futex handshake |
| emulator side | `hw/arm/ipod_touch_nand_chunks.c` | picks the chunked path automatically |
| RAM overlay | `hw/arm/ipod_touch_nand.c` | `overlay=ram` in `<nand>/nand-tune` |

Measured, cold cache, in standalone Chrome: **1.0 downloads 18.57 MiB** and
reaches the home screen in 252 s; **1.1.4 downloads 20.97 MiB** and reaches it
in 296 s (196 s warm). Warm boots download ~nothing. Raw packs are 215.2 and
299.7 MiB.

## The change to the viewer, in five steps

```js
import { registerChunkWorker, loadManifest, stageChunkedNand,
         prefetchBootSet, startChunkFetcher }
  from '../../src/emulator/chunk-loader.js';       // adjust the depth

// 1. before creating the Module: the worker must be controlling the page, or
//    the first chunk requests bypass the cache
await registerChunkWorker('/sw.js');
const manifest = await loadManifest(`/chunked/${BUILD}/`);

await Module({
  arguments: [ /* … unchanged, keep -icount shift=1 … */ ],

  preRun: [async (mod) => {
    mod.FS.mkdir('/fw');
    // 2. stage the INDEX, not the pack: ~0.5 MiB instead of 216
    //    (this also creates /fw/nand and its bank0..bank7 directories)
    await stageChunkedNand(mod.FS, manifest);
    // 3. copy-on-write overlay in RAM. Without it the boot stalls after
    //    launchd for ever, and the FILE-backed writable mode is worse here:
    //    every MEMFS syscall is proxied to the main thread under
    //    -sPROXY_TO_PTHREAD, so the per-read stat() becomes a cross-thread
    //    round trip and the boot stalls outright.
    mod.FS.writeFile('/fw/nand/nand-tune', 'overlay=ram\n');
    // 4. prefetch the recorded boot order, and DO await it: a demand-faulted
    //    chunk costs the guest a whole round trip
    await prefetchBootSet(manifest, {
      onProgress: (m) => status(`prefetch ${m.done}/${m.total}`),
    });
  }],

  // 5. the PAGE starts the fetch worker, once the runtime exports exist
  onRuntimeInitialized: () => {
    startChunkFetcher(Module, {                     // the module object
      script: manifest.fetcher || '/chunk-fetch-worker.js',
      onError: (m) => console.warn('chunk fetch failed', m.url, m.message),
    }).catch((e) => status(`chunk fetcher: ${e.message}`));
  },
});
```

Nothing else about the page changes — same machine line, same `-icount shift=1`,
same display and input code.

## Rebuild `build-wasm` with `--configure` once

A's current `build-wasm` predates the mailbox export, so the fetcher will report
"emulator exports no chunk mailbox". And the cross file gained `-sFETCH`, which
meson only reads at CONFIGURE time:

```bash
scripts/wasm/build-qemu.sh --configure
```

A plain rebuild is a silent no-op for the flag, and the link then fails on
`emscripten_fetch` (the fallback transport).

## Traps, all of them paid for already

- **The page must own the fetch worker.** If the emulator's thread creates it,
  the fetch never completes: a nested dedicated worker is serviced through its
  parent's context, and that parent is blocked in `Atomics.wait`. Measured 10 s
  timeout nested versus 5 ms page-owned.
- **Do not try to fetch synchronously on the emulator's thread.** Chrome refuses
  a synchronous XHR from a module worker, which is what `-sEXPORT_ES6` makes
  Emscripten's pthreads, and `emscripten_fetch(SYNCHRONOUS)` fails the same way
  with **zero bytes and no error**.
- **Keep the `bank0..bank7` directories.** `stageChunkedNand` makes them.
- **Serve with `scripts/wasm/serve.py`.** Chunks are stored already-Brotli'd and
  need `Content-Encoding: br`; the dev server does that, and a plain static
  server will hand the emulator compressed bytes.
- **Verify by bytes, not by belief:** `http://localhost:PORT/__chunk-stats`
  reports what the server actually shipped. Expect ~18.5 MiB (1.0) or ~21 MiB
  (1.1.4) cold, and ~0 warm.

## What to expect once it works

The viewer will paint the same boot it paints now, but the first visit costs
~19-21 MiB instead of 216-300, and a second visit costs nothing. Boot times in
the viewer stay the viewer's — painting costs real time (kernel at 276 s with
`-display wasm` against 118 s with `-display none`), so compare against A's own
previous numbers, not against B's `bench-b` timings.

## Files Session B owns — no need to touch any of them

`web/src/emulator/chunk-loader.js`, `web/sw.js`, `web/chunk-fetch-worker.js`,
`web/bench-b/*`, `scripts/wasm/*`, `hw/arm/ipod_touch_nand*.c`,
`hw/arm/ipod_touch_fb_probe.c`, `tcg/wasm64.c`.

If something in them is wrong for the viewer's needs, say so rather than
patching around it — the seam is meant to serve both pages.
