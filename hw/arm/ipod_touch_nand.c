#include "hw/arm/ipod_touch_nand.h"
#include "hw/core/hw-error.h"
#include "qemu/bswap.h"
#include "trace.h"

#define NAND_PACK_FILENAME "nand.pack"
/* Header + index only, no payload: what a chunk-backed source needs up front. */
#define NAND_PACK_INDEX_FILENAME "nand.pack.idx"

static int get_bank(ITNandState *s) {
    uint32_t bank_bitmap = (s->fmctrl0 >> 1) & 0xFF;
    for(int bank = 0; bank < s->num_banks; bank++) {
        if((bank_bitmap & (1 << bank)) != 0) {
            return bank;
        }
    }
    return -1;
}

static void set_bank(ITNandState *s, uint32_t activate_bank) {
    for(int bank = 0; bank < 8; bank++) {
        // clear bit, toggle if it is active
        s->fmctrl0 &= ~(1 << (bank + 1));
        if(bank == activate_bank) {
            s->fmctrl0 ^= 1 << (bank + 1);
        }
    }
}

/*
 * IT_NAND_WRITABLE=1 makes guest writes VISIBLE to subsequent reads.
 *
 * By default this model is write-ONLY: writes land in "<page>_new.page" and
 * the read path never opens those files, so from the guest's point of view
 * storage silently discards everything. That is safe for a NAND shipped
 * read-only inside an app bundle, but it makes any daemon that must CREATE
 * state spin forever. Measured on M68AP: com.apple.AddressBook creates its
 * SQLite database, reads it back, finds "no such table: ABPerson", and
 * retries ~250 times a second -- pegging the emulated CPU at 98% while the
 * iPod idles at 11%.
 *
 * With this flag, writes go to "<page>.page" and the read path prefers an
 * on-disk page over the immutable pack, so a write is read back within the
 * session. Only enable it when the NAND directory is a THROWAWAY COPY: the
 * iPhone bundle stages a fresh clone per launch (see ipod-app-launcher.sh),
 * which is exactly that. Never point it at a pristine bundle NAND.
 */
/* IT_NAND_RB=1: does the guest ever READ BACK a page it wrote? If writes
 * persist but are never read again, the write path is addressing different
 * pages than the read path -- a mapping bug, not a storage bug (T6). */
static GHashTable *nand_written_pages;

static void nand_note_write(uint32_t bank, uint32_t page)
{
    if (!getenv("IT_NAND_RB")) {
        return;
    }
    if (!nand_written_pages) {
        nand_written_pages = g_hash_table_new(NULL, NULL);
    }
    g_hash_table_add(nand_written_pages, GUINT_TO_POINTER((bank << 24) | page));
}

static void nand_note_read(uint32_t bank, uint32_t page)
{
    static unsigned hits, misses;

    if (!getenv("IT_NAND_RB") || !nand_written_pages) {
        return;
    }
    if (g_hash_table_contains(nand_written_pages,
                              GUINT_TO_POINTER((bank << 24) | page))) {
        if (++hits <= 20 || hits % 100 == 0) {
            fprintf(stderr, "[NAND-RB] read-back HIT bank%u/%u (hits=%u "
                    "misses=%u)\n", bank, page, hits, misses);
        }
    } else {
        misses++;
    }
}

/*
 * ...and a browser cannot set that variable: Emscripten's ENV object is not in
 * EXPORTED_RUNTIME_METHODS, so a page has no way to reach it. It CAN write a
 * file, so "writable=1" in <nand>/nand-tune says the same thing.
 *
 * In the browser this IS the copy-on-write overlay, in its simplest possible
 * form: the NAND directory lives in MEMFS, so a written page is a few kilobytes
 * of heap that the read path prefers over the immutable pack, and it evaporates
 * with the tab. That is enough to reach SpringBoard, which a read-only NAND
 * never does -- daemons that must create state spin for ever. It is NOT enough
 * for persistence across visits; that still wants the real overlay (W6).
 */
/*
 * overlay=ram in <nand>/nand-tune: guest writes go into HEAP, not into files,
 * and reads prefer them. This is the copy-on-write overlay the browser needs
 * (W6), in the smallest form that works.
 *
 * The file-backed writable mode CANNOT serve the browser, and the reason is
 * worth keeping: with -sPROXY_TO_PTHREAD every MEMFS syscall is proxied to the
 * main thread, so the `stat()` this model does before each page read becomes a
 * cross-thread round trip. Measured: the boot stalled outright -- 816 blocks
 * compiled in 181 s and no landmarks, against kernel at 113 s without it.
 *
 * A hash table of 2,112-byte records costs a few tens of MB across a boot and
 * touches no filesystem at all -- which also means the bank<N> directories
 * stop being load-bearing for a packed NAND.
 */
