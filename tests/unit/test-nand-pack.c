/*
 * The IPODNAND pack-access seam: prove the two sources agree.
 *
 * Native memcpy's records out of a mapped 215 MiB pack; the browser serves the
 * same records from compressed chunks fetched over the network. That is the
 * one place where the browser port can silently return WRONG bytes rather than
 * fail, and a wrong page in a filesystem image does not announce itself -- it
 * shows up much later as a corrupt boot.
 *
 * So: run both sources over the same golden fixture
 * (tests/data/nand-pack, regenerate with scripts/wasm/make-pack-fixture.py)
 * and require byte-for-byte agreement, including on the absent pages.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "qapi/error.h"
#include "qobject/qjson.h"
#include "qobject/qdict.h"
#include "qobject/qlist.h"
#include "qobject/qnum.h"
#include "qobject/qstring.h"
#include "hw/arm/ipod_touch_nand_pack.h"

typedef struct {
    GMappedFile *pack_file;      /* the whole fixture pack */
    GMappedFile *index_file;     /* header + index only */
    const uint8_t *pack;
    gsize pack_len;
    QDict *golden;
    uint32_t stride;
    uint32_t pages_per_chunk;
    uint32_t entry_count;
} Fixture;

/*
 * The chunk source under test. It copies the chunk into its own buffer, the
 * way a decompressed chunk arrives in the browser: if the seam ever handed
 * back a pointer into the mapped file by accident, the comparison below would
 * still pass, and this makes that impossible.
 *
 * `resident_limit` models a cache miss -- chunks at or above it are not
 * available -- because "the chunk is not here yet" must be distinguishable
 * from "the page does not exist".
 */
typedef struct {
    const uint8_t *payload;
    uint32_t entry_count;
    uint32_t stride;
    uint32_t pages_per_chunk;
    uint32_t resident_limit;
    uint8_t *buffer;
    unsigned fetches;
} ChunkCache;

static const uint8_t *chunk_fetch(void *opaque, uint32_t chunk,
                                  uint32_t *n_slots)
{
    ChunkCache *cache = opaque;
    uint32_t first = chunk * cache->pages_per_chunk;
    uint32_t slots;

    cache->fetches++;
    if (chunk >= cache->resident_limit || first >= cache->entry_count) {
        return NULL;
    }
    slots = MIN(cache->pages_per_chunk, cache->entry_count - first);
    memcpy(cache->buffer, cache->payload + (uint64_t)first * cache->stride,
           (size_t)slots * cache->stride);
    *n_slots = slots;
    return cache->buffer;
}

static char *fixture_path(const char *name)
{
    return g_test_build_filename(G_TEST_DIST, "..", "data", "nand-pack", name,
                                 NULL);
}

static void fixture_init(Fixture *f)
{
    g_autofree char *pack_path = fixture_path("tiny.pack");
    g_autofree char *index_path = fixture_path("tiny.pack.idx");
    g_autofree char *golden_path = fixture_path("golden.json");
    g_autofree char *golden_text = NULL;
    GError *gerror = NULL;
    QObject *parsed;

    f->pack_file = g_mapped_file_new(pack_path, false, &gerror);
    g_assert_no_error(gerror);
    f->index_file = g_mapped_file_new(index_path, false, &gerror);
    g_assert_no_error(gerror);
    f->pack = (const uint8_t *)g_mapped_file_get_contents(f->pack_file);
    f->pack_len = g_mapped_file_get_length(f->pack_file);

    g_file_get_contents(golden_path, &golden_text, NULL, &gerror);
    g_assert_no_error(gerror);
    parsed = qobject_from_json(golden_text, &error_abort);
    f->golden = qobject_to(QDict, parsed);
    g_assert_nonnull(f->golden);

    f->stride = qdict_get_int(f->golden, "stride");
    f->pages_per_chunk = qdict_get_int(f->golden, "pagesPerChunk");
    f->entry_count = qlist_size(qdict_get_qlist(f->golden, "entries"));
}

