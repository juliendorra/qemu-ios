#include "hw/arm/ipod_touch_nand_ecc.h"
#include "exec/cpu-common.h"
#include "qemu/bswap.h"

static uint64_t itnand_ecc_read(void *opaque, hwaddr addr, unsigned size)
{
    ITNandECCState *s = (ITNandECCState *) opaque;
    //fprintf(stderr, "%s: reading from 0x%08x\n", __func__, addr);

    switch (addr) {
        case NANDECC_STATUS:
            return 0; // TODO: for now, we assume that all ECC operations are successful
        default:
            break;
    }
    return 0;
}

/*
 * The engine is a DMA path, not just an interrupt source.
 *
 * iBoot-204 never uses it -- it moves pages with the ADM -- so for years this
 * model could get away with raising an IRQ and moving no bytes. iBoot-159
 * (iPhone OS 1.0/1.0.x) does the opposite: it makes NO ADM accesses at all,
 * reads the page into the controller through FMFIFO, then programs this block
 * with the destination address (NANDECC_DATA) and starts it. With no data path
 * here, the destination stayed zero and the WMR signature scan compared
 * 0x00000000 against the 0x43303030 it was looking for -- reporting "no
 * signature or no production format" on a NAND that was correct all along.
 *
 * Addresses arrive with bit 31 set (the uncached view of the same memory); the
 * aliases in ipod_touch_memory_setup() back those windows, so the address can
 * be used as-is.
 */
static void itnand_ecc_start(ITNandECCState *s)
{
    ITNandState *nand = s->nand_state;
    unsigned sectors;

    if (nand == NULL || s->data_addr == 0) {
        return;
    }

    /*
     * NANDECC_SETUP bits [1:0] hold (sector count - 1); a sector is 512 bytes.
     * The firmware issues TWO transfers per page and the count is what tells
     * them apart:
     *
     *   4 sectors (setup & 3 == 3) -> the 2048-byte main page
     *   1 sector  (setup & 3 == 0) -> the 64-byte spare/metadata area
     *
     * Copying the main page for both is what kept _LoadVFLCxt failing: it
     * identifies its context page by spare[8] == 0 && spare[9] == 0x80, and
     * with the spare transfer delivering page data instead, those bytes were
     * always zero. Bit 2 of setup is set on the paths that also want ECC
     * correction; it does not select the region.
     */
    sectors = (s->setup & 3) + 1;
    if (sectors >= 4) {
        cpu_physical_memory_write(s->data_addr, nand->page_buffer,
                                  NAND_BYTES_PER_PAGE);
    } else {
        cpu_physical_memory_write(s->data_addr, nand->page_spare_buffer,
                                  NAND_BYTES_PER_SPARE);
    }

    if (getenv("IT_ECC_TRACE")) {
        static unsigned n;
        if (n++ < 60) {
            fprintf(stderr, "[ECC] bank%u/%u data=0x%08x setup=0x%x "
                    "sectors=%u spare[8,9]=%02x %02x\n",
                    nand->buffered_bank, nand->buffered_page, s->data_addr,
                    s->setup, sectors, nand->page_spare_buffer[8],
                    nand->page_spare_buffer[9]);
        }
    }
}

static void itnand_ecc_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    ITNandECCState *s = (ITNandECCState *) opaque;

    switch(addr) {
        case NANDECC_DATA:
            s->data_addr = val;
            break;
        case NANDECC_ECC:
            s->ecc_addr = val;
            break;
        case NANDECC_SETUP:
            s->setup = val;
            break;
        case NANDECC_START:
            itnand_ecc_start(s);
            qemu_irq_raise(s->irq);
            break;
        case NANDECC_CLEARINT:
            qemu_irq_lower(s->irq);
            break;
        default:
            break;
    }
}

static const MemoryRegionOps nand_ecc_ops = {
    .read = itnand_ecc_read,
    .write = itnand_ecc_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void itnand_ecc_init(Object *obj)
{
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    ITNandECCState *s = ITNANDECC(obj);

    memory_region_init_io(&s->iomem, OBJECT(s), &nand_ecc_ops, s, "nandecc", 0x100);
    sysbus_init_irq(sbd, &s->irq);
}

static void itnand_ecc_reset(DeviceState *d)
{
    ITNandECCState *s = (ITNandECCState *) d;

    s->data_addr = 0;
    s->ecc_addr = 0;
    s->status = 0;
    s->setup = 0;
}

static void itnand_ecc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    device_class_set_legacy_reset(dc, itnand_ecc_reset);
}

static const TypeInfo itnand_ecc_info = {
    .name          = TYPE_ITNANDECC,
    .parent        = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(ITNandECCState),
    .instance_init = itnand_ecc_init,
    .class_init    = itnand_ecc_class_init,
};

static void itnand_ecc_register_types(void)
{
    type_register_static(&itnand_ecc_info);
}

type_init(itnand_ecc_register_types)
