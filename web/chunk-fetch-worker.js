/*
 * The NAND chunk fetcher: a CLASSIC worker, created by the PAGE, driven
 * entirely through shared memory.
 *
 * The emulator has to read a NAND chunk from inside QEMU's MMIO path, which
 * cannot await. Two obvious answers do not work, and both failures are quiet:
 *
 *   - a synchronous XMLHttpRequest on the emulator's own thread: Chrome
 *     refuses it ("NetworkError: Failed to execute 'send'") because
 *     -sEXPORT_ES6 makes Emscripten's pthread workers MODULE workers, where
 *     synchronous XHR is unsupported. emscripten_fetch's synchronous mode
 *     fails the same way, silently, since its backend is that same XHR;
 *   - having the emulator's thread create this worker itself: a NESTED
 *     dedicated worker is serviced through its parent's context, so a parent
 *     blocked in Atomics.wait never lets its child's fetch complete. Measured
 *     with web/bench-b/worker-selftest.html: nested times out at 10 s,
 *     page-owned answers in 5 ms.
 *
 * So the page creates this worker, and the emulator never sends it a message.
 * Everything goes through one mailbox in the wasm heap:
 *
 *   emulator thread (pthread)        this worker (page-owned)
 *   ---------------------------      ------------------------------------
 *   write url + destination
 *   state = PENDING
 *   request++ ; futex wake     -->   Atomics.waitAsync sees request change
 *   Atomics.wait(state)              fetch(url)
 *                                    write bytes into the wasm heap
 *                                    status = n ; state = DONE ; notify
 *   <-- wakes, reads the bytes
 *
 * The emulator still never awaits -- it blocks -- so the device model is
 * unchanged, and nothing is copied through postMessage: the wasm memory is
 * SharedArrayBuffer-backed, so this worker writes the chunk exactly where the
 * emulator will read it.
 *
 * This file must stay a CLASSIC worker script (no import/export): a module
 * worker would reintroduce the restriction being worked around.
 */

'use strict';

/* Mailbox layout, in 32-bit words. Must match ITNandChunkMailbox in
 * hw/arm/ipod_touch_nand_chunks.c. */
const W_REQUEST = 0;    /* bumped by the emulator to signal a new request */
const W_STATE = 1;      /* 0 idle, 1 pending, 2 done */
const W_STATUS = 2;     /* bytes written, or a negative code */
const W_READY = 3;      /* set by this worker once it is watching */
const W_URL = 4;        /* address of the URL bytes */
const W_URL_LEN = 5;
const W_BUFFER = 6;     /* address to write the chunk to */
const W_CAPACITY = 7;

const STATE_PENDING = 1;
const STATE_DONE = 2;

/* Negative results the emulator maps back to a failed read. Distinct codes,
 * because this worker's console reaches nobody the page can read: the status
 * word IS the error channel. */
const ERR_OVERSIZE = -6;
const ERR_NETWORK = -20;
const ERR_INTERNAL = -31;

let buffer = null;      /* the wasm heap, a SharedArrayBuffer */
let mailbox = 0;        /* byte address of the mailbox */
const stats = { fetches: 0, bytes: 0, failures: 0 };

/* Views are rebuilt per use: a growable wasm memory detaches its old buffer,
 * and a stale view would write into nothing while reporting success. */
function bytes() {
  return new Uint8Array(buffer);
}

function words() {
  return new Int32Array(buffer);
}

function readString(pointer, length) {
  // .slice(), not .subarray(): TextDecoder REFUSES a view onto a
  // SharedArrayBuffer ("The provided ArrayBufferView value must not be
  // shared"). slice() copies -- 60 bytes of URL, not the chunk.
  return new TextDecoder().decode(bytes().slice(pointer, pointer + length));
}

function finish(status) {
  const view = words();
  const index = mailbox >> 2;
  // Status first, then state: the emulator wakes on state and reads status,
  // so the reverse order would let it read a stale status.
  Atomics.store(view, index + W_STATUS, status);
  Atomics.store(view, index + W_STATE, STATE_DONE);
  Atomics.notify(view, index + W_STATE);
}

async function serve() {
  const view = words();
  const index = mailbox >> 2;
  const destination = Atomics.load(view, index + W_BUFFER) >>> 0;
  const capacity = Atomics.load(view, index + W_CAPACITY) >>> 0;
  let url = '<unread>';

  try {
    url = readString(Atomics.load(view, index + W_URL) >>> 0,
                     Atomics.load(view, index + W_URL_LEN));
    const response = await fetch(url, { credentials: 'same-origin' });
    if (!response.ok) {
      stats.failures++;
      finish(-(1000 + response.status));
      return;
    }
    const body = new Uint8Array(await response.arrayBuffer());
    if (body.length > capacity) {
      stats.failures++;
      finish(ERR_OVERSIZE);
      return;
    }
    bytes().set(body, destination);
    stats.fetches++;
    stats.bytes += body.length;
    finish(body.length);
  } catch (error) {
    stats.failures++;
    self.postMessage({ type: 'error', url, message: String(error) });
    // Everything from decoding the URL to writing the bytes is inside the try:
    // a request that never calls finish() leaves the emulator blocked until
    // its timeout, which reads as a hang rather than as a failed fetch.
    finish(error instanceof TypeError ? ERR_NETWORK : ERR_INTERNAL);
  }
}

/*
 * Watch the request counter. Atomics.waitAsync is what lets a worker "block"
 * on a futex without blocking: it hands back a promise, so this worker stays
 * free to run fetch() and its callbacks.
 */
async function watch() {
  const index = mailbox >> 2;
  let seen = Atomics.load(words(), index + W_REQUEST);

  Atomics.store(words(), index + W_READY, 1);
  self.postMessage({ type: 'watching', mailbox });

  for (;;) {
    const result = Atomics.waitAsync(words(), index + W_REQUEST, seen);
    if (result.async) {
      await result.value;
    }
    const current = Atomics.load(words(), index + W_REQUEST);
    if (current === seen) {
      continue;                      /* spurious wake */
    }
    seen = current;
    if (Atomics.load(words(), index + W_STATE) === STATE_PENDING) {
      await serve();
    }
  }
}

self.onmessage = (event) => {
  const message = event.data || {};
  switch (message.type) {
    case 'init':
      // Either a WebAssembly.Memory or its buffer, since which one the page
      // can reach depends on what Emscripten exports.
      buffer = message.buffer ||
               (message.memory && message.memory.buffer) || null;
      mailbox = message.mailbox | 0;
      if (!buffer) {
        self.postMessage({ type: 'ready', haveMemory: false });
        break;
      }
      self.postMessage({
        type: 'ready',
        haveMemory: true,
        shared: buffer.constructor.name === 'SharedArrayBuffer',
        mailbox,
      });
      if (mailbox) {
        watch();
      }
      break;

    case 'fetch':
      // One-shot form, used by web/bench-b/worker-selftest.html: the mailbox
      // fields arrive in the message rather than in memory.
      if (!buffer) {
        break;
      }
      mailbox = message.statePointer | 0;
      (async () => {
        const view = words();
        const index = mailbox >> 2;
        Atomics.store(view, index + W_URL, message.urlPointer);
        Atomics.store(view, index + W_URL_LEN, message.urlLength);
        Atomics.store(view, index + W_BUFFER, message.bufferPointer);
        Atomics.store(view, index + W_CAPACITY, message.capacity);
        await serve();
      })();
      break;

    case 'stats':
      self.postMessage({ type: 'stats', stats });
      break;

    default:
      break;
  }
};
