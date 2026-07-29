/*
 * Browser chunk source for the IPODNAND pack.
 *
 * Native maps the whole 215 MiB pack; a browser cannot, and should not want
 * to -- a cold boot of iPhone OS 1.0 touches 19.1% of it. This serves pack
 * records out of Brotli-compressed, content-addressed chunks produced by
 * scripts/wasm/chunk-pack.py, fetched on demand.
 *
 * THE FETCH IS SYNCHRONOUS, and that is the whole design. QEMU's MMIO path
 * runs inside a device read and cannot await, so an asynchronous loader would
 * mean restructuring the NAND model (or the CPU loop) around promises.
 * Instead the emulator does a synchronous XMLHttpRequest -- legal on a worker
 * thread, which is where -sPROXY_TO_PTHREAD puts the emulator -- and the
 * service worker answers it from Cache Storage. The emulator never awaits;
 * the caching is somebody else's problem.
 *
 * Chunks are stored already-compressed and served with `Content-Encoding: br`,
 * so the browser decompresses them in the network stack and no Brotli decoder
 * is linked into the emulator.
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 2 of the License, or (at your option)
 * any later version.
 */

#include "qemu/osdep.h"
#include "hw/arm/ipod_touch_nand.h"
#include "hw/arm/ipod_touch_nand_pack.h"

#ifdef EMSCRIPTEN

#include <emscripten.h>
#include <emscripten/fetch.h>
#include <emscripten/threading.h>

/*
 * Resident chunks. 64 x 130,944 B is ~8 MiB for the default 62-page chunk --
 * cheap next to the 128 MiB guest, and enough that sequential reads inside one
 * chunk never refetch. Eviction is least-recently-used by a monotonic clock:
 * unlike the JIT's instance ring there is no browser limit being worked
 * around here, so the simple policy is the right one.
 */
#define CHUNK_CACHE_SLOTS 64

typedef struct {
    uint32_t chunk;
    uint32_t n_slots;
    uint64_t used;               /* 0 = empty */
    uint8_t *data;
} ChunkSlot;

static struct {
    char base[512];              /* URL prefix, e.g. "/chunked/1A543a/chunks/" */
    /* The page creates the fetch worker, so this is only advisory -- it
     * travels in chunk-config.txt so an asset set can name its own fetcher,
     * and web/src/emulator/chunk-loader.js is what reads it. */
    char fetcher[256];
    uint32_t pages_per_chunk;
    uint32_t stride;
    uint32_t chunk_count;
    uint32_t entry_count;
    GMappedFile *hashes_file;    /* 32 bytes per chunk, in chunk order */
    const uint8_t *hashes;
    ChunkSlot slots[CHUNK_CACHE_SLOTS];
    uint64_t clock;
    uint64_t fetches;
    uint64_t hits;
    uint64_t bytes;
} chunks;

/*
 * Transport 1: block on a futex while a PAGE-OWNED classic worker fetches.
 *
 * The emulator writes a request into a mailbox in its own heap and sleeps on
 * it; web/chunk-fetch-worker.js -- created by the page, watching the same
 * mailbox with Atomics.waitAsync -- fetches the chunk, writes it straight into
 * the heap, and wakes the emulator. Nothing is copied through postMessage,
 * and the emulator never awaits.
 *
 * Two arrangements were measured and rejected first, both failing quietly:
 *
 *   - a synchronous XHR on this thread: Chrome refuses it, because
 *     -sEXPORT_ES6 makes Emscripten's pthread workers MODULE workers;
 *   - this thread creating the fetch worker itself: a NESTED worker is
 *     serviced through its parent's context, so a parent blocked in
 *     Atomics.wait stalls its own fetcher. web/bench-b/worker-selftest.html
 *     reproduces both in a second -- nested times out at 10 s, page-owned
 *     answers in 5 ms -- which is why that file exists.
 */
#define EM_JS_PRE(ret, name, args, body...) EM_JS(ret, name, args, body)
#define DEC_PTR(p) bigintToI53Checked(p)

