#include "hw/arm/ipod_touch_adm.h"
#include "hw/core/qdev-properties.h"
#include "hw/arm/ipod_touch_nand.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/log.h"
#include "trace.h"

static void trace_root_read_request(uint32_t cmd, uint16_t count,
                                    const uint32_t *pages,
                                    const uint32_t *banks)
{
    bool relevant = false;

    for (uint16_t i = 0; i < count; i++) {
        if (pages[i] >= 25855 && pages[i] <= 25859) {
            relevant = true;
            break;
        }
    }
    if (!relevant) {
        return;
    }
    for (uint16_t i = 0; i < count; i++) {
        trace_itadm_root_read(cmd, count, i, banks[i], pages[i]);
    }
}

static uint8_t adm_read_u8(IPodTouchADMState *s, hwaddr addr)
{
    uint8_t value;

    address_space_read(&s->downstream_as, addr, MEMTXATTRS_UNSPECIFIED,
                       &value, sizeof(value));
    return value;
}

static uint16_t adm_read_be16(IPodTouchADMState *s, hwaddr addr)
{
    uint16_t value;

    address_space_read(&s->downstream_as, addr, MEMTXATTRS_UNSPECIFIED,
                       &value, sizeof(value));
    return be16_to_cpu(value);
}

static uint32_t adm_read_u32(IPodTouchADMState *s, hwaddr addr)
{
    uint32_t value;

    address_space_read(&s->downstream_as, addr, MEMTXATTRS_UNSPECIFIED,
                       &value, sizeof(value));
    return value;
}

static uint32_t adm_read_be32(IPodTouchADMState *s, hwaddr addr)
{
    return be32_to_cpu(adm_read_u32(s, addr));
}

static void adm_write_completion_records(IPodTouchADMState *s,
                                         uint16_t num_pages)
{
    uint8_t record[0xc] = { 0 };

    record[10] = 0xff;
    for (int i = 0; i < num_pages; i++) {
        address_space_write(&s->downstream_as,
                            s->data3_sec_addr + i * sizeof(record),
                            MEMTXATTRS_UNSPECIFIED, record, sizeof(record));
    }
}

static void set_bank(ITNandState *s, uint8_t activate_bank) {
    for(int bank = 0; bank < 8; bank++) {
        // clear bit, toggle if it is active
        s->fmctrl0 &= ~(1 << (bank + 1));
        if(bank == activate_bank) {
            s->fmctrl0 ^= 1 << (bank + 1);
        }
    }
}

static uint64_t ipod_touch_adm_read(void *opaque, hwaddr offset, unsigned size)
{
    //IPodTouchADMState *s = (IPodTouchADMState *)opaque;

    //fprintf(stderr, "s5l8900_adm_read(): offset = 0x%08x\n", offset);

    switch (offset) {
        case ADM_CTRL: // this seems to be the control register
            return 0x2; // this seems to indicate that the device is ready
        case ADM_CTRL2:
            return 0x10; // indicates that a upload event has been finished
        default:
            break;
    }

    return 0;
}

/*
 * IT_ADM_TRACE=1: every ADM register access, unconditionally. IT_NAND_TRACE
 * only reports the command word behind an ADM_CTRL2 == 0x2 write, so a
 * bootloader that drives this block differently is invisible to it -- which is
 * exactly the situation with iBoot-159 (iPhone OS 1.0/1.0.x), whose page reads
 * produce no IT_NAND_TRACE output at all. Use this to find out what a new
 * firmware actually writes before concluding anything about the NAND content.
 */
