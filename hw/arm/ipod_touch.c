#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/error-report.h"
#include "qemu/crc32c.h"
#include "hw/arm/boot.h"
#include "hw/arm/machines-qom.h"
#include "system/address-spaces.h"
#include "hw/misc/unimp.h"
#include "hw/core/irq.h"
#include "system/reset.h"
#include "system/runstate.h"
#include "system/system.h"
#include "hw/core/platform-bus.h"
#include "hw/block/flash.h"
#include "hw/core/qdev-clock.h"
#include "hw/arm/ipod_touch.h"
#include "hw/arm/exynos4210.h"
#include "hw/dma/pl080.h"
#include "chardev/char.h"
#include "ui/input.h"
#include <openssl/aes.h>

// Global pointer to machine state for wake assist timer access from key handler
IPodTouchMachineState *g_ipod_touch_nms = NULL;

static uint32_t ipod_touch_retained_crc(void)
{
    const size_t chunk_size = 1024 * 1024;
    uint8_t *chunk = g_malloc(chunk_size);
    uint32_t crc = 0;

    for (hwaddr offset = 0; offset < 0x8000000; offset += chunk_size) {
        cpu_physical_memory_read(RAM_MEM_BASE + offset, chunk, chunk_size);
        crc = crc32c(crc, chunk, chunk_size);
    }
    g_free(chunk);
    return crc;
}

void ipod_touch_prepare_retained_wake(void)
{
    if (!g_ipod_touch_nms) {
        return;
    }

    g_ipod_touch_nms->retained_wake_pending = true;

    /* A full 128 MiB physical-memory CRC is useful validation, but doing it
     * twice in every wake path adds seconds of host latency. Keep it as an
     * explicit regression-test mode instead of changing normal timing. */
    if (g_getenv("IPOD_TOUCH_VALIDATE_RETAINED_RAM")) {
        g_ipod_touch_nms->retained_crc_before_reset =
            ipod_touch_retained_crc();
        g_ipod_touch_nms->retained_crc_valid = true;
        fprintf(stderr,
                "[WAKE] Retained LPDDR CRC32C before AP reset: 0x%08x\n",
                g_ipod_touch_nms->retained_crc_before_reset);
    }
}

static uint64_t tvout_workaround_read(void *opaque, hwaddr addr, unsigned size)
{
    return 0;
}

static void tvout_workaround_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{

}