/*
 * Layout is shared with web/chunk-fetch-worker.js, as a plain array of i32 so
 * both sides can address it by word index with no struct-layout guesswork.
 */
typedef struct {
    int32_t request;             /* bumped to signal a request */
    int32_t state;               /* 0 idle, 1 pending, 2 done */
    int32_t status;              /* bytes written, or a negative code */
    int32_t ready;               /* set by the worker once it is watching */
    int32_t url;                 /* address of the URL bytes */
    int32_t url_len;
    int32_t buffer;              /* address to write the chunk to */
    int32_t capacity;
    char url_bytes[512];
} ITNandChunkMailbox;

#define CHUNK_STATE_IDLE    0
#define CHUNK_STATE_PENDING 1
#define CHUNK_STATE_DONE    2

#define CHUNK_ERR_NO_WORKER (-19)
#define CHUNK_ERR_TIMEOUT   (-21)

/* A chunk fetch that takes longer than this is not going to arrive. Hanging
 * the emulator for ever is worse than failing the read and saying so. */
#define CHUNK_FETCH_TIMEOUT_MS 30000

/* One mailbox: NAND reads are serialised by the device model, and a second
 * in-flight request would need a second watcher anyway. */
static ITNandChunkMailbox chunk_mailbox;

/*
 * The page needs this address to point the fetch worker at the mailbox, and an
 * EXPORTED FUNCTION is the only way to hand it over: an EM_JS body runs on
 * whichever thread called it and sees that thread's `Module`, which under
 * -sPROXY_TO_PTHREAD is never the page's. Same pattern as ui/wasm.c's
 * wasm_display_info_addr().
 */
EMSCRIPTEN_KEEPALIVE uint32_t it_nand_chunk_mailbox_addr(void)
{
    return (uint32_t)(uintptr_t)&chunk_mailbox;
}

static int it_nand_fetch_chunk_worker(const char *url, void *buf, size_t cap,
                                      char *err, size_t errcap)
{
    ITNandChunkMailbox *mb = &chunk_mailbox;
    size_t url_len = strlen(url);
    double deadline;
    int got;

    if (!qatomic_read(&mb->ready)) {
        g_strlcpy(err, "no fetch worker is watching the mailbox", errcap);
        return CHUNK_ERR_NO_WORKER;
    }
    /*
     * Atomics.wait is illegal on the browser's main thread. Under
     * -sPROXY_TO_PTHREAD the emulator never runs there, but a build that
     * changed would otherwise throw instead of falling back.
     */
    if (emscripten_is_main_browser_thread()) {
        g_strlcpy(err, "cannot block on the main browser thread", errcap);
        return CHUNK_ERR_NO_WORKER;
    }
    if (url_len >= sizeof(mb->url_bytes)) {
        g_strlcpy(err, "chunk URL is too long for the mailbox", errcap);
        return CHUNK_ERR_NO_WORKER;
    }

    memcpy(mb->url_bytes, url, url_len);
    qatomic_set(&mb->url, (int32_t)(uintptr_t)mb->url_bytes);
    qatomic_set(&mb->url_len, (int32_t)url_len);
    qatomic_set(&mb->buffer, (int32_t)(uintptr_t)buf);
    qatomic_set(&mb->capacity, (int32_t)cap);
    qatomic_set(&mb->status, 0);
    qatomic_set(&mb->state, CHUNK_STATE_PENDING);

    /* Publish the request last, and wake the watcher on it: everything above
     * has to be visible before the worker looks. */
    qatomic_inc(&mb->request);
    emscripten_futex_wake(&mb->request, 1);

    deadline = emscripten_get_now() + CHUNK_FETCH_TIMEOUT_MS;
    while (qatomic_read(&mb->state) == CHUNK_STATE_PENDING) {
        /* Woken by the worker's Atomics.notify; the slice is a safety net, not
         * a poll interval. */
        emscripten_futex_wait(&mb->state, CHUNK_STATE_PENDING, 1000);
        if (qatomic_read(&mb->state) != CHUNK_STATE_PENDING) {
            break;
        }
        if (emscripten_get_now() > deadline) {
            g_snprintf(err, errcap, "no answer in %d ms",
                       CHUNK_FETCH_TIMEOUT_MS);
            qatomic_set(&mb->state, CHUNK_STATE_IDLE);
            return CHUNK_ERR_TIMEOUT;
        }
    }
    got = qatomic_read(&mb->status);
    qatomic_set(&mb->state, CHUNK_STATE_IDLE);
    if (got <= 0) {
        g_snprintf(err, errcap, "worker reported %d", got);
    }
    return got;
}

