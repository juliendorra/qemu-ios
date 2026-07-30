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
#include "hw/arm/ipod_touch_console_tap.h"
#include "hw/arm/exynos4210.h"
#include "hw/dma/pl080.h"
#include "chardev/char.h"
#include "ui/input.h"
#include "hw/arm/ipod_touch_baseband.h"

// Global pointer to machine state for wake assist timer access from key handler
IPodTouchMachineState *g_ipod_touch_nms = NULL;

/*
 * Minimal S5L8900 watchdog. On real hardware iBoot's reboot routine writes the
 * reset bit here and the SoC resets. The N45AP path historically backs this
 * region with inert RAM (a working, shipped configuration), so we only install
 * real reset semantics for M68AP, where iBoot panics early and spins forever
 * waiting for a reset that never comes. Writing the reset value (0x100000)
 * requests a guest reset; other writes are ignored so watchdog "pet" traffic
 * does not reboot the guest.
 */
#define WATCHDOG_RESET_VALUE 0x100000

static void ipod_touch_watchdog_write(void *opaque, hwaddr addr, uint64_t val,
                                      unsigned size)
{
    if (val == WATCHDOG_RESET_VALUE) {
        qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
    }
}

static uint64_t ipod_touch_watchdog_read(void *opaque, hwaddr addr, unsigned size)
{
    return 0;
}

static const MemoryRegionOps ipod_touch_watchdog_ops = {
    .read = ipod_touch_watchdog_read,
    .write = ipod_touch_watchdog_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

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

/*
 * --- TVOut swap-device workaround ---------------------------------------
 *
 * SpringBoard attaches the AppleH1TVOut framebuffer, then waits for the
 * TVOut swap device to tear down before it re-attaches AppleH1CLCD and
 * paints. AppleMBX's teardown polls one field of the swap-device object and
 * never sees it clear, because our MBX is a do-nothing stub that completes no
 * swaps. Upstream "got past TVOut" (f59f20f60e) by overlaying a 4-byte
 * always-zero window on that field.
 *
 * THIS IS A SHORTCUT, and the shortcut's original form was the expensive kind:
 * a magic physical address baked in for ONE kernel build. When the address is
 * wrong nothing complains -- you just get four bytes of kernel heap that read
 * as zero, and a hang somewhere unrelated. That silence cost this project the
 * entire M68AP render investigation, because the iPod's constant does not
 * match the iPhone kernel's heap.
 *
 * So the window is now DERIVED, not declared: the kernel itself prints the
 * object's address ("AppleMBX: Added swap device: AppleH1TVOut  id: c09c8400")
 * and the console tap moves the window there, on any board and any kernel
 * build. The per-board constants remain only as a pre-announcement placement,
 * and a mismatch is reported rather than hidden. If the window is never read
 * by the time SpringBoard starts, we say so loudly.
 *
 * THE CLEAN FIX, still to do: model the MBX swap completion (and/or the TVOut
 * SDO IRQ, which the machine already wires) so the guest driver clears that
 * field itself and no window is needed at all. Tracked in
 * M68AP_RENDER_HANDOFF.md.
 */
#define TVOUT_WA_FIELD_OFFSET 0x160     /* field within the swap-device object */
#define KERNEL_VA_BASE        0xC0000000

/*
 * IT_TVOUT_WA=0 removes the workaround entirely -- no window is mapped and the
 * kernel's swap-device announcement is ignored.
 *
 * It exists so the workaround can be A/B'd on ONE binary, the way IT_MBX_READY
 * lets the MBX fix be. Before this, comparing "with" and "without" meant
 * checking the file out at an older commit and rebuilding, which in a tree that
 * two sessions build in is disruptive enough that the comparison does not get
 * made -- and this workaround became NON-INERT on 1.0 only recently
 * (23cc69c033), which is exactly when a knob is worth having.
 */
static bool tvout_wa_enabled(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *e = getenv("IT_TVOUT_WA");
        cached = !(e && e[0] == '0');
    }
    return cached;
}

static MemoryRegion *tvout_wa_region;
static bool tvout_wa_mapped;        /* nothing is mapped until the guest says where */
static hwaddr tvout_wa_addr;
static uint64_t tvout_wa_reads;
static bool tvout_wa_derived;
static bool tvout_wa_is_tvout;      /* the current window came from a TVOut device */

static uint64_t tvout_workaround_read(void *opaque, hwaddr addr, unsigned size)
{
    tvout_wa_reads++;
    if (getenv("IT_FB_TRACE")) {
        fprintf(stderr, "[TVOUT-WA] rd +0x%x (n=%" PRIu64 ")\n",
                (uint32_t)addr, tvout_wa_reads);
    }
    return 0;
}

static void tvout_workaround_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    if (getenv("IT_FB_TRACE")) {
        fprintf(stderr, "[TVOUT-WA] wr +0x%x = 0x%08x\n",
                (uint32_t)addr, (uint32_t)value);
    }
}

