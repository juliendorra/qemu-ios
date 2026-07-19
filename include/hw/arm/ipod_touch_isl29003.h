#ifndef IPOD_TOUCH_ISL29003_H
#define IPOD_TOUCH_ISL29003_H

#include "qemu/osdep.h"
#include "hw/i2c/i2c.h"

/*
 * ISL29003 ambient light sensor, present on the iPhone 2G (M68AP) at
 * I2C bus 0, address 0x92 (7-bit 0x49). Register map per the openiboot
 * als-ISL29003.c driver. The driver ORs 0x40 (the chip's command bit)
 * into every register address.
 */

#define TYPE_ISL29003 "isl29003"
OBJECT_DECLARE_SIMPLE_TYPE(ISL29003State, ISL29003)

#define ISL29003_REG_COMMAND        0x0
#define ISL29003_REG_CONTROL        0x1
#define ISL29003_REG_INTTHRESHHIGH  0x2
#define ISL29003_REG_INTTHRESHLOW   0x3
#define ISL29003_REG_SENSORLOW      0x4
#define ISL29003_REG_SENSORHIGH     0x5
#define ISL29003_REG_TIMERLOW       0x6
#define ISL29003_REG_TIMERHIGH      0x7

#define ISL29003_NUM_REGS 8

// reported ambient light level (16-bit sensor counts)
#define ISL29003_SENSOR_VALUE 0x0800

typedef struct ISL29003State {
    I2CSlave i2c;
    uint8_t regs[ISL29003_NUM_REGS];
    uint8_t ptr;
    bool addr_byte;
} ISL29003State;

#endif