/*
 * The synchronous XMLHttpRequest path.
 *
 * This is the design Infinite Mac uses and it works in some Chromium builds
 * (measured: the in-app browser here served 320 fetches and 11,095 LRU hits
 * through it). Chrome 149 REFUSES it from an Emscripten pthread --
 *
 *     NetworkError: Failed to execute 'send' on 'XMLHttpRequest':
 *     Failed to load '<url>'
 *
 * -- and the request never reaches the server. The cause is -sEXPORT_ES6,
 * which makes Emscripten's pthread workers MODULE workers, where Chrome does
 * not support synchronous XHR. Ruled out by experiment: not the
 * Content-Encoding (reproduced with the header removed) and not the service
 * worker (reproduced with it bypassed).
 *
 * Kept only as a fallback for an engine where the page has not started the
 * fetch worker.
 */
EM_JS_PRE(int, it_nand_fetch_chunk_xhr, (const char *url, void *buf,
                                         int cap, char *err, int errcap), {
    const target = UTF8ToString(DEC_PTR(url));
    const dst = DEC_PTR(buf);
    const report = (e) => stringToUTF8(String(e), DEC_PTR(err), errcap);

    if (typeof XMLHttpRequest === 'undefined') {
        return -2;
    }
    const xhr = new XMLHttpRequest();
    try {
        xhr.open('GET', target, false);
        xhr.responseType = 'arraybuffer';
        xhr.send(null);
    } catch (e) {
        report(e);
        return -4;
    }
    if (xhr.status !== 200 && xhr.status !== 0) {
        return -(1000 + xhr.status);
    }
    const bytes = new Uint8Array(xhr.response);
    if (bytes.length > cap) {
        return -6;
    }
    HEAPU8.set(bytes, dst);
    return bytes.length;
});

/*
 * Fetch url into buf; returns the byte count, or a negative code.
 *
 * emscripten_fetch with EMSCRIPTEN_FETCH_SYNCHRONOUS, NOT a synchronous
 * XMLHttpRequest. The XHR is the obvious way to keep the emulator's reads
 * synchronous and it is what Infinite Mac uses, but Chrome 149 REFUSES it from
 * an Emscripten pthread:
 *
 *     NetworkError: Failed to execute 'send' on 'XMLHttpRequest':
 *     Failed to load '<url>'
 *
 * and the request never reaches the server at all. The cause is
 * -sEXPORT_ES6, which makes Emscripten's pthread workers MODULE workers, where
 * Chrome does not support synchronous XHR. It is not the Content-Encoding
 * (reproduced with the header removed) and not the service worker (reproduced
 * with the worker bypassed).
 *
 * emscripten_fetch is the supported path: a synchronous fetch is legal on any
 * thread except the browser main thread, which is exactly our case, and it
 * needs -sFETCH at link time (scripts/wasm/build-qemu.sh).
 */
static int it_nand_fetch_chunk_em(const char *url, void *buf, size_t cap,
                                  char *err, size_t errcap)
{
    emscripten_fetch_attr_t attr;
    emscripten_fetch_t *fetch;
    int result;

    emscripten_fetch_attr_init(&attr);
    strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY |
                      EMSCRIPTEN_FETCH_SYNCHRONOUS |
                      EMSCRIPTEN_FETCH_REPLACE;

    fetch = emscripten_fetch(&attr, url);
    if (fetch == NULL) {
        g_strlcpy(err, "emscripten_fetch returned NULL", errcap);
        return -3;
    }
    if (fetch->status != 200 && fetch->status != 0) {
        g_snprintf(err, errcap, "HTTP %u", fetch->status);
        result = -(1000 + (int)fetch->status);
    } else if ((size_t)fetch->numBytes > cap) {
        g_snprintf(err, errcap, "%llu bytes, slot holds %zu",
                   (unsigned long long)fetch->numBytes, cap);
        result = -6;
    } else {
        memcpy(buf, fetch->data, fetch->numBytes);
        result = (int)fetch->numBytes;
    }
    emscripten_fetch_close(fetch);
    return result;
}

