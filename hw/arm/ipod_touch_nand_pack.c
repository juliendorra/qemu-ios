/*
 * IPODNAND pack access. See include/hw/arm/ipod_touch_nand_pack.h.
 *
 * Deliberately free of device-model dependencies (osdep + glib only) so the
 * unit test can link it directly and compare the mapped-file source against
 * the chunk-backed one.
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 2 of the License, or (at your option)
 * any later version.
 */

#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "hw/arm/ipod_touch_nand_pack.h"

static bool pack_parse_header(ITNandPack *pack, const uint8_t *bytes,
                              uint64_t len, uint32_t expected_stride,
                              uint64_t *index_end, Error **errp)
{
    uint32_t version, stride, entry_count;

    memset(pack, 0, sizeof(*pack));

    if (len < IT_NAND_PACK_HEADER_SIZE ||
        memcmp(bytes, IT_NAND_PACK_MAGIC, 8) != 0) {
        error_setg(errp, "not a NAND pack: bad magic or short header");
        return false;
    }
    version = ldl_le_p(bytes + 8);
    stride = ldl_le_p(bytes + 12);
    entry_count = ldl_le_p(bytes + 16);

    if (version != IT_NAND_PACK_VERSION) {
        error_setg(errp, "unsupported NAND pack version %u", version);
        return false;
    }
    if (stride == 0 || (expected_stride && stride != expected_stride)) {
        error_setg(errp, "NAND pack stride %u, expected %u", stride,
                   expected_stride);
        return false;
    }
    *index_end = IT_NAND_PACK_HEADER_SIZE + (uint64_t)entry_count * 4;
    if (len < *index_end) {
        error_setg(errp, "truncated NAND pack index (%" PRIu64 " < %" PRIu64
                   ")", len, *index_end);
        return false;
    }

    pack->index = bytes + IT_NAND_PACK_HEADER_SIZE;
    pack->entry_count = entry_count;
    pack->stride = stride;

    /*
     * The index must be strictly ascending: the lookup binary-searches it, and
     * a pack that violates this would silently return the wrong page rather
     * than fail. Cheap enough to check once at open (~100k entries).
     */
    for (uint32_t i = 1; i < entry_count; i++) {
        if (ldl_le_p(pack->index + (i - 1) * 4) >=
            ldl_le_p(pack->index + i * 4)) {
            error_setg(errp, "unsorted or duplicate NAND pack index at %u", i);
            return false;
        }
    }
    return true;
}

bool it_nand_pack_open_mapped(ITNandPack *pack, const uint8_t *bytes,
                              uint64_t len, uint32_t expected_stride,
                              Error **errp)
{
    uint64_t index_end, expected;

    if (!pack_parse_header(pack, bytes, len, expected_stride, &index_end,
                           errp)) {
        return false;
    }
    expected = index_end + (uint64_t)pack->entry_count * pack->stride;
    if (len != expected) {
        error_setg(errp, "NAND pack is %" PRIu64 " bytes, expected %" PRIu64,
                   len, expected);
        pack->index = NULL;
        return false;
    }
    pack->data = bytes + index_end;
    return true;
}

bool it_nand_pack_open_chunked(ITNandPack *pack, const uint8_t *bytes,
                               uint64_t len, uint32_t expected_stride,
                               uint32_t pages_per_chunk,
                               ITNandChunkFetch fetch, void *opaque,
                               Error **errp)
{
    uint64_t index_end;

    if (pages_per_chunk == 0 || fetch == NULL) {
        error_setg(errp, "chunked NAND pack needs a chunk size and a fetcher");
        return false;
    }
    if (!pack_parse_header(pack, bytes, len, expected_stride, &index_end,
                           errp)) {
        return false;
    }
    /*
     * A payload is tolerated but ignored -- the index file the chunker emits
     * has none, while a whole pack passed here is still perfectly usable as an
     * index. That is what lets the test drive both sources from one fixture.
     */
    pack->data = NULL;
    pack->pages_per_chunk = pages_per_chunk;
    pack->fetch_chunk = fetch;
    pack->opaque = opaque;
    return true;
}

int64_t it_nand_pack_find_slot(const ITNandPack *pack, uint32_t vpn)
{
    uint32_t low = 0;
    uint32_t high = pack->entry_count;

    if (pack->index == NULL) {
        return -1;
    }
    while (low < high) {
        uint32_t middle = low + (high - low) / 2;
        uint32_t candidate = ldl_le_p(pack->index + middle * 4);

        if (candidate < vpn) {
            low = middle + 1;
        } else {
            high = middle;
        }
    }
    if (low == pack->entry_count ||
        ldl_le_p(pack->index + low * 4) != vpn) {
        return -1;
    }
    return low;
}

const uint8_t *it_nand_pack_record(const ITNandPack *pack, uint32_t vpn)
{
    int64_t slot = it_nand_pack_find_slot(pack, vpn);
    uint32_t chunk, offset, n_slots = 0;
    const uint8_t *base;

    if (slot < 0) {
        return NULL;
    }
    if (pack->data != NULL) {
        return pack->data + (uint64_t)slot * pack->stride;
    }

    chunk = (uint32_t)slot / pack->pages_per_chunk;
    offset = (uint32_t)slot % pack->pages_per_chunk;
    base = pack->fetch_chunk(pack->opaque, chunk, &n_slots);
    if (base == NULL || offset >= n_slots) {
        return NULL;
    }
    return base + (uint64_t)offset * pack->stride;
}
