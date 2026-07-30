#include "hw/arm/ipod_touch_multitouch.h"
#include "migration/vmstate.h"
#include "hw/ssi/ssi.h"
#include "hw/core/hw-error.h"
#include "qemu/log.h"

static void ipod_touch_multitouch_inform_frame_ready(
    IPodTouchMultitouchState *s);
static void ipod_touch_multitouch_consume_frame(
    IPodTouchMultitouchState *s);

/* IT_MT_TRACE=1: log the guest<->controller conversation (command starts and
 * the firmware-upload state transitions). The render investigation needs to
 * know whether the guest driver talks to the controller at all and how far
 * the dialogue gets, on both the Z1 (iPhone) and Z2 (iPod) paths. */
static bool it_mt_trace_enabled(void)
{
    static int cached = -1;
    if (cached < 0) {
        cached = getenv("IT_MT_TRACE") != NULL;
    }
    return cached;
}

/* IT_MT_TRACE=2 additionally logs every SPI byte, but only from the first
 * touch onward -- the firmware upload would otherwise bury the interesting
 * part. This exists because an UNKNOWN command is not a harmless no-op in
 * this model: it sets buf_size = 1, so after a single byte cur_cmd resets and
 * every remaining byte of that transaction is re-interpreted as a NEW command.
 * A desync therefore looks like a plausible-but-fictional command sequence. */
static bool mt_trace_bytes_armed;

static bool it_mt_trace_bytes(void)
{
    static int cached = -1;
    if (cached < 0) {
        const char *v = getenv("IT_MT_TRACE");
        cached = (v && atoi(v) >= 2);
    }
    return cached && mt_trace_bytes_armed;
}

#define MT_TRACE(...) do { \
    if (it_mt_trace_enabled()) { \
        fprintf(stderr, "[MT] " __VA_ARGS__); \
    } \
} while (0)

/* Consecutive repeats of the same command (frame polls) collapse: the first
 * few print, then every 256th with its count. */
static void mt_trace_cmd(const char *proto, uint8_t cmd)
{
    static uint8_t last_cmd;
    static uint32_t repeat;

    if (!it_mt_trace_enabled()) {
        return;
    }
    if (cmd == last_cmd) {
        repeat++;
        if (repeat > 8 && (repeat & 0xFF) != 0) {
            return;
        }
    } else {
        last_cmd = cmd;
        repeat = 1;
    }
    fprintf(stderr, "[MT] %s cmd 0x%02x (n=%u)\n", proto, cmd, repeat);
}

/*
 * IT_MT_SENSOR_GRID=<rows>x<cols>: override the sensor grid reported by the
 * SENSOR_INFO report (0xD3). Purely an EXPERIMENT knob, default = the real
 * 15 x 10.
 *
 * It exists because MultitouchSupport.framework normalises the contact
 * position by a range it derives from these two counts (see
 * TOUCH_INVESTIGATION.md, "What MultitouchSupport normalises by"):
 * `_alg_InitRowColXYConvert` builds a row table and a column table by
 * integer-dividing a per-family pitch, then stores min/max in hundredths of a
 * millimetre. If that reading is right, changing a count here MUST move where
 * taps land -- and if it does not, the reading is wrong.
 */
/*
 * IT_MT_FAMILY_ID=<n>: override the multitouch family id (report 0xD1).
 *
 * This is NOT cosmetic. MultitouchSupport.framework dispatches its sensor
 * PITCH constants on this id, and the two firmwares accept different sets:
 *
 *   1.1.4  _alg_InitZephyrPlatformSpecifics: 0x41, 0x42, and the RANGE 0x50-0x52
 *   1.0    same function:                    0x41, 0x50, 0x42 -- exact, no range
 *
 * The model reports 81 = 0x51, which 1.1.4 accepts and 1.0 does NOT. 1.0 then
 * falls through to a generic branch whose pitches are 94/19 and 381/76 instead
 * of the Zephyr 36/7 and 56/11, computes a different sensor range, and
 * normalises every contact by it -- which is exactly the vertical scale error
 * the Calculator map measures on 1.0 and not on 1.1.4.
 *
 * See TOUCH_INVESTIGATION.md. Real Zephyr silicon must have reported an id
 * that 1.0 knew, since 1.0 shipped on the device; 0x51 is a later-generation
 * value only the newer framework recognises.
 */
static uint8_t mt_family_id(uint8_t builtin)
{
    static int cached = -1;

    if (cached < 0) {
        const char *e = getenv("IT_MT_FAMILY_ID");
        cached = e ? (int)(strtol(e, NULL, 0) & 0xFF) : -2;
        if (cached >= 0) {
            fprintf(stderr, "[MT] family id OVERRIDDEN: %#x\n", cached);
        }
    }
    return cached >= 0 ? (uint8_t)cached : builtin;
}

static void mt_sensor_grid(uint8_t *rows, uint8_t *cols)
{
    static int cached = -1;
    static unsigned r = MT_SENSOR_ROWS, c = MT_SENSOR_COLUMNS;

    if (cached < 0) {
        const char *e = getenv("IT_MT_SENSOR_GRID");
        unsigned pr, pc;
        cached = 1;
        if (e && sscanf(e, "%ux%u", &pr, &pc) == 2 && pr && pc &&
            pr <= 0xFF && pc <= 0xFF) {
            r = pr;
            c = pc;
            fprintf(stderr, "[MT] sensor grid OVERRIDDEN: %u rows x %u cols "
                    "(real hardware is %u x %u) -- experiment only\n",
                    r, c, MT_SENSOR_ROWS, MT_SENSOR_COLUMNS);
        }
    }
    *rows = r;
    *cols = c;
}

static void prepare_interface_version_response(IPodTouchMultitouchState *s) {
    memset(s->out_buffer + 1, 0, 15);

    // set the interface version
    s->out_buffer[2] = MT_INTERFACE_VERSION;

    // set the max packet size
    s->out_buffer[3] = (MT_MAX_PACKET_SIZE & 0xFF);
    s->out_buffer[4] = (MT_MAX_PACKET_SIZE >> 8) & 0xFF;

    // compute and set the checksum
    uint32_t checksum = 0;
    for(int i = 0; i < 14; i++) {
        checksum += s->out_buffer[i];
    }

    s->out_buffer[14] = (checksum & 0xFF);
    s->out_buffer[15] = (checksum >> 8) & 0xFF;
}

static void prepare_cmd_status_response(IPodTouchMultitouchState *s) {
    memset(s->out_buffer + 1, 0, 15);

    // TODO we should probably set some CMD status here

    // compute and set the checksum
    uint32_t checksum = 0;
    for(int i = 0; i < 14; i++) {
        checksum += s->out_buffer[i];
    }

    s->out_buffer[14] = (checksum & 0xFF);
    s->out_buffer[15] = (checksum >> 8) & 0xFF;
}

static void prepare_report_info_response(IPodTouchMultitouchState *s, uint8_t report_id) {
    memset(s->out_buffer + 1, 0, 15);

    // set the error
    s->out_buffer[2] = 0;

    // set the report length
    uint32_t report_length = 0;
    if(report_id == MT_REPORT_UNKNOWN1) {
        report_length = MT_REPORT_UNKNOWN1_SIZE;
    }
    else if(report_id == MT_REPORT_FAMILY_ID) {
        report_length = MT_REPORT_FAMILY_ID_SIZE;
    }
    else if(report_id == MT_REPORT_SENSOR_INFO) {
        report_length = MT_REPORT_SENSOR_INFO_SIZE;
    }
    else if(report_id == MT_REPORT_SENSOR_REGION_DESC) {
        report_length = MT_REPORT_SENSOR_REGION_DESC_SIZE;
    }
    else if(report_id == MT_REPORT_SENSOR_REGION_PARAM) {
        report_length = MT_REPORT_SENSOR_REGION_PARAM_SIZE;
    }
    else if(report_id == MT_REPORT_SENSOR_DIMENSIONS) {
        report_length = MT_REPORT_SENSOR_DIMENSIONS_SIZE;
    }
    else {
        /* A real controller reports an unsupported selector to the guest;
         * it does not terminate the whole machine.  The resumed driver asks
         * about optional reports that cold boot does not enumerate. */
        s->out_buffer[2] = 1;
        qemu_log_mask(LOG_GUEST_ERROR,
                      "iPod multitouch: unsupported report-info ID 0x%02x\n",
                      report_id);
    }

    s->out_buffer[3] = (report_length & 0xFF);
    s->out_buffer[4] = (report_length >> 8) & 0xFF;

    // compute and set the checksum
    uint32_t checksum = 0;
    for(int i = 0; i < 14; i++) {
        checksum += s->out_buffer[i];
    }

    s->out_buffer[14] = (checksum & 0xFF);
    s->out_buffer[15] = (checksum >> 8) & 0xFF;
}

