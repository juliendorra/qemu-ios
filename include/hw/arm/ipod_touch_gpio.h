#ifndef IPOD_TOUCH_GPIO_H
#define IPOD_TOUCH_GPIO_H

#include <math.h>
#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/core/sysbus.h"

#define TYPE_IPOD_TOUCH_GPIO                "ipodtouch.gpio"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchGPIOState, IPOD_TOUCH_GPIO)

#define GPIO_BUTTON_POWER 0x1605
#define GPIO_BUTTON_HOME  0x1606

#define GPIO_BUTTON_POWER_IRQ 0x2D
#define GPIO_BUTTON_HOME_IRQ  0x2E

/*
 * M68AP's buttons, read off its own device tree (`buttons` node,
 * compatible "buttons,m68"; see m68ap-artifacts/extracted/DeviceTree.m68ap.bin):
 *
 *   function-button_menu      GPIO 0x1600  flags 0x100
 *   function-button_volup     GPIO 0x1601  flags 0x000
 *   function-button_voldown   GPIO 0x1602  flags 0x000
 *   function-button_ringerab  GPIO 0x1603  flags 0x100
 *   function-button_hold      GPIO 0x1605  flags 0x100   (= power, same as N45AP)
 *
 * The two flag values are two POLARITIES. Pins carrying 0x100 behave like the
 * iPod's power/home in this model (idle low, 1 == pressed); the volume pair
 * carries 0x000 and is idle HIGH, so a pin left at 0 reads as held down.
 * The driver samples this port only twice during boot and then relies on
 * interrupts, so a pin that reads "pressed" at startup stays pressed forever
 * -- which is exactly the stuck ringer/volume HUD on the M68AP home screen.
 * IPOD_TOUCH_GPIO_M68AP_IDLE is therefore the idle level the machine installs.
 */
#define GPIO_BUTTON_M68AP_MENU     0x1600
#define GPIO_BUTTON_M68AP_VOLUP    0x1601
#define GPIO_BUTTON_M68AP_VOLDOWN  0x1602
#define GPIO_BUTTON_M68AP_RINGER   0x1603
#define GPIO_BUTTON_M68AP_HOLD     0x1605

/*
 * The interrupt that goes with each of those pins.
 *
 * N45AP fixes the rule: pin 0x1605 -> IRQ 0x2D and pin 0x1606 -> IRQ 0x2E,
 * i.e. IRQ = 0x28 + (pin & 0xf). Applying it to M68AP's five pins yields
 * {menu 0x28, volup 0x29, voldown 0x2A, ringerab 0x2B, hold 0x2D} -- exactly
 * the set its device tree lists for the `buttons` node, with 0x2C absent
 * because pin 0x1604 is unused. The DT never states the pairing, and its
 * properties are not even stored in pin order (the binary holds ringerab,
 * hold, voldown, menu, volup), so position cannot supply it either.
 *
 * A SECOND, INDEPENDENT field in the same node corroborates the rule. The
 * interrupts property is five (irq, trigger) pairs:
 *
 *   0x2d 7   0x28 7   0x29 5   0x2a 5   0x2b 7
 *
 * Under this pairing every trigger 7 lands on a pin whose GPIO flags are 0x100
 * (hold, menu, ringerab) and every trigger 5 on a pin whose flags are 0x000
 * (volup, voldown) -- a perfect split. Pairing the interrupts against the
 * stored property order instead would demand triggers 7,7,5,7,5, and the list
 * is 7,7,5,5,7, so that reading is REFUTED.
 *
 * It is still inference (set + correlation), not an observed acknowledge, so
 * IT_M68AP_HOME_IRQ overrides it without a rebuild. To settle it: press Home
 * under IT_GPIO_TRACE=stderr and check which status/mask bit the driver acks.
 *
 * Until 2026-07-26 the key handler used N45AP's home pin/IRQ (0x1606/0x2E) on
 * BOTH boards. 0x2E is not in M68AP's list at all, which is why Power (0x1605,
 * shared) worked on the iPhone and Home did nothing.
 */
#define GPIO_BUTTON_M68AP_MENU_IRQ 0x28
#define GPIO_BUTTON_M68AP_HOLD_IRQ 0x2D

#define IPOD_TOUCH_GPIO_M68AP_IDLE \
    ((1 << (GPIO_BUTTON_M68AP_VOLUP & 0xf)) | \
     (1 << (GPIO_BUTTON_M68AP_VOLDOWN & 0xf)))

#define NUM_GPIO_PINS 0x20

typedef struct IPodTouchGPIOState
{
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t gpio_state;
} IPodTouchGPIOState;

#endif