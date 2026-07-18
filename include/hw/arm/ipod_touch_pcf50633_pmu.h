#ifndef HW_PCF50633_PMU_H
#define HW_PCF50633_PMU_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/core/sysbus.h"
#include "hw/i2c/i2c.h"
#include "hw/core/irq.h"
#include "time.h"

#define TYPE_PCF50633                 "pcf50633"
OBJECT_DECLARE_SIMPLE_TYPE(Pcf50633State, PCF50633)

// PCF50633/ApplePCF50635 interrupt status registers (read-clears)
#define PMU_INT1  0x02
#define PMU_INT2  0x03
#define PMU_INT3  0x04
#define PMU_INT4  0x05
#define PMU_INT5  0x06

// Interrupt mask registers (standard PCF50633 addresses — NOT remapped by Apple)
#define PMU_INT1M 0x07
#define PMU_INT2M 0x08
#define PMU_INT3M 0x09
#define PMU_INT4M 0x0A
#define PMU_INT5M 0x0B

// INT1 bits
#define PMU_INT1_ADPINS  0x01
#define PMU_INT1_ADPREM  0x02
#define PMU_INT1_USBINS  0x04
#define PMU_INT1_USBREM  0x08
#define PMU_INT1_ALARM   0x40
#define PMU_INT1_SECOND  0x80

// INT2 bits
#define PMU_INT2_ONKEYR  0x01   // ONKEY rising edge (released)
#define PMU_INT2_ONKEYF  0x02   // ONKEY falling edge (pressed)
#define PMU_INT2_EXTON1R 0x04   // N45 board-level wake-button latch

#define PMU_MBCS1 0x4B
#define PMU_MBCS1_USBPRES 0x01
#define PMU_MBCS1_USBOK 0x02
#define PMU_ADCC1 0x54
#define PMU_ADCS1 0x55
#define PMU_ADCS2 0x56
#define PMU_ADCS3 0x57

// RTC registers
#define PMU_RTCSC 0x59
#define PMU_RTCMN 0x5A
#define PMU_RTCHR 0x5B
#define PMU_RTCWD 0x5C
#define PMU_RTCDT 0x5D
#define PMU_RTCMT 0x5E
#define PMU_RTCYR 0x5F

// PMU control registers (PCF50633)
#define PMU_OOCSHDWN 0x0C   // Standby/shutdown control
#define PMU_OOCWAKE  0x0D   // Wake-up source config
#define PMU_OOCSTAT  0x12   // ONKEY and external wake-input state
#define PMU_OOCSTAT_ONKEY 0x01 // 1 = released, 0 = pressed
#define PMU_GPMEM0   0x67   // Battery-backed general-purpose memory
#define PMU_GPMEM1   0x68
#define PMU_GPMEM2   0x69
#define PMU_GPMEM3   0x6A

/* Apple/iBoot retained-resume state. The kernel leaves bit 7 set before
 * OOCSHDWN, the PMU power-on path supplies bit 5, and iBoot clears bit 7 and
 * sets bit 6 when consuming the token. */
#define PMU_RESUME_STATUS 0x76
#define PMU_RESUME_WAKE   0x20
#define PMU_RESUME_ARMED  0x80

// PMU interrupt GPIO on S5L8900: GPIO interrupt 0x55 = group 2, bit 21
#define PMU_INT_GPIO_GROUP    2
#define PMU_INT_GPIO_BIT      21

typedef struct IPodTouchSYSICState IPodTouchSYSICState;

typedef struct Pcf50633State {
	I2CSlave i2c;
	uint32_t cmd;
	bool has_reg_addr;       // true after first byte (register address) received
	// Interrupt status registers (read-clears)
	uint8_t int1;
	uint8_t int2;
	uint8_t int3;
	uint8_t int4;
	uint8_t int5;
	// Wake cause retained across iBoot's read-clear and re-exposed when iBoot
	// commits the type-4 handoff to the retained kernel.
	uint8_t retained_int2_wake;
	bool retained_int2_reexposed;
	// Interrupt mask registers
	uint8_t int1m;
	uint8_t int2m;
	uint8_t int3m;
	uint8_t int4m;
	uint8_t int5m;
    // General-purpose register file (captures all writes for debugging)
    uint8_t regs[256];
    // VIC references for resetting the retained-wake interrupt domain.
    void *vic0;  // PL192State*, forward-declared as void* to avoid header deps
    void *vic1;
    // LCD reference for the PMU-owned panel rail.
    void *lcd;   // IPodTouchLCDState*, forward-declared as void*
	// Interrupt output connected to SYSIC GPIO
	IPodTouchSYSICState *sysic;
    // True after the guest requests application-processor power loss.
    bool oocshdwn_fired;
	// A Power/Home press received during the final display-off transition.
	// Complete OOCSHDWN first, then perform the retained-RAM SoC reboot.
	bool wake_reset_pending;
    /* Pre-warmed wake: after an untouched OOCSHDWN the retained-RAM wake
     * boot runs immediately with the panel off, and the machine parks in
     * RUN_STATE_SUSPENDED just before iBoot's type-4 handoff. A later
     * Power/Home press only has to resume the kernel, so the visible wake
     * latency is the kernel-resume portion alone. The kernel reads the PMU
     * RTC and the retained wake cause after the park point, so wall-clock
     * time and the reported wake reason stay correct. */
    bool prewarm_active;
    bool prewarm_parked;
    bool prewarm_wake_requested;
    /* vm_stop() must run from a bottom half: calling it inside a virtual-
     * clock timer callback deadlocks (pause_all_vcpus disables the clock
     * whose timer list is being dispatched while a vCPU waits on the BQL
     * in an MMIO access). */
    QEMUBH *prewarm_park_bh;
} Pcf50633State;

// Set ONKEY state: call when power button is pressed/released.
// pressed=true sets ONKEYF (falling edge), pressed=false sets ONKEYR (rising).
void pcf50633_set_onkey(Pcf50633State *s, bool pressed);

/* Mark the next reset as a retained wake; optionally checksum retained RAM. */
void ipod_touch_prepare_retained_wake(void);

// Re-evaluate PMU nIRQ output. Call after SYSIC clears GPIO_INTSTAT for
// the PMU's GPIO group — if the PMU still has pending interrupts, it will
// re-assert the GPIO line (level-triggered behavior).
void pcf50633_update_irq(Pcf50633State *s);

#endif