static GHashTable *nand_overlay;      /* (bank<<24)|page -> 2112 bytes */

/*
 * W6, the persistence half: get the overlay OUT of the tab and back in.
 *
 * Restore is a file read at init -- the page writes <nand>/overlay.bin in
 * preRun, before the machine starts, so nothing has to cross a thread boundary.
 * Saving cannot work that way, because the guest is running when the page wants
 * a copy, so it goes through a request the EMULATOR thread services
 * (ui/wasm.c's drain timer) and publishes for the page to read.
 *
 * Format, deliberately trivial and self-describing:
 *
 *   magic "ITNOVL1"  u8[8]      (NUL-padded)
 *   record count     u32 LE
 *   page size        u32 LE     so a stride change is caught, not mis-read
 *   records          count x { u32 LE key, page_size bytes }
 *
 * The key is the model's own (bank << 24) | page. This is NOT a migration
 * stream: it is keyed to the PACK, not to the engine's vmstate layout, so it
 * survives an emulator rebuild -- which is the whole reason persistence lives
 * here rather than in a VM snapshot. (A snapshot does not capture NAND writes
 * at all: the overlay has no VMStateDescription.)
 */
#define NAND_OVERLAY_MAGIC "ITNOVL1"
#define NAND_OVERLAY_HEADER 16

static uint32_t nand_overlay_page_size(void)
{
    return NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE;
}

static void nand_overlay_restore(ITNandState *s)
{
    char filename[PATH_MAX];
    g_autofree char *blob = NULL;
    gsize len = 0;
    uint32_t count, page_size, i;
    const uint8_t *p;

    g_snprintf(filename, sizeof(filename), "%s/overlay.bin", s->nand_path);
    if (!g_file_get_contents(filename, &blob, &len, NULL)) {
        return;                                  /* first visit */
    }
    if (len < NAND_OVERLAY_HEADER ||
        memcmp(blob, NAND_OVERLAY_MAGIC, strlen(NAND_OVERLAY_MAGIC)) != 0) {
        fprintf(stderr, "[NAND] overlay.bin is not an overlay - ignoring\n");
        return;
    }
    memcpy(&count, blob + 8, 4);
    memcpy(&page_size, blob + 12, 4);
    if (page_size != nand_overlay_page_size()) {
        fprintf(stderr, "[NAND] overlay.bin has page size %u, this NAND uses "
                "%u - ignoring rather than corrupting it\n",
                page_size, nand_overlay_page_size());
        return;
    }
    if (len != (gsize)NAND_OVERLAY_HEADER + (gsize)count * (4 + page_size)) {
        fprintf(stderr, "[NAND] overlay.bin is truncated (%zu bytes for %u "
                "records) - ignoring\n", (size_t)len, count);
        return;
    }

    p = (const uint8_t *)blob + NAND_OVERLAY_HEADER;
    for (i = 0; i < count; i++) {
        uint32_t key;
        uint8_t *record = g_malloc(page_size);

        memcpy(&key, p, 4);
        memcpy(record, p + 4, page_size);
        g_hash_table_insert(nand_overlay, GUINT_TO_POINTER(key), record);
        p += 4 + page_size;
    }
    fprintf(stderr, "[NAND] restored %u overlay pages from a previous visit\n",
            count);
}

/* Serialize for the page. Caller frees. Runs on the emulator thread. */
uint8_t *it_nand_overlay_save(uint32_t *out_len)
{
    uint32_t page_size = nand_overlay_page_size();
    GHashTableIter iter;
    gpointer key, value;
    uint32_t count, i = 0;
    uint8_t *blob, *p;

    *out_len = 0;
    if (nand_overlay == NULL) {
        return NULL;
    }
    count = g_hash_table_size(nand_overlay);
    *out_len = NAND_OVERLAY_HEADER + count * (4 + page_size);
    blob = g_malloc0(*out_len);
    memcpy(blob, NAND_OVERLAY_MAGIC, strlen(NAND_OVERLAY_MAGIC));
    memcpy(blob + 8, &count, 4);
    memcpy(blob + 12, &page_size, 4);

    p = blob + NAND_OVERLAY_HEADER;
    g_hash_table_iter_init(&iter, nand_overlay);
    while (g_hash_table_iter_next(&iter, &key, &value) && i < count) {
        uint32_t k = GPOINTER_TO_UINT(key);
        memcpy(p, &k, 4);
        memcpy(p + 4, value, page_size);
        p += 4 + page_size;
        i++;
    }
    return blob;
}