static void adm_trace_access(const char *dir, hwaddr offset, uint64_t value)
{
    static uint32_t seen_off[64], seen_val[64], counts[64], nseen;
    uint32_t i;

    if (!getenv("IT_ADM_TRACE")) {
        return;
    }
    for (i = 0; i < nseen; i++) {
        if (seen_off[i] == offset && seen_val[i] == (uint32_t)value) {
            break;
        }
    }
    if (i == nseen) {
        if (nseen == ARRAY_SIZE(seen_off)) {
            return;
        }
        seen_off[nseen] = offset;
        seen_val[nseen] = (uint32_t)value;
        nseen++;
    }
    if (++counts[i] <= 2 || counts[i] % 4096 == 0) {
        fprintf(stderr, "[ADM] %s offset 0x%02x value 0x%08x n=%u\n",
                dir, (unsigned)offset, (uint32_t)value, counts[i]);
    }
}

static void ipod_touch_adm_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    IPodTouchADMState *s = (IPodTouchADMState *)opaque;

    adm_trace_access("write", offset, value);

    switch(offset) {
        case ADM_CTRL:
            if(value == 0x3) {
                // some kind of start-up command?
                // write some bits to data2_sec_addr to indicate that the device is started
                uint32_t started = 0x50;
                uint32_t bank_ids[NAND_NUM_BANKS];

                address_space_write(&s->downstream_as, s->data2_sec_addr,
                                    MEMTXATTRS_UNSPECIFIED,
                                    (uint8_t *)&started, sizeof(started));

                // dunno, write some bytes to data4_sec_addr to indicate that the NAND banks are ready
                for(int i = 0; i < NAND_NUM_BANKS; i++) {
                    bank_ids[i] = i < s->nand_state->num_banks ?
                                  NAND_CHIP_ID : UINT32_MAX;
                }

                address_space_write(&s->downstream_as, s->data3_sec_addr,
                                    MEMTXATTRS_UNSPECIFIED,
                                    (uint8_t *)bank_ids, sizeof(bank_ids));
            }
            break;
        case ADM_CTRL2:
            if(value == 0x2) {
                // read the command and initialize the right device
                uint32_t page;
                uint16_t num_pages;
                uint8_t bank;
                /*
                 * The command block's offset within data2 belongs to the
                 * ADM/FMC firmware the kernel uploaded, not to the hardware:
                 * 1.1.x ("CalmADMFMCFirmware-17") uses +0x1104, 1.0/1.0.x
                 * ("CalmADMFMCFirmware-14") uses +0x0824. The two layouts are
                 * otherwise identical -- IT_ADM_DIFF shows the same
                 * 0x500/0x300/0x300/0x100 table at base+0x10 in both, which is
                 * how the second base was derived. Pick whichever holds a
                 * command.
                 */
                hwaddr cmdbase = s->data2_sec_addr + 0x1104;
                hwaddr page_off = 0x244;
                uint32_t cmd = adm_read_u32(s, cmdbase + 0x24);
                if (cmd == 0) {
                    hwaddr alt = s->data2_sec_addr + 0x0824;
                    uint32_t altcmd = adm_read_u32(s, alt + 0x24);
                    if (altcmd != 0) {
                        cmdbase = alt;
                        cmd = altcmd;
                        /* -14 keeps the page number 0x200 further out than
                         * -17 does; measured with IT_ADM_DIFF, which shows it
                         * incrementing big-endian at data2+0x0c68 as the
                         * kernel scans. */
                        page_off = 0x444;
                    }
                }
                /* IT_NAND_TRACE=1: which NAND operations the guest actually
                 * issues. 0x200/0x300 are reads, 0x500 is a page WRITE. If a
                 * board never emits 0x500 its storage is read-only in
                 * practice, whatever the mount flags say. */
                if (getenv("IT_NAND_TRACE")) {
                    static uint32_t seen[8], counts[8], nseen;
                    uint32_t i;
                    for (i = 0; i < nseen && seen[i] != cmd; i++) {
                    }
                    if (i == nseen && nseen < 8) {
                        seen[nseen++] = cmd;
                    }
                    if (i < 8) {
                        counts[i]++;
                        if (counts[i] <= 3 || counts[i] % 512 == 0) {
                            fprintf(stderr, "[ADM] nand cmd 0x%x n=%u\n",
                                    cmd, counts[i]);
                        }
                    }
                }
                /*
                 * IT_ADM_DUMP=1: where in the data2 section does this firmware
                 * keep its command block?
                 *
                 * The offset is NOT a property of the hardware -- it belongs to
                 * the ADM/FMC firmware blob the kernel uploads, and different
                 * iPhone OS releases upload different blobs. 1.1.x loads
                 * "CalmADMFMCFirmware-17" and puts the command word at
                 * data2 + 0x1104 + 0x24, which is what the decoding below
                 * assumes. 1.0/1.0.x loads "CalmADMFMCFirmware-14", whose block
                 * sits at data2 + 0x840 instead, so that fixed offset reads
                 * zeroes and every command decodes as 0x0.
                 */
                if (getenv("IT_ADM_DUMP")) {
                    static unsigned n;
                    if (n++ < 3) {
                        uint8_t w[0x40];
                        fprintf(stderr, "[ADM-SCAN] data2=0x%08x\n",
                                (uint32_t)s->data2_sec_addr);
                        for (hwaddr off = 0; off < 0x4000; off += 0x40) {
                            bool nz = false;
                            address_space_read(&s->downstream_as,
                                               s->data2_sec_addr + off,
                                               MEMTXATTRS_UNSPECIFIED, w,
                                               sizeof(w));
                            for (int k = 0; k < (int)sizeof(w); k++) {
                                if (w[k]) { nz = true; break; }
                            }
                            if (nz) {
                                fprintf(stderr, "   +%04x:", (unsigned)off);
                                for (int k = 0; k < 32; k++) {
                                    fprintf(stderr, " %02x", w[k]);
                                }
                                fprintf(stderr, "\n");
                            }
                        }
                    }
                }
                /*
                 * IT_ADM_FIELDS=1: dump the firmware-14 command window across
                 * consecutive commands so the varying words (page, bank, count)
                 * can be identified by diffing, instead of decoding the
                 * uploaded CalmRISC blob.
                 */
                if (getenv("IT_ADM_FIELDS")) {
                    static unsigned n;
                    if (n++ < 10) {
                        uint8_t w[0x40];
                        struct { const char *nm; hwaddr base; } secs[] = {
                            { "data1", s->data1_sec_addr },
                            { "data3", s->data3_sec_addr },
                        };
                        fprintf(stderr, "[ADM-F] #%u\n", n);
                        for (unsigned si = 0; si < ARRAY_SIZE(secs); si++) {
                            for (hwaddr off = 0; off < 0x1000; off += 0x20) {
                                bool nz = false;
                                address_space_read(&s->downstream_as,
                                                   secs[si].base + off,
                                                   MEMTXATTRS_UNSPECIFIED, w,
                                                   0x20);
                                for (int k = 0; k < 0x20; k++) {
                                    if (w[k]) { nz = true; break; }
                                }
                                if (!nz) { continue; }
                                fprintf(stderr, "   %s+%04x:", secs[si].nm,
                                        (unsigned)off);
                                for (int k = 0; k < 0x20; k++) {
                                    fprintf(stderr, " %02x", w[k]);
                                }
                                fprintf(stderr, "\n");
                            }
                        }
                    }
                }
                /*
                 * IT_ADM_DIFF=1: which words does the guest change between one
                 * engine kick and the next?
                 *
                 * The command-block offset is a property of the ADM/FMC
                 * firmware the kernel uploads, not of the hardware: 1.1.x
                 * ("CalmADMFMCFirmware-17") uses data2 + 0x1104, 1.0/1.0.x
                 * ("CalmADMFMCFirmware-14") uses something else, and scanning
                 * for non-zero data only finds static tables. Whatever the
                 * guest WRITES just before kicking the engine is the command
                 * block, wherever it lives -- so snapshot the sections and
                 * diff them here rather than guessing an offset.
                 */
                if (getenv("IT_ADM_DIFF")) {
                    static uint8_t *prev[3];
                    static unsigned n;
                    /* data2+0x1c68 holds a BIG-ENDIAN pointer to a further
                     * buffer (and +0x1c6c its length): the per-command ring the
                     * counter at data2+0xc68 indexes into. Diff it too. */
                    uint32_t ringp = be32_to_cpu(adm_read_u32(
                        s, s->data2_sec_addr + 0x1c68));
                    const struct { const char *nm; hwaddr base; } secs[3] = {
                        { "data2", s->data2_sec_addr },
                        { "ring",  ringp },
                        { "data3", s->data3_sec_addr },
                    };
                    const size_t span = 0x2000;

                    unsigned first = (unsigned)strtoul(getenv("IT_ADM_DIFF"),
                                                       NULL, 0);
                    if (first < 2) {
                        first = 1;
                    }
                    n++;
                    if (n >= first && n < first + 10) {
                        fprintf(stderr, "[ADM-DIFF] kick #%u\n", n);
                        for (int si = 0; si < 3; si++) {
                            uint8_t *cur = g_malloc0(span);
                            address_space_read(&s->downstream_as,
                                               secs[si].base,
                                               MEMTXATTRS_UNSPECIFIED, cur,
                                               span);
                            if (prev[si]) {
                                unsigned shown = 0;
                                for (size_t off = 0; off + 4 <= span;
                                     off += 4) {
                                    uint32_t a = ldl_le_p(prev[si] + off);
                                    uint32_t b = ldl_le_p(cur + off);
                                    if (a == b) {
                                        continue;
                                    }
                                    if (shown++ < 12) {
                                        fprintf(stderr,
                                                "   %s+%04x: %08x -> %08x\n",
                                                secs[si].nm, (unsigned)off,
                                                a, b);
                                    }
                                }
                                if (shown > 12) {
                                    fprintf(stderr,
                                            "   %s: +%u more changed words\n",
                                            secs[si].nm, shown - 12);
                                }
                                g_free(prev[si]);
                            }
                            prev[si] = cur;
                        }
                    }
                }
                // printf("Setting command: 0x%08x\n", cmd);
                // for(int i = 0; i < 20; i++) {
                //     printf("0x%08x ", buf[i]);
                // }
                // printf("\n");
                /*
                 * IT_ADM_SEQ=<limit>: every engine kick in order, with the
                 * fields that identify it.
                 *
                 * The model implements no erase, and IT_NAND_CMDS shows the
                 * guest issuing no erase OPCODE either -- yet its FTL happily
                 * writes a fresh context over pages that still hold the old
                 * one, which is only sane if it believes the block was erased.
                 * If an erase is being issued, it is one of the ADM commands
                 * this model already accepts, so log them all in order and see
                 * what surrounds a context write.
                 */
                if (getenv("IT_ADM_SEQ")) {
                    static unsigned n;
                    unsigned limit = (unsigned)strtoul(getenv("IT_ADM_SEQ"),
                                                       NULL, 0);
                    if (limit < 2) {
                        limit = 100000;
                    }
                    if (n++ < limit) {
                        fprintf(stderr,
                                "[ADM-SEQ] #%u cmd 0x%x count=%u bank=%u "
                                "page=%u\n", n, cmd,
                                adm_read_be16(s, cmdbase + 0x28),
                                adm_read_u8(s, cmdbase + 0x44),
                                adm_read_be32(s, cmdbase + page_off));
                    }
                }
                switch(cmd) {
                    case 0x200:
                        // read multiple pages simultaneously from the same bank
                        s->nand_state->reading_multiple_pages = true;
                        num_pages = adm_read_be16(
                            s, cmdbase + 0x28);
                        if (num_pages > ARRAY_SIZE(
                                s->nand_state->pages_to_read)) {
                            qemu_log_mask(LOG_GUEST_ERROR,
                                          "iPod ADM: invalid page count %u\n",
                                          num_pages);
                            break;
                        }
                        //printf("Reading %d pages at once, ", num_pages);

                        page = adm_read_be32(
                            s, cmdbase + page_off);
                        //printf("starting with page %d\n", page);

                        /*
                         * The transfer is striped across the banks present on
                         * the board.  N45AP has eight; M68AP has four.  The
                         * old fixed-eight loop assigned no entries at all for
                         * a four-page M68AP request, returning stale/zero data
                         * to the vnode pager even though the HFS image itself
                         * was correct.
                         */
                        for (int i = 0; i < num_pages; i++) {
                            s->nand_state->pages_to_read[i] =
                                page + i / s->nand_state->num_banks;
                            s->nand_state->banks_to_read[i] =
                                i % s->nand_state->num_banks;
                        }
                        trace_root_read_request(cmd, num_pages,
                                                s->nand_state->pages_to_read,
                                                s->nand_state->banks_to_read);

                        s->nand_state->fmdnum = (num_pages * 0x800);
                        s->nand_state->cur_bank_reading = -1;

                        adm_write_completion_records(s, num_pages);
                        
                        break;
                    case 0x300:
                        // seems to be the NAND read command, read the page(s) + bank and instruct the flash device
                        s->nand_state->reading_multiple_pages = false;
                        num_pages = adm_read_be16(
                            s, cmdbase + 0x28);
                        if (num_pages > ARRAY_SIZE(
                                s->nand_state->pages_to_read)) {
                            qemu_log_mask(LOG_GUEST_ERROR,
                                          "iPod ADM: invalid page count %u\n",
                                          num_pages);
                            break;
                        }
                        if(num_pages == 1) {
                            // TODO this can probably be refactored to re-use the logic to read multiple pages!
                            bank = adm_read_u8(
                                s, cmdbase + 0x44);

                            page = adm_read_be32(
                                s, cmdbase + page_off);
                            if (getenv("IT_ADM_PAGES")) {
                                static unsigned n;
                                if (n++ < 4000) {
                                    fprintf(stderr, "[ADM-PAGE] bank%u page %u\n",
                                            bank, page);
                                }
                            }
                            if (page >= 25855 && page <= 25859) {
                                trace_itadm_root_read(cmd, num_pages, 0, bank,
                                                     page);
                            }
                            //printf("Reading single page: %d (bank: %d)\n", page, bank);

                            // set the bank, page, and operation.
                            set_bank(s->nand_state, bank);
                            memory_region_dispatch_write(&s->nand_state->iomem, NAND_FMDNUM, 0x800 - 1, MO_32, MEMTXATTRS_UNSPECIFIED);
                            memory_region_dispatch_write(&s->nand_state->iomem, NAND_FMADDR0, page << 16, MO_32, MEMTXATTRS_UNSPECIFIED);
                            memory_region_dispatch_write(&s->nand_state->iomem, NAND_FMADDR1, (page >> 16) & 0xFF, MO_32, MEMTXATTRS_UNSPECIFIED);
                            memory_region_dispatch_write(&s->nand_state->iomem, NAND_CMD, NAND_CMD_READ, MO_32, MEMTXATTRS_UNSPECIFIED);

                            // write the spare of the page to the 3rd data section
                            nand_set_buffered_page(s->nand_state, page);
                            address_space_rw(&s->downstream_as, s->data3_sec_addr, MEMTXATTRS_UNSPECIFIED, (uint8_t *)s->nand_state->page_spare_buffer, NAND_BYTES_PER_SPARE, 1);
                        }
                        else if(num_pages > 1) {
                            // read scattered pages
                            // printf("Reading %d scattered pages\n", num_pages);
                            s->nand_state->reading_multiple_pages = true;
                            for(int i = 0; i < num_pages; i++) {
                                page = adm_read_be32(
                                    s, cmdbase + page_off +
                                    4 * i);
                                bank = adm_read_u8(
                                    s, cmdbase + 0x44 + i);
                                // printf("Page: %d, bank: %d\n", page, bank);

                                s->nand_state->pages_to_read[i] = page;
                                s->nand_state->banks_to_read[i] = bank;
                            }
                            trace_root_read_request(
                                cmd, num_pages,
                                s->nand_state->pages_to_read,
                                s->nand_state->banks_to_read);

                            s->nand_state->fmdnum = (num_pages * 0x800);
                            s->nand_state->cur_bank_reading = -1;

                            adm_write_completion_records(s, num_pages);
                        }
                        break;
                    case 0x400:
                        /*
                         * WriteMultiple -- the write counterpart of 0x200's
                         * read-multiple, and NOT the flush/standby it looks
                         * like from where it appears in the log.
                         *
                         * It shows up immediately before every sleep because
                         * that is when the FTL commits its dirty pages, so
                         * dropping it (which this model did, as "Unrecognized
                         * ADM command: 1024") threw away exactly the writes
                         * the guest cared most about keeping. The FIL
                         * advertises ReadMultiple / ReadScattered /
                         * WriteMultiple at init; 0x200 / 0x300 / 0x400 are
                         * those three. Both firmware blobs issue it -- this
                         * was never a 1.0-only gap.
                         *
                         * Measured shape (IT_ADM_UNK / IT_NAND_WRITE, iPhone
                         * OS 1.0, blob -14; 1.1.4 with blob -17 is identical
                         * at its own base):
                         *   +0x28  page count (4, 0xc, 0x10 seen)
                         *   page_off  the BASE page, repeated once per bank
                         *   +0x44  the bank bytes, 00 01 02 03
                         *   data3  one 0xc-byte spare record per page
                         *
                         * The descriptor carries only num_banks page entries,
                         * not `count` of them: on a 12- and a 16-page command
                         * the first four words differed while entries 4..7
                         * stayed byte-identical, i.e. those are stale from an
                         * earlier command. Reading them as a per-page list
                         * sent every entry past the eighth to bank0/page0 --
                         * the FIL signature page. So the layout is 0x200's:
                         * bank = i % num_banks, page = base + i / num_banks.
                         * The page sequence confirms it -- a 16-page write at
                         * base 26514 is followed by one at 26522, which under
                         * a page-per-entry reading would have rewritten pages
                         * the same command had just programmed.
                         */
                        num_pages = adm_read_be16(s, cmdbase + 0x28);
                        if (num_pages == 0 ||
                            num_pages > ARRAY_SIZE(
                                s->nand_state->pages_to_write)) {
                            qemu_log_mask(LOG_GUEST_ERROR,
                                          "iPod ADM: invalid write page count "
                                          "%u\n", num_pages);
                            break;
                        }
                        page = adm_read_be32(s, cmdbase + page_off);
                        if (page == 0) {
                            /*
                             * Page 0 of bank 0 holds the FIL signature the
                             * whole WMR stack keys off. The FTL never targets
                             * it, so a zero base means the descriptor was not
                             * understood -- drop the command rather than
                             * program over the signature.
                             */
                            qemu_log_mask(LOG_GUEST_ERROR,
                                          "iPod ADM: write-multiple with base "
                                          "page 0, ignored\n");
                            break;
                        }
                        for (int i = 0; i < num_pages; i++) {
                            s->nand_state->pages_to_write[i] =
                                page + i / s->nand_state->num_banks;
                            s->nand_state->banks_to_write[i] =
                                i % s->nand_state->num_banks;
                            /*
                             * Copy the spares NOW: the page data itself
                             * arrives later through the FIFO, and data3 is the
                             * guest's own buffer to reuse in the meantime.
                             */
                            address_space_read(
                                &s->downstream_as,
                                s->data3_sec_addr +
                                    i * NAND_ADM_SPARE_RECORD,
                                MEMTXATTRS_UNSPECIFIED,
                                s->nand_state->spares_to_write[i],
                                NAND_ADM_SPARE_RECORD);
                        }
                        if (getenv("IT_NAND_WRITE")) {
                            fprintf(stderr,
                                    "[ADM-WRITEMULTI] %u pages from bank0/page "
                                    "%u across %u banks\n", num_pages, page,
                                    s->nand_state->num_banks);
                        }
                        nand_begin_multi_write(s->nand_state, num_pages);
                        break;
                    case 0x100:
                        /*
                         * Bank inventory, issued once per boot right after the
                         * firmware upload and before any page traffic. Nothing
                         * to do: it carries no page count, no bank and no page
                         * list (measured with IT_ADM_UNK), and the chip-ID
                         * table it asks about is already in data3 -- the model
                         * puts it there on the ADM_CTRL == 3 start-up. 1.1.x
                         * issues it too and has always proceeded past it with
                         * this model doing nothing, which is the evidence that
                         * "nothing" is the right answer rather than a gap.
                         */
                        break;
                    case 0x500:
                        // writing a page
                        bank = adm_read_u8(
                            s, cmdbase + 0x44);
                        page = adm_read_be32(
                            s, cmdbase + page_off);

                        // set the bank, page, and operation.
                        //printf("Activating bank for writing: %d, page: %d\n", bank, page);
                        set_bank(s->nand_state, bank);
                        nand_set_buffered_page(s->nand_state, page);
                        /*
                         * Take the SPARE from the guest, exactly where the
                         * read path hands it back (data3). Without this the
                         * page was written with whatever spare the previous
                         * READ had left in the buffer, i.e. every guest write
                         * carried stale FTL metadata -- the logical page
                         * number, status and version belong to a different
                         * page, so the FTL cannot find its own data again.
                         * Measured consequence: SQLite's journal pages reach
                         * the NAND, yet the database it just wrote reads back
                         * as empty ("no such table"), and daemons that create
                         * state retry forever (T6).
                         */
                        /*
                         * Take the RECORD, not a spare-sized block. The guest
                         * writes one 0xc-byte record here (the same shape the
                         * read completions and WriteMultiple use); everything
                         * past it in data3 belongs to the model -- at start-up
                         * this is where the bank chip-ID table goes. Copying
                         * NAND_BYTES_PER_SPARE bytes stamped 52 bytes of our
                         * own scratch, NAND_CHIP_ID included, into the spare of
                         * every singly-written page, which is not something any
                         * guest ever put there. The pristine image's own
                         * convention is record-then-zeroes, and that is what
                         * the multi-page path already writes.
                         */
                        memset(s->nand_state->page_spare_buffer, 0,
                               NAND_BYTES_PER_SPARE);
                        address_space_read(&s->downstream_as,
                                           s->data3_sec_addr,
                                           MEMTXATTRS_UNSPECIFIED,
                                           s->nand_state->page_spare_buffer,
                                           NAND_ADM_SPARE_RECORD);
                        s->nand_state->fmdnum = NAND_BYTES_PER_PAGE;
                        s->nand_state->words_this_page = 0;
                        s->nand_state->is_writing = true;
                        s->nand_state->writing_multiple_pages = false;
                        break;
                    default:
                        /*
                         * IT_ADM_UNK=1: dump the command window for a command
                         * this model does not implement.
                         *
                         * IT_ADM_DIFF needs the kick index up front, which is
                         * no use for 0x400 -- it is issued immediately before
                         * a sleep, hundreds of kicks in and at no fixed count.
                         * Trigger on the command itself instead, and show the
                         * whole descriptor (base..base+0x60), the page-number
                         * window and the head of data3, so it is visible which
                         * fields the guest bothered to populate.
                         */
                        if (getenv("IT_ADM_UNK")) {
                            static unsigned n;
                            if (n++ < 8) {
                                uint8_t w[0x60];
                                fprintf(stderr,
                                        "[ADM-UNK] #%u cmd 0x%x cmdbase=%s "
                                        "count=0x%04x bank=0x%02x\n",
                                        n, cmd,
                                        page_off == 0x444 ? "fw14" : "fw17",
                                        adm_read_be16(s, cmdbase + 0x28),
                                        adm_read_u8(s, cmdbase + 0x44));
                                address_space_read(&s->downstream_as, cmdbase,
                                                   MEMTXATTRS_UNSPECIFIED, w,
                                                   sizeof(w));
                                for (int r = 0; r < 6; r++) {
                                    fprintf(stderr, "   cmd+%02x:", r * 16);
                                    for (int k = 0; k < 16; k++) {
                                        fprintf(stderr, " %02x",
                                                w[r * 16 + k]);
                                    }
                                    fprintf(stderr, "\n");
                                }
                                address_space_read(&s->downstream_as,
                                                   cmdbase + page_off,
                                                   MEMTXATTRS_UNSPECIFIED, w,
                                                   0x20);
                                fprintf(stderr, "   page+00:");
                                for (int k = 0; k < 0x20; k++) {
                                    fprintf(stderr, " %02x", w[k]);
                                }
                                fprintf(stderr, "\n");
                                address_space_read(&s->downstream_as,
                                                   s->data3_sec_addr,
                                                   MEMTXATTRS_UNSPECIFIED, w,
                                                   0x20);
                                fprintf(stderr, "   data3 +00:");
                                for (int k = 0; k < 0x20; k++) {
                                    fprintf(stderr, " %02x", w[k]);
                                }
                                fprintf(stderr, "\n");
                            }
                        }
                        printf("Unrecognized ADM command: %d\n", cmd);
                        break;
                }
                qemu_irq_raise(s->irq);
            }
            if((value & 0x2) == 0) {
                qemu_irq_lower(s->irq);
            }
            break;
        case ADM_CODE_SEC_ADDR:
            s->code_sec_addr = value;
            break;
        case ADM_DATA1_SEC_ADDR:
            s->data1_sec_addr = value;
            break;
        case ADM_DATA2_SEC_ADDR:
            s->data2_sec_addr = value;
            break;
        case ADM_DATA3_SEC_ADDR:
            s->data3_sec_addr = value;
            break;
        default:
            break;
    }
}

