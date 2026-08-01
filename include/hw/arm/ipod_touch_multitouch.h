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
/*
 * Family id, reported by report 0xD1 -- and it is NOT metadata: MultitouchSupport
 * dispatches its sensor PITCH constants on it, so this value chooses the guest's
 * coordinate system. See TOUCH_INVESTIGATION.md, "FOUND: the family id".
 *
 * Zephyr 1 (iPhone 2G / M68AP) must be a value iPhone OS 1.0 recognises, since
 * 1.0 shipped on that phone. 1.0 accepts 0x41, 0x50, 0x42 EXACTLY; 1.1.4 accepts
 * 0x41, 0x42 and the range 0x50-0x52. 0x50 is the only sane choice: measured, it
 * makes both firmwares agree to within +-0.5 px on every hit-box edge.
 *
 * Zephyr 2 (iPod touch 1G / N45AP) is DIFFERENT SILICON and keeps the historical
 * 81 = 0x51 -- inside 1.1.x's accepted range, never measured, and deliberately
 * not disturbed. Two chips legitimately reporting two ids is BOARD-awareness,
 * which the machine is entitled to; it is not firmware-awareness, which is
 * forbidden.
 */
#define MT_FAMILY_ID             0x51   /* Zephyr 2 / N45AP -- unchanged */
#define MT_FAMILY_ID_Z1          0x50   /* Zephyr 1 / M68AP -- measured  */
#define MT_ENDIANNESS            0x1
#define MT_SENSOR_ROWS           15
#define MT_SENSOR_COLUMNS        10
#define MT_BCD_VERSION           51
#define MT_SENSOR_REGION_DESC    0x0
#define MT_SENSOR_REGION_PARAM   0x0
#define MT_MAX_PACKET_SIZE       0x294 // 660
#define MT_SENSOR_SURFACE_WIDTH  5000
#define MT_SENSOR_SURFACE_HEIGHT 7500

/*
 * The internal surface is NOT the advertised one, and that is CORRECT -- do not
 * "fix" it. This was tried on 2026-07-30 and measured wrong.
 *
 * MT_REPORT_SENSOR_DIMENSIONS hands the guest 5000 x 7500 while a finger is
 * placed at fx * 4602, fy * 7306, which reads like an obvious bug. It is not:
 * the DRIVER maps sensor->screen with the internal scale, not with the
 * dimensions it was told, so the two disagreeing is what makes a tap land where
 * it was aimed.
 *
 * Measured, A/B on one binary, restoring the same snapshot each time and
 * tapping x=235 -- which a column profile of the real framebuffer puts in the
 * GAP between the row-3 icons at 170..226 and 246..302:
 *
 *   internal scale (this)   lands 235, in the gap   -> nothing launches  CORRECT
 *   advertised scale        lands 255, on Settings  -> Settings launches WRONG
 *
 * Predicting from "the guest divides by what it was advertised" gets both
 * results backwards. Whatever the driver actually uses, it agrees with these
 * expressions, so changing them introduces an ~8% horizontal shift on every
 * build -- the opposite of the intended repair.
 *
 * IT_MT_SENSOR_SCALE=advertised reproduces the wrong behaviour for anyone who
 * wants to re-run the comparison.
 *
 * The HEIGHT was also suspected, and that suspicion is now CLOSED -- it was
 * wrong. iPhone OS 1.0 used to show a vertical SCALE error here (21.0 px at
 * panel y 223.5, 18.0 at 294, 13.5 at 366, slope -0.0527) that 1.1.4 did not.
 * The cause was never these constants: it was MT_FAMILY_ID. 1.0 did not
 * recognise the id this model reported, so MultitouchSupport fell through to a
 * generic branch with different sensor PITCH constants and computed a different
 * normalisation range. Fixed by reporting an id 1.0 knows; see the comment on
 * MT_FAMILY_ID_Z1 above and TOUCH_INVESTIGATION.md.
 *
 * IT_MT_SENSOR_SCALE=aspect remains ONLY as a warning. It gives the surface the
 * panel's aspect ratio, which repaired 1.0's symptom exactly (slope -0.0527 ->
 * -0.0000) and BROKE 1.1.4 by a comparable amount (+0.0070 -> +0.0631). It was
 * a plausible fix aimed at the wrong constant, caught by measuring both builds
 * instead of one. Do not adopt it, and do not repeat the shape of the mistake:
 * a positional error is almost always in what this model DECLARES (family id,
 * sensor grid), not in how it PLACES the contact.
 */
#define MT_ADVERTISED_SENSOR_SURFACE_WIDTH  MT_SENSOR_SURFACE_WIDTH
#define MT_ADVERTISED_SENSOR_SURFACE_HEIGHT MT_SENSOR_SURFACE_HEIGHT
#define MT_DEFAULT_SENSOR_SURFACE_WIDTH  ((9000 - MT_SENSOR_SURFACE_WIDTH) * 84 / 73)
#define MT_DEFAULT_SENSOR_SURFACE_HEIGHT ((13850 - MT_SENSOR_SURFACE_HEIGHT) * 84 / 73)
/* The panel is 320x480; this is the width carrying that same ratio. */
#define MT_ASPECT_SENSOR_SURFACE_HEIGHT (MT_DEFAULT_SENSOR_SURFACE_WIDTH * 3 / 2)

uint32_t mt_sensor_surface_width(void);
uint32_t mt_sensor_surface_height(void);

#define MT_INTERNAL_SENSOR_SURFACE_WIDTH  mt_sensor_surface_width()
#define MT_INTERNAL_SENSOR_SURFACE_HEIGHT mt_sensor_surface_height()

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
    /*
     * The wake press is consumed as a PMU wake CAUSE and never reaches the
     * guest as a button, so iPhone OS never sees user activity and re-sleeps
     * within seconds of waking. This timer delivers it for real, once the
     * resumed kernel is up. See ipod_touch_wake_activity().
     */
    QEMUTimer *wake_activity_timer;
    bool wake_activity_pressed;
    float touch_x;
    float touch_y;
    float prev_touch_x;
    float prev_touch_y;
    uint64_t last_frame_timestamp;

    // Zephyr1 (iPhone 2G) protocol state
    bool zephyr1;
    uint32_t z1_upload_cksum;   // checksum of the last bootloader upload
    bool z1_raw_upload;         // inside the raw main-firmware upload stream
    bool z1_frame_len_sent;     // 1.0 took the length; the frame read is next
    uint32_t z1_raw_sum;
    uint8_t z1_verify_matched;  // bytes matched of the 05 00 00 06 verify pattern
    uint8_t z1_verify_resp[4];
} IPodTouchMultitouchState;

void ipod_touch_multitouch_on_touch(IPodTouchMultitouchState *s);
void ipod_touch_multitouch_transaction_end(IPodTouchMultitouchState *s);
void ipod_touch_multitouch_on_release(IPodTouchMultitouchState *s);

/* Keep IRQ/FIQ delivery open while the guest leaves its masked idle path. */

#endif