static void nand_overlay_restore(ITNandState *s);

static bool nand_overlay_enabled(ITNandState *s)
{
    static int cached = -1;
    char filename[PATH_MAX];
    g_autofree char *text = NULL;
    const char *v = getenv("IT_NAND_OVERLAY");

    if (cached >= 0) {
        return cached;
    }
    cached = 0;
    if (v && *v) {
        cached = strcmp(v, "0") != 0;
    } else {
        g_snprintf(filename, sizeof(filename), "%s/nand-tune", s->nand_path);
        if (g_file_get_contents(filename, &text, NULL, NULL) &&
            strstr(text, "overlay=ram") != NULL) {
            cached = 1;
        }
    }
    if (cached) {
        nand_overlay = g_hash_table_new_full(NULL, NULL, NULL, g_free);
        fprintf(stderr, "[NAND] copy-on-write overlay in RAM: guest writes "
                "shadow the pack for this session\n");
        nand_overlay_restore(s);
    }
    return cached;
}

static uint8_t *nand_overlay_lookup(uint32_t bank, uint32_t page)
{
    if (nand_overlay == NULL) {
        return NULL;
    }
    return g_hash_table_lookup(nand_overlay,
                               GUINT_TO_POINTER((bank << 24) | page));
}

static bool nand_writable(ITNandState *s)
{
    static int cached = -1;
    char filename[PATH_MAX];
    g_autofree char *text = NULL;
    const char *v;

    if (cached >= 0) {
        return cached;
    }
    v = getenv("IT_NAND_WRITABLE");
    if (v && *v) {
        cached = strcmp(v, "0") != 0;
        return cached;
    }
    cached = 0;
    g_snprintf(filename, sizeof(filename), "%s/nand-tune", s->nand_path);
    if (g_file_get_contents(filename, &text, NULL, NULL) &&
        strstr(text, "writable=1") != NULL) {
        cached = 1;
        fprintf(stderr, "[NAND] writable: guest writes shadow the pack "
                "(%s)\n", filename);
    }
    return cached;
}

/*
 * A browser build serves records from a chunk cache instead of a mapped file
 * (BROWSER_WASM_SESSION_B.md, B2/B3). Installing a source here before machine
 * init makes nand_open_pack() map the small INDEX file -- header plus one u32
 * per page, 428 KiB for iPhone OS 1.0 -- rather than the 215 MiB pack, and
 * route every record through the chunk fetcher.
 *
 * Native installs nothing and keeps the mapped-file path, which stays the
 * correctness oracle; tests/unit/test-nand-pack.c proves the two agree.
 */
static struct {
    uint32_t pages_per_chunk;
    ITNandChunkFetch fetch;
    void *opaque;
} nand_chunk_source;

void it_nand_set_chunk_source(uint32_t pages_per_chunk, ITNandChunkFetch fetch,
                              void *opaque)
{
    nand_chunk_source.pages_per_chunk = pages_per_chunk;
    nand_chunk_source.fetch = fetch;
    nand_chunk_source.opaque = opaque;
}

static void nand_open_pack(ITNandState *s)
{
    char filename[PATH_MAX];
    const uint8_t *contents;
    bool chunked;
    gsize length;
    GError *gerror = NULL;
    Error *error = NULL;

    if (s->pack_checked) {
        return;
    }
    s->pack_checked = true;

    /*
     * A chunked NAND directory has no nand.pack at all -- that is the whole
     * point -- so the presence of the small index file plus a chunk config is
     * what selects the browser path. Nothing to configure, and a native tree
     * (which has the pack and no chunk config) can never take it by accident.
     */
#ifdef EMSCRIPTEN
    it_nand_chunks_init(s->nand_path);
#endif
    chunked = nand_chunk_source.fetch != NULL;

    g_snprintf(filename, sizeof(filename), "%s/%s", s->nand_path,
               chunked ? NAND_PACK_INDEX_FILENAME : NAND_PACK_FILENAME);
    s->pack_file = g_mapped_file_new(filename, false, &gerror);
    if (s->pack_file == NULL) {
        if (gerror == NULL) {
            hw_error("Unable to map NAND pack %s", filename);
        }
        if (!g_error_matches(gerror, G_FILE_ERROR, G_FILE_ERROR_NOENT)) {
            hw_error("Unable to map NAND pack %s: %s", filename,
                     gerror->message);
        }
        g_clear_error(&gerror);
        return;
    }

    contents = (const uint8_t *)g_mapped_file_get_contents(s->pack_file);
    length = g_mapped_file_get_length(s->pack_file);

    if (chunked
        ? !it_nand_pack_open_chunked(&s->pack, contents, length,
                                     NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE,
                                     nand_chunk_source.pages_per_chunk,
                                     nand_chunk_source.fetch,
                                     nand_chunk_source.opaque, &error)
        : !it_nand_pack_open_mapped(&s->pack, contents, length,
                                    NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE,
                                    &error)) {
        hw_error("%s: %s", filename, error_get_pretty(error));
    }
}