static const MemoryRegionOps tvout_workaround_ops = {
    .read = tvout_workaround_read,
    .write = tvout_workaround_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

/*
 * Place (or move) the window. NOTHING IS MAPPED UNTIL THE KERNEL SAYS WHERE.
 *
 * The per-board constant used to be installed at machine init, before the guest
 * had announced anything. Since T3 the real address is DERIVED from the kernel's
 * own "AppleMBX: Added swap device: ... id: ..." line, so a blind default has no
 * remaining job -- it is four bytes of guessed kernel heap, and MBX_HANDOFF.md
 * is blunt about what that silence cost once. Worse, it is now known to be
 * actively harmful: the same offset that is inert on a TVOut object lands inside
 * a LIVE AppleH1CLCD on iPhone OS 1.0, where the kernel dereferences the zero it
 * reads and panics (fault_addr=0x0, 2 of 2 boots).
 *
 * So 1.0 and the iPod carry no window at all unless a TVOut device is announced,
 * and 1.1.4 gets one only at an address the kernel supplied.
 */
static void tvout_workaround_move(hwaddr pa)
{
    if (!tvout_wa_region || pa == tvout_wa_addr) {
        return;
    }
    if (tvout_wa_mapped) {
        memory_region_del_subregion(get_system_memory(), tvout_wa_region);
    }
    memory_region_add_subregion_overlap(get_system_memory(), pa,
                                        tvout_wa_region, 1);
    if (tvout_wa_mapped) {
        fprintf(stderr, "[TVOUT-WA] window moved 0x%08x -> 0x%08x "
                "(derived from the guest's own announcement)\n",
                (uint32_t)tvout_wa_addr, (uint32_t)pa);
    } else {
        fprintf(stderr, "[TVOUT-WA] window PLACED at 0x%08x (first placement; "
                "derived from the guest's own announcement -- nothing was "
                "mapped before this)\n", (uint32_t)pa);
    }
    tvout_wa_mapped = true;
    tvout_wa_addr = pa;
}

/*
 * M68AP button idle levels.
 *
 * The iPhone's five buttons sit on GPIO port 0x16 (see ipod_touch_gpio.h for
 * the device-tree evidence). MEASURED polarity for the volume pair: the pins
 * are ACTIVE LOW, so the model's all-zero port reads as "both volume buttons
 * held down" from the moment the OS starts. The buttons driver samples this
 * port only twice and then relies on interrupts, so the press never ends: the
 * ringer/volume HUD appears on the home screen and stays there forever.
 * (Proof: idling volup high drives the ringer volume to MINIMUM -- voldown
 * still held -- and idling voldown high drives it to MAXIMUM.)
 *
 * Why this is applied when the kernel starts rather than at reset: iBoot also
 * samples this port, at t=0.087 s (pc=0x180024ba), to decide its boot mode.
 * Presenting released volume buttons that early makes it take a path that
 * panics the kernel before the OS version is even set -- reproduced 3/3.
 * Installing the levels at the kernel banner is after iBoot's sampling and
 * long before the driver's first read (t=64 s), so both consumers see what
 * they expect.
 *
 * SHORTCUT, recorded honestly: the faithful model would drive these pins from
 * reset and understand what iBoot does with them. Override the mask with
 * IT_M68AP_GPIO_IDLE=<hex> (0 disables) to keep experimenting.
 */
static void ipod_touch_button_idle_level(void)
{
    IPodTouchMachineState *nms = g_ipod_touch_nms;
    const char *env;
    uint32_t mask;

    if (!nms || !nms->gpio_state || nms->board_id != BOARD_ID_M68AP) {
        return;
    }
    mask = IPOD_TOUCH_GPIO_M68AP_IDLE;
    env = getenv("IT_M68AP_GPIO_IDLE");
    if (env && *env) {
        mask = (uint32_t)strtoul(env, NULL, 0);
    }
    if (!mask || (nms->gpio_state->gpio_state & mask) == mask) {
        return;
    }
    nms->gpio_state->gpio_state |= mask;
    fprintf(stderr, "[BTN] M68AP button idle levels applied (mask 0x%x): "
            "volume buttons released\n", mask);
}

/*
 * Console tap. Three jobs: derive the workaround address from the kernel's own
 * "Added swap device" announcement, install the button idle levels once the
 * kernel is running, and refuse to fail silently.
 */
static void ipod_touch_console_line(const char *line)
{
    /*
     * Match ANY swap device, not the TVOut literal.
     *
     * TV-out does not exist in iPhone OS 1.0: `AppleH1TVOut` appears 0 times in
     * its kernelcache (4 times in 1.1.4), because TV-out arrived in the 1.1
     * line. 1.0 announces
     *
     *   AppleMBX: Added swap device: AppleH1CLCD  id: c0813200
     *   AppleMBX: Using AppleH1CLCD as legacy swap device
     *
     * so a match on "AppleH1TVOut" never fired on 1.0 (measured: zero
     * [TVOUT-WA] lines in a full 1.0 boot) and the swap-device field was never
     * neutralised there. AppleMBX's teardown polls the same field whichever
     * device is registered, so derive from whatever the kernel names.
     */
    const char *p = strstr(line, "Added swap device: ");

    /* Only the swap-device handling is gated: the Darwin-banner hook below
     * applies the button idle levels and is unrelated to this workaround. */
    if (p && tvout_wa_enabled()) {
        char devname[32] = "?";
        bool is_tvout;

        sscanf(p + 19, "%31s", devname);
        is_tvout = strstr(devname, "TVOut") != NULL;
        /*
         * PREFER TVOut when the kernel announces more than one. 1.1.4 announces
         * BOTH -- AppleH1TVOut at VA 0xc09c8400 and AppleH1CLCD at 0xc09c8800 --
         * and a plain last-one-wins would silently move the window off the
         * object the original workaround targeted, on a build that works today.
         * 1.0 announces only AppleH1CLCD ("legacy swap device"), so it still
         * gets a window; the iPod (1.1 / 3A101a) announces only AppleH1CLCD too.
         */
        if (tvout_wa_is_tvout && !is_tvout) {
            return;
        }
        tvout_wa_is_tvout = is_tvout;
        /*
         * ONLY place the window on a TVOut swap device.
         *
         * Generalising this to any swap device (23cc69c033) was measured to
         * BREAK iPhone OS 1.0, deterministically -- 2 of 2 boots with it,
         * 2 of 2 without, on one binary via IT_TVOUT_WA:
         *
         *   enabled   scanout 0.0%     1738 serial lines, then
         *             panic(cpu 0 caller 0xC00628CC): kernel abort type 4:
         *             fault_type=0x1, fault_addr=0x0
         *   disabled  scanout 45.4%    3230 lines, home screen
         *
         * The mechanism is the fault address. This window is a 4-byte MMIO
         * region whose reads return ZERO, punched over a field of the swap
         * device object. On a TVOut object that field is the one the hung
         * teardown polls, so reading zero is the point. On 1.0 the same offset
         * lands in AppleH1CLCD -- a LIVE object the kernel dereferences -- and
         * it faults on the zero it reads.
         *
         * And it bought nothing: the commit that generalised it reported
         * 3_home_returns still 0.00%, so 1.0 was never fixed by it, only
         * broken. The naming/reporting improvements from that commit are kept;
         * only the targeting is restored.
         */
        if (!is_tvout) {
            fprintf(stderr, "[TVOUT-WA] %s is not a TVOut swap device - "
                    "leaving the window alone (placing it here faults 1.0; "
                    "set IT_TVOUT_WA=0 to remove the window entirely)\n",
                    devname);
            return;
        }
        p = strstr(p, "id:");
        if (p) {
            uint32_t va = (uint32_t)strtoul(p + 3, NULL, 16);
            if (va >= KERNEL_VA_BASE) {
                hwaddr pa = (va - KERNEL_VA_BASE) + RAM_MEM_BASE +
                            TVOUT_WA_FIELD_OFFSET;
                tvout_wa_derived = true;
                if (pa != tvout_wa_addr) {
                    fprintf(stderr, "[TVOUT-WA] board default 0x%08x is WRONG "
                            "for this kernel (%s swap device at VA 0x%08x) - "
                            "moving the window\n",
                            (uint32_t)tvout_wa_addr, devname, va);
                } else {
                    fprintf(stderr, "[TVOUT-WA] derived 0x%08x from the guest "
                            "(%s swap device VA 0x%08x + 0x%x) - matches the "
                            "board default\n", (uint32_t)pa, devname, va,
                            TVOUT_WA_FIELD_OFFSET);
                }
                tvout_workaround_move(pa);
            }
        }
        return;
    }
    if (strstr(line, "Darwin Kernel Version")) {
        ipod_touch_button_idle_level();
    }
    /* SpringBoard is the consumer that hangs when the window is misplaced, so
     * its start is the moment to check that the window is real. */
    if (!tvout_wa_reads && strstr(line, "SpringBoard[")) {
        static bool warned;
        if (!warned) {
            warned = true;
            fprintf(stderr,
                    "[TVOUT-WA] WARNING: window at 0x%08x has never been read "
                    "(%s). If the display now hangs after "
                    "IOMobileFramebufferUserClient::attach(AppleH1TVOut), this "
                    "is why: the swap-device object is elsewhere in this "
                    "kernel build.\n", (uint32_t)tvout_wa_addr,
                    tvout_wa_derived ? "address was derived"
                                     : "no 'Added swap device' line seen");
        }
    }
}

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
 * iBoot's warm resume runs a mandatory pre-boot charging wait before the
 * type-4 handoff: its boot task enters a charging dispatcher whose loop sleeps
 * in 5-second chunks until a charge budget of 10,000,000 microseconds elapses.
 * On this emulated device the charge is fiction — there is no battery — so
 * every Power/Home wake stalled about 10 seconds inside iBoot before
 * `System Wake`. Shrink both durations in the freshly loaded iBoot image
 * (never the file on disk).
 *
 * Both words are LOCATED BY PATTERN, not by address. They used to be the fixed
 * offsets 0x21458 and 0x09980, which were read off the N45AP iBoot: 0x09980
 * is an iPod address, so on the iPhone the poll-sleep word has NEVER matched
 * and that half of the fix has been silently skipped since it was written.
 * The bare constants are not unique either (10,000,000 occurs 4x and 5,000,000
 * 3x in every image), so each is anchored on its neighbouring pool words:
 *
 *   budget: 8 zero bytes, 0x00000008, <10,000,000>, 0x00000140
 *   poll:   the pool pointer 0x18021398 immediately followed by <5,000,000>
 *
 * Measured unique, and landing on the correct site, in all four images on hand
 * (4A102 0x21458/0xa200, 3A109a 0x21458/0xa1c0, 1A543a 0x21438/0x9b14, and
 * N45AP 0x21458/0x9980 — the last reproducing the original hardcoded pair).
 * A non-unique match leaves the word alone and says so.
 */
static bool ipod_touch_find_unique(const uint8_t *hay, size_t hay_len,
                                   const uint8_t *needle, size_t needle_len,
                                   size_t *offset_out)
{
    size_t found = 0;

    if (hay_len < needle_len) {
        return false;
    }
    for (size_t i = 0; i + needle_len <= hay_len; i++) {
        if (memcmp(hay + i, needle, needle_len) == 0) {
            if (++found > 1) {
                return false;
            }
            *offset_out = i;
        }
    }
    return found == 1;
}

static void ipod_touch_patch_iboot_charge_wait(uint8_t *iboot, size_t size)
{
    static const uint8_t budget_sig[] = {
        0, 0, 0, 0, 0, 0, 0, 0,   /* padding ahead of the pool entry  */
        0x08, 0x00, 0x00, 0x00,   /* 0x00000008                       */
        0x80, 0x96, 0x98, 0x00,   /* 10,000,000  <- patched           */
        0x40, 0x01, 0x00, 0x00,   /* 0x00000140                       */
    };
    static const uint8_t poll_sig[] = {
        0x98, 0x13, 0x02, 0x18,   /* pool pointer 0x18021398          */
        0x40, 0x4b, 0x4c, 0x00,   /* 5,000,000   <- patched           */
    };
    static const struct {
        const char *what;
        const uint8_t *sig;
        size_t sig_len;
        size_t word_off;
        uint32_t replacement;
    } patches[] = {
        { "budget", budget_sig, sizeof(budget_sig), 12, 5000 },
        { "poll sleep", poll_sig, sizeof(poll_sig), 4, 100000 },
    };

    for (size_t i = 0; i < ARRAY_SIZE(patches); i++) {
        size_t at;

        if (!ipod_touch_find_unique(iboot, size, patches[i].sig,
                                    patches[i].sig_len, &at)) {
            fprintf(stderr, "[WAKE] iBoot charge-wait %s pattern is not unique "
                    "in this image; leaving unpatched\n", patches[i].what);
            continue;
        }
        stl_le_p(iboot + at + patches[i].word_off, patches[i].replacement);
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
        ipod_touch_patch_iboot_charge_wait(iboot_data, iboot_size);
        cpu_physical_memory_write(IBOOT_BASE, iboot_data, iboot_size);
        g_free(iboot_data);
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
        nms->lcd_state->relight_input_fast = false;
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
/*
 * MBX register 0x12C bit 6 (0x40) -- the bit AppleMBX spins on.
 *
 * On iPhone OS 1.0, dismissing an app with HOME leaves
 * com.apple.driver.AppleMBX in a nine-instruction loop:
 *
 *   0xc0336010  mov r0, r4              ; the MBX object
 *   0xc0336014  mov r1, #0x12c          ; the register offset
 *   0xc0336018  blx r5                  ; -> ldr r0,[r0,r1]; bx lr  (register read)
 *   0xc0336024  tst r3, #0x40
 *   0xc0336028  beq 0xc0336010          ; loop while bit 6 is CLEAR
 *
 * i.e. `do { v = mbx_read(base, 0x12C); } while (!(v & 0x40));`. Measured with
 * scripts/spin-locate.py (30/30 PC samples in the kernel, exact period 9) and
 * attributed with scripts/kernel-addr-symbolize.py against the RELEASE
 * kernelcache's kmod_info list. This stub returned 0x100 -- bit 8 set, bit 6
 * clear -- so the driver span forever at ~0.98 host cores and starved every
 * other process, which is why no further GSEvent was delivered and why touch
 * died together with the button (see IN_APP_BUTTON_INVESTIGATION.md).
 *
 * Reporting the bit as set is a STUB ANSWER, not a model: it says "the
 * operation you are waiting for is complete" unconditionally. The honest fix is
 * task T1 -- model swap completion and wire the TVOut SDO IRQ (MBX_HANDOFF.md).
 * `IT_MBX_READY=0` restores the old value for an A/B.
 */
/*
 * The register conversation, measured with IT_MBX_TRACE=1 during a 1.0 app
 * dismissal, says these are PowerVR-style EVENT registers:
 *
 *   rd 0x12c = 0x140          status
 *   WR 0x134 = 0x00000040     clear bit 6      <-- the guest ACKs the event
 *   WR 0x134 = 0x0000ffff     clear everything
 *   WR 0x130 = 0x00000000     host enable = 0  <-- interrupts MASKED OFF
 *   WR 0x130 = 0x0000ffff     (once, during setup)
 *   rd 0x1020 = 0x00010000 ; WR 0x1020 = 0x00010001 ; rd 0x1020 = 0x00010000
 *                             kick bit 0, read back clear = "accepted, done"
 *
 * So: 0x12C = event status, 0x130 = host enable (mask), 0x134 = host clear.
 * Two things follow, and both matter:
 *
 *  - The driver POLLS. It writes 0x130 = 0 (all events masked) and then spins on
 *    0x12C. So an interrupt would not be consumed on this path, and "wire the
 *    TVOut SDO IRQ" -- T1's phrasing, inherited from the render investigation --
 *    is NOT the lever for this bug. The IRQ line below exists for the paths that
 *    DO enable events, and is gated by the guest's own mask, so it cannot
 *    misfire while the mask is zero.
 *  - Reporting bit 6 permanently set (IT_MBX_READY, the shipped default) is a
 *    lie the guest can see: it writes 0x134 = 0x40 to acknowledge, re-reads, and
 *    the bit is still there. IT_MBX_EVENTS=1 selects the modelled behaviour --
 *    the bit is raised when an operation is kicked and cleared when the guest
 *    acknowledges it.
 *
 * Neither mode models any actual rendering; there is no datasheet, and the
 * region is otherwise a stub. See MBX_HANDOFF.md and
 * IN_APP_BUTTON_INVESTIGATION.md.
 */
#define MBX_EVENT_READY   0x100     /* bit 8: what the stub always reported */
#define MBX_EVENT_DONE    0x040     /* bit 6: the bit AppleMBX spins on */

static uint32_t mbx_event_status = MBX_EVENT_READY;
static uint32_t mbx_event_enable;
static qemu_irq mbx_irq;

static bool mbx_events_modelled(void)
{
    static int mode = -1;

    if (mode < 0) {
        const char *e = getenv("IT_MBX_EVENTS");
        mode = e && e[0] != '0';
    }
    return mode;
}

static bool mbx_ready_bit(void)
{
    static int ready = -1;

    if (ready < 0) {
        const char *e = getenv("IT_MBX_READY");
        ready = !(e && e[0] == '0');
    }
    return ready;
}

static void mbx_update_irq(void)
{
    if (mbx_irq) {
        qemu_set_irq(mbx_irq, (mbx_event_status & mbx_event_enable) != 0);
    }
}

static uint32_t mbx_status_12c(void)
{
    if (mbx_events_modelled()) {
        return mbx_event_status;
    }
    return mbx_ready_bit() ? (MBX_EVENT_READY | MBX_EVENT_DONE)
                           : MBX_EVENT_READY;
}

/*
 * IT_MBX_TRACE=1: the MBX register conversation.
 *
 * The whole region is a stub, so the only way to learn what the driver actually
 * expects is to watch the accesses. Needed because the completion the guest
 * waits for cannot be modelled honestly without knowing which register kicks
 * the operation and which reports it done. Repeats collapse per register: the
 * first 12 print, then every 1024th, so a poll cannot bury a one-off write.
 */
static void mbx_trace(const char *dir, hwaddr addr, uint64_t val)
{
    static int enabled = -1;
    static uint32_t counts[0x400];

    if (enabled < 0) {
        enabled = getenv("IT_MBX_TRACE") != NULL;
    }
    if (!enabled) {
        return;
    }
    uint32_t slot = (uint32_t)((addr >> 2) & 0x3FF);
    uint32_t n = ++counts[slot];
    if (n > 12 && (n & 0x3FF) != 0) {
        return;
    }
    fprintf(stderr, "[MBX] %s 0x%05x = 0x%08x (n=%u)\n",
            dir, (uint32_t)addr, (uint32_t)val, n);
}

static uint64_t s5l8900_mbx_read(void *opaque, hwaddr addr, unsigned size)
{
    uint64_t r = 0;

    switch (addr) {
        case 0x12c:
            r = mbx_status_12c();
            break;
        case 0x130:
            r = mbx_event_enable;
            break;
        case 0xf00:
            r = (1 << 0x18) | 0x10000;
            break;
        case 0x1020:
            r = 0x10000;
            break;
        default:
            break;
    }
    mbx_trace("rd", addr, r);
    return r;
}

static void s5l8900_mbx_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    mbx_trace("WR", addr, val);

    if (!mbx_events_modelled()) {
        return;             /* the shipped default: registers are inert */
    }
    switch (addr) {
        case 0x130:         /* event host enable (mask) */
            mbx_event_enable = (uint32_t)val;
            mbx_update_irq();
            break;
        case 0x134:         /* event host clear: write 1s to ack */
            mbx_event_status &= ~(uint32_t)val;
            mbx_event_status |= MBX_EVENT_READY;
            mbx_update_irq();
            break;
        /*
         * The KICK. Derived from the trace, not guessed: with IT_MBX_EVENTS=1
         * and completion tied to 0x1020 bit 0 the guest span 46 MILLION times on
         * `rd 0x12c = 0x100`, which disproved that guess outright. The ordered
         * trace shows the last write before the endless poll is
         *
         *   WR 0x00824 = 0x0001d000   WR 0x00828 = 0x00000022
         *   WR 0x0082c = 0x00000025   WR 0x00838 = 0x00000001
         *   WR 0x0083c = 0x00021000   WR 0x006d8 = 0x09000000   <-- then poll
         *
         * i.e. a command descriptor at 0x824..0x83c followed by a kick at 0x6d8.
         */
        case 0x6d8:
        case 0x1020:        /* 0x1020 bit 0 also starts work on other paths */
            if (addr == 0x6d8 || (val & 1)) {
                /* Nothing is actually rendered, so completion is immediate. */
                mbx_event_status |= MBX_EVENT_DONE;
                mbx_update_irq();
            }
            break;
        default:
            break;
    }
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

    /*
     * Uncached alias of SDRAM: bit 31 of the address selects the uncached view
     * (0x88000000 == 0x08000000 | 0x80000000). iBoot-159 hands such addresses
     * to the NAND ECC/DMA engine, which is why they must be backed.
     */
    MemoryRegion *ram_uncached_alias = g_new(MemoryRegion, 1);
    memory_region_init_alias(ram_uncached_alias, OBJECT(machine),
                             "ram-uncached-alias", main_ram, 0, 0x8000000);
    memory_region_add_subregion(sysmem, RAM_MEM_BASE | UNCACHED_MEM_BIT,
                                ram_uncached_alias);

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
        MemoryRegion *iboot_ram = allocate_ram(sysmem, "iboot", IBOOT_BASE,
                                               0x400000);
        /*
         * ...and the same uncached view of the iBoot RAM window, which is where
         * iBoot-159's heap lives: its NAND read buffer at 0x98031258 is the
         * uncached alias of 0x18031258, just past the 0x22000-byte image. With
         * this unmapped the ECC engine's DMA had nowhere to land, so the WMR
         * signature scan compared zeroes against the 0x43303030 it expected and
         * reported "no signature or no production format" -- on a NAND that was
         * correct all along.
         */
        MemoryRegion *iboot_uncached = g_new(MemoryRegion, 1);
        memory_region_init_alias(iboot_uncached, OBJECT(machine),
                                 "iboot-uncached-alias", iboot_ram, 0,
                                 0x400000);
        memory_region_add_subregion(sysmem, IBOOT_BASE | UNCACHED_MEM_BIT,
                                    iboot_uncached);
        address_space_rw(nsas, IBOOT_BASE, MEMTXATTRS_UNSPECIFIED, (uint8_t *)file_data, fsize, 1);
     }

    // // load LLB
    // file_data = NULL;
    // if (g_file_get_contents("/Users/martijndevos/Documents/ipod_touch_emulation/LLB.n45ap.RELEASE", (char **)&file_data, &fsize, NULL)) {
    //     allocate_ram(sysmem, "llb", LLB_BASE, align_64k_high(fsize));
    //     address_space_rw(nsas, LLB_BASE, MEMTXATTRS_UNSPECIFIED, (uint8_t *)file_data, fsize, 1);
    //  }

    allocate_ram(sysmem, "edgeic", EDGEIC_MEM_BASE, 0x1000);
    if (nms->board_id == BOARD_ID_M68AP) {
        // M68AP iBoot reboots by poking the watchdog then spinning; back it
        // with real reset semantics so an early panic reboots instead of
        // hanging forever. N45AP keeps the historical inert-RAM behavior.
        MemoryRegion *watchdog = g_new(MemoryRegion, 1);
        memory_region_init_io(watchdog, OBJECT(machine), &ipod_touch_watchdog_ops,
                              nms, "watchdog", align_64k_high(0x1));
        memory_region_add_subregion(sysmem, WATCHDOG_MEM_BASE, watchdog);
    } else {
        allocate_ram(sysmem, "watchdog", WATCHDOG_MEM_BASE, align_64k_high(0x1));
    }

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

static char *ipod_touch_get_epoch(Object *obj, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    return g_strdup_printf("%u", nms->sysic_epoch_override);
}

static void ipod_touch_set_epoch(Object *obj, const char *value, Error **errp)
{
    IPodTouchMachineState *nms = IPOD_TOUCH_MACHINE(obj);
    nms->sysic_epoch_override = (uint32_t)g_ascii_strtoull(value, NULL, 0);
}

static void ipod_touch_instance_init(Object *obj)
{
	object_property_add_str(obj, "bootrom", ipod_touch_get_bootrom_path, ipod_touch_set_bootrom_path);
    object_property_set_description(obj, "bootrom", "Path to the S5L8900 bootrom binary");

    object_property_add_str(obj, "iboot", ipod_touch_get_iboot_path, ipod_touch_set_iboot_path);
    object_property_set_description(obj, "iboot", "Path to the iBoot binary");

    object_property_add_str(obj, "nand", ipod_touch_get_nand_path, ipod_touch_set_nand_path);
    object_property_set_description(obj, "nand", "Path to the NAND files");

    object_property_add_str(obj, "epoch", ipod_touch_get_epoch, ipod_touch_set_epoch);
    object_property_set_description(obj, "epoch",
        "Override the SYSIC POWER_ID security epoch (default: per-board; N45AP=2, M68AP=3). "
        "Lets cross-board firmware boot, e.g. -M iPhone-2G,epoch=2 with n45ap images.");
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

/*
 * Home is on a DIFFERENT pin on the two boards: N45AP puts it on 0x1606 (IRQ
 * 0x2E), M68AP's device tree puts button_menu on 0x1600 (IRQ 0x28, derived --
 * see ipod_touch_gpio.h). Power/hold is 0x1605/0x2D on both, which is why the
 * board-blind code that shipped until 2026-07-26 slept the iPhone with P and
 * then ignored H forever.
 */
static bool ipod_touch_is_m68ap(void)
{
    return g_ipod_touch_nms &&
           g_ipod_touch_nms->board_id == BOARD_ID_M68AP;
}

static uint32_t ipod_touch_home_pin(void)
{
    return ipod_touch_is_m68ap() ? GPIO_BUTTON_M68AP_MENU : GPIO_BUTTON_HOME;
}

static uint32_t ipod_touch_home_irq(void)
{
    const char *env;

    if (!ipod_touch_is_m68ap()) {
        return GPIO_BUTTON_HOME_IRQ;
    }
    env = getenv("IT_M68AP_HOME_IRQ");
    if (env && *env) {
        return (uint32_t)strtoul(env, NULL, 0);
    }
    return GPIO_BUTTON_M68AP_MENU_IRQ;
}

static void ipod_touch_key_event(void *opaque, int keycode)
{
    bool do_irq = false;
    bool is_power = false;
    int gpio_group = 0, gpio_selector = 0;

    IPodTouchMultitouchState *s = (IPodTouchMultitouchState *)opaque;

    if (getenv("IT_KEY_TRACE")) {
        fprintf(stderr, "[KEYTRACE] keycode=%d pmu=%p active=%d parked=%d "
                "no_park=%d sup_pwr=%d sup_home=%d oocshdwn=%d\n",
                keycode, (void *)s->pmu,
                s->pmu ? s->pmu->prewarm_active : -1,
                s->pmu ? s->pmu->prewarm_parked : -1,
                s->pmu ? s->pmu->prewarm_no_park : -1,
                s->suppress_power_release, s->suppress_home_release,
                s->pmu ? s->pmu->oocshdwn_fired : -1);
    }

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
            /*
             * Undo the SUSPEND, not just the stop.
             *
             * The park uses vm_stop(RUN_STATE_SUSPENDED), and QEMU records that
             * in a separate, sticky flag: vm_prepare_start() reads
             *
             *     RunState state = vm_was_suspended ? RUN_STATE_SUSPENDED
             *                                       : RUN_STATE_RUNNING;
             *
             * so a bare vm_start() on a machine that has ever parked puts it
             * straight back to SUSPENDED and never resumes the vCPUs. Only a
             * system reset clears the flag (vm_set_suspended(false) at the end
             * of qemu_system_reset), which is why the reset-based wake branch
             * below has always worked and the plain resume branch has not.
             *
             * Live, the damage was invisible: the reset branch is the common
             * one on iBoot-159, and the resume branch left the flag set on a
             * machine that then ran anyway because vm_start() had already been
             * called once before. It becomes visible the moment the machine is
             * MIGRATED -- migration/global_state.c ships vm_was_suspended, so
             * every snapshot of a device that had ever slept restored to a
             * suspended machine: a live-looking panel (the restored frame,
             * painted once) with no vCPU running behind it.
             */
            vm_set_suspended(false);
            if (pmu->prewarm_no_park) {
                /* Parked without a type-4 commit (iBoot-159), so the guest is
                 * sitting in its power-off spin rather than just before the
                 * kernel handoff: resuming it would only continue the spin
                 * with the panel dark. Start the retained-RAM wake boot for
                 * real, on demand. Costs a boot, but only when the user asks
                 * for one -- which is the difference between this and the
                 * spontaneous reboot loop it replaces. */
                fprintf(stderr, "[WAKE] %s starting retained-RAM wake boot\n",
                        keycode == 25 ? "Power" : "Home");
                pmu->retained_int2_reexposed = false;
                ipod_touch_prepare_retained_wake();
                qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
                vm_start();
                return;
            }
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
     * After Power locks the device the guest stays fully awake for many
     * seconds with the panel dark before it commits to deep sleep; buttons
     * in that window must be delivered normally so the OS relights the
     * display, exactly like hardware. Only the short final commit window is
     * different: the kernel arms resume-token bit 7 (0x76 <- 0x80) moments
     * before OOCSHDWN — and iBoot rewrites the register to 0x40 on every
     * wake — so an armed token while awake uniquely identifies that window.
     * A press after that point must be remembered as a hardware wake
     * request so the PMU can reset the SoC once shutdown completes.
     * (Earlier gates — a dark framebuffer, or INT1M == 0xB0 — also matched
     * the ordinary locked or post-resume states and swallowed presses,
     * which made wake appear to take 15+ seconds.)
     */
    if ((keycode == 25 || keycode == 35) && s->pmu &&
        !s->pmu->oocshdwn_fired &&
        (s->pmu->regs[PMU_RESUME_STATUS] & PMU_RESUME_ARMED)) {
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
        // home button (M68AP calls it button_menu and puts it on another pin)
        uint32_t home_pin = ipod_touch_home_pin();
        uint32_t home_irq = ipod_touch_home_irq();

        gpio_group = home_irq / NUM_GPIO_PINS;
        gpio_selector = home_irq % NUM_GPIO_PINS;

        if(keycode == 35 && (s->gpio_state->gpio_state & (1 << (home_pin & 0xf))) == 0) {
            s->gpio_state->gpio_state |= (1 << (home_pin & 0xf));
            do_irq = true;
        }
        else if(keycode == 163) {
            s->gpio_state->gpio_state &= ~(1 << (home_pin & 0xf));
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
        /* Remember the press so a sleep commit racing with it can turn into
         * an immediate wake instead of a silent pre-warm park. */
        if ((keycode == 25 || keycode == 35) && s->pmu) {
            s->pmu->last_button_press_ns =
                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }

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

    nms->board_id = IPOD_TOUCH_MACHINE_GET_CLASS(machine)->board_id;

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
    // M68AP iBoot's miu_init() requires POWER_ID epoch 3; N45AP expects 2.
    // An explicit -M ...,epoch=N wins (used to boot cross-board firmware).
    sysic_state->power_epoch = nms->sysic_epoch_override ?
        nms->sysic_epoch_override : ((nms->board_id == BOARD_ID_M68AP) ? 3 : 2);
    memory_region_add_subregion(sysmem, SYSIC_MEM_BASE, &sysic_state->iomem);
    busdev = SYS_BUS_DEVICE(dev);
    for(int grp = 0; grp < GPIO_NUMINTGROUPS; grp++) {
        sysbus_connect_irq(busdev, grp, s5l8900_get_irq(nms, S5L8900_GPIO_IRQS[grp]));
    }

    // init GPIO
    dev = qdev_new("ipodtouch.gpio");
    IPodTouchGPIOState *gpio_state = IPOD_TOUCH_GPIO(dev);
    nms->gpio_state = gpio_state;
    /* M68AP button idle levels are installed once the KERNEL starts, not at
     * reset -- see ipod_touch_button_idle_level() and the comment above it. */
    memory_region_add_subregion(sysmem, GPIO_MEM_BASE, &gpio_state->iomem);

    // init SDIO
    dev = qdev_new("ipodtouch.sdio");
    qemu_configure_nic_device(dev, true, "mv8686");
    IPodTouchSDIOState *sdio_state = IPOD_TOUCH_SDIO(dev);
    nms->sdio_state = sdio_state;
    sysbus_realize(SYS_BUS_DEVICE(dev), &error_fatal);
    memory_region_add_subregion(sysmem, SDIO_MEM_BASE, &sdio_state->iomem);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0,
                       s5l8900_get_irq(nms, S5L8900_SDIO_IRQ));

    dev = exynos4210_uart_create(UART0_MEM_BASE, 256, 0, serial_hd(0), nms->irq[0][24]);
    if (!dev) {
        printf("Failed to create uart0 device!\n");
        abort();
    }

    // The iPhone's S-Gold2 baseband hangs off UART1; give it an AT-command
    // stub there so radio (and vibrator) bring-up sees "OK" instead of silence.
    Chardev *uart1_chr = serial_hd(1);
    if (nms->board_id == BOARD_ID_M68AP && !getenv("IT_M68AP_NO_BASEBAND")) {
        uart1_chr = qemu_chardev_new("sgold2-baseband", TYPE_CHARDEV_SGOLD2, NULL, NULL, &error_fatal);
    }
    dev = exynos4210_uart_create(UART1_MEM_BASE, 256, 1, uart1_chr, nms->irq[0][25]);
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
    // the iPhone's touch controller runs the Zephyr1 firmware/protocol
    // (IT_FORCE_MT_Z2=1 is a lab knob: answer in Zephyr2 semantics instead,
    // to probe whether SpringBoard's render wait involves the Z1 dialogue)
    spi2_state->mt->zephyr1 = (nms->board_id == BOARD_ID_M68AP) &&
                              !getenv("IT_FORCE_MT_Z2");
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
    nand_state->num_banks = (nms->board_id == BOARD_ID_M68AP) ? 4 : 8;
    nms->nand_state = nand_state;
    memory_region_add_subregion(sysmem, NAND_MEM_BASE, &nand_state->iomem);

    // init NAND ECC module
    dev = qdev_new("itnand_ecc");
    ITNandECCState *nand_ecc_state = ITNANDECC(dev);
    nms->nand_ecc_state = nand_ecc_state;
    nand_ecc_state->nand_state = nand_state;
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

    if (nms->board_id == BOARD_ID_M68AP) {
        // the iPhone's ISL29003 ambient light sensor (8-bit address 0x92)
        i2c_slave_create_simple(i2c_state->bus, "isl29003", 0x49);
    }

    /*
     * The PMU is on a DIFFERENT I2C controller per board. The M68AP device
     * tree makes `pmu,pcf50635` a child of the i2c0 node; N45AP puts it on
     * i2c1. Attaching it to i2c1 for both meant every M68AP PMU read returned
     * 0xFF (an unanswered bus), which 1.1.x survives -- it just believes it is
     * permanently on external power and disables idle sleep -- but which stalls
     * the 1.0 kernel in IOIpodUSBDevice's power path.
     */
    I2CBus *pmu_bus = i2c_state->bus;

    dev = qdev_new("ipodtouch.i2c");
    i2c_state = IPOD_TOUCH_I2C(dev);
    nms->i2c1_state = i2c_state;
    busdev = SYS_BUS_DEVICE(dev);
    sysbus_connect_irq(busdev, 0, s5l8900_get_irq(nms, S5L8900_I2C1_IRQ));
    memory_region_add_subregion(sysmem, I2C1_MEM_BASE, &i2c_state->iomem);

    if (nms->board_id != BOARD_ID_M68AP) {
        pmu_bus = i2c_state->bus;   /* N45AP: i2c1 */
    }

    // init the PMU
    I2CSlave *pmu = i2c_slave_create_simple(pmu_bus, "pcf50633", 0x73);
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
    /*
     * The MBX event line. The S5L8900 IRQ map in ipod_touch.h has no MBX entry
     * -- only LCD (0xD), TVOUT_SDO (0x1E) and TVOUT_MIXER (0x26) -- and T1's
     * belief that MBX completion surfaces through the TVOut SDO IRQ comes from a
     * guest log line ("AppleMBX: Added swap device: AppleH1TVOut"), not from an
     * observed acknowledge. So the line is PLUMBED but not bound by default:
     * IT_MBX_IRQ=<n> attaches it to SoC interrupt n (30 = TVOUT_SDO), which lets
     * that hypothesis be tested with an env var instead of a rebuild. Left
     * unbound, mbx_update_irq() is a no-op and nothing can regress -- and note
     * the guest masks all MBX events (0x130 = 0) on the dismissal path anyway,
     * so an interrupt is not what that path is waiting for.
     */
    const char *mbx_irq_env = getenv("IT_MBX_IRQ");
    if (mbx_irq_env && mbx_irq_env[0] && mbx_irq_env[0] != '0') {
        int n = (int)strtol(mbx_irq_env, NULL, 0);
        if (n > 0 && n < 0x40) {
            mbx_irq = s5l8900_get_irq(nms, n);
            fprintf(stderr, "[MBX] event line bound to SoC IRQ %d\n", n);
        }
    }

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

    /*
     * The TVOut swap-device window. The region is CREATED here but deliberately
     * NOT MAPPED: the address comes from the kernel's own announcement (see
     * tvout_workaround_move). The per-board constants survive only as the
     * expected value, so a mismatch can still be reported.
     */
    if (tvout_wa_enabled()) {
        iomem = g_new(MemoryRegion, 1);
        memory_region_init_io(iomem, OBJECT(nms), &tvout_workaround_ops, NULL, "tvoutworkaround", 0x4);
        tvout_wa_region = iomem;
        tvout_wa_addr = (nms->board_id == BOARD_ID_M68AP) ?
                        TVOUT_WORKAROUND_M68AP_MEM_BASE :
                        TVOUT_WORKAROUND_MEM_BASE;
        fprintf(stderr, "[TVOUT-WA] armed but NOT mapped; expecting 0x%08x, "
                "waiting for the guest to announce a TVOut swap device\n",
                (uint32_t)tvout_wa_addr);
    } else {
        fprintf(stderr, "[TVOUT-WA] disabled by IT_TVOUT_WA=0\n");
    }
    ipod_touch_console_tap_install(ipod_touch_console_line);

    qemu_register_reset(ipod_touch_cpu_reset, nms);

    qemu_input_handler_register(DEVICE(spi2_state->mt),
                                &ipod_touch_key_handler);
}

static void ipod_touch_machine_class_init(ObjectClass *obj, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(obj);
    IPodTouchMachineClass *imc = IPOD_TOUCH_MACHINE_CLASS(obj);
    mc->desc = "iPod Touch 1G (N45AP)";
    mc->init = ipod_touch_machine_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("arm1176");
    mc->default_nic = TYPE_IPOD_TOUCH_SDIO;
    imc->board_id = BOARD_ID_N45AP;
}

/*
 * The iPhone (2G, M68AP) is the same S5L8900 SoC with the same peripheral
 * layout; the device-specific behaviour lives in the firmware images passed
 * on the command line (m68ap iBoot/NOR/NAND). It therefore inherits the
 * whole iPod Touch machine and only overrides its identity.
 */
static void iphone_2g_machine_class_init(ObjectClass *obj, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(obj);
    IPodTouchMachineClass *imc = IPOD_TOUCH_MACHINE_CLASS(obj);
    mc->desc = "iPhone 2G (M68AP)";
    imc->board_id = BOARD_ID_M68AP;
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

static const TypeInfo iphone_2g_machine_info = {
    .name          = TYPE_IPHONE_2G_MACHINE,
    .parent        = TYPE_IPOD_TOUCH_MACHINE,
    .class_init    = iphone_2g_machine_class_init,
};

static void ipod_touch_machine_types(void)
{
    type_register_static(&ipod_touch_machine_info);
    type_register_static(&iphone_2g_machine_info);
}

type_init(ipod_touch_machine_types)
