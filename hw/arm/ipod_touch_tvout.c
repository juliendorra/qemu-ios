#include "hw/arm/ipod_touch_tvout.h"
#include "target/arm/cpu.h"
#include "hw/core/cpu.h"
#include "qapi/error.h"

/* Part of IT_FB_TRACE (see ipod_touch_lcd.c): the TVOut register traffic.
 * M68AP's SpringBoard attaches the AppleH1TVOut framebuffer and never
 * detaches it (N45AP's does within two log lines); what the driver programs
 * here before its silent wait identifies the completion it expects from
 * this otherwise RAM-backed stub. */
static bool it_tvout_trace_enabled(void)
{
    static int cached = -1;
    if (cached < 0) {
        cached = getenv("IT_FB_TRACE") != NULL;
    }
    return cached;
}

static void it_tvout_trace(IPodTouchTVOutState *s, const char *dir,
                           hwaddr offset, uint64_t value)
{
    static uint32_t n;

    if (!it_tvout_trace_enabled()) {
        return;
    }
    n++;
    if (n <= 256 || (n & 0x3FF) == 0) {
        /* pc/lr name the guest code doing it -- with the SDO interrupt
         * modelled, the interesting traffic is the ISR's own reads, and
         * those are worthless without knowing WHO read. Symbolize with
         * scripts/kernel-addr-symbolize.py. */
        uint32_t pc = 0, lr = 0;
        if (current_cpu) {
            CPUARMState *env = &ARM_CPU(current_cpu)->env;
            pc = env->regs[15];
            lr = env->regs[14];
        }
        fprintf(stderr, "[TVOUT%d] %s 0x%03x = 0x%08x pc=0x%08x lr=0x%08x "
                "(n=%u)\n", s->index, dir, (uint32_t)offset, (uint32_t)value,
                pc, lr, n);
    }
}

/*
 * IT_TVOUT_SDO=1: model the SDO field interrupt (T1, MBX_HANDOFF.md).
 *
 * Measured mechanism (2026-07-31, instrumented 4A102 boots): SpringBoard's
 * TVOut attach queues ONE swap request; AppleH1CLCD's queue-advance
 * (0xc0381b84) stores it as in-flight at [swapdev+0x160] and programs the
 * hardware; the request completes only when the display side reports the
 * frame done (IOMobileGraphicsFamily 0xc037c400 records completion parts
 * into [req+0x7c] until it matches [req+0x80]). Our TVOut was a RAM-backed
 * stub whose SDO IRQ never fired, so the request never completed and
 * teardown blocked -- that is the hang the always-zero swap-device window
 * papers over.
 *
 * The guest's own ISR (AppleH1TVOut, 0xc0383c64 in 4A102 -- symbol-less,
 * decoded from disassembly and CONFIRMED live by pc/lr-attributed register
 * traces) defines the register contract this model implements:
 *
 *   [TVOUT3 + 0x280]  field-interrupt status, bit 0. The ISR returns
 *                     unless bit 0 is set, then ACKS by writing 1
 *                     (write-1-to-clear). TVOUT3 (0x39300000) is the DT's
 *                     tv-out@1300000 node -- the SDO block proper.
 *   [TVOUT2 + 0x004]  bit 1 = current field parity (read by the ISR after
 *                     the ack; TVOUT2 is [obj+0x1f4], the block whose
 *                     reg 0x000 the driver enables with 0x5 and whose
 *                     bit 2 is the shadow-update-pending flag).
 *   [TVOUT3 + 0x040]  bit 1 selects WHICH parity completes a frame; the
 *                     guest never writes it here, it reads 0, and with it
 *                     0 the ISR runs its completion (callback + queue
 *                     advance, 0xc0383b9c) on EVEN fields -- once per
 *                     interlaced frame, ~30 Hz.
 *
 * So: while the guest has the SDO path enabled (TVOUT2 reg 0x000 bit 0), a
 * ~60 Hz field timer on instance 3 toggles the parity bit in its peer
 * (instance 2), latches status bit 0 and raises SoC IRQ 0x1E. The first
 * experiment proved the chain end-to-end BEFORE this contract was exact:
 * even with the latch on the wrong instance (never acked, self-throttled),
 * the guest completed the swap itself -- [swapdev+0x160] went
 * 0xc2afbd00 -> 0 in guest RAM -- and reached the home screen with
 * IT_TVOUT_WA=0, i.e. with no zero-window mapped at all.
 *
 * STORM GUARD, because this subsystem's failure mode is the exynos UART
 * lesson: if a tick arrives and the previous interrupt is still unacked,
 * the line is LOWERED and nothing is raised that tick -- an ISR that never
 * acks sees at most ~30 Hz of self-clearing pulses, never a held level.
 */
bool ipod_touch_tvout_sdo_modelled(void)
{
    static int cached = -1;
    if (cached < 0) {
        /* Default ON since 2026-07-31: verified on 4A102 (boot to home,
         * lock/unlock battery), N45AP (3/3 cycles) and 1A543a (inert -- no
         * TVOut driver in the 1.0 line). IT_TVOUT_SDO=0 restores the old
         * stub AND re-enables the derived zero-window (ipod_touch.c gates
         * the window on this), which is the A/B for this model. */
        const char *e = getenv("IT_TVOUT_SDO");
        cached = !(e && e[0] == '0');
    }
    return cached;
}