static bool nand_read_packed_page(ITNandState *s, uint32_t bank,
                                  uint32_t page)
{
    uint32_t vpn = page * NAND_NUM_BANKS + bank;
    const uint8_t *record;

    nand_open_pack(s);
    record = it_nand_pack_record(&s->pack, vpn);
    if (record == NULL) {
        return false;
    }

    memcpy(s->page_buffer, record, NAND_BYTES_PER_PAGE);
    memcpy(s->page_spare_buffer, record + NAND_BYTES_PER_PAGE,
           NAND_BYTES_PER_SPARE);
    return true;
}

/* IT_NAND_TRACE_PAGES=<path> records every page FETCH (buffer miss) as a
 * little-endian u32 virtual page number, in access order.
 *
 * This exists for the browser port: the delivery design turns on how much of
 * the ~300 MB base pack a cold boot actually touches, and in what order. The
 * distinct set sizes the download, the order gives the prefetch list. Buffer
 * hits are deliberately not recorded -- they never reach storage, and in the
 * browser they would never reach the chunk cache either.
 *
 * scripts/wasm/analyze-nand-trace.py consumes this. Off unless the variable is
 * set; when on it costs one buffered fwrite per miss. */
static void nand_trace_page(uint32_t bank, uint32_t page)
{
    static FILE *trace;
    static int checked;
    uint32_t vpn;

    if (!checked) {
        const char *path = getenv("IT_NAND_TRACE_PAGES");

        checked = 1;
        if (path && *path) {
            trace = fopen(path, "wb");
            if (trace == NULL) {
                fprintf(stderr, "[NANDTRACE] cannot write %s\n", path);
            } else {
                fprintf(stderr, "[NANDTRACE] recording page fetches to %s\n",
                        path);
            }
        }
    }
    if (trace == NULL) {
        return;
    }
    vpn = page * NAND_NUM_BANKS + bank;
    fwrite(&vpn, sizeof(vpn), 1, trace);
    /* Flushed continuously so a run stopped with SIGKILL still yields a usable
     * trace: these boots are ended by a watchdog, not by a clean exit. */
    fflush(trace);
}

