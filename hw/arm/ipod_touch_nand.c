#include "hw/arm/ipod_touch_nand.h"
#include "hw/core/hw-error.h"
#include "qemu/bswap.h"
#include "trace.h"

#define NAND_PACK_FILENAME "nand.pack"
#define NAND_PACK_MAGIC "IPODNAND"
#define NAND_PACK_VERSION 1
#define NAND_PACK_HEADER_SIZE 20

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

static bool nand_writable(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *v = getenv("IT_NAND_WRITABLE");
        cached = (v && *v && strcmp(v, "0") != 0);
    }
    return cached;
}

static void nand_open_pack(ITNandState *s)
{
    char filename[PATH_MAX];
    const uint8_t *contents;
    uint32_t version;
    uint32_t page_size;
    uint32_t entry_count;
    uint64_t expected_size;
    gsize length;
    GError *error = NULL;

    if (s->pack_checked) {
        return;
    }
    s->pack_checked = true;
    g_snprintf(filename, sizeof(filename), "%s/%s", s->nand_path,
               NAND_PACK_FILENAME);
    s->pack_file = g_mapped_file_new(filename, false, &error);
    if (s->pack_file == NULL) {
        if (error == NULL) {
            hw_error("Unable to map NAND pack %s", filename);
        }
        if (!g_error_matches(error, G_FILE_ERROR, G_FILE_ERROR_NOENT)) {
            hw_error("Unable to map NAND pack %s: %s", filename,
                     error->message);
        }
        g_clear_error(&error);
        return;
    }

    contents = (const uint8_t *)g_mapped_file_get_contents(s->pack_file);
    length = g_mapped_file_get_length(s->pack_file);
    if (length < NAND_PACK_HEADER_SIZE ||
        memcmp(contents, NAND_PACK_MAGIC, 8) != 0) {
        hw_error("Invalid NAND pack header in %s", filename);
    }
    version = ldl_le_p(contents + 8);
    page_size = ldl_le_p(contents + 12);
    entry_count = ldl_le_p(contents + 16);
    expected_size = NAND_PACK_HEADER_SIZE + (uint64_t)entry_count * 4 +
                    (uint64_t)entry_count *
                    (NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE);
    if (version != NAND_PACK_VERSION ||
        page_size != NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE ||
        expected_size != length) {
        hw_error("Unsupported or truncated NAND pack %s", filename);
    }

    s->pack_entry_count = entry_count;
    s->pack_entries = contents + NAND_PACK_HEADER_SIZE;
    s->pack_data = s->pack_entries + (uint64_t)entry_count * 4;
    for (uint32_t index = 1; index < entry_count; index++) {
        if (ldl_le_p(s->pack_entries + (index - 1) * 4) >=
            ldl_le_p(s->pack_entries + index * 4)) {
            hw_error("Unsorted or duplicate NAND pack index in %s", filename);
        }
    }
}

static bool nand_read_packed_page(ITNandState *s, uint32_t bank,
                                  uint32_t page)
{
    uint32_t vpn = page * NAND_NUM_BANKS + bank;
    uint32_t low = 0;
    uint32_t high;

    nand_open_pack(s);
    high = s->pack_entry_count;
    while (low < high) {
        uint32_t middle = low + (high - low) / 2;
        uint32_t candidate = ldl_le_p(s->pack_entries + middle * 4);

        if (candidate < vpn) {
            low = middle + 1;
        } else {
            high = middle;
        }
    }
    if (low == s->pack_entry_count ||
        ldl_le_p(s->pack_entries + low * 4) != vpn) {
        return false;
    }

    memcpy(s->page_buffer,
           s->pack_data + (uint64_t)low *
           (NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE),
           NAND_BYTES_PER_PAGE);
    memcpy(s->page_spare_buffer,
           s->pack_data + (uint64_t)low *
           (NAND_BYTES_PER_PAGE + NAND_BYTES_PER_SPARE) +
           NAND_BYTES_PER_PAGE,
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
        sprintf(filename, "%s/bank%d/%d.page", s->nand_path, bank, page);
        struct stat st = {0};
        if (!(nand_writable() && stat(filename, &st) == 0) &&
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
    sprintf(filename, "%s/bank%d/%d%s.page", s->nand_path,
            s->buffered_bank, s->buffered_page,
            nand_writable() ? "" : "_new");
    f = fopen(filename, "wb");
    if (f == NULL) { hw_error("Unable to read file!"); }
    nand_note_write(s->buffered_bank, s->buffered_page);
    fwrite(s->page_buffer, sizeof(char), NAND_BYTES_PER_PAGE, f);
    fwrite(s->page_spare_buffer, sizeof(char), NAND_BYTES_PER_SPARE, f);
    fclose(f);

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