/*
 * Pick a transport once, then stay on it. All three are synchronous from the
 * emulator's point of view; probing on every chunk would pay the failing
 * ones' cost for the whole run.
 *
 *   1. the futex + page-owned-worker path -- works in Chrome, and is the design
 *   2. a synchronous XHR -- fewer moving parts, but Chrome refuses it from a
 *      module worker (which -sEXPORT_ES6 makes these)
 *   3. emscripten_fetch(SYNCHRONOUS) -- same XHR underneath, so it fails the
 *      same way, silently; kept because it costs nothing and a future
 *      Emscripten may back it differently
 */
static int it_nand_fetch_chunk(const char *url, void *buf, size_t cap,
                               char *err, size_t errcap)
{
    static int transport;          /* 0 unknown, 1 worker, 2 xhr, 3 fetch */
    int got;

    if (transport == 0 || transport == 1) {
        got = it_nand_fetch_chunk_worker(url, buf, cap, err, errcap);
        if (got > 0) {
            if (transport == 0) {
                fprintf(stderr, "[NANDCHUNK] transport: futex + the page's "
                        "fetch worker\n");
            }
            transport = 1;
            return got;
        }
        if (transport == 1) {
            return got;            /* it worked before: this is a real error */
        }
    }
    if (transport == 0 || transport == 2) {
        got = it_nand_fetch_chunk_xhr(url, buf, (int)cap, err, (int)errcap);
        if (got > 0) {
            if (transport == 0) {
                fprintf(stderr, "[NANDCHUNK] transport: synchronous XHR\n");
            }
            transport = 2;
            return got;
        }
        if (transport == 2) {
            return got;
        }
    }
    got = it_nand_fetch_chunk_em(url, buf, cap, err, errcap);
    if (got > 0) {
        if (transport == 0) {
            fprintf(stderr, "[NANDCHUNK] transport: emscripten_fetch\n");
        }
        transport = 3;
    }
    return got;
}

static const uint8_t *chunk_fetch(void *opaque, uint32_t chunk,
                                  uint32_t *n_slots)
{
    ChunkSlot *victim = NULL;
    char url[640];
    char hex[65];
    uint32_t expected_slots;
    char error[256];
    size_t capacity = (size_t)chunks.pages_per_chunk * chunks.stride;
    int got;

    if (chunk >= chunks.chunk_count) {
        return NULL;
    }

    for (int i = 0; i < CHUNK_CACHE_SLOTS; i++) {
        ChunkSlot *slot = &chunks.slots[i];

        if (slot->used != 0 && slot->chunk == chunk) {
            slot->used = ++chunks.clock;
            chunks.hits++;
            *n_slots = slot->n_slots;
            return slot->data;
        }
        if (victim == NULL || slot->used < victim->used) {
            victim = slot;
        }
    }

    for (int i = 0; i < 32; i++) {
        g_snprintf(hex + i * 2, 3, "%02x", chunks.hashes[chunk * 32 + i]);
    }
    g_snprintf(url, sizeof(url), "%s%s", chunks.base, hex);

    if (victim->data == NULL) {
        victim->data = g_malloc(capacity);
    }
    error[0] = '\0';
    got = it_nand_fetch_chunk(url, victim->data, capacity, error,
                              sizeof(error));
    if (got <= 0 || (size_t)got % chunks.stride != 0) {
        fprintf(stderr, "[NANDCHUNK] fetch failed: chunk %u (%s) -> %d %s "
                "(-3 no fetch, -6 oversize, -10xx HTTP xx)\n",
                chunk, url, got, error);
        victim->used = 0;
        return NULL;
    }

    /* The last chunk is short; every other one must be full, and a chunk that
     * is neither is a corrupt or truncated asset rather than a cache miss. */
    expected_slots = MIN(chunks.pages_per_chunk,
                         chunks.entry_count - chunk * chunks.pages_per_chunk);
    if ((uint32_t)got / chunks.stride != expected_slots) {
        fprintf(stderr, "[NANDCHUNK] chunk %u has %u records, expected %u\n",
                chunk, (uint32_t)got / chunks.stride, expected_slots);
        victim->used = 0;
        return NULL;
    }

    victim->chunk = chunk;
    victim->n_slots = expected_slots;
    victim->used = ++chunks.clock;
    chunks.fetches++;
    chunks.bytes += got;
    if (chunks.fetches % 64 == 0) {
        fprintf(stderr, "[NANDCHUNK] fetched=%llu hits=%llu resident<=%d "
                "bytes=%.1f MiB\n", (unsigned long long)chunks.fetches,
                (unsigned long long)chunks.hits, CHUNK_CACHE_SLOTS,
                chunks.bytes / 1048576.0);
    }
    *n_slots = expected_slots;
    return victim->data;
}

