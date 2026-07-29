#ifndef HW_ARM_IPOD_TOUCH_NAND_H
#define HW_ARM_IPOD_TOUCH_NAND_H

#include "qemu/osdep.h"
#include "hw/core/platform-bus.h"
#include "hw/core/irq.h"
#include "qemu/lockable.h"
#include "hw/arm/ipod_touch_nand_pack.h"

#define NAND_NUM_BANKS 8
#define NAND_BYTES_PER_PAGE 2048
#define NAND_BYTES_PER_SPARE 64

/*
 * The ADM hands the spare over in 12-byte records, one per page, packed in the
 * data3 section -- the same 0xc-byte record the model writes back as a read
 * completion (logical page, status, and the 0xff FTL mark at byte 10). The
 * on-media spare area is 64 bytes; everything past the record is padding.
 */
#define NAND_ADM_SPARE_RECORD 0xc

#define NAND_CHIP_ID 0xA514D3AD

#define NAND_FMCTRL0  0x0
#define NAND_FMCTRL1  0x4
#define NAND_CMD      0x8
#define NAND_FMADDR0  0xC
#define NAND_FMADDR1  0x10
#define NAND_FMANUM   0x2C
#define NAND_FMDNUM   0x30
#define NAND_FMCSTAT  0x48
#define NAND_FMFIFO   0x80
#define NAND_RSCTRL   0x100

#define NAND_CMD_ID  0x90
#define NAND_CMD_READ 0x30
#define NAND_CMD_READSTATUS 0x70

#define FILESYSTEM_START_VPN 206851
#define FILESYSTEM_NUM_PAGES 132854

#define TYPE_ITNAND "itnand"
OBJECT_DECLARE_SIMPLE_TYPE(ITNandState, ITNAND)

typedef struct ITNandState {
    SysBusDevice busdev;
    MemoryRegion iomem;
    uint32_t fmctrl0;
    uint32_t fmctrl1;
    uint32_t fmaddr0;
    uint32_t fmaddr1;
    uint32_t fmanum;
    uint32_t fmdnum;
	uint32_t rsctrl;
	uint32_t cmd;
	uint8_t reading_spare;
    qemu_irq irq;

    uint8_t *page_buffer;
    uint8_t *page_spare_buffer;
    uint32_t buffered_bank;
    uint32_t buffered_page;
    bool reading_multiple_pages;
    uint32_t cur_bank_reading;
    uint32_t banks_to_read[512]; // used when in multiple page read mode
    uint32_t pages_to_read[512]; // used when in multiple page read mode
    bool is_writing;
    /* FIFO words accepted since the current page's write began. A page that
     * flushes with fewer than a full page of words was never delivered -- the
     * model would be inventing its contents. */
    uint32_t words_this_page;
    /*
     * Multi-page write (ADM command 0x400, the WriteMultiple the FIL
     * advertises). Like the multi-page READ above, the guest primes one
     * descriptor and then streams every page through the single FIFO, so the
     * target bank/page and the spare have to be swapped in at each 2 KiB
     * boundary rather than taken from the FMADDR registers.
     */
    bool writing_multiple_pages;
    uint32_t cur_page_writing;
    uint32_t num_pages_writing;
    uint32_t banks_to_write[512];
    uint32_t pages_to_write[512];
    uint8_t spares_to_write[512][NAND_ADM_SPARE_RECORD];
    QemuMutex lock;
    char *nand_path;
    bool pack_checked;
    GMappedFile *pack_file;
    ITNandPack pack;
    uint8_t last_spare_type;
    uint8_t num_banks;
} ITNandState;

/*
 * Serve pack records from a chunk cache instead of the mapped nand.pack.
 * Call before machine init; the model then maps "nand.pack.idx" (header +
 * index) and asks `fetch` for each chunk. Browser-only -- native leaves this
 * unset and keeps the mapped-file path.
 */
void it_nand_set_chunk_source(uint32_t pages_per_chunk, ITNandChunkFetch fetch,
                              void *opaque);

#ifdef EMSCRIPTEN
/* Install the browser chunk source if <nand_path> holds a chunked NAND
 * (chunk-config.txt + chunk-hashes.bin). See hw/arm/ipod_touch_nand_chunks.c. */
bool it_nand_chunks_init(const char *nand_path);
#endif

void nand_set_buffered_page(ITNandState *s, uint32_t page);
void nand_begin_multi_write(ITNandState *s, uint32_t num_pages);

#endif
