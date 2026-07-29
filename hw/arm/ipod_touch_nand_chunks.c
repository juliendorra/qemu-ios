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
 * Kept as the first choice because where it works it is the cheapest path.
 */
#define EM_JS_PRE(ret, name, args, body...) EM_JS(ret, name, args, body)
#define DEC_PTR(p) bigintToI53Checked(p)

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
 * Try the XHR, then emscripten_fetch. Whichever works first wins for the rest
 * of the run: both are synchronous, and probing on every chunk would pay the
 * failing one's cost forever.
 *
 * WARNING (2026-07-29): on Chrome 149 BOTH fail -- the XHR with a NetworkError
 * and emscripten_fetch with a silent zero-byte result, because its backend is
 * that same XHR. A browser where neither works needs the Atomics.wait design:
 * the emulator thread blocks on a futex while a CLASSIC (non-module) worker
 * does an async fetch and writes into the shared heap. That is the known next
 * step, and it is why this function keeps the failure codes.
 */
static int it_nand_fetch_chunk(const char *url, void *buf, size_t cap,
                               char *err, size_t errcap)
{
    static int transport;          /* 0 unknown, 1 xhr, 2 emscripten_fetch */
    int got;

    if (transport != 2) {
        got = it_nand_fetch_chunk_xhr(url, buf, (int)cap, err, (int)errcap);
        if (got > 0) {
            transport = 1;
            return got;
        }
        if (transport == 1) {
            return got;            /* it worked before; this is a real error */
        }
    }
    got = it_nand_fetch_chunk_em(url, buf, cap, err, errcap);
    if (got > 0) {
        transport = 2;
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
        }
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