static const MemoryRegionOps tvout_workaround_ops = {
    .read = tvout_workaround_read,
    .write = tvout_workaround_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static MemoryRegion *allocate_ram(MemoryRegion *top, const char *name,
                                  uint32_t addr, uint32_t size)
{
    MemoryRegion *sec = g_new(MemoryRegion, 1);

    memory_region_init_ram(sec, NULL, name, size, &error_fatal);
    memory_region_add_subregion(top, addr, sec);
    return sec;
}

static uint32_t align_64k_high(uint32_t addr)
{
    return (addr + 0xffffull) & ~0xffffull;
}

static void ipod_touch_cpu_setup(MachineState *machine, MemoryRegion **sysmem, ARMCPU **cpu, AddressSpace **nsas)
{
    Object *cpuobj = object_new(machine->cpu_type);
    *cpu = ARM_CPU(cpuobj);
    CPUState *cs = CPU(*cpu);

    *sysmem = get_system_memory();

    object_property_set_link(cpuobj, "memory", OBJECT(*sysmem), &error_abort);

    object_property_set_bool(cpuobj, "has_el3", false, NULL);

    object_property_set_bool(cpuobj, "has_el2", false, NULL);

    object_property_set_bool(cpuobj, "realized", true, &error_fatal);

    *nsas = cpu_get_address_space(cs, ARMASIdx_NS);

    object_unref(cpuobj);
}

static void ipod_touch_install_8900_ops(void)
{
    const uint32_t verify_stub[] = {
        0xe3b00001, /* MOVS R0, #1 */
        0xe12fff1e, /* BX LR */
    };
    const uint32_t decrypt_stub[] = {
        0xe59f1100, /* LDR R1, [PC, #0x100] */
        0xe5810000, /* STR R0, [R1] */
        0xe3b00001, /* MOVS R0, #1 */
        0xe12fff1e, /* BX LR */
    };
    const uint32_t engine_base = ENGINE_8900_MEM_BASE;

    cpu_physical_memory_write(LLB_BASE + 0x80, verify_stub,
                              sizeof(verify_stub));
    cpu_physical_memory_write(LLB_BASE + 0x100, decrypt_stub,
                              sizeof(decrypt_stub));
    cpu_physical_memory_write(LLB_BASE + 0x208, &engine_base,
                              sizeof(engine_base));
}

/*
 * iBoot-204's warm resume runs a mandatory pre-boot charging wait before the
 * type-4 handoff: its boot task (0x18004c76) enters a charging dispatcher
 * (0x180099f4) whose loop sleeps in 5-second chunks (pool literal at
 * 0x18009980) until a charge budget derived from the constant at 0x18021458
 * (10,000,000 microseconds) elapses. On this emulated device the charge is
 * fiction — there is no battery — so every Power/Home wake stalled about
 * 10 seconds inside iBoot before `System Wake`. Shrink both durations in the
 * freshly reloaded volatile iBoot image (never the file on disk). Each word
 * is verified first so a different iBoot build passes through unpatched.
 */
static void ipod_touch_patch_iboot_charge_wait(void)
{
    static const struct {
        uint32_t addr;
        uint32_t expected;
        uint32_t replacement;
    } patches[] = {
        /* Charging-loop total budget: 10,000,000 us -> 5,000 us. */
        { IBOOT_BASE + 0x21458, 10000000, 5000 },
        /* Charging-loop poll sleep: 5,000,000 us -> 100,000 us. */
        { IBOOT_BASE + 0x09980, 5000000, 100000 },
    };

    for (size_t i = 0; i < ARRAY_SIZE(patches); i++) {
        uint32_t current;

        cpu_physical_memory_read(patches[i].addr, &current, sizeof(current));
        if (current != patches[i].expected) {
            fprintf(stderr, "[WAKE] iBoot charge-wait word at 0x%08x is "
                    "0x%08x (expected 0x%08x); leaving unpatched\n",
                    patches[i].addr, current, patches[i].expected);
            continue;
        }
        cpu_physical_memory_write(patches[i].addr, &patches[i].replacement,
                                  sizeof(patches[i].replacement));
    }
}

static void ipod_touch_cpu_reset(void *opaque)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE((MachineState *)opaque);
    bool retained_wake = nms->retained_wake_pending;
    ARMCPU *cpu = nms->cpu;
    CPUState *cs = CPU(cpu);
    uint8_t *iboot_data = NULL;
    unsigned long iboot_size = 0;
    uint8_t *volatile_boot_ram;

    if (nms->low_vrom_alias && nms->low_ram_alias) {
        memory_region_set_enabled(nms->low_vrom_alias, false);
        /* Address zero is retained LPDDR only for the type-4 wake handoff.
         * Exposing it during a normal cold boot breaks early kernel DMA. */
        memory_region_set_enabled(nms->low_ram_alias, retained_wake);
    }

    if (nms->retained_crc_valid) {
        uint32_t crc_after_reset = ipod_touch_retained_crc();

        fprintf(stderr, "[WAKE] Retained LPDDR CRC32C at reset: 0x%08x (%s)\n",
                crc_after_reset,
                crc_after_reset == nms->retained_crc_before_reset ?
                "stable" : "CHANGED");
        nms->retained_crc_valid = false;
    }
    nms->retained_wake_pending = false;

    /*
     * A real OOCSHDWN wake reloads iBoot after the application processor has
     * lost power, while SDRAM containing the kernel sleep image is retained.
     * The old reset handler jumped back into the already-used iBoot RAM, so
     * its stale heap immediately panicked on the second entry. Recreate the
     * volatile boot memory on every SoC reset without clearing main SDRAM.
     */
    volatile_boot_ram = g_malloc0(0x400000);
    cpu_physical_memory_write(IBOOT_BASE, volatile_boot_ram, 0x400000);
    g_free(volatile_boot_ram);

    if (!g_file_get_contents(nms->iboot_path, (char **)&iboot_data,
                             &iboot_size, NULL)) {
        error_report("Unable to reload iBoot from %s", nms->iboot_path);
    } else {
        cpu_physical_memory_write(IBOOT_BASE, iboot_data, iboot_size);
        g_free(iboot_data);
        ipod_touch_patch_iboot_charge_wait();
    }

    volatile_boot_ram = g_malloc0(0x30000);
    cpu_physical_memory_write(LLB_BASE, volatile_boot_ram, 0x30000);
    g_free(volatile_boot_ram);
    ipod_touch_install_8900_ops();

    if (nms->spi2_state && nms->spi2_state->mt &&
        nms->spi2_state->mt->pmu) {
        Pcf50633State *pmu = nms->spi2_state->mt->pmu;

        pmu->oocshdwn_fired = false;
        pmu->wake_reset_pending = false;

        /* The PCF50633 nIRQ pin is level-triggered and the always-on PMU is
         * holding the wake cause pending while the application processor
         * powers back up, so the pin is already low when iBoot starts. The
         * SoC reset cleared the emulated GPIO/VIC view of that level; without
         * re-expressing it, iBoot's wake path never receives the PMU
         * interrupt and burns its full 10-second wake-event timeout before
         * reading INT1/INT2 anyway. */
        if (retained_wake) {
            pcf50633_update_irq(pmu);
        }
    }
    if (nms->lcd_state) {
        /* On a retained wake, OOCSHDWN left the physical panel rail off.
         * iBoot reports displayEnabled=0 and must not expose its temporary
         * battery/logo scanout. The resumed kernel turns scanout back on when
         * it reprograms the OS framebuffer. */
        nms->lcd_state->panel_off = retained_wake;
        nms->lcd_state->retained_resume = retained_wake;
        nms->lcd_state->retained_input_wait = retained_wake;
        nms->lcd_state->invalidate = 1;
        nms->lcd_state->input_ready = false;
        nms->lcd_state->input_ready_frames = 0;
    }
    cpu_reset(cs);

    //env->regs[0] = nms->kbootargs_pa;
    //cpu_set_pc(CPU(cpu), 0xc00607ec);
    cpu_set_pc(CPU(cpu), IBOOT_BASE);
    //env->regs[0] = 0x9000000;
    //cpu_set_pc(CPU(cpu), LLB_BASE + 0x100);
    //cpu_set_pc(CPU(cpu), VROM_MEM_BASE);
}

/*
USB PHYS
*/
static uint64_t s5l8900_usb_phys_read(void *opaque, hwaddr addr, unsigned size)
{
    s5l8900_usb_phys_s *s = opaque;

    switch(addr)
    {
    case 0x0: // OPHYPWR
        return s->usb_ophypwr;

    case 0x4: // OPHYCLK
        return s->usb_ophyclk;

    case 0x8: // ORSTCON
        return s->usb_orstcon;

    case 0x20: // OPHYTUNE
        return s->usb_ophytune;

    default:
        fprintf(stderr, "%s: read invalid location 0x%08x\n", __func__, addr);
        return 0;
    }

    return 0;
}

