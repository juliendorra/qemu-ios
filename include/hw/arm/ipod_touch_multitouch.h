#ifndef IPOD_TOUCH_MULTITOUCH_H
#define IPOD_TOUCH_MULTITOUCH_H

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/ssi/ssi.h"
#include "hw/arm/ipod_touch_sysic.h"
#include "hw/arm/ipod_touch_gpio.h"
#include "hw/arm/ipod_touch_pcf50633_pmu.h"

// Forward declaration to avoid circular include (lcd.h includes multitouch.h)
typedef struct IPodTouchLCDState IPodTouchLCDState;

#define TYPE_IPOD_TOUCH_MULTITOUCH                "ipodtouch.multitouch"
OBJECT_DECLARE_SIMPLE_TYPE(IPodTouchMultitouchState, IPOD_TOUCH_MULTITOUCH)

#define MT_INTERFACE_VERSION     0x1
#define MT_FAMILY_ID             81
#define MT_ENDIANNESS            0x1
#define MT_SENSOR_ROWS           15
#define MT_SENSOR_COLUMNS        10
#define MT_BCD_VERSION           51
#define MT_SENSOR_REGION_DESC    0x0
#define MT_SENSOR_REGION_PARAM   0x0
#define MT_MAX_PACKET_SIZE       0x294 // 660
#define MT_SENSOR_SURFACE_WIDTH  5000
#define MT_SENSOR_SURFACE_HEIGHT 7500

// internal surface width/height
#define MT_INTERNAL_SENSOR_SURFACE_WIDTH  (9000 - MT_SENSOR_SURFACE_WIDTH) * 84 / 73
#define MT_INTERNAL_SENSOR_SURFACE_HEIGHT (13850 - MT_SENSOR_SURFACE_HEIGHT) * 84 / 73

// report IDs
#define MT_REPORT_UNKNOWN1            0x70
#define MT_REPORT_FAMILY_ID           0xD1
#define MT_REPORT_SENSOR_INFO         0xD3
#define MT_REPORT_SENSOR_REGION_DESC  0xD0
#define MT_REPORT_SENSOR_REGION_PARAM 0xA1
#define MT_REPORT_SENSOR_DIMENSIONS   0xD9

// report sizes
#define MT_REPORT_UNKNOWN1_SIZE            0x1
#define MT_REPORT_FAMILY_ID_SIZE           0x1
#define MT_REPORT_SENSOR_INFO_SIZE         0x5
#define MT_REPORT_SENSOR_REGION_DESC_SIZE  0x1
#define MT_REPORT_SENSOR_REGION_PARAM_SIZE 0x1
#define MT_REPORT_SENSOR_DIMENSIONS_SIZE   0x8

#define MT_CMD_HBPP_DATA_PACKET      0x30
#define MT_CMD_GET_CMD_STATUS        0xE1
#define MT_CMD_GET_INTERFACE_VERSION 0xE2
#define MT_CMD_GET_REPORT_INFO       0xE3
#define MT_CMD_SHORT_CONTROL_WRITE   0xE4
#define MT_CMD_SHORT_CONTROL_READ    0xE6
#define MT_CMD_FRAME_READ            0xEA
#define MT_CMD_READ_INTERRUPT_DATA   0xEB
#define MT_REPLY_INTERRUPT_DATA      0xE1

// frame types
#define MT_FRAME_TYPE_PATH 0x44

// frame event types
#define MT_EVENT_TOUCH_FULL_END 0x0
#define MT_EVENT_TOUCH_START 0x3
#define MT_EVENT_TOUCH_MOVED 0x4
#define MT_EVENT_TOUCH_ENDED 0x7

#define MT_MOTION_REPORT_HZ 60
#define MT_FULL_END_DELAY_NS (NANOSECONDS_PER_SECOND / 10)

/*
 * Zephyr1 (iPhone 2G) protocol, per openiboot multitouch-z1.c. Unlike the
 * Zephyr2's HBPP/E1-EB command set, the Z1 bootloader takes 0x400-byte 0xC2
 * data packets, a 05 00 00 06 checksum-verify, and a 0xC4 execute; the
 * running firmware answers 0xAA-framed request/response transactions.
 */
