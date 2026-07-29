/*
 * IPODNAND pack access -- the seam between "the whole pack is a mapped file"
 * and "records arrive as compressed chunks over the network".
 *
 * The pack layout (produced by scripts/pack-ipod-nand.py) is:
 *
 *     magic "IPODNAND" | u32 version | u32 stride | u32 entry_count
 *     entry_count x u32 virtual page number, ASCENDING      <- the index
 *     entry_count x <stride> bytes of page+spare            <- the payload
 *
 * A record is found by binary search in the index; its SLOT (the position in
 * the index) is also its position in the payload. The browser port chunks the
 * payload at slot granularity -- chunk N holds slots [N*pages_per_chunk, ...) --
 * so a chunk-backed source only needs the header and the index up front, and
 * can fetch the rest on demand.
 *
 * Both sources go through it_nand_pack_record(); native keeps the mapped-file
 * one and remains the correctness oracle. tests/unit/test-nand-pack.c proves
 * the two return identical bytes for every virtual page in a golden fixture.
 *
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 2 of the License, or (at your option)
 * any later version.
 */

#ifndef IPOD_TOUCH_NAND_PACK_H
#define IPOD_TOUCH_NAND_PACK_H

#include "qapi/error.h"

#define IT_NAND_PACK_MAGIC "IPODNAND"
#define IT_NAND_PACK_VERSION 1
#define IT_NAND_PACK_HEADER_SIZE 20

/*
 * Hand back at least `*n_slots * stride` bytes for the given chunk, or NULL if
 * the chunk cannot be served.
 *
 * This call is SYNCHRONOUS on purpose: the NAND model is driven from QEMU's
 * MMIO path, which cannot await. In the browser the service worker is what
 * makes that possible -- it intercepts the request, and the emulator never
 * awaits. The returned pointer must stay valid until the next call.
 */
typedef const uint8_t *(*ITNandChunkFetch)(void *opaque, uint32_t chunk,
                                           uint32_t *n_slots);

typedef struct ITNandPack {
    const uint8_t *index;        /* entry_count little-endian u32 VPNs */
    uint32_t entry_count;
    uint32_t stride;             /* bytes per record: page + spare */

    /* mapped-file source: the whole payload, contiguous */
    const uint8_t *data;

    /* chunk-backed source: used when data == NULL */
    uint32_t pages_per_chunk;
    ITNandChunkFetch fetch_chunk;
    void *opaque;
} ITNandPack;

/*
 * Parse a complete pack (header + index + payload). `bytes` must stay valid
 * for the lifetime of `pack`; nothing is copied.
 */
bool it_nand_pack_open_mapped(ITNandPack *pack, const uint8_t *bytes,
                              uint64_t len, uint32_t expected_stride,
                              Error **errp);

/*
 * Parse a pack INDEX -- header + index only, no payload -- and serve records
 * from `fetch`. This is what the browser uses: the index is ~4 bytes per page
 * (428 KiB for iPhone OS 1.0) and downloads up front, the payload arrives as
 * chunks.
 */
bool it_nand_pack_open_chunked(ITNandPack *pack, const uint8_t *bytes,
                               uint64_t len, uint32_t expected_stride,
                               uint32_t pages_per_chunk,
                               ITNandChunkFetch fetch, void *opaque,
                               Error **errp);

/* Slot holding `vpn`, or -1 when the pack does not carry that page. */
int64_t it_nand_pack_find_slot(const ITNandPack *pack, uint32_t vpn);

/*
 * The `stride`-byte record for `vpn`, or NULL if absent (or, for a chunked
 * pack, if its chunk is not resident). Valid until the next call on a chunked
 * pack; for the lifetime of the mapping on a mapped one.
 */
const uint8_t *it_nand_pack_record(const ITNandPack *pack, uint32_t vpn);

#endif /* IPOD_TOUCH_NAND_PACK_H */