static bool it_tvout_sdo_enabled(void)
{
    return ipod_touch_tvout_sdo_modelled();
}

#define TVOUT_SDO_FIELD_INTERVAL_NS (16680000)  /* ~59.94 Hz fields */
#define TVOUT_SDO_FIELD_BIT 0x1
#define TVOUT_SDO_PARITY_REG 0x004              /* on instance 2 */
#define TVOUT_SDO_PARITY_BIT 0x2

static void ipod_touch_tvout_field_tick(void *opaque)
{
    IPodTouchTVOutState *s = (IPodTouchTVOutState *)opaque;    /* instance 3 */

    s->field_parity ^= 1;
    if (s->peer) {
        if (s->field_parity) {
            s->peer->data[TVOUT_SDO_PARITY_REG] |= TVOUT_SDO_PARITY_BIT;
        } else {
            s->peer->data[TVOUT_SDO_PARITY_REG] &= ~TVOUT_SDO_PARITY_BIT;
        }
    }
    if (s->irq_high) {
        /* Previous field interrupt never acked: back off instead of
         * holding the level (see the storm guard note above). */
        qemu_set_irq(s->irq, 0);
        s->irq_high = false;
    } else {
        s->data[SDO_IRQ] |= TVOUT_SDO_FIELD_BIT;
        qemu_set_irq(s->irq, 1);
        s->irq_high = true;
        if (it_tvout_trace_enabled()) {
            static uint32_t ticks;
            ticks++;
            if (ticks <= 8 || (ticks & 0x3FF) == 0) {
                fprintf(stderr, "[TVOUT%d] SDO field tick -> IRQ "
                        "(parity=%u, n=%u)\n", s->index, s->field_parity,
                        ticks);
            }
        }
    }
    timer_mod(s->frame_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              TVOUT_SDO_FIELD_INTERVAL_NS);
}

static uint64_t ipod_touch_tvout_read(void *opaque, hwaddr offset, unsigned size)
{
    IPodTouchTVOutState *s = (IPodTouchTVOutState *)opaque;

    it_tvout_trace(s, "rd", offset, s->data[offset]);
    return s->data[offset];
}

static void ipod_touch_tvout_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    IPodTouchTVOutState *s = (IPodTouchTVOutState *)opaque;

    it_tvout_trace(s, "wr", offset, value);

    if (it_tvout_sdo_enabled()) {
        if (s->index == 3 && offset == SDO_IRQ) {
            /* write-1-to-clear ack -- the ISR writes 1 here (0xc0383c8c) */
            s->data[SDO_IRQ] &= ~(uint32_t)value;
            if (!(s->data[SDO_IRQ] & TVOUT_SDO_FIELD_BIT) && s->irq_high) {
                qemu_set_irq(s->irq, 0);
                s->irq_high = false;
            }
            return;
        }
        /* The enable lives on instance 2 (reg 0x000 bit 0, written as 0x5:
         * enable + shadow-update-pending); the timer lives on instance 3. */
        if (s->index == 2 && offset == 0x000 && s->peer) {
            IPodTouchTVOutState *sdo = s->peer;
            bool enable = (value & 1) != 0;
            if (enable && !sdo->frame_timer_running) {
                if (!sdo->frame_timer) {
                    sdo->frame_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                                    ipod_touch_tvout_field_tick,
                                                    sdo);
                }
                timer_mod(sdo->frame_timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                          TVOUT_SDO_FIELD_INTERVAL_NS);
                sdo->frame_timer_running = true;
                fprintf(stderr, "[TVOUT%d] SDO enabled (0x000 = 0x%08x): "
                        "field interrupt modelled at ~60 Hz (IT_TVOUT_SDO)\n",
                        s->index, (uint32_t)value);
            } else if (!enable && sdo->frame_timer_running) {
                timer_del(sdo->frame_timer);
                sdo->frame_timer_running = false;
                if (sdo->irq_high) {
                    qemu_set_irq(sdo->irq, 0);
                    sdo->irq_high = false;
                }
                sdo->data[SDO_IRQ] = 0;
                fprintf(stderr, "[TVOUT%d] SDO disabled: field interrupt "
                        "stopped\n", s->index);
            }
        }
    }

    s->data[offset] = value;
}

static const MemoryRegionOps ipod_touch_tvout_ops = {
    .read = ipod_touch_tvout_read,
    .write = ipod_touch_tvout_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_tvout_init(Object *obj)
{
    DeviceState *dev = DEVICE(obj);
    IPodTouchTVOutState *s = IPOD_TOUCH_TVOUT(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_tvout_ops, s, "tvout", 4096);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void ipod_touch_tvout_class_init(ObjectClass *klass, const void *data)
{

}

static const TypeInfo ipod_touch_tvout_type_info = {
    .name = TYPE_IPOD_TOUCH_TVOUT,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchTVOutState),
    .instance_init = ipod_touch_tvout_init,
    .class_init = ipod_touch_tvout_class_init,
};

static void ipod_touch_tvout_register_types(void)
{
    type_register_static(&ipod_touch_tvout_type_info);
}

type_init(ipod_touch_tvout_register_types)