void nand_set_buffered_page(ITNandState *s, uint32_t page) {
    uint32_t bank = get_bank(s);
    if(bank == -1) {
        hw_error("Active bank not set while nand_read with page %d is called (reading multiple pages: %d)!", page, s->reading_multiple_pages);
    }

    if(bank != s->buffered_bank || page != s->buffered_page) {
        // refresh the buffered page
        nand_trace_page(bank, page);
        char filename[200];
        bool present = true;
        const uint8_t *overlaid = NULL;

        if (nand_overlay_enabled(s)) {
            overlaid = nand_overlay_lookup(bank, page);
        }
        if (overlaid != NULL) {
            /* A page the guest has written this session shadows the pack, and
             * costs one hash lookup rather than a proxied stat(). */
            memcpy(s->page_buffer, overlaid, NAND_BYTES_PER_PAGE);
            memcpy(s->page_spare_buffer, overlaid + NAND_BYTES_PER_PAGE,
                   NAND_BYTES_PER_SPARE);
            s->buffered_page = page;
            s->buffered_bank = bank;
            nand_note_read(bank, page);
            return;
        }
        sprintf(filename, "%s/bank%d/%d.page", s->nand_path, bank, page);
        struct stat st = {0};
        if (!(nand_writable(s) && stat(filename, &st) == 0) &&
            nand_read_packed_page(s, bank, page)) {
            /* The immutable base pack replaces the per-page open/read path.
             * When writable, a page the guest has written shadows the pack. */
        }
        else if (stat(filename, &st) == -1) {
            // page storage does not exist - initialize an empty buffer
            present = false;
            /*
             * IT_NAND_ERASED_FF=1: a page with no backing store reads as
             * ERASED -- 0xFF everywhere -- which is what the silicon does.
             *
             * The default below instead hands back zeroes with a hand-placed
             * 0xFF at spare[0xA], i.e. a page that looks WRITTEN and valid,
             * carrying logical page 0. Most of a generated NAND has no backing
             * store, so under that default every unwritten page in the device
             * answers "yes, I hold valid data". Anything that walks a block
             * looking for the frontier between written and erased pages --
             * FTL_Open picking the newest context from the last valid page,
             * for one -- can never find it.
             */
            if (getenv("IT_NAND_ERASED_FF")) {
                memset(s->page_buffer, 0xFF, NAND_BYTES_PER_PAGE);
                memset(s->page_spare_buffer, 0xFF, NAND_BYTES_PER_SPARE);
            } else {
                memset(s->page_buffer, 0, NAND_BYTES_PER_PAGE);
                memset(s->page_spare_buffer, 0, NAND_BYTES_PER_SPARE);
                s->page_spare_buffer[0xA] = 0xFF; // make sure we add the FTL mark to an empty page
            }
        }
        else {
            FILE *f = fopen(filename, "rb");
            if (f == NULL) { hw_error("Unable to read file!"); }
            fread(s->page_buffer, sizeof(char), NAND_BYTES_PER_PAGE, f);
            fread(s->page_spare_buffer, sizeof(char), NAND_BYTES_PER_SPARE, f);
            fclose(f);
        }

        s->buffered_page = page;
        s->buffered_bank = bank;
        nand_note_read(bank, page);
        /* IT_NAND_WATCH=<bank>/<page>[,...]: report when the guest reads
         * specific physical pages. Used to tell "the guest never looked at
         * this file" from "the guest read it and rejected it" -- the two
         * have identical symptoms at the application level. */
        {
            const char *watch = getenv("IT_NAND_WATCH");
            if (watch) {
                char needle[40];
                /* EXACT token match. A plain strstr() matched "1/6" inside
                 * "1/60547" and reported reads that never happened -- the
                 * first use of this trace produced a void measurement. */
                snprintf(needle, sizeof(needle), ",%u/%u,", bank, page);
                char list[512];
                snprintf(list, sizeof(list), ",%s,", watch);
                if (strstr(list, needle)) {
                    fprintf(stderr, "[NAND-WATCH] guest read bank%u/%u "
                            "(present=%d)\n", bank, page, present);
                }
            }
        }
        /* Context-page transitions are the useful geometry diagnostic.  Keep
         * the opt-in trace sparse so it does not perturb guest startup timing. */
        if (s->page_spare_buffer[9] == 0x43 || s->last_spare_type == 0x43) {
            trace_itnand_read_page(bank, page, present,
                                   s->page_spare_buffer[9]);
        }
        /* Keep the root-filesystem boundary diagnostic sparse while comparing
         * the working N45AP path with M68AP.  The candidate context, volume
         * header, and first extents-tree reads fall in this five-page window. */
        if (page >= 25855 && page <= 25859) {
            trace_itnand_root_page(bank, page, present,
                                   ldl_le_p(s->page_buffer),
                                   ldl_le_p(s->page_buffer + 0x20),
                                   ldl_le_p(s->page_buffer + 0x400),
                                   s->page_spare_buffer[9]);
        }
        s->last_spare_type = s->page_spare_buffer[9];
        // printf("Buffered bank: %d, page: %d\n", s->buffered_bank, s->buffered_page);
    }
}

/*
 * Commit whatever is in the page buffer to the currently buffered bank/page.
 * Factored out of the FIFO handler because a multi-page write has to do this
 * once per 2 KiB rather than once per transfer.
 */
