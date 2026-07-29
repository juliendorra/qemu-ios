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

/* Emscripten hands pointers to EM_JS as BigInt under -sMEMORY64=1; the same
 * decode the WebAssembly TCG backend uses (tcg/wasm64.c). */
#define DEC_PTR(p) bigintToI53Checked(p)

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
 * Fetch url into buf; returns the byte count, or -1.
 *
 * responseType on a synchronous XHR throws InvalidAccessError on a Window but
 * is allowed on a worker, which is where this runs. The binary-string fallback
 * exists so that a run on the main thread degrades instead of dying.
 */
EM_JS(int, it_nand_fetch_chunk_js, (const char *url, void *buf, int cap), {
    const target = UTF8ToString(DEC_PTR(url));
    const dst = DEC_PTR(buf);
    const xhr = new XMLHttpRequest();
    xhr.open('GET', target, false);
    let binaryString = false;
    try {
        xhr.responseType = 'arraybuffer';
    } catch (e) {
        xhr.overrideMimeType('text/plain; charset=x-user-defined');
        binaryString = true;
    }
    try {
        xhr.send(null);
    } catch (e) {
        return -1;
    }
    if (xhr.status !== 200 && xhr.status !== 0) {
        return -1;
    }
    if (binaryString) {
        const text = xhr.responseText;
        if (text.length > cap) {
            return -1;
        }
        for (let i = 0; i < text.length; i++) {
            HEAPU8[dst + i] = text.charCodeAt(i) & 0xff;
        }
        return text.length;
    }
    const bytes = new Uint8Array(xhr.response);
    if (bytes.length > cap) {
        return -1;
    }
    HEAPU8.set(bytes, dst);
    return bytes.length;
});

static const uint8_t *chunk_fetch(void *opaque, uint32_t chunk,
                                  uint32_t *n_slots)
{
    ChunkSlot *victim = NULL;
    char url[640];
    char hex[65];
    uint32_t expected_slots;
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
    got = it_nand_fetch_chunk_js(url, victim->data, (int)capacity);
    if (got <= 0 || (size_t)got % chunks.stride != 0) {
        fprintf(stderr, "[NANDCHUNK] fetch failed: chunk %u (%s) -> %d\n",
                chunk, url, got);
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