static void prepare_short_control_response(IPodTouchMultitouchState *s, uint8_t report_id) {
    memset(s->out_buffer + 1, 0, 15);

    if(report_id == MT_REPORT_FAMILY_ID) {
        s->out_buffer[3] = mt_family_id(MT_FAMILY_ID);
    }
    else if(report_id == MT_REPORT_SENSOR_INFO) {
        s->out_buffer[3] = MT_ENDIANNESS;
        mt_sensor_grid(&s->out_buffer[4], &s->out_buffer[5]);
        s->out_buffer[6] = (MT_BCD_VERSION & 0xFF);
        s->out_buffer[7] = (MT_BCD_VERSION >> 8) & 0xFF;
    }
    else if(report_id == MT_REPORT_SENSOR_REGION_DESC) {
        s->out_buffer[3] = MT_SENSOR_REGION_DESC;
    }
    else if(report_id == MT_REPORT_SENSOR_REGION_PARAM) {
        s->out_buffer[3] = MT_SENSOR_REGION_PARAM;
    }
    else if(report_id == MT_REPORT_SENSOR_DIMENSIONS) {
        uint32_t *ob_int32 = (uint32_t *)&s->out_buffer[3];
        ob_int32[0] = MT_SENSOR_SURFACE_WIDTH;
        ob_int32[1] = MT_SENSOR_SURFACE_HEIGHT;
    }
    else {
        s->out_buffer[2] = 1;
        qemu_log_mask(LOG_GUEST_ERROR,
                      "iPod multitouch: unsupported short-control ID 0x%02x\n",
                      report_id);
    }

    // compute and set the checksum
    uint32_t checksum = 0;
    for(int i = 0; i < 14; i++) {
        checksum += s->out_buffer[i];
    }

    s->out_buffer[14] = (checksum & 0xFF);
    s->out_buffer[15] = (checksum >> 8) & 0xFF;
}

static uint32_t z1_report_length(uint8_t report_id)
{
    switch(report_id) {
        case MT_REPORT_UNKNOWN1:            return MT_REPORT_UNKNOWN1_SIZE;
        case MT_REPORT_FAMILY_ID:           return MT_REPORT_FAMILY_ID_SIZE;
        case MT_REPORT_SENSOR_INFO:         return MT_REPORT_SENSOR_INFO_SIZE;
        case MT_REPORT_SENSOR_REGION_DESC:  return MT_REPORT_SENSOR_REGION_DESC_SIZE;
        case MT_REPORT_SENSOR_REGION_PARAM: return MT_REPORT_SENSOR_REGION_PARAM_SIZE;
        case MT_REPORT_SENSOR_DIMENSIONS:   return MT_REPORT_SENSOR_DIMENSIONS_SIZE;
        default:                            return 0;
    }
}

// 0x8F: reply 0xAA .. .. .. (err<<4|len_hi) len_lo ck_hi ck_lo
static void z1_prepare_report_info_response(IPodTouchMultitouchState *s, uint8_t report_id)
{
    uint32_t len = z1_report_length(report_id);
    uint8_t err = 0;

    if(len == 0) {
        err = 1;
        qemu_log_mask(LOG_GUEST_ERROR,
                      "iPhone multitouch: unsupported Z1 report-info ID 0x%02x\n",
                      report_id);
    }

    s->out_buffer[4] = (err << 4) | ((len >> 8) & 0xF);
    s->out_buffer[5] = len & 0xFF;

    uint16_t checksum = (report_id + s->out_buffer[4] + s->out_buffer[5]) & 0xFFFF;
    s->out_buffer[6] = (checksum >> 8) & 0xFF;
    s->out_buffer[7] = checksum & 0xFF;
}

// 0x82: reply 0xAA .. .. .. <data> ck_hi ck_lo (checksum covers the id + data)
static void z1_prepare_report_response(IPodTouchMultitouchState *s, uint8_t report_id)
{
    uint32_t len = z1_report_length(report_id);

    s->buf_size = len + 6;
    memset(s->out_buffer + 1, 0, s->buf_size - 1);

    uint8_t *data = &s->out_buffer[4];
    switch(report_id) {
        case MT_REPORT_FAMILY_ID:
            data[0] = mt_family_id(MT_FAMILY_ID_Z1);
            break;
        case MT_REPORT_SENSOR_INFO:
            data[0] = MT_ENDIANNESS;
            mt_sensor_grid(&data[1], &data[2]);
            data[3] = (MT_BCD_VERSION >> 8) & 0xFF;
            data[4] = MT_BCD_VERSION & 0xFF;
            break;
        case MT_REPORT_SENSOR_REGION_DESC:
            data[0] = MT_SENSOR_REGION_DESC;
            break;
        case MT_REPORT_SENSOR_REGION_PARAM:
            data[0] = MT_SENSOR_REGION_PARAM;
            break;
        case MT_REPORT_SENSOR_DIMENSIONS: {
            uint32_t *dims = (uint32_t *)data;
            dims[0] = MT_SENSOR_SURFACE_WIDTH;
            dims[1] = MT_SENSOR_SURFACE_HEIGHT;
            break;
        }
        default:
            // the driver only asks for reports it enumerated via 0x8F
            break;
    }

    uint16_t checksum = report_id;
    for(int i = 0; i < len; i++) {
        checksum += data[i];
    }
    checksum &= 0xFFFF;
    s->out_buffer[len + 4] = (checksum >> 8) & 0xFF;
    s->out_buffer[len + 5] = checksum & 0xFF;
}

/*
 * iPhone OS 1.0 fetches a frame WITHOUT sending a command byte.
 *
 * 1.1.4's driver polls 0x64/0x65 for the length and then reads it with 0x68.
 * 1.0's AppleMultitouchSPI (root fs 1A543a) instead calls
 * deviceReadResultData(len), which is a plain full-duplex read: the MOSI bytes
 * are whatever was left in its transmit buffer (measured: 46 46 46 46 46 ff ff
 * ff), and only MISO matters. readOneFrameOfData() does it twice -- first an
 * 8-byte read for the pending length, then a read of length+1 for the frame.
 *
 * Both replies use the same 0xAA framing the command path already produces:
 * byte 0 = 0xAA, then the payload, then a big-endian 16-bit sum of every
 * payload byte. Nothing about the packet layout differs between the two
 * firmwares -- only the absence of the command byte -- so both helpers below
 * emit exactly what the 0x64/0x65 and 0x68 branches emit.
 *
 * Without this, the leading garbage byte hit z1_transfer()'s default arm,
 * which sets buf_size = 1, so every following byte was re-read as another
 * "command". The driver saw an all-zero reply, decided the controller was
 * wedged, and re-uploaded the Zephyr firmware -- forever. Touch never worked
 * on 1.0 at all.
 */
#define MT_Z1_CMD_UNSOLICITED 0xFE  /* internal: a read with no command byte */

static bool z1_is_command(uint8_t value)
{
    switch(value) {
        case MT_Z1_CMD_BL_PACKET:
        case MT_Z1_CMD_BL_VERIFY:
        case MT_Z1_CMD_BL_EXECUTE:
        case MT_Z1_CMD_IFACE_VERSION:
        case MT_Z1_CMD_REPORT_INFO:
        case MT_Z1_CMD_GET_REPORT:
        case MT_Z1_CMD_FRAME_NOP1:
        case MT_Z1_CMD_FRAME_NOP2:
        case MT_Z1_CMD_FRAME_READ:
            return true;
        default:
            return false;
    }
}

static uint16_t z1_pending_frame_len(IPodTouchMultitouchState *s)
{
    if(!s->next_frame) {
        return 0;
    }
    return sizeof(MTFrameHeader) + sizeof(FingerData) + 2;
}