#define MT_Z1_CMD_BL_PACKET     0xC2 // bootloader data packet (or 4-byte blank packet)
#define MT_Z1_CMD_BL_VERIFY     0x05 // checksum verify, replied with 0xD0 00 ck ck
#define MT_Z1_CMD_BL_EXECUTE    0xC4
#define MT_Z1_CMD_IFACE_VERSION 0xD0
#define MT_Z1_CMD_REPORT_INFO   0x8F
#define MT_Z1_CMD_GET_REPORT    0x82
#define MT_Z1_CMD_FRAME_NOP1    0x64 // frame-length poll (driver alternates the two)
#define MT_Z1_CMD_FRAME_NOP2    0x65
#define MT_Z1_CMD_FRAME_READ    0x68
#define MT_Z1_REPLY_OK          0xAA
#define MT_Z1_BL_PACKET_SIZE    0x400
#define MT_Z1_MAX_PACKET_SIZE   0x400

// ATN interrupt: GPIO 0xa3 on the iPhone (group 5, bit 3), 0x9b on the iPod (group 4, bit 27)
#define MT_ATN_INT_GROUP_Z1 5
#define MT_ATN_INT_BIT_Z1   3
#define MT_ATN_INT_GROUP_Z2 4
#define MT_ATN_INT_BIT_Z2   27

typedef struct MTFrameLengthPacket
{
    uint8_t cmd;
    uint8_t length1;
    uint8_t length2;
    uint8_t unused[11];
    uint8_t checksum1;
    uint8_t checksum2;
} __attribute__((__packed__)) MTFrameLengthPacket;

typedef struct MTFrameHeader
{
    uint8_t type;
    uint8_t frameNum;
    uint8_t headerLen;
    uint8_t unk_3;
    uint32_t timestamp;
    uint8_t unk_8;
    uint8_t unk_9;
    uint8_t unk_A;
    uint8_t unk_B;
    uint16_t unk_C;
    uint16_t isImage;

    uint8_t numFingers;
    uint8_t fingerDataLen;
    uint16_t unk_12;
    uint16_t unk_14;
    uint16_t unk_16;
} __attribute__((__packed__)) MTFrameHeader;

typedef struct MTFramePacket
{
    uint8_t cmd;
    uint8_t unused1;
    uint8_t length1;
    uint8_t length2;
    uint8_t checksum_pad;
    MTFrameHeader header;
} __attribute__((__packed__)) MTFramePacket;

typedef struct FingerData
{
    uint8_t id;
    uint8_t event;
    uint8_t unk_2;
    uint8_t unk_3;
    int16_t x;
    int16_t y;
    int16_t velX;
    int16_t velY;
    uint16_t radius2;
    uint16_t radius3;
    uint16_t angle;
    uint16_t radius1;
    uint16_t contactDensity;
    uint16_t unk_16;
    uint16_t unk_18;
    uint16_t unk_1A;
} __attribute__((__packed__)) FingerData;

typedef struct MTFrame {
    MTFrameLengthPacket frame_length;
    MTFramePacket frame_packet;
    FingerData finger_data; // TODO we assume one finger for now
    uint8_t checksum1;
    uint8_t checksum2;
} __attribute__((__packed__)) MTFrame;

typedef struct IPodTouchMultitouchState {
    SSIPeripheral ssidev;
    uint8_t cur_cmd;
    uint8_t *out_buffer;
    uint8_t *in_buffer;
    uint32_t buf_size;
    uint32_t buf_ind;
    uint32_t in_buffer_ind;
    bool frame_data_pending;
    bool firmware_transfer_seen;
    bool firmware_loaded;
    uint8_t hbpp_atn_ack_response[2];
    MTFrame *next_frame;
    MTFrame *deferred_frame;
    uint32_t frame_counter;
    bool touch_down;
    QEMUTimer *touch_timer;
    QEMUTimer *touch_end_timer;
    IPodTouchSYSICState *sysic;
    IPodTouchGPIOState *gpio_state;
    CPUState *cpu;
    Pcf50633State *pmu;
    IPodTouchLCDState *lcd;  // for display wake control
    bool suppress_power_release;
    bool suppress_home_release;
    float touch_x;
    float touch_y;
    float prev_touch_x;
    float prev_touch_y;
    uint64_t last_frame_timestamp;

    // Zephyr1 (iPhone 2G) protocol state
    bool zephyr1;
    uint32_t z1_upload_cksum;   // checksum of the last bootloader upload
    bool z1_raw_upload;         // inside the raw main-firmware upload stream
    uint32_t z1_raw_sum;
    uint8_t z1_verify_matched;  // bytes matched of the 05 00 00 06 verify pattern
    uint8_t z1_verify_resp[4];
} IPodTouchMultitouchState;

void ipod_touch_multitouch_on_touch(IPodTouchMultitouchState *s);
void ipod_touch_multitouch_transaction_end(IPodTouchMultitouchState *s);
void ipod_touch_multitouch_on_release(IPodTouchMultitouchState *s);

/* Keep IRQ/FIQ delivery open while the guest leaves its masked idle path. */

#endif
