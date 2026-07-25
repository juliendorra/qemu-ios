#include "hw/arm/ipod_touch_multitouch.h"
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
        s->out_buffer[3] = MT_FAMILY_ID;
    }
    else if(report_id == MT_REPORT_SENSOR_INFO) {
        s->out_buffer[3] = MT_ENDIANNESS;
        s->out_buffer[4] = MT_SENSOR_ROWS;
        s->out_buffer[5] = MT_SENSOR_COLUMNS;
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
            data[0] = MT_FAMILY_ID;
            break;
        case MT_REPORT_SENSOR_INFO:
            data[0] = MT_ENDIANNESS;
            data[1] = MT_SENSOR_ROWS;
            data[2] = MT_SENSOR_COLUMNS;
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

    if(s->cur_cmd == 0) {
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
            /* The direct EA path uses the original 75-byte frame. */
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
                ipod_touch_multitouch_consume_frame(s);
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

static MTFrame *get_frame(IPodTouchMultitouchState *s, uint8_t event, float x, float y, uint16_t radius1, uint16_t radius2, uint16_t radius3, uint16_t contactDensity) {
    MTFrame *frame = calloc(1, sizeof(*frame));

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

    if (s->sysic) {
        int grp = s->zephyr1 ? MT_ATN_INT_GROUP_Z1 : MT_ATN_INT_GROUP_Z2;
        int bit = s->zephyr1 ? MT_ATN_INT_BIT_Z1 : MT_ATN_INT_BIT_Z2;
        s->sysic->gpio_int_status[grp] &= ~(1 << bit);
        qemu_irq_lower(s->sysic->gpio_irqs[grp]);
    }
}

static void ipod_touch_multitouch_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    device_class_set_legacy_reset(dc, ipod_touch_multitouch_reset);
    k->realize = ipod_touch_multitouch_realize;
    k->transfer = ipod_touch_multitouch_transfer;
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