/*
 * The length announcement deviceGetResultLength() reads: an 8-byte
 * transaction of which only the first five bytes are parsed,
 *
 *     AA  len_hi  len_lo  ck_hi  ck_lo   -- ck = len_hi + len_lo
 *
 * NOTE this is NOT where 1.1.4's 0x64/0x65 poll carries the length (bytes
 * 4/5, checksum 6/7). Same 0xAA framing, different offsets, so the two paths
 * cannot share a builder. Disassembly, 1A543a AppleMultitouchSPI +0x2750:
 * `ldrb rx[1]; ldrb rx[2]; orr r12, r3, r2 lsl #8` for the length, then
 * `add r1, r2, r3` versus `orr` of rx[3]/rx[4] for the checksum.
 *
 * The driver also rejects a length above the max packet size it learned from
 * the interface-version reply (MT_Z1_MAX_PACKET_SIZE), which our 54 clears.
 */
static void z1_prepare_frame_length_reply(IPodTouchMultitouchState *s)
{
    uint16_t frame_len = z1_pending_frame_len(s);
    uint16_t checksum;

    s->buf_size = 8;
    memset(s->out_buffer, 0, s->buf_size);
    s->out_buffer[0] = MT_Z1_REPLY_OK;
    s->out_buffer[1] = (frame_len >> 8) & 0xFF;
    s->out_buffer[2] = frame_len & 0xFF;
    checksum = (s->out_buffer[1] + s->out_buffer[2]) & 0xFFFF;
    s->out_buffer[3] = (checksum >> 8) & 0xFF;
    s->out_buffer[4] = checksum & 0xFF;
}

// the frame itself, identical to the 0x68 reply: the driver asks for len + 1
static void z1_prepare_frame_reply(IPodTouchMultitouchState *s)
{
    uint16_t payload_len = sizeof(MTFrameHeader) + sizeof(FingerData);
    uint16_t checksum = 0;

    if(!s->next_frame) {
        s->buf_size = 4;
        memset(s->out_buffer, 0, s->buf_size);
        s->out_buffer[0] = MT_Z1_REPLY_OK;
        return;
    }

    s->buf_size = payload_len + 3;
    memset(s->out_buffer, 0, s->buf_size);
    s->out_buffer[0] = MT_Z1_REPLY_OK;
    memcpy(s->out_buffer + 1, &s->next_frame->frame_packet.header,
           sizeof(MTFrameHeader));
    memcpy(s->out_buffer + 1 + sizeof(MTFrameHeader),
           &s->next_frame->finger_data, sizeof(FingerData));
    for(int i = 0; i < payload_len; i++) {
        checksum += s->out_buffer[1 + i];
    }
    s->out_buffer[payload_len + 1] = (checksum >> 8) & 0xFF;
    s->out_buffer[payload_len + 2] = checksum & 0xFF;
}

static const uint8_t z1_verify_pattern[4] = { 0x05, 0x00, 0x00, 0x06 };

/*
 * The Z1 main firmware is clocked out raw, with no command framing, and the
 * SPI model carries no chip-select boundaries. The upload is tx-only (the
 * driver ignores responses until the separate verify transaction), so we
 * absorb bytes into a running checksum and tentatively answer the verify
 * pattern whenever the byte stream matches it: a false partial match only
 * wastes response bytes the driver never looks at.
 */
static uint32_t z1_raw_upload_transfer(IPodTouchMultitouchState *s, uint8_t value)
{
    uint8_t ret = 0;

    if(value == z1_verify_pattern[s->z1_verify_matched]) {
        if(s->z1_verify_matched == 0) {
            uint16_t checksum = s->z1_raw_sum & 0xFFFF;
            s->z1_verify_resp[0] = 0xD0;
            s->z1_verify_resp[1] = 0x00;
            s->z1_verify_resp[2] = (checksum >> 8) & 0xFF;
            s->z1_verify_resp[3] = checksum & 0xFF;
        }
        ret = s->z1_verify_resp[s->z1_verify_matched];
        s->z1_verify_matched++;
        if(s->z1_verify_matched == 4) {
            // a full verify transaction ends the raw upload stream
            s->z1_upload_cksum = s->z1_raw_sum & 0xFFFF;
            s->z1_raw_upload = false;
            s->z1_verify_matched = 0;
            s->firmware_loaded = true;
            MT_TRACE("Z1 main-firmware upload verified (cksum 0x%04x) -> "
                     "firmware_loaded=1\n", s->z1_upload_cksum);
        }
        return ret;
    }

    // the partial match was firmware data after all - fold it into the sum
    for(int i = 0; i < s->z1_verify_matched; i++) {
        s->z1_raw_sum += z1_verify_pattern[i];
    }
    s->z1_verify_matched = 0;

    if(value == z1_verify_pattern[0]) {
        uint16_t checksum = s->z1_raw_sum & 0xFFFF;
        s->z1_verify_resp[0] = 0xD0;
        s->z1_verify_resp[1] = 0x00;
        s->z1_verify_resp[2] = (checksum >> 8) & 0xFF;
        s->z1_verify_resp[3] = checksum & 0xFF;
        ret = s->z1_verify_resp[0];
        s->z1_verify_matched = 1;
    }
    else {
        s->z1_raw_sum += value;
    }

    return ret;
}