static void nand_flush_buffered_page(ITNandState *s)
{
    char filename[200];
    FILE *f;

    qemu_mutex_lock(&s->lock);
    qemu_mutex_unlock(&s->lock);

    if (nand_overlay_enabled(s)) {
        uint8_t *record = nand_overlay_lookup(s->buffered_bank,
                                              s->buffered_page);

        if (record == NULL) {
            record = g_malloc(NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE);
            g_hash_table_insert(nand_overlay,
                                GUINT_TO_POINTER((s->buffered_bank << 24) |
                                                 s->buffered_page), record);
        }
        memcpy(record, s->page_buffer, NAND_BYTES_PER_PAGE);
        memcpy(record + NAND_BYTES_PER_PAGE, s->page_spare_buffer,
               NAND_BYTES_PER_SPARE);
        nand_note_write(s->buffered_bank, s->buffered_page);
    } else {
        sprintf(filename, "%s/bank%d/%d%s.page", s->nand_path,
                s->buffered_bank, s->buffered_page,
                nand_writable(s) ? "" : "_new");
        f = fopen(filename, "wb");
        if (f == NULL) { hw_error("Unable to read file!"); }
        nand_note_write(s->buffered_bank, s->buffered_page);
        fwrite(s->page_buffer, sizeof(char), NAND_BYTES_PER_PAGE, f);
        fwrite(s->page_spare_buffer, sizeof(char), NAND_BYTES_PER_SPARE, f);
        fclose(f);
    }

    if (getenv("IT_NAND_WRITE")) {
        /*
         * Separate counters per mode, deliberately: single-page writes run
         * into the thousands during boot and a shared cap hides the handful of
         * multi-page ones entirely -- which is exactly what happened the first
         * time this was measured.
         */
        static unsigned n[2];
        unsigned *cnt = &n[s->writing_multiple_pages ? 1 : 0];
        /* IT_NAND_WRITE=<limit> raises the per-mode cap; the default hides
         * anything past the first 200, which is how the one page that breaks
         * a reboot managed to be written with no trace line at all. */
        unsigned limit = (unsigned)strtoul(getenv("IT_NAND_WRITE"), NULL, 0);
        if (limit < 2) {
            limit = 200;
        }
        if ((*cnt)++ < limit) {
            fprintf(stderr, "[NAND-WRITE] bank%u page %u spare %08x %08x "
                    "mark 0x%02x (multi %d, %u/%u, fmdnum %u, words %u/%u)\n",
                    s->buffered_bank, s->buffered_page,
                    ldl_le_p(s->page_spare_buffer),
                    ldl_le_p(s->page_spare_buffer + 4),
                    s->page_spare_buffer[0xa], s->writing_multiple_pages,
                    s->cur_page_writing, s->num_pages_writing, s->fmdnum,
                    s->words_this_page, NAND_BYTES_PER_PAGE / 4);
        }
    }
}

/* Point the write at entry `idx` of the multi-page descriptor. */
static void nand_begin_write_page(ITNandState *s, uint32_t idx)
{
    set_bank(s, s->banks_to_write[idx]);
    /*
     * Loads the existing contents, which the incoming 2 KiB then overwrites in
     * full -- but it is also what sets buffered_bank/buffered_page, which the
     * flush above writes to. It refreshes page_spare_buffer from the media, so
     * the guest's spare has to go in AFTER it, not before.
     */
    nand_set_buffered_page(s, s->pages_to_write[idx]);
    s->words_this_page = 0;
    memset(s->page_spare_buffer, 0, NAND_BYTES_PER_SPARE);
    memcpy(s->page_spare_buffer, s->spares_to_write[idx],
           NAND_ADM_SPARE_RECORD);
}

void nand_begin_multi_write(ITNandState *s, uint32_t num_pages)
{
    s->writing_multiple_pages = true;
    s->reading_multiple_pages = false;
    s->num_pages_writing = num_pages;
    s->cur_page_writing = 0;
    s->fmdnum = num_pages * NAND_BYTES_PER_PAGE;
    s->is_writing = true;
    nand_begin_write_page(s, 0);
}