static void fixture_clear(Fixture *f)
{
    qobject_unref(f->golden);
    g_mapped_file_unref(f->index_file);
    g_mapped_file_unref(f->pack_file);
}

/* The mapped source alone: does it return the bytes the fixture pins? */
static void test_mapped_matches_golden(void)
{
    Fixture f;
    ITNandPack pack;
    const QListEntry *entry;

    fixture_init(&f);
    g_assert_true(it_nand_pack_open_mapped(&pack, f.pack, f.pack_len, f.stride,
                                           &error_abort));
    g_assert_cmpuint(pack.entry_count, ==, f.entry_count);

    QLIST_FOREACH_ENTRY(qdict_get_qlist(f.golden, "entries"), entry) {
        QDict *item = qobject_to(QDict, qlist_entry_obj(entry));
        uint32_t vpn = qdict_get_int(item, "vpn");
        const uint8_t *record = it_nand_pack_record(&pack, vpn);
        g_autofree char *digest = NULL;

        g_assert_nonnull(record);
        digest = g_compute_checksum_for_data(G_CHECKSUM_SHA256, record,
                                             f.stride);
        g_assert_cmpstr(digest, ==, qdict_get_str(item, "sha256"));
    }
    fixture_clear(&f);
}

/*
 * The point of the whole exercise: mapped and chunked must be
 * indistinguishable, on present pages and absent ones alike.
 */
static void test_chunked_matches_mapped(void)
{
    Fixture f;
    ITNandPack mapped, chunked;
    ChunkCache cache;
    const QListEntry *entry;

    fixture_init(&f);
    g_assert_true(it_nand_pack_open_mapped(&mapped, f.pack, f.pack_len,
                                           f.stride, &error_abort));

    cache = (ChunkCache) {
        .payload = f.pack + IT_NAND_PACK_HEADER_SIZE + f.entry_count * 4,
        .entry_count = f.entry_count,
        .stride = f.stride,
        .pages_per_chunk = f.pages_per_chunk,
        .resident_limit = UINT32_MAX,
        .buffer = g_malloc(f.pages_per_chunk * f.stride),
    };
    /* Opened from the INDEX file -- header plus index, no payload at all,
     * which is what a browser downloads up front. */
    g_assert_true(it_nand_pack_open_chunked(
        &chunked,
        (const uint8_t *)g_mapped_file_get_contents(f.index_file),
        g_mapped_file_get_length(f.index_file), f.stride, f.pages_per_chunk,
        chunk_fetch, &cache, &error_abort));

    QLIST_FOREACH_ENTRY(qdict_get_qlist(f.golden, "entries"), entry) {
        QDict *item = qobject_to(QDict, qlist_entry_obj(entry));
        uint32_t vpn = qdict_get_int(item, "vpn");
        const uint8_t *a = it_nand_pack_record(&mapped, vpn);
        const uint8_t *b = it_nand_pack_record(&chunked, vpn);

        g_assert_nonnull(a);
        g_assert_nonnull(b);
        g_assert_true(a != b);                 /* genuinely different storage */
        g_assert_cmpmem(a, f.stride, b, f.stride);
    }

    QLIST_FOREACH_ENTRY(qdict_get_qlist(f.golden, "absent"), entry) {
        uint32_t vpn = qnum_get_uint(qobject_to(QNum, qlist_entry_obj(entry)));

        g_assert_null(it_nand_pack_record(&mapped, vpn));
        g_assert_null(it_nand_pack_record(&chunked, vpn));
        g_assert_cmpint(it_nand_pack_find_slot(&mapped, vpn), ==, -1);
        g_assert_cmpint(it_nand_pack_find_slot(&chunked, vpn), ==, -1);
    }

    g_assert_cmpuint(cache.fetches, >, 0);
    g_free(cache.buffer);
    fixture_clear(&f);
}