static uint32_t z1_transfer(IPodTouchMultitouchState *s, uint32_t value)
{
    if(s->z1_raw_upload) {
        return z1_raw_upload_transfer(s, (uint8_t)value);
    }

    if(s->cur_cmd == 0 && value == 0) {
        // clock padding while idle
        return 0;
    }

    /* A read with no command byte (iPhone OS 1.0). Only taken while a frame is
     * actually in flight, so a genuinely unknown command still reaches the
     * default arm below, and 1.1.4 -- which pads with zeroes and always leads
     * with a real command -- is untouched. */
    if(s->cur_cmd == 0 && !z1_is_command((uint8_t)value) &&
       (s->next_frame || s->z1_frame_len_sent)) {
        s->cur_cmd = MT_Z1_CMD_UNSOLICITED;
        mt_trace_cmd("Z1", MT_Z1_CMD_UNSOLICITED);
        free(s->out_buffer);
        free(s->in_buffer);
        s->out_buffer = malloc(MT_Z1_MAX_PACKET_SIZE + 0x10);
        s->in_buffer = malloc(MT_Z1_MAX_PACKET_SIZE + 0x10);
        s->buf_ind = 0;
        s->in_buffer_ind = 0;

        if(s->z1_frame_len_sent) {
            z1_prepare_frame_reply(s);
            MT_TRACE("Z1 unsolicited frame read (%u bytes)\n", s->buf_size);
        } else {
            z1_prepare_frame_length_reply(s);
            MT_TRACE("Z1 unsolicited length read -> %u\n",
                     z1_pending_frame_len(s));
        }
    }
    else if(s->cur_cmd == 0) {
        // start a new command
        s->cur_cmd = value;
        mt_trace_cmd("Z1", (uint8_t)value);
        free(s->out_buffer);
        free(s->in_buffer);
        s->out_buffer = malloc(MT_Z1_MAX_PACKET_SIZE + 0x10);
        s->in_buffer = malloc(MT_Z1_MAX_PACKET_SIZE + 0x10);
        memset(s->out_buffer, 0, MT_Z1_MAX_PACKET_SIZE + 0x10);
        s->buf_ind = 0;
        s->in_buffer_ind = 0;

        switch(value) {
            case MT_Z1_CMD_BL_PACKET:
                // blank packet until the address byte proves otherwise
                s->buf_size = 4;
                break;
            case MT_Z1_CMD_BL_VERIFY:
            {
                s->buf_size = 4;
                uint16_t checksum = s->z1_upload_cksum & 0xFFFF;
                s->out_buffer[0] = 0xD0;
                s->out_buffer[1] = 0x00;
                s->out_buffer[2] = (checksum >> 8) & 0xFF;
                s->out_buffer[3] = checksum & 0xFF;
                break;
            }
            case MT_Z1_CMD_BL_EXECUTE:
                s->buf_size = 4;
                break;
            case MT_Z1_CMD_IFACE_VERSION:
                s->buf_size = 4;
                s->out_buffer[0] = MT_Z1_REPLY_OK;
                s->out_buffer[1] = MT_INTERFACE_VERSION;
                s->out_buffer[2] = (MT_Z1_MAX_PACKET_SIZE >> 8) & 0xFF;
                s->out_buffer[3] = MT_Z1_MAX_PACKET_SIZE & 0xFF;
                break;
            case MT_Z1_CMD_REPORT_INFO:
                s->buf_size = 8;
                s->out_buffer[0] = MT_Z1_REPLY_OK;
                // the rest is filled in once the report ID arrives
                break;
            case MT_Z1_CMD_GET_REPORT:
                // provisional; corrected to len+6 once the report ID arrives
                s->buf_size = MT_Z1_MAX_PACKET_SIZE;
                s->out_buffer[0] = MT_Z1_REPLY_OK;
                break;
            case MT_Z1_CMD_FRAME_NOP1:
            case MT_Z1_CMD_FRAME_NOP2:
            {
                s->buf_size = 8;
                s->out_buffer[0] = MT_Z1_REPLY_OK;
                uint16_t frame_len = 0;
                if(s->next_frame) {
                    frame_len = sizeof(MTFrameHeader) + sizeof(FingerData) + 2;
                }
                s->out_buffer[4] = (frame_len >> 8) & 0xFF;
                s->out_buffer[5] = frame_len & 0xFF;
                uint16_t checksum = (s->out_buffer[4] + s->out_buffer[5]) & 0xFFFF;
                s->out_buffer[6] = (checksum >> 8) & 0xFF;
                s->out_buffer[7] = checksum & 0xFF;
                break;
            }
            case MT_Z1_CMD_FRAME_READ:
            {
                if(s->next_frame) {
                    uint16_t payload_len = sizeof(MTFrameHeader) + sizeof(FingerData);
                    s->buf_size = payload_len + 3;
                    s->out_buffer[0] = MT_Z1_REPLY_OK;
                    memcpy(s->out_buffer + 1, &s->next_frame->frame_packet.header,
                           sizeof(MTFrameHeader));
                    memcpy(s->out_buffer + 1 + sizeof(MTFrameHeader),
                           &s->next_frame->finger_data, sizeof(FingerData));
                    uint16_t checksum = 0;
                    for(int i = 0; i < payload_len; i++) {
                        checksum += s->out_buffer[1 + i];
                    }
                    s->out_buffer[payload_len + 1] = (checksum >> 8) & 0xFF;
                    s->out_buffer[payload_len + 2] = checksum & 0xFF;
                }
                else {
                    // frame read raced a consumed frame: give an empty, valid reply
                    s->buf_size = 4;
                    s->out_buffer[0] = MT_Z1_REPLY_OK;
                }
                break;
            }
            default:
                qemu_log_mask(LOG_GUEST_ERROR,
                              "iPhone multitouch: ignoring unknown Z1 command 0x%02x\n",
                              (uint8_t)value);
                s->buf_size = 1;
                break;
        }
    }

    s->in_buffer[s->in_buffer_ind] = value;
    s->in_buffer_ind++;

    if(s->cur_cmd == MT_Z1_CMD_BL_PACKET && s->in_buffer_ind == 2) {
        // a real data packet carries the (nonzero) target address high byte
        if(s->in_buffer[1] != 0) {
            s->buf_size = MT_Z1_BL_PACKET_SIZE;
        }
    }
    else if(s->cur_cmd == MT_Z1_CMD_REPORT_INFO && s->in_buffer_ind == 2) {
        MT_TRACE("Z1 report-info for report 0x%02x\n", s->in_buffer[1]);
        z1_prepare_report_info_response(s, s->in_buffer[1]);
    }
    else if(s->cur_cmd == MT_Z1_CMD_GET_REPORT && s->in_buffer_ind == 2) {
        MT_TRACE("Z1 get-report 0x%02x\n", s->in_buffer[1]);
        z1_prepare_report_response(s, s->in_buffer[1]);
    }

    uint8_t ret_val = s->out_buffer[s->buf_ind];
    s->buf_ind++;

    if(s->buf_ind == s->buf_size) {
        if(s->cur_cmd == MT_Z1_CMD_BL_PACKET) {
            if(s->buf_size == MT_Z1_BL_PACKET_SIZE) {
                // checksum covers the header and payload, not the trailing
                // checksum bytes; the zero padding contributes nothing
                uint32_t checksum = 0;
                for(int i = 0; i < MT_Z1_BL_PACKET_SIZE - 2; i++) {
                    checksum += s->in_buffer[i];
                }
                s->z1_upload_cksum = checksum & 0xFFFF;
                s->firmware_transfer_seen = true;
                MT_TRACE("Z1 bootloader data packet done (cksum 0x%04x)\n",
                         s->z1_upload_cksum);
            }
            else {
                // a blank packet announces the raw main-firmware stream
                s->z1_raw_upload = true;
                s->z1_raw_sum = 0;
                s->z1_verify_matched = 0;
                MT_TRACE("Z1 raw main-firmware upload begins\n");
            }
        }
        else if(s->cur_cmd == MT_Z1_CMD_FRAME_READ && s->next_frame &&
                s->buf_size > 4) {
            ipod_touch_multitouch_consume_frame(s);
        }
        else if(s->cur_cmd == MT_Z1_CMD_UNSOLICITED) {
            if(s->z1_frame_len_sent) {
                /* the frame itself has now been handed over */
                s->z1_frame_len_sent = false;
                if(s->next_frame && s->buf_size > 4) {
                    ipod_touch_multitouch_consume_frame(s);
                }
            } else if(s->buf_size == 8) {
                /* only announce a payload the driver can actually come back
                 * for; a zero length means "nothing pending" */
                s->z1_frame_len_sent = s->next_frame != NULL;
            }
        }

        // we're done with the command
        s->cur_cmd = 0;
        s->buf_size = 0;
    }

    return ret_val;
}

static uint32_t mt_transfer_inner(SSIPeripheral *dev, uint32_t value);

static uint32_t ipod_touch_multitouch_transfer(SSIPeripheral *dev, uint32_t value)
{
    IPodTouchMultitouchState *s = IPOD_TOUCH_MULTITOUCH(dev);
    uint8_t cmd_before = s->cur_cmd;
    uint32_t ind_before = s->buf_ind, size_before = s->buf_size;
    uint32_t ret = mt_transfer_inner(dev, value);

    if (it_mt_trace_bytes()) {
        fprintf(stderr, "[MTB] in 0x%02x -> out 0x%02x  (cmd 0x%02x "
                "%u/%u -> cmd 0x%02x %u/%u)\n", (uint8_t)value, (uint8_t)ret,
                cmd_before, ind_before, size_before,
                s->cur_cmd, s->buf_ind, s->buf_size);
    }
    return ret;
}

