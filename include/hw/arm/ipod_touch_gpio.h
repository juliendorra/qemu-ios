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