static void s5l8900_usb_phys_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    s5l8900_usb_phys_s *s = opaque;

    switch(addr)
    {
    case 0x0: // OPHYPWR
        s->usb_ophypwr = val;
        return;

    case 0x4: // OPHYCLK
        s->usb_ophyclk = val;
        return;

    case 0x8: // ORSTCON
        s->usb_orstcon = val;
        return;

    case 0x20: // OPHYTUNE
        s->usb_ophytune = val;
        return;

    default:
        //hw_error("%s: write invalid location 0x%08x.\n", __func__, offset);
        fprintf(stderr, "%s: write invalid location 0x%08x\n", __func__, addr);
    }
}

static const MemoryRegionOps usb_phys_ops = {
    .read = s5l8900_usb_phys_read,
    .write = s5l8900_usb_phys_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

/*
MBX
*/
static uint64_t s5l8900_mbx_read(void *opaque, hwaddr addr, unsigned size)
{
    //fprintf(stderr, "%s: read from location 0x%08x\n", __func__, addr);
    switch(addr)
    {
        case 0x12c:
            return 0x100;
        case 0xf00:
            return (1 << 0x18) | 0x10000; // seems to be some kind of identifier
        case 0x1020:
            return 0x10000;
        default:
            break;
    }
    return 0;
}

static void s5l8900_mbx_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    //fprintf(stderr, "%s: writing 0x%08x to 0x%08x\n", __func__, val, addr);
    // do nothing
}

static const MemoryRegionOps mbx_ops = {
    .read = s5l8900_mbx_read,
    .write = s5l8900_mbx_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_memory_setup(MachineState *machine, MemoryRegion *sysmem,
                                    AddressSpace *nsas)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(machine);
    DriveInfo *dinfo;

    /* VROM copies its executable helpers into the 128 KiB SRAM0 window. */
    allocate_ram(sysmem, "sram0", LLB_BASE, 0x20000);
    allocate_ram(sysmem, "sram1", SRAM1_MEM_BASE, 0x10000);

    MemoryRegion *main_ram = allocate_ram(sysmem, "ram", RAM_MEM_BASE,
                                          0x8000000);

    // load the bootrom (vrom)
    uint8_t *file_data = NULL;
    unsigned long fsize;
    if (g_file_get_contents(nms->bootrom_path, (char **)&file_data, &fsize, NULL)) {
        MemoryRegion *vrom = allocate_ram(sysmem, "vrom", VROM_MEM_BASE,
                                           0x10000);

        address_space_rw(nsas, VROM_MEM_BASE, MEMTXATTRS_UNSPECIFIED, (uint8_t *)file_data, fsize, 1);

        nms->low_vrom_alias = g_new(MemoryRegion, 1);
        memory_region_init_alias(nms->low_vrom_alias, OBJECT(machine),
                                 "vrom-low-alias", vrom, 0, 0x10000);
        memory_region_add_subregion_overlap(sysmem, 0,
                                            nms->low_vrom_alias, 1);
    }

    /* Reset maps VROM at zero. iBoot later remaps retained LPDDR there;
     * type-4 disables the MMU and branches to the kernel trampoline at zero. */
    nms->low_ram_alias = g_new(MemoryRegion, 1);
    memory_region_init_alias(nms->low_ram_alias, OBJECT(machine),
                             "ram-low-alias", main_ram, 0, 0x8000000);
    memory_region_set_enabled(nms->low_ram_alias, false);
    memory_region_add_subregion_overlap(sysmem, 0, nms->low_ram_alias, 0);

    /* The dumped VROM delegates image verification/decryption to two
     * device-specific 8900 service entries that are not present in the dump. */
    uint32_t service_entry = LLB_BASE + 0x80;
    address_space_rw(nsas, VROM_MEM_BASE + 0x8c,
                     MEMTXATTRS_UNSPECIFIED, (uint8_t *)&service_entry,
                     sizeof(service_entry), true);
    service_entry = LLB_BASE + 0x100;
    address_space_rw(nsas, VROM_MEM_BASE + 0x90,
                     MEMTXATTRS_UNSPECIFIED, (uint8_t *)&service_entry,
                     sizeof(service_entry), true);

    /* VROM calls the same unavailable services directly while loading LLB.
     * Branch to the SRAM shims; BX LR there returns to VROM's caller. */
    uint32_t service_branch = 0xea7ffe7b; /* 0x2000068c -> 0x22000080 */
    address_space_rw(nsas, VROM_MEM_BASE + 0x68c,
                     MEMTXATTRS_UNSPECIFIED, (uint8_t *)&service_branch,
                     sizeof(service_branch), true);
    service_branch = 0xea7ffe54; /* 0x200007a8 -> 0x22000100 */
    address_space_rw(nsas, VROM_MEM_BASE + 0x7a8,
                     MEMTXATTRS_UNSPECIFIED, (uint8_t *)&service_branch,
                     sizeof(service_branch), true);

    // load iBoot
    file_data = NULL;
    if (g_file_get_contents(nms->iboot_path, (char **)&file_data, &fsize, NULL)) {
        allocate_ram(sysmem, "iboot", IBOOT_BASE, 0x400000);
        address_space_rw(nsas, IBOOT_BASE, MEMTXATTRS_UNSPECIFIED, (uint8_t *)file_data, fsize, 1);
     }

    // // load LLB
    // file_data = NULL;
    // if (g_file_get_contents("/Users/martijndevos/Documents/ipod_touch_emulation/LLB.n45ap.RELEASE", (char **)&file_data, &fsize, NULL)) {
    //     allocate_ram(sysmem, "llb", LLB_BASE, align_64k_high(fsize));
    //     address_space_rw(nsas, LLB_BASE, MEMTXATTRS_UNSPECIFIED, (uint8_t *)file_data, fsize, 1);
    //  }

    allocate_ram(sysmem, "edgeic", EDGEIC_MEM_BASE, 0x1000);
    allocate_ram(sysmem, "watchdog", WATCHDOG_MEM_BASE, align_64k_high(0x1));

    allocate_ram(sysmem, "iis0", IIS0_MEM_BASE, align_64k_high(0x1));
    allocate_ram(sysmem, "iis1", IIS1_MEM_BASE, align_64k_high(0x1));
    allocate_ram(sysmem, "iis2", IIS2_MEM_BASE, align_64k_high(0x1));

    allocate_ram(sysmem, "mpvd", MPVD_MEM_BASE, 0x70000);
    allocate_ram(sysmem, "h264bpd", H264BPD_MEM_BASE, 4096);

    allocate_ram(sysmem, "framebuffer", FRAMEBUFFER_MEM_BASE, align_64k_high(4 * 320 * 480));

    // setup 1MB NOR
    dinfo = drive_get(IF_PFLASH, 0, 0);
    if (!dinfo) {
        printf("A NOR image must be given with the -pflash parameter\n");
        abort();
    }

    BlockBackend *nor_blk = blk_by_legacy_dinfo(dinfo);

    if (!pflash_cfi02_register(NOR_MEM_BASE, "nor", 1024 * 1024,
                               nor_blk,
                               4096, 1, 2,
                               0x00bf, 0x273f, 0x0, 0x0, 0x555, 0x2aa, 0)) {
        printf("Error registering NOR flash!\n");
        abort();
    }
}