static uint32_t mt_transfer_inner(SSIPeripheral *dev, uint32_t value)
{
    IPodTouchMultitouchState *s = IPOD_TOUCH_MULTITOUCH(dev);

    if(s->zephyr1) {
        return z1_transfer(s, value);
    }

    //printf("<MULTITOUCH> Got value: 0x%02x\n", value);

    /* The SPI controller can clock zero padding while the device is idle,
     * especially while the guest driver reinitializes after a power cycle. */
    if (s->cur_cmd == 0 && value == 0) {
        return 0;
    }

    if (s->cur_cmd == 0 && s->frame_data_pending && s->next_frame) {
        /* READ_INTERRUPT_DATA returns a 16-byte length reply first.  The
         * guest then performs a separate, dummy-filled SPI read for the
        * aligned interrupt packet itself. */
        s->cur_cmd = 0xff; /* internal packet-read state */
        s->buf_size = sizeof(MTFrame) - sizeof(MTFrameLengthPacket);
        free(s->out_buffer);
        free(s->in_buffer);
        s->out_buffer = malloc(s->buf_size);
        memcpy(s->out_buffer, &s->next_frame->frame_packet, s->buf_size);
        s->buf_ind = 0;
        s->in_buffer = malloc(s->buf_size);
        s->in_buffer_ind = 0;
        s->frame_data_pending = false;
        ipod_touch_multitouch_consume_frame(s);
    }
    else if(s->cur_cmd == 0) {
        // we're currently not in a command - start a new command
        s->cur_cmd = value;
        mt_trace_cmd("Z2", (uint8_t)value);
        free(s->out_buffer);
        free(s->in_buffer);
        s->out_buffer = malloc(0x100);
        s->out_buffer[0] = value; // the response header
        s->buf_ind = 0;
        s->in_buffer = malloc(0x100);
        s->in_buffer_ind = 0;
        
        if(value == 0x18) { // filler packet??
            s->buf_size = 2;
            s->out_buffer[1] = 0xE1;
        }
        else if(value == 0x1A) { // HBPP ACK
            s->buf_size = 2;
            if(s->hbpp_atn_ack_response[0] == 0 && s->hbpp_atn_ack_response[1] == 0) {
                // return the default ACK response
                s->out_buffer[0] = 0x4B;
                s->out_buffer[1] = 0xC1;
            }
            else {
                s->out_buffer[0] = s->hbpp_atn_ack_response[0];
                s->out_buffer[1] = s->hbpp_atn_ack_response[1];
            }
             
        }
        else if(value == 0x1C) { // read register
            s->buf_size = 8;
            memset(s->out_buffer, 0, 8); // just return zeros
        }
        else if(value == 0x1D) { // execute
            s->buf_size = 12;
            memset(s->out_buffer, 0, 12); // just return zeros
        }
        else if(value == 0x1F) { // calibration
            s->buf_size = 2;
            s->out_buffer[1] = 0x0;
        }
        else if(value == 0x1E) { // write register
            s->buf_size = 16;
            memset(s->out_buffer, 0, 16); // just return zeros
        }
        else if(value == 0x1F) { // calibration
            s->buf_size = 2;
            s->out_buffer[1] = 0x0;
        }
        else if(value == MT_CMD_HBPP_DATA_PACKET) {
            s->buf_size = 20; // should be enough initially, until we get the packet length
            memset(s->out_buffer + 1, 0, 20 - 1); // just return zeros
        }
        else if(value == 0x47) { // unknown command, probably used to clear the interrupt
            s->buf_size = 2;
        }
        else if(value == MT_CMD_GET_CMD_STATUS) {
            s->buf_size = 16;
            prepare_cmd_status_response(s);
        }
        else if(value == MT_CMD_GET_INTERFACE_VERSION) {
            s->buf_size = 16;
            prepare_interface_version_response(s);
        }
        else if(value == MT_CMD_GET_REPORT_INFO) {
            s->buf_size = 16;
        }
        else if(value == MT_CMD_SHORT_CONTROL_WRITE) {
            s->buf_size = 16;
        }
        else if(value == MT_CMD_SHORT_CONTROL_READ) {
            s->buf_size = 16;
        }
        else if(value == MT_CMD_FRAME_READ) {
            /* The direct EA path uses the original 75-byte frame.
             *
             * The frame is NOT consumed here. The iPod OS 1.x driver splits
             * this one 75-byte reply across TWO SPI transactions -- 16 bytes
             * for the length packet, then 59 for the payload (measured: TX
             * headers `ea 01 00` and `ea 01 01`, RXCNT 16 then 59). Consuming
             * at command start freed the frame while the driver still had the
             * whole payload to fetch, so the second transaction read zeros and
             * every touch was dropped. Consumption now happens where the frame
             * has actually been handed over: either at the end of a full
             * 75-byte read below, or in the payload path that
             * transaction_end() arms. */
            s->buf_size = sizeof(MTFrame);
            if (s->next_frame) {
                size_t data_size = sizeof(MTFramePacket) +
                                   sizeof(FingerData);

                memcpy(s->out_buffer, &s->next_frame->frame_length,
                       sizeof(MTFrameLengthPacket));
                memcpy(s->out_buffer + sizeof(MTFrameLengthPacket),
                       &s->next_frame->frame_packet, data_size);
                s->out_buffer[sizeof(MTFrameLengthPacket) + data_size] =
                    s->next_frame->checksum1;
                s->out_buffer[sizeof(MTFrameLengthPacket) + data_size + 1] =
                    s->next_frame->checksum2;
            } else {
                /* The driver can poll the legacy frame command immediately
                 * after consuming an interrupt packet.  No queued frame is
                 * a normal empty result, not a host-fatal NULL response. */
                memset(s->out_buffer, 0, s->buf_size);
            }
        }
        else if (value == MT_CMD_READ_INTERRUPT_DATA) {
            s->buf_size = sizeof(MTFrameLengthPacket);
            if (s->next_frame) {
                memcpy(s->out_buffer, &s->next_frame->frame_length,
                       sizeof(MTFrameLengthPacket));
                /* The queued object remains the original all-EA legacy
                 * frame.  Only the EB length transaction uses the E1 reply
                 * marker, with a checksum over that distinct header. */
                s->out_buffer[0] = MT_REPLY_INTERRUPT_DATA;
                s->out_buffer[14] = 0;
                s->out_buffer[15] = 0;
                uint16_t checksum = 0;
                for (int i = 0; i < 14; i++) {
                    checksum += s->out_buffer[i];
                }
                s->out_buffer[14] = checksum & 0xff;
                s->out_buffer[15] = checksum >> 8;
                s->frame_data_pending = true;
            } else {
                memset(s->out_buffer, 0, s->buf_size);
            }
        }
        else {
            qemu_log_mask(LOG_GUEST_ERROR,
                          "iPod multitouch: ignoring unknown command 0x%02x\n",
                          value);
            s->buf_size = 1;
            s->out_buffer[0] = 0;
        }
    }

    s->in_buffer[s->in_buffer_ind] = value;
    s->in_buffer_ind++;

    if(s->cur_cmd == MT_CMD_HBPP_DATA_PACKET && s->in_buffer_ind == 10) {
        // verify the header checksum
        uint32_t checksum = 0;
        for(int i = 2; i < 8; i++) {
            checksum += s->in_buffer[i];
        }

        if(checksum != (s->in_buffer[8] << 8 | s->in_buffer[9])) {
            hw_error("HBPP data header checksum doesn't match!");
        }

        uint32_t data_len = (s->in_buffer[2] << 10) | (s->in_buffer[3] << 2) + 5;
        // extend the lengths of the in/out buffers
        free(s->in_buffer);
        s->in_buffer = malloc(data_len + 0x10);

        free(s->out_buffer);
        s->out_buffer = malloc(data_len);
        memset(s->out_buffer, 0, data_len);
        s->buf_size = data_len;
        s->buf_ind = 0;
    }
    else if(s->cur_cmd == MT_CMD_GET_REPORT_INFO && s->in_buffer_ind == 2) {
        prepare_report_info_response(s, s->in_buffer[1]);
    }
    else if(s->cur_cmd == MT_CMD_SHORT_CONTROL_WRITE && s->in_buffer_ind == 16) {
        // TODO we should persist the report here!
    }
    else if(s->cur_cmd == MT_CMD_SHORT_CONTROL_READ && s->in_buffer_ind == 2) {
        prepare_short_control_response(s, s->in_buffer[1]);
    }

    // TODO process register writes!

    uint8_t ret_val = s->out_buffer[s->buf_ind];
    s->buf_ind++;

    //printf("<MULTITOUCH> Got value: 0x%02x, returning 0x%02x (index: %d, buffer length: %d)\n", value, ret_val, s->buf_ind, s->buf_size);

    if(s->buf_ind == s->buf_size) {
        /* The driver sends the large firmware HBPP transaction followed by a
         * small calibration transaction. The reset Z2 is ready only after
         * that complete sequence. */
        if (s->cur_cmd == MT_CMD_HBPP_DATA_PACKET) {
            if (s->buf_size > 0x1000) {
                s->firmware_transfer_seen = true;
                MT_TRACE("Z2 HBPP firmware transfer seen (%u bytes)\n",
                         s->buf_size);
            } else if (s->firmware_transfer_seen) {
                s->firmware_loaded = true;
                MT_TRACE("Z2 calibration after firmware -> firmware_loaded=1\n");
            }
        }
        //printf("Finished command 0x%02x\n", s->cur_cmd);

        if (s->cur_cmd == MT_CMD_FRAME_READ && s->next_frame) {
            /* A driver that clocked the whole 75-byte reply in one transaction
             * now has the frame. The split readers are handed over in the
             * payload path instead -- see transaction_end(). */
            ipod_touch_multitouch_consume_frame(s);
        }

        if(s->cur_cmd == 0x1E) {
            // make sure we return a success status on the next HBPP ACK
            s->hbpp_atn_ack_response[0] = 0x4A;
            s->hbpp_atn_ack_response[1] = 0xD1;
        }

        if (s->cur_cmd == 0xff) {
            free(s->out_buffer);
            free(s->in_buffer);
            s->out_buffer = NULL;
            s->in_buffer = NULL;
        }

        // we're done with the command
        s->cur_cmd = 0;
        s->buf_size = 0;
        //free(s->out_buffer);
        //free(s->in_buffer);
    }

    return ret_val;
}

