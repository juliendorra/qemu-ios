/*
 * Page-side half of chunked NAND delivery.
 *
 * Stages the small, always-needed part of a chunked NAND into MEMFS -- the
 * pack index, the chunk hash table, the chunk config -- and asks the service
 * worker to prefetch the recorded boot working set. The payload itself is
 * never staged: the emulator fetches chunks synchronously as it reads them
 * (hw/arm/ipod_touch_nand_chunks.c), which is what turns a 215.6 MiB download
 * into ~18.6 MiB.
 *
 * Cold vs warm is entirely the service worker's Cache Storage: a second boot
 * reads every chunk out of it and touches the network for nothing.
 */

export async function registerChunkWorker(scriptUrl = '/sw.js') {
  if (!('serviceWorker' in navigator)) {
    throw new Error('no service worker: chunked delivery needs one, because ' +
                    "the emulator's reads are synchronous");
  }
  const registration = await navigator.serviceWorker.register(scriptUrl,
                                                              { scope: '/' });
  await navigator.serviceWorker.ready;
  // A page that was not controlled at load time (first visit) has to wait for
  // the worker to claim it, or the first chunk requests bypass the cache.
  if (!navigator.serviceWorker.controller) {
    await new Promise((resolve) => {
      navigator.serviceWorker.addEventListener('controllerchange', resolve,
                                               { once: true });
    });
  }
  return registration;
}

export async function loadManifest(baseUrl) {
  // Absolutise first: URL() rejects a RELATIVE base ("/chunked/1A543a/") with
  // a bare "Invalid base URL", which reads like a bad manifest rather than a
  // bad call.
  baseUrl = new URL(baseUrl, location.href).href;
  const url = new URL('chunk-manifest.json', baseUrl).href;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`chunk manifest ${url}: HTTP ${response.status}`);
  }
  const manifest = await response.json();
  if (manifest.format !== 'ipod-nand-chunks-v1') {
    throw new Error(`unexpected chunk format ${manifest.format}`);
  }
  manifest.baseUrl = baseUrl;
  return manifest;
}

/* Absolute URL of one chunk, the same string the emulator builds in C. */
export function chunkUrl(manifest, index) {
  return new URL(manifest.base + manifest.hashes[index], manifest.baseUrl).href;
}

/*
 * Write the parts of a chunked NAND the emulator needs on disk. The bank
 * directories are NOT optional even though nothing here is writable: the
 * model opens <nand>/bank<N>/<page>_new.page on the first guest write and
 * hw_error()s -- killing the emulator -- if the directory is missing.
 */
export async function stageChunkedNand(FS, manifest, dir = '/fw/nand') {
  const parts = ['nand.pack.idx', 'chunk-hashes.bin', 'chunk-config.txt'];
  let staged = 0;

  try {
    FS.mkdir(dir);
  } catch (e) { /* already there */ }
  for (let bank = 0; bank < 8; bank++) {
    try {
      FS.mkdir(`${dir}/bank${bank}`);
    } catch (e) { /* already there */ }
  }

  for (const name of parts) {
    const response = await fetch(new URL(name, manifest.baseUrl).href);
    if (!response.ok) {
      throw new Error(`${name}: HTTP ${response.status}`);
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    FS.writeFile(`${dir}/${name}`, bytes);
    staged += bytes.length;
  }
  return staged;
}

/*
 * Start the worker that actually fetches chunks, and point it at the
 * emulator's mailbox.
 *
 * THE PAGE HAS TO OWN THIS WORKER. The emulator's own thread cannot create it:
 * a nested dedicated worker is serviced through its parent's context, and the
 * parent here spends its time blocked in Atomics.wait, so its child's fetch
 * never completes (measured: 10 s timeout nested, 5 ms page-owned --
 * web/bench-b/worker-selftest.html). Nor can the emulator fetch on its own
 * thread: Chrome refuses a synchronous XHR from a module worker, which is what
 * -sEXPORT_ES6 makes Emscripten's pthreads.
 *
 * Call after the runtime is initialised: it needs the exported
 * `_it_nand_chunk_mailbox_addr`, and the module's heap buffer, which is the
 * SharedArrayBuffer both sides write into.
 */
export function startChunkFetcher(Module, {
  script = '/chunk-fetch-worker.js', onError,
} = {}) {
  const addr = Module._it_nand_chunk_mailbox_addr?.();
  if (!addr) {
    throw new Error('emulator exports no chunk mailbox: rebuild, or the ' +
                    'EMSCRIPTEN_KEEPALIVE export was dropped');
  }
  const buffer = Module.HEAPU8?.buffer;
  if (!buffer || buffer.constructor.name !== 'SharedArrayBuffer') {
    throw new Error('the wasm heap is not shared; the page is not ' +
                    'cross-origin isolated');
  }

  const worker = new Worker(script);
  worker.onmessage = (event) => {
    const message = event.data || {};
    if (message.type === 'error') {
      onError?.(message);
    }
  };
  return new Promise((resolve, reject) => {
    worker.onerror = (event) => reject(new Error(`${script}: ${event.message}`));
    const listener = (event) => {
      const message = event.data || {};
      if (message.type === 'watching') {
        worker.removeEventListener('message', listener);
        resolve(worker);
      } else if (message.type === 'ready' && !message.haveMemory) {
        reject(new Error('fetch worker got no memory'));
      }
    };
    worker.addEventListener('message', listener);
    worker.postMessage({ type: 'init', buffer, mailbox: addr });
  });
}

/*
 * Hand the boot chunk order to the service worker. Resolves when the prefetch
 * finishes; callers that want the boot to start immediately should not await
 * it -- a demand-faulted chunk is served correctly either way, just slower.
 */
export function prefetchBootSet(manifest, { concurrency = 6, limit = Infinity,
                                            onProgress } = {}) {
  const worker = navigator.serviceWorker.controller;
  if (!worker) {
    return Promise.reject(new Error('no controlling service worker'));
  }
  const chunks = manifest.prefetch.slice(0, limit);
  const urls = chunks.map((index) => chunkUrl(manifest, index));

  return new Promise((resolve) => {
    const listener = (event) => {
      const message = event.data || {};
      if (message.type === 'prefetch-progress') {
        onProgress?.(message);
      } else if (message.type === 'prefetch-done') {
        navigator.serviceWorker.removeEventListener('message', listener);
        resolve(message);
      }
    };
    navigator.serviceWorker.addEventListener('message', listener);
    worker.postMessage({ type: 'prefetch', urls, concurrency });
  });
}

/* Bytes the service worker has taken from the network since the last reset. */
export function chunkStats() {
  const worker = navigator.serviceWorker.controller;
  if (!worker) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    const listener = (event) => {
      if ((event.data || {}).type === 'stats') {
        navigator.serviceWorker.removeEventListener('message', listener);
        resolve(event.data.stats);
      }
    };
    navigator.serviceWorker.addEventListener('message', listener);
    worker.postMessage({ type: 'stats' });
  });
}

export function postToChunkWorker(type) {
  navigator.serviceWorker.controller?.postMessage({ type });
}