static const MemoryRegionOps ipod_touch_adm_ops = {
    .read = ipod_touch_adm_read,
    .write = ipod_touch_adm_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_adm_realize(DeviceState *dev, Error **errp)
{
    IPodTouchADMState *s = IPOD_TOUCH_ADM(dev);

    if (!s->downstream) {
        error_setg(errp, "ADM 'downstream' link not set");
        return;
    }

    address_space_init(&s->downstream_as, s->downstream, "adm-downstream");
}

static const Property adm_properties[] = {
    DEFINE_PROP_LINK("downstream", IPodTouchADMState, downstream,
                     TYPE_MEMORY_REGION, MemoryRegion *),
};

static void ipod_touch_adm_init(Object *obj)
{
    IPodTouchADMState *s = IPOD_TOUCH_ADM(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_adm_ops, s, TYPE_IPOD_TOUCH_ADM, 0x1000);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void ipod_touch_adm_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = ipod_touch_adm_realize;
    device_class_set_props(dc, adm_properties);
}

static const TypeInfo ipod_touch_adm_type_info = {
    .name = TYPE_IPOD_TOUCH_ADM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchADMState),
    .instance_init = ipod_touch_adm_init,
    .class_init = ipod_touch_adm_class_init,
};

static void ipod_touch_adm_register_types(void)
{
    type_register_static(&ipod_touch_adm_type_info);
}

type_init(ipod_touch_adm_register_types)