/*
 * End of an SPI transaction: drop any half-consumed command.
 *
 * This model had NO transaction framing. `cur_cmd`/`buf_ind` reset only once
 * the guest clocked exactly `buf_size` bytes, so a driver that asked for
 * FEWER bytes than our reply is long -- e.g. a short status poll, or a 0xEB
 * frame poll it abandons once the length reads zero -- left the device stuck
 * mid-command. The next command byte was then eaten as data and every byte
 * after it misread as a new command: a permanent desync that made
 * slide-to-unlock work exactly once per boot (T7).
 *
 * The controller supplies the boundary: R_RXCNT is the length the driver
 * asked for, and reaching 0 ends the transfer (measured: a 16-byte read is 4
 * runs of an 8-byte FIFO, completing when RXCNT hits 0).
 *
 * Chip-select would be the textbook signal. An earlier note here claimed the
 * guest never drives it; that is WRONG (IT_SPI_CS_TRACE=1, 2026-07-27): the
 * iPod driver writes R_PIN once per transaction, 84 times in a two-minute
 * boot. It only ever ASSERTS, though -- every write is 0x00000000, never the
 * deassert -- so CS gives a start-of-transaction marker, not an edge pair.
 * Wiring it up would draw the boundary in exactly the same places RXCNT does
 * (measured across an 0xEA frame read: one CS write before the 16-byte length
 * part, another before the 59-byte payload part), so it would neither fix nor
 * worsen the split-read problem below. It is fidelity, not a bug fix.
 *
 * Protocol state that legitimately spans transactions is preserved:
 * `frame_data_pending` (the EB length reply and the frame read are two
 * transactions by design) and the firmware-upload flags.
 */
void ipod_touch_multitouch_transaction_end(IPodTouchMultitouchState *s)
{
    if (!s || !s->cur_cmd) {
        return;
    }
    if (s->buf_ind < s->buf_size) {
        MT_TRACE("transaction ended mid-command 0x%02x at %u/%u - resetting\n",
                 s->cur_cmd, s->buf_ind, s->buf_size);
    }

    /*
     * ...except that one short 0xEA read is NOT an abandoned command. The iPod
     * OS 1.x driver fetches a frame in two transactions: 16 bytes for the
     * length packet, then 59 for the payload, one logical 75-byte reply split
     * in half (it re-asserts chip-select between them, so no boundary signal
     * can tell the two apart from the outside). Stopping at exactly the length
     * packet means "the payload is still owed" -- arm the same handover the
     * 0xEB path uses, and leave the frame queued so it is still there for it.
     */
    if (s->cur_cmd == MT_CMD_FRAME_READ &&
        s->buf_size == sizeof(MTFrame) &&
        s->buf_ind == sizeof(MTFrameLengthPacket) &&
        s->next_frame) {
        s->frame_data_pending = true;
        MT_TRACE("0xEA length packet delivered; payload owed\n");
    }

    s->cur_cmd = 0;
    s->buf_size = 0;
    s->buf_ind = 0;
    s->in_buffer_ind = 0;
}

/*
 * The DEFAULT is the internal scale, which measurement says is the correct one
 * (see the block comment in ipod_touch_multitouch.h). IT_MT_SENSOR_SCALE=
 * advertised switches to scaling by the advertised dimensions, which shifts
 * every touch ~8% horizontally -- kept only so the comparison can be re-run.
 */
enum {
    MT_SCALE_DEFAULT = 0,   /* internal surface; measured correct horizontally */
    MT_SCALE_ADVERTISED,    /* the wrong fix, kept so it can be re-run          */
    MT_SCALE_ASPECT,        /* candidate: give the surface the panel's ratio    */
};

static int mt_sensor_scale_mode(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *e = getenv("IT_MT_SENSOR_SCALE");
        cached = MT_SCALE_DEFAULT;
        if (e && strcmp(e, "advertised") == 0) {
            cached = MT_SCALE_ADVERTISED;
            fprintf(stderr, "[MT] sensor scale: ADVERTISED (%u x %u) -- this is "
                    "the MEASURED-WRONG setting; touches land ~8%% right of "
                    "where they were aimed\n",
                    MT_ADVERTISED_SENSOR_SURFACE_WIDTH,
                    MT_ADVERTISED_SENSOR_SURFACE_HEIGHT);
        } else if (e && strcmp(e, "aspect") == 0) {
            cached = MT_SCALE_ASPECT;
            fprintf(stderr, "[MT] sensor scale: ASPECT (%u x %u) -- fixes iPhone "
                    "OS 1.0's vertical scale error and BREAKS 1.1.4's by a "
                    "comparable amount; measurement only, never a default\n",
                    MT_DEFAULT_SENSOR_SURFACE_WIDTH,
                    MT_ASPECT_SENSOR_SURFACE_HEIGHT);
        }
    }
    return cached;
}

uint32_t mt_sensor_surface_width(void)
{
    /* Only the HEIGHT is under question; the width measured correct. */
    return mt_sensor_scale_mode() == MT_SCALE_ADVERTISED
        ? MT_ADVERTISED_SENSOR_SURFACE_WIDTH
        : MT_DEFAULT_SENSOR_SURFACE_WIDTH;
}

uint32_t mt_sensor_surface_height(void)
{
    switch (mt_sensor_scale_mode()) {
    case MT_SCALE_ADVERTISED: return MT_ADVERTISED_SENSOR_SURFACE_HEIGHT;
    case MT_SCALE_ASPECT:     return MT_ASPECT_SENSOR_SURFACE_HEIGHT;
    default:                  return MT_DEFAULT_SENSOR_SURFACE_HEIGHT;
    }
}

/*
 * IT_MT_TIP_CORRECTION=<panel pixels>: cancel iPhone OS's OWN finger-tip
 * projection, so a mouse click lands where the pointer is.
 *
 * This is a deliberate UX choice, not a bug fix, and the default is 0 --
 * historically faithful. iPhone OS shifts every contact UPWARD on purpose,
 * because a fingertip occludes what it is pointing at and the contact centroid
 * sits below where the user believes they are aiming. Driven by a mouse, which
 * is exact and occludes nothing, that compensation is a pure error.
 *
 * The magnitude is not guesswork; it is read out of the guest (see
 * TOUCH_INVESTIGATION.md, "Where the up-shift comes from"). SpringBoard reads
 * the `SBFingerProjection` preference, default **3.5 typographic points**,
 * converts it with 25.4/72 to 1.2347 mm, and hands it to the MultitouchHID
 * plugin as `FingerTipVerticalOffset`; the plugin converts mm to pixels at
 * screen/sensor-surface = 320/50 = 480/75 = 6.4 px/mm. That is **7.90 px**, and
 * both 1A543a and 4A102 carry identical code and the same default.
 *
 * Measured hit-box centre shifts, for anyone choosing a value:
 *
 *   1.0    +7.6 px  (once the separate vertical SCALE error is corrected)
 *   1.1.4  +11.5 px (no scale error; ~3.6 px more than the projection alone,
 *                    which is the guest's own hit-box asymmetry, not this)
 *
 * Horizontal needs no correction: measured within +-3.5 px on 1.0 and +-1 px
 * on 1.1.4, so this knob is deliberately vertical-only.
 */
#define MT_PANEL_HEIGHT_PX 480.0f

