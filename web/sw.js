/*
 * Service worker for chunked NAND delivery.
 *
 * The emulator reads the NAND from inside QEMU's MMIO path, which cannot
 * await, so it issues a SYNCHRONOUS XMLHttpRequest from its worker thread
 * (hw/arm/ipod_touch_nand_chunks.c). This worker is what makes that cheap:
 * it intercepts those requests and answers them from Cache Storage, so the
 * emulator's synchronous read is a memory copy on every boot after the first.
 *
 * The emulator never learns any of this exists. That is the point -- it is
 * what keeps QEMU's device model unmodified apart from the pack seam.
 *
 * Two more jobs:
 *   - prefetch, driven by the boot order recorded from a real boot, so the
 *     guest is not serialized on demand-faults;
 *   - accounting, so "a cold boot downloads 18.6 MiB, a warm boot downloads
 *     nothing" is a measurement rather than a claim.
 */

const CACHE = 'ipod-nand-chunks-v1';

/* Anything under a directory named `chunks/` is a content-addressed chunk:
 * immutable, so a cache hit never needs revalidation. */
const CHUNK_PATH = /\/chunks\/[0-9a-f]{64}$/;

let stats = { hits: 0, misses: 0, bytesFromNetwork: 0, bytesServed: 0 };

self.addEventListener('install', (event) => {
  // Take over immediately: the page registers the worker and then boots, and
  // waiting for a navigation would mean the first boot bypasses the cache.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

async function fromCacheOrNetwork(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) {
    stats.hits++;
    stats.bytesServed += Number(cached.headers.get('X-Chunk-Bytes') || 0);
    return cached;
  }

  const response = await fetch(request);
  if (!response.ok) {
    stats.misses++;
    return response;
  }
  // Read the body out and re-wrap it. The chunk is stored Brotli-compressed
  // and served with Content-Encoding: br, so the bytes here are already
  // DECOMPRESSED -- carrying the original encoding headers into the cache
  // would describe the body wrongly.
  const bytes = await response.arrayBuffer();
  const encoded = Number(response.headers.get('X-Encoded-Length') ||
                         response.headers.get('Content-Length') || 0);
  stats.misses++;
  stats.bytesFromNetwork += encoded || bytes.byteLength;
  stats.bytesServed += bytes.byteLength;

  const stored = new Response(bytes, {
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Chunk-Bytes': String(bytes.byteLength),
      // Same-origin under COEP: require-corp needs this on every subresource.
      'Cross-Origin-Resource-Policy': 'same-origin',
    },
  });
  await cache.put(request, stored.clone());
  return stored;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || !CHUNK_PATH.test(url.pathname)) {
    return;
  }
  event.respondWith(fromCacheOrNetwork(event.request));
});

/*
 * Prefetch in the recorded boot order, a few at a time. Ordered, not
 * scattershot: the emulator asks for these chunks in roughly this sequence,
 * so fetching them in order means the demand-faults it does take are the ones
 * furthest ahead.
 */
async function prefetch(urls, concurrency, client) {
  let next = 0;
  let done = 0;
  const started = Date.now();

  async function worker() {
    while (next < urls.length) {
      const index = next++;
      try {
        await fromCacheOrNetwork(new Request(urls[index]));
      } catch (e) {
        /* A prefetch failure is not fatal: the emulator will demand-fault it
         * later and that request reports its own error. */
      }
      done++;
      if (done % 25 === 0 || done === urls.length) {
        client?.postMessage({ type: 'prefetch-progress', done,
                              total: urls.length, stats });
      }
    }
  }

  await Promise.all(Array.from({ length: concurrency }, worker));
  client?.postMessage({
    type: 'prefetch-done', total: urls.length, stats,
    seconds: (Date.now() - started) / 1000,
  });
}

self.addEventListener('message', (event) => {
  const message = event.data || {};
  if (message.type === 'prefetch') {
    event.waitUntil(prefetch(message.urls, message.concurrency || 6,
                             event.source));
  } else if (message.type === 'stats') {
    event.source?.postMessage({ type: 'stats', stats });
  } else if (message.type === 'reset-stats') {
    stats = { hits: 0, misses: 0, bytesFromNetwork: 0, bytesServed: 0 };
  } else if (message.type === 'clear-cache') {
    event.waitUntil(caches.delete(CACHE).then(() =>
      event.source?.postMessage({ type: 'cache-cleared' })));
  }
});