static char *ipod_touch_get_bootrom_path(Object *obj, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    return g_strdup(nms->bootrom_path);
}

static void ipod_touch_set_bootrom_path(Object *obj, const char *value, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    g_strlcpy(nms->bootrom_path, value, sizeof(nms->bootrom_path));
}

static char *ipod_touch_get_iboot_path(Object *obj, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    return g_strdup(nms->iboot_path);
}

static void ipod_touch_set_iboot_path(Object *obj, const char *value, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    g_strlcpy(nms->iboot_path, value, sizeof(nms->iboot_path));
}

static char *ipod_touch_get_nand_path(Object *obj, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    return g_strdup(nms->nand_path);
}

static void ipod_touch_set_nand_path(Object *obj, const char *value, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    g_strlcpy(nms->nand_path, value, sizeof(nms->nand_path));
}

static void ipod_touch_instance_init(Object *obj)
{
	object_property_add_str(obj, "bootrom", ipod_touch_get_bootrom_path, ipod_touch_set_bootrom_path);
    object_property_set_description(obj, "bootrom", "Path to the S5L8900 bootrom binary");

    object_property_add_str(obj, "iboot", ipod_touch_get_iboot_path, ipod_touch_set_iboot_path);
    object_property_set_description(obj, "iboot", "Path to the iBoot binary");

    object_property_add_str(obj, "nand", ipod_touch_get_nand_path, ipod_touch_set_nand_path);
    object_property_set_description(obj, "nand", "Path to the NAND files");
}

static inline qemu_irq s5l8900_get_irq(IPodTouchMachineState *s, int n)
{
    return s->irq[n / S5L8900_VIC_SIZE][n % S5L8900_VIC_SIZE];
}

static uint32_t s5l8900_usb_hwcfg[] = {
    0,
    0x7a8f60d0,
    0x082000e8,
    0x01f08024
};