static float mt_tip_correction_px(void)
{
    static int cached = -1;
    static float px;

    if (cached < 0) {
        const char *e = getenv("IT_MT_TIP_CORRECTION");
        px = e ? strtof(e, NULL) : 0.0f;
        cached = 1;
        if (px != 0.0f) {
            fprintf(stderr, "[MT] tip correction: reporting contacts %.2f panel "
                    "px LOWER, to cancel iPhone OS's own finger projection "
                    "(faithful default is 0)\n", px);
        }
    }
    return px;
}

static MTFrame *get_frame(IPodTouchMultitouchState *s, uint8_t event, float x, float y, uint16_t radius1, uint16_t radius2, uint16_t radius3, uint16_t contactDensity) {
    MTFrame *frame = calloc(1, sizeof(*frame));

    /* y here is the LCD handler's fy = 1 - screen_y/2^15, so the sensor origin
     * is at the BOTTOM of the panel: moving a contact DOWN the screen means
     * DECREASING y. Applied here rather than at the call sites so the velocity
     * computation below sees the corrected value too. */
    float correction = mt_tip_correction_px();
    if (correction != 0.0f) {
        y -= correction / MT_PANEL_HEIGHT_PX;
        y = MIN(MAX(y, 0.0f), 1.0f);
    }

    uint16_t data_len = sizeof(MTFrameHeader) + sizeof(FingerData) + 2;

    /// create the frame length packet
    frame->frame_length.cmd = MT_CMD_FRAME_READ;
    frame->frame_length.length1 = (data_len & 0xFF);
    frame->frame_length.length2 = (data_len >> 8) & 0xFF;

    uint16_t checksum = 0;
    for(int i = 0; i < 14; i++) {
        checksum += ((uint8_t *) &frame->frame_length)[i];
    }
    frame->frame_length.checksum1 = (checksum & 0xFF);
    frame->frame_length.checksum2 = (checksum >> 8) & 0xFF;

    // create the frame packet
    /* E1 identifies the 16-byte length reply.  The legacy Z2 frame itself
     * retains the EA marker expected by the iPod OS 1.x driver. */
    frame->frame_packet.cmd = MT_CMD_FRAME_READ;
    frame->frame_packet.length1 = (data_len & 0xFF);
    frame->frame_packet.length2 = (data_len >> 8) & 0xFF;

    checksum = 0;
    for(int i = 0; i < 4; i++) {
        checksum += ((uint8_t *) &frame->frame_packet)[i];
    }

    // the first five bytes have to sum up to 0.
    frame->frame_packet.checksum_pad = 0xFF - (checksum & 0xFF) + 1;

    frame->frame_packet.header.type = MT_FRAME_TYPE_PATH;
    frame->frame_packet.header.frameNum = s->frame_counter;
    frame->frame_packet.header.headerLen = sizeof(MTFrameHeader);
    uint64_t elapsed_ms = qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL);
    frame->frame_packet.header.timestamp = elapsed_ms;
    frame->frame_packet.header.numFingers = 1;
    frame->frame_packet.header.fingerDataLen = sizeof(FingerData);

    // create the finger data
    frame->finger_data.id = 1;
    frame->finger_data.event = event;
    frame->finger_data.unk_2 = 2;
    frame->finger_data.unk_3 = 1;

    // compute the velocity
    int diff_x = (int)((x - s->prev_touch_x) * MT_INTERNAL_SENSOR_SURFACE_WIDTH);
    int diff_y = (int)((y - s->prev_touch_y) * MT_INTERNAL_SENSOR_SURFACE_HEIGHT);
    uint64_t elapsed_delta_ms = MAX(elapsed_ms - s->last_frame_timestamp, 1);
    int64_t velocity_x = (int64_t)diff_x * 1000 / elapsed_delta_ms;
    int64_t velocity_y = (int64_t)diff_y * 1000 / elapsed_delta_ms;

    frame->finger_data.velX = CLAMP(velocity_x, INT16_MIN, INT16_MAX);
    frame->finger_data.velY = CLAMP(velocity_y, INT16_MIN, INT16_MAX);

    frame->finger_data.x = (int)(x * MT_INTERNAL_SENSOR_SURFACE_WIDTH);
    frame->finger_data.y = (int)(y * MT_INTERNAL_SENSOR_SURFACE_HEIGHT);
    frame->finger_data.radius1 = radius1;
    frame->finger_data.radius2 = radius2;
    frame->finger_data.radius3 = radius3;
    frame->finger_data.angle = 19317;
    frame->finger_data.contactDensity = contactDensity; // seems to be a medium press

    // compute the checksum over the frame data.
    checksum = 0;
    for(int i = 0; i < data_len - 2; i++) {
        checksum += ((uint8_t *) &frame->frame_packet.header)[i];
    }
    frame->checksum1 = (checksum & 0xFF);
    frame->checksum2 = (checksum >> 8) & 0xFF;

    s->last_frame_timestamp = elapsed_ms;
    s->prev_touch_x = x;
    s->prev_touch_y = y;
    s->frame_counter += 1;

    return frame;
}

static void ipod_touch_multitouch_inform_frame_ready(IPodTouchMultitouchState *s) {
    int grp = s->zephyr1 ? MT_ATN_INT_GROUP_Z1 : MT_ATN_INT_GROUP_Z2;
    int bit = s->zephyr1 ? MT_ATN_INT_BIT_Z1 : MT_ATN_INT_BIT_Z2;

    MT_TRACE("ATN edge (group %d bit %d)\n", grp, bit);

    s->sysic->gpio_int_status[grp] |= (1 << bit);
    /* The AP reset can leave QEMU's qemu_irq level high even after the VIC
     * raw bit was reset. Generate the physical ATN edge explicitly so a
     * retained-kernel wake cannot lose the first post-reset frame. */
    qemu_irq_lower(s->sysic->gpio_irqs[grp]);
    qemu_irq_raise(s->sysic->gpio_irqs[grp]);
}

static void ipod_touch_multitouch_queue_frame(IPodTouchMultitouchState *s,
                                               MTFrame *frame)
{
    uint8_t event = frame->finger_data.event;

    if (!s->next_frame) {
        s->next_frame = frame;
        ipod_touch_multitouch_inform_frame_ready(s);
        return;
    }

    /* Motion is level-like state: while ATN is already pending, retain only
     * the newest position. Never replace the frame whose EB length reply has
     * already been issued, and never let motion replace a release boundary. */
    if (event == MT_EVENT_TOUCH_MOVED) {
        if (!s->frame_data_pending &&
            s->next_frame->finger_data.event == MT_EVENT_TOUCH_MOVED) {
            free(s->next_frame);
            s->next_frame = frame;
        } else if (!s->deferred_frame ||
                   s->deferred_frame->finger_data.event ==
                       MT_EVENT_TOUCH_MOVED) {
            free(s->deferred_frame);
            s->deferred_frame = frame;
        } else {
            free(frame);
        }
        return;
    }

    /* A release supersedes any deferred movement, but not a previously
     * queued release boundary. */
    if (!s->deferred_frame ||
        s->deferred_frame->finger_data.event == MT_EVENT_TOUCH_MOVED) {
        free(s->deferred_frame);
        s->deferred_frame = frame;
    } else {
        free(frame);
    }
}

static void ipod_touch_multitouch_consume_frame(IPodTouchMultitouchState *s)
{
    uint8_t event = s->next_frame->finger_data.event;

    /* The definitive "the guest actually took this touch" signal: anything
     * else (ATN raised, bytes clocked, screen changed) can be true while the
     * driver still drops the frame. Tests assert on this. */
    MT_TRACE("frame consumed (event %u)\n", event);

    free(s->next_frame);
    s->next_frame = s->deferred_frame;
    s->deferred_frame = NULL;

    if (s->next_frame) {
        ipod_touch_multitouch_inform_frame_ready(s);
    }

    /* The protocol's final no-contact frame follows consumption of TOUCH_END,
     * so a slow guest cannot lose the end frame to a host-side timer. */
    if (event == MT_EVENT_TOUCH_ENDED) {
        timer_mod(s->touch_end_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      MT_FULL_END_DELAY_NS);
    }
}

