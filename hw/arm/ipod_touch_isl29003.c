#include "hw/arm/ipod_touch_isl29003.h"

static int isl29003_event(I2CSlave *i2c, enum i2c_event event)
{
    ISL29003State *s = ISL29003(i2c);

    if (event == I2C_START_SEND) {
        s->addr_byte = true;
    }

    return 0;
}

static uint8_t isl29003_recv(I2CSlave *i2c)
{
    ISL29003State *s = ISL29003(i2c);
    uint8_t reg = s->ptr & (ISL29003_NUM_REGS - 1);
    uint8_t ret;

    switch (reg) {
        case ISL29003_REG_SENSORLOW:
            ret = ISL29003_SENSOR_VALUE & 0xFF;
            break;
        case ISL29003_REG_SENSORHIGH:
            ret = (ISL29003_SENSOR_VALUE >> 8) & 0xFF;
            break;
        default:
            // COMMAND/CONTROL/thresholds read back as written
            ret = s->regs[reg];
            break;
    }

    // auto-increment so 16-bit sensor reads see low then high byte
    s->ptr = (reg + 1) & (ISL29003_NUM_REGS - 1);

    return ret;
}

static int isl29003_send(I2CSlave *i2c, uint8_t data)
{
    ISL29003State *s = ISL29003(i2c);

    if (s->addr_byte) {
        // the driver sets the chip's 0x40 command bit on every address
        s->ptr = data & (ISL29003_NUM_REGS - 1);
        s->addr_byte = false;
    }
    else {
        uint8_t reg = s->ptr & (ISL29003_NUM_REGS - 1);
        s->regs[reg] = data;
        s->ptr = (reg + 1) & (ISL29003_NUM_REGS - 1);
    }

    return 0;
}

static void isl29003_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    k->event = isl29003_event;
    k->recv = isl29003_recv;
    k->send = isl29003_send;
}

static const TypeInfo isl29003_info = {
    .name          = TYPE_ISL29003,
    .parent        = TYPE_I2C_SLAVE,
    .instance_size = sizeof(ISL29003State),
    .class_init    = isl29003_class_init,
};

static void isl29003_register_types(void)
{
    type_register_static(&isl29003_info);
}

type_init(isl29003_register_types)