static uint64_t itnand_read(void *opaque, hwaddr addr, unsigned size)
{
    ITNandState *s = (ITNandState *) opaque;
    if(s->reading_multiple_pages) {
        //fprintf(stderr, "%s: reading from 0x%08x\n", __func__, addr);
    }

    switch (addr) {
        case NAND_FMCTRL0:
            return s->fmctrl0;
        case NAND_FMFIFO:
            if(s->cmd == NAND_CMD_ID) {
                int bank = get_bank(s);
                uint32_t value = bank >= 0 ? NAND_CHIP_ID : UINT32_MAX;
                trace_itnand_id(bank, value, s->num_banks);
                return value;
            }
            else if(s->cmd == NAND_CMD_READSTATUS) {
                return (1 << 6);
            }
            else {
                uint32_t read_val = 0;
                if(s->reading_multiple_pages) {
                    // which bank are we at?
                    if(s->fmdnum % 0x800 == 0) {
                        s->cur_bank_reading += 1;
                        //printf("WILL TURN TO BANK %d (cnt: %d)\n", s->cur_bank_reading, s->fmdnum);
                        set_bank(s, s->banks_to_read[s->cur_bank_reading]);
                    }

                    // compute the offset in the page
                    uint32_t page_offset = s->fmdnum % 0x800;
                    if(page_offset == 0) { page_offset = 0x800; }
                    nand_set_buffered_page(s, s->pages_to_read[s->cur_bank_reading]);
                    //printf("Reading page %d\n", s->pages_to_read[s->cur_bank_reading]);
                    read_val = ((uint32_t *)s->page_buffer)[(NAND_BYTES_PER_PAGE - page_offset) / 4];
                    //printf("FMDNUM: %d, offset: %d\n", s->fmdnum, (NAND_BYTES_PER_PAGE - page_offset) / 4);
                    //printf("Page offset: %d, bytes: 0x%08x\n", page_offset, read_val);
                }
                else {
                    uint32_t page = (s->fmaddr1 << 16) | (s->fmaddr0 >> 16);
                    uint32_t idx;
                    nand_set_buffered_page(s, page);
                    //printf("Reading page %d\n", page);

                    if(s->reading_spare) {
                        idx = (NAND_BYTES_PER_SPARE - s->fmdnum - 1) / 4;
                        read_val = ((uint32_t *)s->page_spare_buffer)[idx];
                    } else {
                        idx = (NAND_BYTES_PER_PAGE - s->fmdnum - 1) / 4;
                        read_val = ((uint32_t *)s->page_buffer)[idx];
                    }
                    /* IT_NAND_FIFO=1: the single-page FIFO index arithmetic.
                     * The word index is derived from FMDNUM's ABSOLUTE value,
                     * which assumes the guest primed FMDNUM with the full
                     * transfer length. A firmware that asks for a short read
                     * lands somewhere else in the page and silently gets
                     * zeroes. */
                    {
                        const char *fifo_trace = getenv("IT_NAND_FIFO");
                        static unsigned n;
                        unsigned limit = fifo_trace ?
                            (unsigned)strtoul(fifo_trace, NULL, 0) : 0;
                        if (limit <= 1) {
                            limit = 64;
                        }
                        if (fifo_trace && idx == 0 && n++ < limit) {
                            fprintf(stderr, "[NAND-FIFO] page %u fmdnum %u "
                                    "spare %d -> word[%u] = 0x%08x\n",
                                    page, s->fmdnum, s->reading_spare,
                                    idx, read_val);
                        }
                    }
                }
                s->fmdnum -= 4;
                return read_val;
            }

        case NAND_FMCSTAT: {
            /* Bits 1..12 = "everything ready, including our eight banks".
             * Bit 0 is deliberately NOT set here, which iBoot-204 tolerates.
             * IT_NAND_FMCSTAT=<value> overrides it so a firmware that polls a
             * different bit can be tested without a rebuild. */
            const char *override = getenv("IT_NAND_FMCSTAT");
            if (override) {
                return (uint64_t)strtoul(override, NULL, 0);
            }
            return (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12);
        }
        case NAND_RSCTRL:
            return s->rsctrl;
        default:
            break;
    }
    return 0;
}

