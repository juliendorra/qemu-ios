#include "hw/arm/ipod_touch_nand.h"
#include "qemu/bswap.h"

#define NAND_PACK_FILENAME "nand.pack"
#define NAND_PACK_MAGIC "IPODNAND"
#define NAND_PACK_VERSION 1
#define NAND_PACK_HEADER_SIZE 20

static int get_bank(ITNandState *s) {
    uint32_t bank_bitmap = (s->fmctrl0 >> 1) & 0xFF;
    for(int bank = 0; bank < NAND_NUM_BANKS; bank++) {
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

void nand_set_buffered_page(ITNandState *s, uint32_t page) {
    uint32_t bank = get_bank(s);
    if(bank == -1) {
        hw_error("Active bank not set while nand_read with page %d is called (reading multiple pages: %d)!", page, s->reading_multiple_pages);
    }

    if(bank != s->buffered_bank || page != s->buffered_page) {
        // refresh the buffered page
        char filename[200];
        sprintf(filename, "%s/bank%d/%d.page", s->nand_path, bank, page);
        struct stat st = {0};
        if (nand_read_packed_page(s, bank, page)) {
            /* The immutable base pack replaces the per-page open/read path. */
        }
        else if (stat(filename, &st) == -1) {
            // page storage does not exist - initialize an empty buffer
            memset(s->page_buffer, 0, NAND_BYTES_PER_PAGE);
            memset(s->page_spare_buffer, 0, NAND_BYTES_PER_SPARE);
            s->page_spare_buffer[0xA] = 0xFF; // make sure we add the FTL mark to an empty page
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
        // printf("Buffered bank: %d, page: %d\n", s->buffered_bank, s->buffered_page);
    }
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
                return NAND_CHIP_ID;
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
                    nand_set_buffered_page(s, page);
                    //printf("Reading page %d\n", page);

                    if(s->reading_spare) {
                        read_val = ((uint32_t *)s->page_spare_buffer)[(NAND_BYTES_PER_SPARE - s->fmdnum - 1) / 4];
                    } else {
                        read_val = ((uint32_t *)s->page_buffer)[(NAND_BYTES_PER_PAGE - s->fmdnum - 1) / 4];
                    }
                }
                s->fmdnum -= 4;
                return read_val;
            }

        case NAND_FMCSTAT:
            return (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12); // this indicates that everything is ready, including our eight banks
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

            //printf("Setting offset %d: %d\n", s->fmdnum, (NAND_BYTES_PER_PAGE - s->fmdnum) / 4);
            ((uint32_t *)s->page_buffer)[(NAND_BYTES_PER_PAGE - s->fmdnum) / 4] = val;
            s->fmdnum -= 4;

            if(s->fmdnum == 0) {
                // we're done!
                s->is_writing = false;

                // flush the page buffer to the disk
                //printf("Flushing page %d, bank %d\n", s->buffered_page, s->buffered_bank);
                qemu_mutex_lock(&s->lock);
                qemu_mutex_unlock(&s->lock);
                {
                    char filename[200];
                    sprintf(filename, "%s/bank%d/%d_new.page", s->nand_path, s->buffered_bank, s->buffered_page);
                    FILE *f = fopen(filename, "wb");
                    if (f == NULL) { hw_error("Unable to read file!"); }
                    fwrite(s->page_buffer, sizeof(char), NAND_BYTES_PER_PAGE, f);
                    fwrite(s->page_spare_buffer, sizeof(char), NAND_BYTES_PER_SPARE, f);
                    fclose(f);
                }
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

static void itnand_class_init(ObjectClass *oc, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->reset = itnand_reset;
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