static void ipod_touch_key_event(void *opaque, int keycode)
{
    bool do_irq = false;
    bool is_power = false;
    int gpio_group = 0, gpio_selector = 0;

    IPodTouchMultitouchState *s = (IPodTouchMultitouchState *)opaque;

    if (keycode == 153 && s->suppress_power_release) {
        if (s->pmu) {
            /* The AP may still be in iBoot, but the always-on PMU sees the
             * physical key release and retains that edge for the kernel. */
            s->pmu->regs[PMU_OOCSTAT] |= PMU_OOCSTAT_ONKEY;
            s->pmu->int2 |= PMU_INT2_ONKEYR;
            s->pmu->retained_int2_wake |= PMU_INT2_ONKEYR;
        }
        s->suppress_power_release = false;
        return;
    }
    if (keycode == 35 && s->suppress_home_release) {
        return;
    }
    if (keycode == 163 && s->suppress_home_release) {
        s->suppress_home_release = false;
        return;
    }

    /*
     * A pre-warmed wake boot is already running (or parked just before the
     * type-4 handoff). Power/Home only needs to supply the hardware wake
     * cause and, when parked, restart the machine: the kernel resume then
     * reads the PMU RTC and the retained wake cause with correct values.
     */
    if ((keycode == 25 || keycode == 35) && s->pmu &&
        s->pmu->prewarm_active) {
        Pcf50633State *pmu = s->pmu;

        if (keycode == 25) {
            pmu->regs[PMU_OOCSTAT] &= ~PMU_OOCSTAT_ONKEY;
            pmu->int2 |= PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
            pmu->retained_int2_wake |= PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
            s->suppress_power_release = true;
        } else {
            pmu->int2 |= PMU_INT2_EXTON1R;
            pmu->retained_int2_wake |= PMU_INT2_EXTON1R;
            s->suppress_home_release = true;
        }
        pmu->prewarm_wake_requested = true;
        pcf50633_update_irq(pmu);

        if (pmu->prewarm_parked) {
            pmu->prewarm_parked = false;
            pmu->prewarm_active = false;
            pmu->retained_int2_reexposed = true;
            fprintf(stderr, "[WAKE] %s completed pre-warmed wake\n",
                    keycode == 25 ? "Power" : "Home");
            vm_start();
        } else {
            /* The wake boot has not reached its park point yet; the type-4
             * commit will now pass straight through instead of parking. */
            fprintf(stderr, "[WAKE] %s requested wake during pre-warm boot\n",
                    keycode == 25 ? "Power" : "Home");
        }
        return;
    }

    /*
     * OOCSHDWN is a real application-processor power loss. Wake therefore
     * starts a new SoC boot with retained SDRAM instead of returning from the
     * kernel's terminal B . loop. Pristine iBoot/SRAM are restored by the
     * machine reset callback; the MOSX/SUSP markers in main RAM survive.
     */
    if ((keycode == 25 || keycode == 35) && s->cpu && s->pmu &&
        s->pmu->oocshdwn_fired) {
        CPUARMState *env = &ARM_CPU(s->cpu)->env;
        uint32_t pc = env->regs[15];
        uint32_t cpsr = cpsr_read(env);
        bool in_poweroff_loop = pc >= 0xc005a6c0 && pc <= 0xc005a6d8 &&
            (cpsr & CPSR_I) && (cpsr & CPSR_F);

        if (in_poweroff_loop) {
            /* Bit 7 was armed by the kernel before OOCSHDWN and must remain
             * set until iBoot consumes the retained token. The live ONKEY
             * edges were consumed while entering sleep; retained Power/Home
             * wake is reported in the second ApplePCF50635 event byte. */
            s->pmu->regs[PMU_RESUME_STATUS] |= PMU_RESUME_WAKE;
            if (keycode == 25) {
                s->pmu->regs[PMU_OOCSTAT] &= ~PMU_OOCSTAT_ONKEY;
                s->pmu->int2 |= PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
                s->pmu->retained_int2_wake |=
                    PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
            } else {
                s->pmu->int2 |= PMU_INT2_EXTON1R;
                s->pmu->retained_int2_wake |= PMU_INT2_EXTON1R;
            }
            s->pmu->retained_int2_reexposed = false;
            if (keycode == 25) {
                s->suppress_power_release = true;
            } else {
                s->suppress_home_release = true;
            }
            fprintf(stderr, "[WAKE] %s requested retained-RAM SoC reboot\n",
                    keycode == 25 ? "Power" : "Home");
            ipod_touch_prepare_retained_wake();
            qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
            return;
        }
    }

    /*
     * The guest can spend several seconds in its display-off/driver-shutdown
     * transition before the final OOCSHDWN write. If Power or Home arrives in
     * that interval, remember the hardware wake request and let shutdown
     * finish. The PMU will reset the SoC immediately after OOCSHDWN.
     */
    if ((keycode == 25 || keycode == 35) && s->lcd && s->pmu &&
        !s->pmu->oocshdwn_fired &&
        ipod_touch_lcd_framebuffer_is_dark(s->lcd)) {
        s->pmu->wake_reset_pending = true;
        if (keycode == 25) {
            s->pmu->regs[PMU_OOCSTAT] &= ~PMU_OOCSTAT_ONKEY;
            s->pmu->int2 |= PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
            s->pmu->retained_int2_wake |=
                PMU_INT2_ONKEYF | PMU_INT2_EXTON1R;
            s->suppress_power_release = true;
        } else {
            s->pmu->int2 |= PMU_INT2_EXTON1R;
            s->pmu->retained_int2_wake |= PMU_INT2_EXTON1R;
            s->suppress_home_release = true;
        }
        fprintf(stderr, "[WAKE] %s queued during OOCSHDWN transition\n",
                keycode == 25 ? "Power" : "Home");
        return;
    }

    if(keycode == 25 || keycode == 153) {
        // power button
        is_power = true;
        gpio_group = GPIO_BUTTON_POWER_IRQ / NUM_GPIO_PINS;
        gpio_selector = GPIO_BUTTON_POWER_IRQ % NUM_GPIO_PINS;

        if(keycode == 25 && (s->gpio_state->gpio_state & (1 << (GPIO_BUTTON_POWER & 0xf))) == 0) {
            s->gpio_state->gpio_state |= (1 << (GPIO_BUTTON_POWER & 0xf));
            do_irq = true;
        }
        else if(keycode == 153) {
            s->gpio_state->gpio_state &= ~(1 << (GPIO_BUTTON_POWER & 0xf));
            do_irq = true;
        }
    }
    else if(keycode == 35 || keycode == 163) {
        // home button
        gpio_group = GPIO_BUTTON_HOME_IRQ / NUM_GPIO_PINS;
        gpio_selector = GPIO_BUTTON_HOME_IRQ % NUM_GPIO_PINS;

        if(keycode == 35 && (s->gpio_state->gpio_state & (1 << (GPIO_BUTTON_HOME & 0xf))) == 0) {
            s->gpio_state->gpio_state |= (1 << (GPIO_BUTTON_HOME & 0xf));
            do_irq = true;
        }
        else if(keycode == 163) {
            s->gpio_state->gpio_state &= ~(1 << (GPIO_BUTTON_HOME & 0xf));
            do_irq = true;
        }
    }

    // Log CPU state for sleep/wake debugging.
    if (s->cpu) {
        ARMCPU *arm_cpu = ARM_CPU(s->cpu);
        CPUARMState *env = &arm_cpu->env;
        uint32_t cpsr = cpsr_read(env);
        fprintf(stderr, "[BTN] keycode=%d  PC=0x%08x  I=%d F=%d  power=%d\n",
                keycode, env->regs[15], (cpsr >> 7) & 1, (cpsr >> 6) & 1, is_power);
    }

    if(do_irq) {
        // Always raise the GPIO interrupt for all buttons.
        // The VIC / SYSIC handle delivery during normal operation.
        s->sysic->gpio_int_status[gpio_group] |= (1 << gpio_selector);
        qemu_irq_raise(s->sysic->gpio_irqs[gpio_group]);

        // Schedule auto-lower to create an edge-triggered pulse.
        timer_mod(s->sysic->gpio_irq_lower_timers[gpio_group],
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + GPIO_IRQ_PULSE_NS);

    }

    // Signal the PMU for normal, guest-owned Power transitions. Deep-sleep
    // wake is handled above as an SoC reboot and never reaches this block.
    if (s->pmu && is_power) {
        if (keycode == 25) {
            pcf50633_set_onkey(s->pmu, true);

        } else if (keycode == 153) {
            pcf50633_set_onkey(s->pmu, false);
        }
    }
}