void ipod_touch_multitouch_on_touch(IPodTouchMultitouchState *s) {
    mt_trace_bytes_armed = true;
    s->touch_down = true;

    ipod_touch_multitouch_queue_frame(
        s, get_frame(s, MT_EVENT_TOUCH_START, s->touch_x, s->touch_y,
                     100, 660, 580, 150));

    timer_mod(s->touch_timer,
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  NANOSECONDS_PER_SECOND / MT_MOTION_REPORT_HZ);
}

void ipod_touch_multitouch_on_release(IPodTouchMultitouchState *s) {
    ipod_touch_multitouch_queue_frame(
        s, get_frame(s, MT_EVENT_TOUCH_ENDED, s->touch_x, s->touch_y,
                     0, 0, 0, 0));
    s->touch_down = false;

    timer_del(s->touch_timer);
}

static void touch_timer_tick(void *opaque)
{
    IPodTouchMultitouchState *s = (IPodTouchMultitouchState *)opaque;

    ipod_touch_multitouch_queue_frame(
        s, get_frame(s, MT_EVENT_TOUCH_MOVED, s->touch_x, s->touch_y,
                     100, 660, 580, 150));

    if(s->touch_down) {
        // reschedule the timer
        timer_mod(s->touch_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      NANOSECONDS_PER_SECOND / MT_MOTION_REPORT_HZ);
    }
}

static void touch_end_timer_tick(void *opaque)
{
    IPodTouchMultitouchState *s = (IPodTouchMultitouchState *)opaque;
    ipod_touch_multitouch_queue_frame(
        s, get_frame(s, MT_EVENT_TOUCH_FULL_END, s->touch_x, s->touch_y,
                     0, 0, 0, 0));
    s->touch_down = false;
}

static void ipod_touch_multitouch_realize(SSIPeripheral *d, Error **errp)
{
    IPodTouchMultitouchState *s = IPOD_TOUCH_MULTITOUCH(d);
    memset(s->hbpp_atn_ack_response, 0, 2);
    s->touch_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, touch_timer_tick, s);
    s->touch_end_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, touch_end_timer_tick, s);

    s->prev_touch_x = 0;
    s->prev_touch_y = 0;
    s->last_frame_timestamp = 0;
}

static void ipod_touch_multitouch_reset(DeviceState *dev)
{
    IPodTouchMultitouchState *s = IPOD_TOUCH_MULTITOUCH(dev);

    MT_TRACE("controller reset (zephyr1=%d)\n", s->zephyr1);
    timer_del(s->touch_timer);
    timer_del(s->touch_end_timer);

    if (s->out_buffer &&
        s->out_buffer != (uint8_t *)s->next_frame) {
        free(s->out_buffer);
    }
    free(s->in_buffer);
    free(s->next_frame);
    free(s->deferred_frame);

    s->cur_cmd = 0;
    s->out_buffer = NULL;
    s->in_buffer = NULL;
    s->next_frame = NULL;
    s->deferred_frame = NULL;
    s->buf_size = 0;
    s->buf_ind = 0;
    s->in_buffer_ind = 0;
    s->frame_data_pending = false;
    s->firmware_transfer_seen = false;
    s->firmware_loaded = false;
    memset(s->hbpp_atn_ack_response, 0,
           sizeof(s->hbpp_atn_ack_response));
    s->frame_counter = 0;
    s->touch_down = false;
    s->touch_x = 0;
    s->touch_y = 0;
    s->prev_touch_x = 0;
    s->prev_touch_y = 0;
    s->last_frame_timestamp = 0;

    s->z1_upload_cksum = 0;
    s->z1_raw_upload = false;
    s->z1_raw_sum = 0;
    s->z1_verify_matched = 0;
    s->z1_frame_len_sent = false;

    if (s->sysic) {
        int grp = s->zephyr1 ? MT_ATN_INT_GROUP_Z1 : MT_ATN_INT_GROUP_Z2;
        int bit = s->zephyr1 ? MT_ATN_INT_BIT_Z1 : MT_ATN_INT_BIT_Z2;
        s->sysic->gpio_int_status[grp] &= ~(1 << bit);
        qemu_irq_lower(s->sysic->gpio_irqs[grp]);
    }
}

/*
 * Migration.
 *
 * Found by measurement, like the others. A snapshot taken with the panel ASLEEP
 * restored touch-responsive; one taken with a LIVE home screen restored with
 * dead touch. The difference is not the panel -- it is that waking a sleeping
 * device re-uploads the controller firmware ("Retained touch input ready after
 * Z2 reload"), which rebuilt this device's state by accident. A live snapshot
 * never does that, so the controller came back at RESET: firmware_loaded false,
 * and the driver's frame reads answered by a device that believes it has no
 * firmware.
 *
 * So the FIRMWARE and PROTOCOL state migrates, and the transient state does not:
 *
 *   out_buffer / in_buffer      malloc'd SPI transaction scratch
 *   next_frame / deferred_frame malloc'd queued touch frames
 *
 * Those are pointers, and a snapshot caught mid-transaction would restore
 * dangling ones. post_load resets them to a clean IDLE state instead. The cost
 * is at most one touch frame in flight at the instant of the snapshot; the
 * alternative is a device whose buffer pointers do not match its indices.
 *
 * The touch coordinates are floats and are not migrated either: they only mean
 * anything while a finger is down, and post_load lifts the finger.
 */
static int ipod_touch_multitouch_post_load(void *opaque, int version_id)
{
    IPodTouchMultitouchState *s = (IPodTouchMultitouchState *)opaque;

    /* A clean idle protocol state: no half-finished SPI transaction, no queued
     * frame, and no finger down. */
    g_free(s->out_buffer);
    g_free(s->in_buffer);
    s->out_buffer = NULL;
    s->in_buffer = NULL;
    s->cur_cmd = 0;
    s->buf_size = 0;
    s->buf_ind = 0;
    s->in_buffer_ind = 0;
    s->frame_data_pending = false;
    g_free(s->next_frame);
    g_free(s->deferred_frame);
    s->next_frame = NULL;
    s->deferred_frame = NULL;
    s->touch_down = false;
    return 0;
}

static const VMStateDescription vmstate_ipod_touch_multitouch = {
    .name = "ipod-touch-multitouch",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = ipod_touch_multitouch_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_SSI_PERIPHERAL(ssidev, IPodTouchMultitouchState),
        VMSTATE_BOOL(firmware_transfer_seen, IPodTouchMultitouchState),
        VMSTATE_BOOL(firmware_loaded, IPodTouchMultitouchState),
        VMSTATE_UINT8_ARRAY(hbpp_atn_ack_response, IPodTouchMultitouchState, 2),
        VMSTATE_UINT32(frame_counter, IPodTouchMultitouchState),
        VMSTATE_UINT64(last_frame_timestamp, IPodTouchMultitouchState),
        VMSTATE_BOOL(suppress_power_release, IPodTouchMultitouchState),
        VMSTATE_BOOL(suppress_home_release, IPodTouchMultitouchState),
        /* Zephyr1 upload/verify progress. zephyr1 itself is set from the board
         * at machine init and must NOT come from the stream. */
        VMSTATE_UINT32(z1_upload_cksum, IPodTouchMultitouchState),
        VMSTATE_BOOL(z1_raw_upload, IPodTouchMultitouchState),
        VMSTATE_BOOL(z1_frame_len_sent, IPodTouchMultitouchState),
        VMSTATE_UINT32(z1_raw_sum, IPodTouchMultitouchState),
        VMSTATE_UINT8(z1_verify_matched, IPodTouchMultitouchState),
        VMSTATE_UINT8_ARRAY(z1_verify_resp, IPodTouchMultitouchState, 4),
        VMSTATE_END_OF_LIST()
    },
};

static void ipod_touch_multitouch_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    device_class_set_legacy_reset(dc, ipod_touch_multitouch_reset);
    k->realize = ipod_touch_multitouch_realize;
    k->transfer = ipod_touch_multitouch_transfer;
    dc->vmsd = &vmstate_ipod_touch_multitouch;
}

static const TypeInfo ipod_touch_multitouch_type_info = {
    .name = TYPE_IPOD_TOUCH_MULTITOUCH,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(IPodTouchMultitouchState),
    .class_init = ipod_touch_multitouch_class_init,
};

static void ipod_touch_multitouch_register_types(void)
{
    type_register_static(&ipod_touch_multitouch_type_info);
}

type_init(ipod_touch_multitouch_register_types)