/* A page whose chunk has not arrived must read as absent, not as garbage. */
static void test_chunk_miss_is_not_data(void)
{
    Fixture f;
    ITNandPack mapped, chunked;
    ChunkCache cache;
    unsigned served = 0, missed = 0;

    fixture_init(&f);
    g_assert_true(it_nand_pack_open_mapped(&mapped, f.pack, f.pack_len,
                                           f.stride, &error_abort));
    cache = (ChunkCache) {
        .payload = f.pack + IT_NAND_PACK_HEADER_SIZE + f.entry_count * 4,
        .entry_count = f.entry_count,
        .stride = f.stride,
        .pages_per_chunk = f.pages_per_chunk,
        .resident_limit = 1,        /* only the first chunk is cached */
        .buffer = g_malloc(f.pages_per_chunk * f.stride),
    };
    g_assert_true(it_nand_pack_open_chunked(
        &chunked,
        (const uint8_t *)g_mapped_file_get_contents(f.index_file),
        g_mapped_file_get_length(f.index_file), f.stride, f.pages_per_chunk,
        chunk_fetch, &cache, &error_abort));

    for (uint32_t slot = 0; slot < f.entry_count; slot++) {
        uint32_t vpn = ldl_le_p(mapped.index + slot * 4);
        const uint8_t *b = it_nand_pack_record(&chunked, vpn);

        if (slot < f.pages_per_chunk) {
            g_assert_nonnull(b);
            g_assert_cmpmem(it_nand_pack_record(&mapped, vpn), f.stride,
                            b, f.stride);
            served++;
        } else {
            g_assert_null(b);
            missed++;
        }
    }
    g_assert_cmpuint(served, ==, f.pages_per_chunk);
    g_assert_cmpuint(missed, >, 0);

    g_free(cache.buffer);
    fixture_clear(&f);
}

/* Malformed packs must be rejected at open, not misread at lookup. */
static void test_rejects_bad_packs(void)
{
    Fixture f;
    ITNandPack pack;
    g_autofree uint8_t *copy = NULL;
    Error *err = NULL;

    fixture_init(&f);

    g_assert_false(it_nand_pack_open_mapped(&pack, f.pack, f.pack_len - 1,
                                            f.stride, &err));
    g_assert_nonnull(err);
    error_free(err);
    err = NULL;

    copy = g_memdup2(f.pack, f.pack_len);
    copy[0] = 'X';                                       /* magic */
    g_assert_false(it_nand_pack_open_mapped(&pack, copy, f.pack_len, f.stride,
                                            &err));
    error_free(err);
    err = NULL;

    memcpy(copy, f.pack, f.pack_len);
    stl_le_p(copy + 8, IT_NAND_PACK_VERSION + 1);        /* version */
    g_assert_false(it_nand_pack_open_mapped(&pack, copy, f.pack_len, f.stride,
                                            &err));
    error_free(err);
    err = NULL;

    /* Swap two index entries: the binary search would quietly return the
     * wrong page, so this has to be caught at open. */
    memcpy(copy, f.pack, f.pack_len);
    {
        uint32_t a = ldl_le_p(copy + IT_NAND_PACK_HEADER_SIZE);
        uint32_t b = ldl_le_p(copy + IT_NAND_PACK_HEADER_SIZE + 4);
        stl_le_p(copy + IT_NAND_PACK_HEADER_SIZE, b);
        stl_le_p(copy + IT_NAND_PACK_HEADER_SIZE + 4, a);
    }
    g_assert_false(it_nand_pack_open_mapped(&pack, copy, f.pack_len, f.stride,
                                            &err));
    error_free(err);

    fixture_clear(&f);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/nand-pack/mapped-matches-golden",
                    test_mapped_matches_golden);
    g_test_add_func("/nand-pack/chunked-matches-mapped",
                    test_chunked_matches_mapped);
    g_test_add_func("/nand-pack/chunk-miss-is-not-data",
                    test_chunk_miss_is_not_data);
    g_test_add_func("/nand-pack/rejects-bad-packs", test_rejects_bad_packs);
    return g_test_run();
}