static void itnand_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    ITNandState *s = (ITNandState *) opaque;
    if(s->reading_multiple_pages) {
        //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, val, addr);
    }
    

    switch(addr) {
        case NAND_FMCTRL0:
            s->fmctrl0 = val;
            break;
        case NAND_FMCTRL1:
            s->fmctrl1 = val;
            break;
        case NAND_FMADDR0:
            s->fmaddr0 = val;
            break;
        case NAND_FMADDR1:
            s->fmaddr1 = val;
            break;
        case NAND_FMANUM:
            s->fmanum = val;
            break;
        case NAND_CMD:
            /*
             * IT_NAND_CMDS=1: which NAND opcodes the guest actually issues.
             * The model only ever acts on ID/READ/READSTATUS, so anything else
             * -- an ERASE above all -- is accepted here and then silently
             * dropped, and the guest's picture of the media diverges from the
             * model's without a single error being reported.
             */
            if (getenv("IT_NAND_CMDS")) {
                static uint32_t seen[16], counts[16], nseen;
                uint32_t i;
                for (i = 0; i < nseen && seen[i] != val; i++) {
                }
                if (i == nseen && nseen < 16) {
                    seen[nseen++] = val;
                }
                if (i < 16) {
                    counts[i]++;
                    if (counts[i] <= 2 || counts[i] % 4096 == 0) {
                        fprintf(stderr, "[NAND-CMD] 0x%02x n=%u\n",
                                (uint32_t)val, counts[i]);
                    }
                }
            }
            s->cmd = val;
            break;
        case NAND_FMDNUM:
            if(val == NAND_BYTES_PER_SPARE - 1) {
                s->reading_spare = 1;
            } else {
                s->reading_spare = 0;
            }
            s->fmdnum = val;
            break;
        case NAND_FMFIFO:
            if(!s->is_writing) {
                // printf("%s: NAND_FMFIFO writing while not in writing mode!\n", __func__);
                return;
            }

            if (s->writing_multiple_pages) {
                /*
                 * FMDNUM counts the WHOLE transfer down here, exactly as it
                 * does on the multi-page read path, so the word index has to
                 * come from the offset within the current page -- the absolute
                 * value would run off the end of the buffer on page 2.
                 */
                uint32_t page_offset = s->fmdnum % NAND_BYTES_PER_PAGE;

                if (page_offset == 0) {
                    page_offset = NAND_BYTES_PER_PAGE;
                }
                ((uint32_t *)s->page_buffer)
                    [(NAND_BYTES_PER_PAGE - page_offset) / 4] = val;
                s->fmdnum -= 4;
                s->words_this_page++;

                if (s->fmdnum % NAND_BYTES_PER_PAGE == 0) {
                    nand_flush_buffered_page(s);
                    s->words_this_page = 0;
                    s->cur_page_writing++;
                    if (s->fmdnum == 0 ||
                        s->cur_page_writing >= s->num_pages_writing) {
                        s->is_writing = false;
                        s->writing_multiple_pages = false;
                    } else {
                        nand_begin_write_page(s, s->cur_page_writing);
                    }
                }
                break;
            }

            //printf("Setting offset %d: %d\n", s->fmdnum, (NAND_BYTES_PER_PAGE - s->fmdnum) / 4);
            ((uint32_t *)s->page_buffer)[(NAND_BYTES_PER_PAGE - s->fmdnum) / 4] = val;
            s->fmdnum -= 4;
            s->words_this_page++;

            if(s->fmdnum == 0) {
                // we're done!
                s->is_writing = false;
                nand_flush_buffered_page(s);
            }
            break;
        case NAND_RSCTRL:
            s->rsctrl = val;
            break;
        default:
            break;
    }
}

static const MemoryRegionOps nand_ops = {
    .read = itnand_read,
    .write = itnand_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void itnand_init(Object *obj)
{
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    ITNandState *s = ITNAND(obj);

    memory_region_init_io(&s->iomem, OBJECT(s), &nand_ops, s, "nand", 0x1000);
    sysbus_init_irq(sbd, &s->irq);

    s->page_buffer = (uint8_t *)malloc(NAND_BYTES_PER_PAGE);
    s->page_spare_buffer = (uint8_t *)malloc(NAND_BYTES_PER_SPARE);
    s->buffered_page = -1;
    s->buffered_bank = -1;

    qemu_mutex_init(&s->lock);
}

static void itnand_finalize(Object *obj)
{
    ITNandState *s = ITNAND(obj);

    if (s->pack_file != NULL) {
        g_mapped_file_unref(s->pack_file);
    }
    free(s->page_spare_buffer);
    free(s->page_buffer);
}

static void itnand_reset(DeviceState *d)
{
    ITNandState *s = (ITNandState *) d;

    s->fmctrl0 = 0;
    s->fmctrl1 = 0;
    s->fmaddr0 = 0;
    s->fmaddr1 = 0;
    s->fmanum = 0;
    s->fmdnum = 0;
    s->rsctrl = 0;
    s->cmd = 0;
    s->reading_spare = 0;
    s->buffered_page = -1;
}

static void itnand_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    device_class_set_legacy_reset(dc, itnand_reset);
}

static const TypeInfo itnand_info = {
    .name          = TYPE_ITNAND,
    .parent        = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(ITNandState),
    .instance_init = itnand_init,
    .instance_finalize = itnand_finalize,
    .class_init    = itnand_class_init,
};

static void itnand_register_types(void)
{
    type_register_static(&itnand_info);
}

type_init(itnand_register_types)