static void ipod_touch_input_event(DeviceState *dev, QemuConsole *src,
                                   InputEvent *evt)
{
    InputKeyEvent *key = evt->u.key.data;
    int qcode = qemu_input_key_value_to_qcode(key->key);
    int keycode;

    switch (qcode) {
    case Q_KEY_CODE_P:
        keycode = key->down ? 25 : 153;
        break;
    case Q_KEY_CODE_H:
        keycode = key->down ? 35 : 163;
        break;
    default:
        return;
    }

    ipod_touch_key_event(IPOD_TOUCH_MULTITOUCH(dev), keycode);
}

static const QemuInputHandler ipod_touch_key_handler = {
    .name = "ipod-touch-buttons",
    .mask = INPUT_EVENT_MASK_KEY,
    .event = ipod_touch_input_event,
};

static void ipod_touch_machine_init(MachineState *machine)
{
	IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(machine);
	MemoryRegion *sysmem;
    AddressSpace *nsas;
    ARMCPU *cpu;

    ipod_touch_cpu_setup(machine, &sysmem, &cpu, &nsas);

    // setup clock
    nms->sysclk = clock_new(OBJECT(machine), "SYSCLK");
    clock_set_hz(nms->sysclk, 12000000ULL);

    nms->cpu = cpu;
    g_ipod_touch_nms = nms;

    // setup VICs
    nms->irq = g_malloc0(sizeof(qemu_irq *) * 2);
    DeviceState *dev = pl192_manual_init("vic0", qdev_get_gpio_in(DEVICE(nms->cpu), ARM_CPU_IRQ), qdev_get_gpio_in(DEVICE(nms->cpu), ARM_CPU_FIQ), NULL);
    PL192State *s = PL192(dev);
    nms->vic0 = s;
    memory_region_add_subregion(sysmem, VIC0_MEM_BASE, &nms->vic0->iomem);
    nms->irq[0] = g_malloc0(sizeof(qemu_irq) * 32);
    for (int i = 0; i < 32; i++) { nms->irq[0][i] = qdev_get_gpio_in(dev, i); }

    dev = pl192_manual_init("vic1", NULL);
    s = PL192(dev);
    nms->vic1 = s;
    memory_region_add_subregion(sysmem, VIC1_MEM_BASE, &nms->vic1->iomem);
    nms->irq[1] = g_malloc0(sizeof(qemu_irq) * 32);
    for (int i = 0; i < 32; i++) { nms->irq[1][i] = qdev_get_gpio_in(dev, i); }

    // // chain VICs together
    nms->vic1->daisy = nms->vic0;

    // init clock 0
    dev = qdev_new("ipodtouch.clock");
    IPodTouchClockState *clock0_state = IPOD_TOUCH_CLOCK(dev);
    nms->clock0 = clock0_state;
    memory_region_add_subregion(sysmem, CLOCK0_MEM_BASE, &clock0_state->iomem);

    // init clock 1
    dev = qdev_new("ipodtouch.clock");
    IPodTouchClockState *clock1_state = IPOD_TOUCH_CLOCK(dev);
    nms->clock1 = clock1_state;
    memory_region_add_subregion(sysmem, CLOCK1_MEM_BASE, &clock1_state->iomem);

    // init the timer
    dev = qdev_new("ipodtouch.timer");
    IPodTouchTimerState *timer_state = IPOD_TOUCH_TIMER(dev);
    nms->timer1 = timer_state;
    memory_region_add_subregion(sysmem, TIMER1_MEM_BASE, &timer_state->iomem);
    SysBusDevice *busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_TIMER1_IRQ));
    timer_state->sysclk = nms->sysclk;

    // init sysic
    dev = qdev_new("ipodtouch.sysic");
    IPodTouchSYSICState *sysic_state = IPOD_TOUCH_SYSIC(dev);
    nms->sysic = sysic_state;
    memory_region_add_subregion(sysmem, SYSIC_MEM_BASE, &sysic_state->iomem);
    busdev = SYS_BUS_DEVICE(dev);
    for(int grp = 0; grp < GPIO_NUMINTGROUPS; grp++) {
        sysbus_connect_irq(busdev, grp, s5l8900_get_irq(nms, S5L8900_GPIO_IRQS[grp]));
    }

    // init GPIO
    dev = qdev_new("ipodtouch.gpio");
    IPodTouchGPIOState *gpio_state = IPOD_TOUCH_GPIO(dev);
    nms->gpio_state = gpio_state;
    memory_region_add_subregion(sysmem, GPIO_MEM_BASE, &gpio_state->iomem);

    // init SDIO
    dev = qdev_new("ipodtouch.sdio");
    IPodTouchSDIOState *sdio_state = IPOD_TOUCH_SDIO(dev);
    nms->sdio_state = sdio_state;
    memory_region_add_subregion(sysmem, SDIO_MEM_BASE, &sdio_state->iomem);

    dev = exynos4210_uart_create(UART0_MEM_BASE, 256, 0, serial_hd(0), nms->irq[0][24]);
    if (!dev) {
        printf("Failed to create uart0 device!\n");
        abort();
    }

    dev = exynos4210_uart_create(UART1_MEM_BASE, 256, 1, serial_hd(1), nms->irq[0][25]);
    if (!dev) {
        printf("Failed to create uart1 device!\n");
        abort();
    }

    dev = exynos4210_uart_create(UART2_MEM_BASE, 256, 2, serial_hd(2), nms->irq[0][26]);
    if (!dev) {
        printf("Failed to create uart2 device!\n");
        abort();
    }

    dev = exynos4210_uart_create(UART3_MEM_BASE, 256, 3, serial_hd(3), nms->irq[0][27]);
    if (!dev) {
        printf("Failed to create uart3 device!\n");
        abort();
    }

    dev = exynos4210_uart_create(UART4_MEM_BASE, 256, 4, serial_hd(4), nms->irq[0][28]);
    if (!dev) {
        printf("Failed to create uart4 device!\n");
        abort();
    }

    // init spis
    set_spi_base(0);
    sysbus_create_simple("s5l8900spi", SPI0_MEM_BASE,
                         s5l8900_get_irq(nms, S5L8900_SPI0_IRQ));

    set_spi_base(1);
    dev = sysbus_create_simple("s5l8900spi", SPI1_MEM_BASE,
                               s5l8900_get_irq(nms, S5L8900_SPI1_IRQ));
    S5L8900SPIState *spi1_state = S5L8900SPI(dev);

    set_spi_base(2);
    dev = sysbus_create_simple("s5l8900spi", SPI2_MEM_BASE, s5l8900_get_irq(nms, S5L8900_SPI2_IRQ));
    S5L8900SPIState *spi2_state = S5L8900SPI(dev);
    spi2_state->mt->sysic = sysic_state;
    spi2_state->mt->gpio_state = gpio_state;
    spi2_state->mt->cpu = CPU(cpu);
    nms->spi2_state = spi2_state;

    ipod_touch_memory_setup(machine, sysmem, nsas);

    // init LCD
    dev = qdev_new("ipodtouch.lcd");
    IPodTouchLCDState *lcd_state = IPOD_TOUCH_LCD(dev);
    lcd_state->sysmem = sysmem;
    lcd_state->mt = spi2_state->mt;
    spi1_state->panel->lcd = lcd_state;
    spi2_state->mt->lcd = lcd_state;
    nms->lcd_state = lcd_state;
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_LCD_IRQ));
    memory_region_add_subregion(sysmem, DISPLAY_MEM_BASE, &lcd_state->iomem);
    sysbus_realize(busdev, &error_fatal);

    // init AES engine
    dev = qdev_new("ipodtouch.aes");
    S5L8900AESState *aes_state = IPOD_TOUCH_AES(dev);
    nms->aes_state = aes_state;
    memory_region_add_subregion(sysmem, AES_MEM_BASE, &aes_state->iomem);

    // init SHA1 engine
    dev = qdev_new("ipodtouch.sha1");
    S5L8900SHA1State *sha1_state = IPOD_TOUCH_SHA1(dev);
    nms->sha1_state = sha1_state;
    memory_region_add_subregion(sysmem, SHA1_MEM_BASE, &sha1_state->iomem);

    // init 8900 engine
    MemoryRegion *iomem = g_new(MemoryRegion, 1);
    memory_region_init_io(iomem, OBJECT(s), &engine_8900_ops, nsas, "8900engine", 0x100);
    memory_region_add_subregion(sysmem, ENGINE_8900_MEM_BASE, iomem);

    // init NAND flash
    dev = qdev_new("itnand");
    ITNandState *nand_state = ITNAND(dev);
    nand_state->nand_path = &nms->nand_path;
    nms->nand_state = nand_state;
    memory_region_add_subregion(sysmem, NAND_MEM_BASE, &nand_state->iomem);

    // init NAND ECC module
    dev = qdev_new("itnand_ecc");
    ITNandECCState *nand_ecc_state = ITNANDECC(dev);
    nms->nand_ecc_state = nand_ecc_state;
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_NAND_ECC_IRQ));
    memory_region_add_subregion(sysmem, NAND_ECC_MEM_BASE, &nand_ecc_state->iomem);

    // init USB OTG
    dev = ipod_touch_init_usb_otg(nms->irq[0][13], s5l8900_usb_hwcfg);
    synopsys_usb_state *usb_otg = S5L8900USBOTG(dev);
    nms->usb_otg = usb_otg;
    memory_region_add_subregion(sysmem, USBOTG_MEM_BASE, &nms->usb_otg->iomem);

    // init USB PHYS
    s5l8900_usb_phys_s *usb_state = malloc(sizeof(s5l8900_usb_phys_s));
    nms->usb_phys = usb_state;
    usb_state->usb_ophypwr = 0;
    usb_state->usb_ophyclk = 0;
    usb_state->usb_orstcon = 0;
    usb_state->usb_ophytune = 0;

    iomem = g_new(MemoryRegion, 1);
    memory_region_init_io(iomem, OBJECT(s), &usb_phys_ops, usb_state, "usbphys", 0x40);
    memory_region_add_subregion(sysmem, USBPHYS_MEM_BASE, iomem);

    // init two pl080 DMAC0 devices
    dev = qdev_new("pl080");
    PL080State *pl080_1 = PL080(dev);
    object_property_set_link(OBJECT(dev), "downstream", OBJECT(sysmem), &error_fatal);
    /* The NAND FIFO stub is always ready and uses DMAC request input 2. */
    qdev_prop_set_uint32(dev, "request-mask", 1u << 2);
    memory_region_add_subregion(sysmem, DMAC0_MEM_BASE, &pl080_1->iomem);
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_realize(busdev, &error_fatal);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_DMAC0_IRQ));

    dev = qdev_new("pl080");
    PL080State *pl080_2 = PL080(dev);
    object_property_set_link(OBJECT(dev), "downstream", OBJECT(sysmem), &error_fatal);
    /* SPI2 TX uses DMAC1 request input 14. The FIFO stub accepts data
     * synchronously, so its transmit request remains asserted. */
    qdev_prop_set_uint32(dev, "request-mask", 1u << 14);
    memory_region_add_subregion(sysmem, DMAC1_MEM_BASE, &pl080_2->iomem);
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_realize(busdev, &error_fatal);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_DMAC1_IRQ));

    // Init I2C
    dev = qdev_new("ipodtouch.i2c");
    IPodTouchI2CState *i2c_state = IPOD_TOUCH_I2C(dev);
    nms->i2c0_state = i2c_state;
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_I2C0_IRQ));
    memory_region_add_subregion(sysmem, I2C0_MEM_BASE, &i2c_state->iomem);

    // init the accelerometer
    I2CSlave *accelerometer = i2c_slave_create_simple(i2c_state->bus, "lis302dl", 0x1D);

    dev = qdev_new("ipodtouch.i2c");
    i2c_state = IPOD_TOUCH_I2C(dev);
    nms->i2c1_state = i2c_state;
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_I2C1_IRQ));
    memory_region_add_subregion(sysmem, I2C1_MEM_BASE, &i2c_state->iomem);

    // init the PMU
    I2CSlave *pmu = i2c_slave_create_simple(i2c_state->bus, "pcf50633", 0x73);
    spi2_state->mt->pmu = PCF50633(pmu);
    // Wire PMU interrupt output to SYSIC (GPIO 0x55 = group 2, bit 21)
    PCF50633(pmu)->sysic = sysic_state;
    PCF50633(pmu)->vic0 = nms->vic0;
    PCF50633(pmu)->vic1 = nms->vic1;
    PCF50633(pmu)->lcd = nms->lcd_state;
    sysic_state->pmu = PCF50633(pmu);  // SYSIC→PMU callback for GPIO re-assertion

    // init the ADM
    dev = qdev_new("ipodtouch.adm");
    IPodTouchADMState *adm_state = IPOD_TOUCH_ADM(dev);
    adm_state->nand_state = nand_state;
    nms->adm_state = adm_state;
    object_property_set_link(OBJECT(dev), "downstream", OBJECT(sysmem), &error_fatal);
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_realize(busdev, &error_fatal);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_ADM_IRQ));
    memory_region_add_subregion(sysmem, ADM_MEM_BASE, &adm_state->iomem);

    // init MBX
    iomem = g_new(MemoryRegion, 1);
    memory_region_init_io(iomem, OBJECT(nms), &mbx_ops, NULL, "mbx", 0x1000000);
    memory_region_add_subregion(sysmem, MBX_MEM_BASE, iomem);

    // init the chip ID module
    dev = qdev_new("ipodtouch.chipid");
    IPodTouchChipIDState *chipid_state = IPOD_TOUCH_CHIPID(dev);
    nms->chipid_state = chipid_state;
    memory_region_add_subregion(sysmem, CHIPID_MEM_BASE, &chipid_state->iomem);

    // init the TVOut instances
    dev = qdev_new("ipodtouch.tvout");
    IPodTouchTVOutState *tvout_state = IPOD_TOUCH_TVOUT(dev);
    tvout_state->index = 1;
    nms->tvout1_state = tvout_state;
    memory_region_add_subregion(sysmem, TVOUT1_MEM_BASE, &tvout_state->iomem);

    dev = qdev_new("ipodtouch.tvout");
    tvout_state = IPOD_TOUCH_TVOUT(dev);
    tvout_state->index = 2;
    nms->tvout2_state = tvout_state;
    memory_region_add_subregion(sysmem, TVOUT2_MEM_BASE, &tvout_state->iomem);
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_TVOUT_SDO_IRQ));

    dev = qdev_new("ipodtouch.tvout");
    tvout_state = IPOD_TOUCH_TVOUT(dev);
    tvout_state->index = 3;
    nms->tvout3_state = tvout_state;
    memory_region_add_subregion(sysmem, TVOUT3_MEM_BASE, &tvout_state->iomem);

    // setup workaround for TVOut
    iomem = g_new(MemoryRegion, 1);
    memory_region_init_io(iomem, OBJECT(nms), &tvout_workaround_ops, NULL, "tvoutworkaround", 0x4);
    memory_region_add_subregion(sysmem, TVOUT_WORKAROUND_MEM_BASE, iomem);

    qemu_register_reset(ipod_touch_cpu_reset, nms);

    qemu_input_handler_register(DEVICE(spi2_state->mt),
                                &ipod_touch_key_handler);
}

static void ipod_touch_machine_class_init(ObjectClass *obj, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(obj);
    mc->desc = "iPod Touch";
    mc->init = ipod_touch_machine_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("arm1176");
}

static const TypeInfo ipod_touch_machine_info = {
    .name          = TYPE_IPOD_TOUCH_MACHINE,
    .parent        = TYPE_MACHINE,
    .instance_size = sizeof(IPodTouchMachineState),
    .class_size    = sizeof(IPodTouchMachineClass),
    .class_init    = ipod_touch_machine_class_init,
    .instance_init = ipod_touch_instance_init,
    .interfaces    = arm_machine_interfaces,
};

static void ipod_touch_machine_types(void)
{
    type_register_static(&ipod_touch_machine_info);
}

type_init(ipod_touch_machine_types)
