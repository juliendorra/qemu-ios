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

static void ipod_touch_adm_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    IPodTouchADMState *s = (IPodTouchADMState *)opaque;

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
                uint32_t cmd = adm_read_u32(
                    s, s->data2_sec_addr + 0x1104 + 0x24);
                // printf("Setting command: 0x%08x\n", cmd);
                // for(int i = 0; i < 20; i++) {
                //     printf("0x%08x ", buf[i]);
                // }
                // printf("\n");
                switch(cmd) {
                    case 0x200:
                        // read multiple pages simultaneously from the same bank
                        s->nand_state->reading_multiple_pages = true;
                        num_pages = adm_read_be16(
                            s, s->data2_sec_addr + 0x1104 + 0x28);
                        if (num_pages > ARRAY_SIZE(
                                s->nand_state->pages_to_read)) {
                            qemu_log_mask(LOG_GUEST_ERROR,
                                          "iPod ADM: invalid page count %u\n",
                                          num_pages);
                            break;
                        }
                        //printf("Reading %d pages at once, ", num_pages);

                        page = adm_read_be32(
                            s, s->data2_sec_addr + 0x1104 + 0x244);
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
                            s, s->data2_sec_addr + 0x1104 + 0x28);
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
                                s, s->data2_sec_addr + 0x1104 + 0x44);

                            page = adm_read_be32(
                                s, s->data2_sec_addr + 0x1104 + 0x244);
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
                                    s, s->data2_sec_addr + 0x1104 + 0x244 +
                                    4 * i);
                                bank = adm_read_u8(
                                    s, s->data2_sec_addr + 0x1104 + 0x44 + i);
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
                    case 0x500:
                        // writing a page
                        bank = adm_read_u8(
                            s, s->data2_sec_addr + 0x1104 + 0x44);
                        page = adm_read_be32(
                            s, s->data2_sec_addr + 0x1104 + 0x244);

                        // set the bank, page, and operation.
                        //printf("Activating bank for writing: %d, page: %d\n", bank, page);
                        set_bank(s->nand_state, bank);
                        nand_set_buffered_page(s->nand_state, page);
                        s->nand_state->fmdnum = NAND_BYTES_PER_PAGE;
                        s->nand_state->is_writing = true;
                        break;
                    default:
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