static bool read_config(const char *path)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;

    if (!g_file_get_contents(path, &text, NULL, NULL)) {
        return false;
    }
    lines = g_strsplit(text, "\n", -1);
    for (char **line = lines; *line != NULL; line++) {
        char *eq = strchr(*line, '=');
        const char *value;

        if (eq == NULL) {
            continue;
        }
        *eq = '\0';
        value = eq + 1;
        if (g_str_equal(*line, "pagesPerChunk")) {
            chunks.pages_per_chunk = strtoul(value, NULL, 10);
        } else if (g_str_equal(*line, "chunks")) {
            chunks.chunk_count = strtoul(value, NULL, 10);
        } else if (g_str_equal(*line, "entries")) {
            chunks.entry_count = strtoul(value, NULL, 10);
        } else if (g_str_equal(*line, "stride")) {
            chunks.stride = strtoul(value, NULL, 10);
        } else if (g_str_equal(*line, "base")) {
            g_strlcpy(chunks.base, value, sizeof(chunks.base));
        } else if (g_str_equal(*line, "fetcher")) {
            g_strlcpy(chunks.fetcher, value, sizeof(chunks.fetcher));
        }
    }
    if (chunks.fetcher[0] == '\0') {
        g_strlcpy(chunks.fetcher, "/chunk-fetch-worker.js",
                  sizeof(chunks.fetcher));
    }
    return chunks.pages_per_chunk > 0 && chunks.chunk_count > 0 &&
           chunks.entry_count > 0 && chunks.stride > 0 &&
           chunks.base[0] != '\0';
}

bool it_nand_chunks_init(const char *nand_path)
{
    g_autofree char *config = g_strdup_printf("%s/chunk-config.txt", nand_path);
    g_autofree char *hashes = g_strdup_printf("%s/chunk-hashes.bin", nand_path);
    GError *error = NULL;

    if (chunks.hashes != NULL) {
        return true;
    }
    if (!read_config(config)) {
        return false;
    }
    chunks.hashes_file = g_mapped_file_new(hashes, false, &error);
    if (chunks.hashes_file == NULL) {
        fprintf(stderr, "[NANDCHUNK] %s: %s\n", hashes,
                error ? error->message : "unreadable");
        g_clear_error(&error);
        return false;
    }
    if (g_mapped_file_get_length(chunks.hashes_file) !=
        (gsize)chunks.chunk_count * 32) {
        fprintf(stderr, "[NANDCHUNK] %s does not hold %u hashes\n", hashes,
                chunks.chunk_count);
        return false;
    }
    chunks.hashes =
        (const uint8_t *)g_mapped_file_get_contents(chunks.hashes_file);
    fprintf(stderr, "[NANDCHUNK] %u chunks of %u pages from %s\n",
            chunks.chunk_count, chunks.pages_per_chunk, chunks.base);
    it_nand_set_chunk_source(chunks.pages_per_chunk, chunk_fetch, &chunks);
    return true;
}

#endif /* EMSCRIPTEN */
