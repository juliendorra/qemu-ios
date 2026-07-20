#include "hw/arm/ipod_touch_mv8686.h"

/* CCCR register numbers (SDIO spec part E1) */
#define CCCR_REVISION   0x00
#define CCCR_SD_SPEC    0x01
#define CCCR_IOE        0x02
#define CCCR_IOR        0x03
#define CCCR_IEN        0x04
#define CCCR_INT_PEND   0x05
#define CCCR_IO_ABORT   0x06
#define CCCR_BUS_IFACE  0x07
#define CCCR_CARD_CAP   0x08
#define CCCR_CIS_PTR    0x09 /* ..0x0B */
#define CCCR_FN0_BLK    0x10 /* ..0x11 */
#define CCCR_POWER      0x12

/* FBR1 register numbers, relative to 0x100 */
#define FBR_IFACE_CODE  0x00
#define FBR_EXT_CODE    0x01
#define FBR_CIS_PTR     0x09 /* ..0x0B */
#define FBR_BLKSIZE     0x10 /* ..0x11 */

/* fn1 register values confirmed against Linux libertas/if_sdio.h */
#define FN1_IOPORT0     0x00
#define FN1_IOPORT1     0x01
#define FN1_IOPORT2     0x02
#define FN1_CONFIG      0x03
#define FN1_H_INT_MASK  0x04
#define FN1_H_INT_STATUS 0x05
#define FN1_H_INT_RSR   0x06
#define FN1_H_INT_STATUS2 0x07
#define FN1_RD_BASE     0x10
#define FN1_STATUS      0x20  /* IO_RDY 0x08 | CIS_RDY 0x04 | UL_RDY 0x02 | DL_RDY 0x01 */
#define FN1_C_INT_MASK  0x24
#define FN1_C_INT_STATUS 0x28
#define FN1_C_INT_RSR   0x2C
#define FN1_SCRATCH     0x34  /* 16-bit LE; rx packet length when pending */
#define FN1_FW_STATUS   0x40  /* 16-bit LE; 0xFEDC = firmware ready */
#define FN1_RX_LEN      0x42
#define FN1_RX_UNIT     0x43
#define MV_FIRMWARE_OK  0xfedc

#define H_INT_UPLD      0x01  /* card-to-host packet pending */
#define H_INT_DNLD      0x02  /* host-to-card download done / buffer free */

/* SDIO packet framing (Libertas): [le16 total size][le16 type][payload] */
#define MVMS_DAT        0
#define MVMS_CMD        1
#define MVMS_TXDONE     2
#define MVMS_EVENT      3

/* Firmware command set (libertas/host.h) */
#define CMD_GET_HW_SPEC             0x0003
#define CMD_802_11_RESET            0x0005
#define CMD_802_11_SCAN             0x0006
#define CMD_MAC_MULTICAST_ADR       0x0010
#define CMD_802_11_AUTHENTICATE     0x0011
#define CMD_802_11_SET_WEP          0x0013
#define CMD_802_11_SNMP_MIB         0x0016
#define CMD_MAC_REG_ACCESS          0x0019
#define CMD_802_11_RADIO_CONTROL    0x001c
#define CMD_802_11_RF_CHANNEL       0x001d
#define CMD_802_11_RF_TX_POWER      0x001e
#define CMD_802_11_RSSI             0x001f
#define CMD_802_11_PS_MODE          0x0021
#define CMD_802_11_DEAUTHENTICATE   0x0024
#define CMD_MAC_CONTROL             0x0028
#define CMD_802_11_DEEP_SLEEP       0x003e
#define CMD_802_11_MAC_ADDRESS      0x004d
#define CMD_802_11_ASSOCIATE        0x0050
#define CMD_RET(c)                  (0x8000 | (c))
/* Unlike normal replies, the Libertas firmware reports an association
 * response using the legacy 0x8012 command ID. */
#define CMD_RET_802_11_ASSOCIATE    0x8012

/* Firmware events (libertas/host.h MACREG_INT_CODE_*) */
#define MV_EVENT_LINK_SENSED        4
#define MV_EVENT_DEAUTHENTICATED    8
#define MV_EVENT_DEEP_SLEEP_AWAKE   16

#define MV8686_SSID     "iPod Emulator Network"
#define MV8686_CHANNEL  6

static const uint8_t mv8686_bssid[6] = { 0x02, 0x1a, 0x11, 0xe0, 0x86, 0x86 };

static bool mv8686_wifi_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        const char *env = getenv("IPOD_MV_WIFI");
        enabled = (!env || !env[0] || strcmp(env, "0") != 0) ? 1 : 0;
    }
    return enabled;
}

static bool mv8686_trace_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        const char *env = getenv("IPOD_SDIO_TRACE");
        enabled = (env && env[0] && strcmp(env, "0") != 0) ? 1 : 0;
    }
    return enabled;
}

static void G_GNUC_PRINTF(1, 2) mv_trace(const char *fmt, ...)
{
    va_list ap;

    if (!mv8686_trace_enabled()) {
        return;
    }
    fprintf(stderr, "[mv8686] ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n");
}

static void mv_trace_hex(const char *tag, const uint8_t *buf, size_t len)
{
    char line[3 * 48 + 1];
    size_t n = MIN(len, (size_t)48);
    size_t i;

    if (!mv8686_trace_enabled()) {
        return;
    }
    for (i = 0; i < n; i++) {
        sprintf(line + 3 * i, "%02x ", buf[i]);
    }
    line[3 * n] = 0;
    fprintf(stderr, "[mv8686] %s len=%zu: %s%s\n", tag, len, line,
            len > n ? "..." : "");
}

/* Minimal CIS blobs. CIS0 identifies the card (Marvell 0x02DF, device
 * 0x9103); CIS1 carries the function extension with the max block size. */
static const uint8_t mv8686_cis0[] = {
    0x20, 0x04, 0xdf, 0x02, 0x03, 0x91,            /* MANFID */
    0x21, 0x02, 0x0c, 0x00,                        /* FUNCID: wireless */
    0x22, 0x04, 0x00, 0x00, 0x00, 0x08,            /* FUNCE fn0: blk 2048 */
    0xff,
};

static const uint8_t mv8686_cis1[] = {
    0x20, 0x04, 0xdf, 0x02, 0x03, 0x91,            /* MANFID */
    0x21, 0x02, 0x0c, 0x00,                        /* FUNCID: wireless */
    /* FUNCE type 1 (function): 42 bytes, max block size 512 @ offset 12 */
    0x22, 0x2a, 0x01,
    0x01, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x02, /* max blk size 0x200 */
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00,
    0xff,
};

static void mv8686_update_int(MV8686State *c)
{
    uint8_t status = c->dnld_pending ? H_INT_DNLD : 0;

    if (c->rx_head) {
        status |= H_INT_UPLD;
    }
    /* the scratch registers double as the firmware-ready magic until the
     * mailbox is live; only then do they carry the rx packet length */
    if (c->dl_state == MV8686_FW_READY) {
        uint32_t rx_len = c->rx_head ? c->rx_head->len : 0;
        c->fn1[FN1_SCRATCH] = rx_len & 0xff;
        c->fn1[FN1_SCRATCH + 1] = (rx_len >> 8) & 0xff;
    }
    c->fn1[FN1_H_INT_STATUS] = status;

    if (c->set_card_irq) {
        /* interrupt the host while an upload is pending */
        c->set_card_irq(c->irq_opaque,
                        c->rx_head != NULL || c->dnld_pending);
    }
}

static void mv8686_queue_packet(MV8686State *c, uint16_t type,
                                const void *payload, size_t len)
{
    MV8686Packet *p;

    if (len + 4 > MV8686_MAX_PKT) {
        mv_trace("queue: oversized packet dropped (%zu)", len);
        return;
    }
    p = g_malloc0(sizeof(*p));
    p->len = len + 4;
    p->data[0] = p->len & 0xff;
    p->data[1] = (p->len >> 8) & 0xff;
    p->data[2] = type & 0xff;
    p->data[3] = (type >> 8) & 0xff;
    memcpy(p->data + 4, payload, len);
    p->next = NULL;
    if (c->rx_tail) {
        c->rx_tail->next = p;
        c->rx_tail = p;
    } else {
        c->rx_head = c->rx_tail = p;
    }
    mv8686_update_int(c);
}

static void mv8686_queue_event(MV8686State *c, uint32_t code)
{
    /* 8686 SDIO event packets carry the event ID directly in the low byte
     * of a little-endian cause word.  The three-bit shift belongs to the
     * older 8385 register-based event path, not this mailbox protocol. */
    uint32_t cause = code;
    uint8_t payload[4];

    payload[0] = cause & 0xff;
    payload[1] = (cause >> 8) & 0xff;
    payload[2] = (cause >> 16) & 0xff;
    payload[3] = (cause >> 24) & 0xff;
    mv8686_queue_packet(c, MVMS_EVENT, payload, sizeof(payload));
}

static void mv8686_wake_timer(void *opaque)
{
    MV8686State *c = opaque;

    if (c->deep_sleep) {
        c->deep_sleep = false;
        mv8686_queue_event(c, MV_EVENT_DEEP_SLEEP_AWAKE);
    }
}

/* --- firmware command handling ------------------------------------- */

static uint16_t le16(const uint8_t *p) { return p[0] | (p[1] << 8); }
static void put_le16(uint8_t *p, uint16_t v) { p[0] = v & 0xff; p[1] = v >> 8; }
static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = v & 0xff; p[1] = (v >> 8) & 0xff;
    p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}

static void mv8686_cmd_response(MV8686State *c, uint16_t cmd, uint16_t seq,
                                const uint8_t *body, size_t body_len)
{
    uint8_t resp[MV8686_MAX_PKT - 4];
    size_t total = 8 + body_len;

    if (total > sizeof(resp)) {
        total = sizeof(resp);
        body_len = total - 8;
    }
    put_le16(resp + 0, cmd == CMD_802_11_ASSOCIATE ?
             CMD_RET_802_11_ASSOCIATE : CMD_RET(cmd));
    put_le16(resp + 2, total);
    put_le16(resp + 4, seq);
    put_le16(resp + 6, 0);          /* result: success */
    if (body_len) {
        memcpy(resp + 8, body, body_len);
    }
    mv8686_queue_packet(c, MVMS_CMD, resp, total);
}

static void mv8686_handle_scan(MV8686State *c, uint16_t seq)
{
    /* Response: le16 bssdescriptsize, u8 nr_sets, then one descriptor:
     * le16 size, bssid[6], u8 rssi, u8 timestamp[8], le16 beacon
     * interval, le16 capability, then IEs (SSID, rates, DS param). */
    uint8_t body[256];
    uint8_t *d = body + 3;   /* descriptor area */
    uint8_t *p = d + 2;      /* skip per-descriptor size, filled below */
    size_t ssid_len = strlen(MV8686_SSID);
    uint16_t desc_size;

    memcpy(p, mv8686_bssid, 6); p += 6;
    *p++ = 60;                       /* RSSI */
    memset(p, 0, 8); p += 8;         /* timestamp */
    put_le16(p, 100); p += 2;        /* beacon interval */
    put_le16(p, 0x0001); p += 2;     /* capability: ESS, open */
    *p++ = 0x00; *p++ = ssid_len;    /* SSID IE */
    memcpy(p, MV8686_SSID, ssid_len); p += ssid_len;
    *p++ = 0x01; *p++ = 4;           /* supported rates IE */
    *p++ = 0x82; *p++ = 0x84; *p++ = 0x8b; *p++ = 0x96;
    *p++ = 0x03; *p++ = 1;           /* DS parameter set (channel) */
    *p++ = MV8686_CHANNEL;

    desc_size = p - (d + 2);
    put_le16(d, desc_size);
    put_le16(body, desc_size + 2);   /* bssdescriptsize incl. size field */
    body[2] = 1;                     /* nr_sets */

    mv8686_cmd_response(c, CMD_802_11_SCAN, seq, body, p - body);
}

static void mv8686_handle_cmd(MV8686State *c, const uint8_t *pkt, size_t len)
{
    uint16_t cmd, size, seq;
    const uint8_t *body;
    size_t body_len;

    if (len < 8) {
        mv_trace("cmd: short packet (%zu)", len);
        return;
    }
    cmd = le16(pkt + 0);
    size = le16(pkt + 2);
    seq = le16(pkt + 4);
    body = pkt + 8;
    body_len = MIN((size_t)(size >= 8 ? size - 8 : 0), len - 8);
    c->cmd_seq = seq;

    mv_trace("cmd 0x%04x size=%u seq=%u", cmd, size, seq);

    switch (cmd) {
    case CMD_GET_HW_SPEC: {
        uint8_t spec[0x26];
        memset(spec, 0, sizeof(spec));
        put_le16(spec + 0, 0x0103);          /* hwifversion */
        put_le16(spec + 2, 0x0000);          /* version */
        put_le16(spec + 4, 1);               /* nr_txpd */
        put_le16(spec + 6, 32);              /* nr_mcast_adr */
        memcpy(spec + 8, c->mac, 6);         /* permanentaddr */
        put_le16(spec + 14, 0x10);           /* regioncode: US */
        put_le16(spec + 16, 1);              /* nr_antenna */
        put_le32(spec + 18, 0x092e0000);     /* fwrelease */
        put_le32(spec + 34, 0x00000000);     /* fwcapinfo */
        mv8686_cmd_response(c, cmd, seq, spec, sizeof(spec));
        break;
    }
    case CMD_802_11_SCAN:
        mv8686_handle_scan(c, seq);
        break;
    case CMD_802_11_ASSOCIATE: {
        uint8_t resp[10];
        memset(resp, 0, sizeof(resp));
        put_le16(resp + 0, 0x0001);          /* capability */
        put_le16(resp + 2, 0x0000);          /* status: success */
        put_le16(resp + 4, 0xc001);          /* AID */
        /* minimal rates IE so parsers that expect one find one */
        resp[6] = 0x01; resp[7] = 0x02; resp[8] = 0x82; resp[9] = 0x84;
        c->associated = true;
        mv8686_cmd_response(c, cmd, seq, resp, sizeof(resp));
        mv8686_queue_event(c, MV_EVENT_LINK_SENSED);
        if (c->deferred_frame_len) {
            size_t deferred_len = c->deferred_frame_len;

            c->deferred_frame_len = 0;
            mv8686_receive_frame(c, c->deferred_frame, deferred_len);
        }
        break;
    }
    case CMD_802_11_DEAUTHENTICATE:
        c->associated = false;
        mv8686_cmd_response(c, cmd, seq, body, body_len);
        mv8686_queue_event(c, MV_EVENT_DEAUTHENTICATED);
        break;
    case CMD_802_11_DEEP_SLEEP:
        /* This command is deliberately fire-and-forget.  Marvell's
         * firmware specification says that the card enters deep sleep
         * immediately and sends no command response.  Apple keeps the
         * command on a special completion path; returning a conventional
         * 0x803e response leaves it there until the watchdog resets Wi-Fi. */
        c->deep_sleep = true;
        break;
    case CMD_802_11_MAC_ADDRESS: {
        uint8_t resp[8];
        memset(resp, 0, sizeof(resp));
        if (body_len >= 2) {
            put_le16(resp, le16(body));      /* echo action */
        }
        memcpy(resp + 2, c->mac, 6);
        mv8686_cmd_response(c, cmd, seq, resp, sizeof(resp));
        break;
    }
    case CMD_802_11_RSSI: {
        uint8_t resp[8];
        put_le16(resp + 0, 40);              /* SNR */
        put_le16(resp + 2, 0);               /* noise floor */
        put_le16(resp + 4, 40);              /* avg SNR */
        put_le16(resp + 6, 0);               /* avg NF */
        mv8686_cmd_response(c, cmd, seq, resp, sizeof(resp));
        break;
    }
    case CMD_802_11_RADIO_CONTROL:
        if (body_len >= 2 && le16(body) == 1) { /* action: set */
            c->radio_on = body_len >= 4 && (le16(body + 2) & 1);
        }
        mv8686_cmd_response(c, cmd, seq, body, body_len);
        break;
    default:
        /* echo the request body back with a success result; GET-style
         * commands then see their own defaults */
        mv8686_cmd_response(c, cmd, seq, body, body_len);
        break;
    }
}

static void mv8686_handle_tx_data(MV8686State *c, const uint8_t *pkt,
                                  size_t len)
{
    /* struct txpd: u32 tx_status, u32 tx_control, u32 tx_packet_location,
     * le16 tx_packet_length, ... frame follows at tx_packet_location. */
    uint32_t loc;
    uint16_t frame_len;

    if (len < 24) {
        mv_trace("tx: short txpd (%zu)", len);
        return;
    }
    loc = pkt[8] | (pkt[9] << 8) | (pkt[10] << 16) | (pkt[11] << 24);
    frame_len = le16(pkt + 12);
    if (loc == 0 || loc > len) {
        loc = 24;
    }
    if (loc + frame_len > len) {
        frame_len = len - loc;
    }
    mv_trace_hex("tx frame", pkt + loc, frame_len);
    if (c->send_frame) {
        c->send_frame(c->net_opaque, pkt + loc, frame_len);
    }
}

void mv8686_receive_frame(MV8686State *c, const uint8_t *frame, size_t len)
{
    /* struct rxpd (20 bytes) + frame */
    uint8_t pkt[MV8686_MAX_PKT - 4];

    mv_trace_hex("rx frame", frame, len);

    if (len + 20 > sizeof(pkt)) {
        return;
    }

    /* Slirp may synchronously return the first DHCP reply while the guest's
     * ASSOCIATE command is still queued behind its optimistic DHCP request.
     * Preserve that reply, but expose it only after the association response
     * and link event so the old network stack does not discard it. */
    if (!c->associated) {
        if (!c->deferred_frame_len) {
            memcpy(c->deferred_frame, frame, len);
            c->deferred_frame_len = len;
            mv_trace("deferred pre-association frame (%zu bytes)", len);
        }
        return;
    }

    memset(pkt, 0, 20);
    pkt[2] = 40;                     /* snr */
    put_le16(pkt + 4, len);          /* pkt_len */
    pkt[7] = 3;                      /* rx_rate */
    put_le32(pkt + 8, 20);           /* pkt_ptr */
    memcpy(pkt + 20, frame, len);
    mv8686_queue_packet(c, MVMS_DAT, pkt, 20 + len);
}

/* --- SDIO transport ------------------------------------------------- */

static void mv8686_stage_eeprom(MV8686State *c, const uint8_t *req,
                                uint32_t req_len)
{
    uint8_t *p = c->eeprom;

    /* AppleMRVL868x::parseEEPROM() expects a big-endian record stream:
     *
     *   de ad 00 04 be ef ca fe       image header
     *   [be16 key][be16 words][data]  records, words includes the header
     *
     * Key 1 becomes the "tx-calibration" device-tree property and key 2
     * becomes "local-mac-address".  The start path rejects calibration
     * whose first 128 bytes are all 0x00 or all 0xff, so use a stable,
     * non-uniform behavioral-model payload.  The real RF calibration is
     * neither consumed by QEMU nor needed by the modeled firmware.
     *
     * The helper reports EEPROM response length through scratch 0x34/35;
     * it is a signed 16-bit chunk length, not the 0xFEDC firmware-ready
     * marker.  Apple reads exactly 0x800 bytes before parsing. */
    memset(c->eeprom, 0xff, sizeof(c->eeprom));
    *p++ = 0xde; *p++ = 0xad;
    *p++ = 0x00; *p++ = 0x04;
    *p++ = 0xbe; *p++ = 0xef;
    *p++ = 0xca; *p++ = 0xfe;

    /* Key 1: 128-byte Wi-Fi calibration; (4 + 128) / 2 = 66 words. */
    *p++ = 0x00; *p++ = 0x01;
    *p++ = 0x00; *p++ = 0x42;
    for (unsigned i = 0; i < 128; i++) {
        *p++ = i;
    }

    /* Key 2: six-byte Wi-Fi MAC; (4 + 6) / 2 = 5 words. */
    *p++ = 0x00; *p++ = 0x02;
    *p++ = 0x00; *p++ = 0x05;
    memcpy(p, c->mac, sizeof(c->mac));
    c->eeprom_len = sizeof(c->eeprom);

    c->dl_state = MV8686_EEPROM_READ;
    /* no further host writes expected before the read */
    c->fn1[FN1_RD_BASE] = 0;
    c->fn1[FN1_RD_BASE + 1] = 0;
    /* Signal the staged response length through both register pairs. */
    c->fn1[FN1_RX_LEN] = c->eeprom_len & 0xff;
    c->fn1[FN1_RX_UNIT] = (c->eeprom_len >> 8) & 0xff;
    c->fn1[FN1_SCRATCH] = c->eeprom_len & 0xff;
    c->fn1[FN1_SCRATCH + 1] = (c->eeprom_len >> 8) & 0xff;
    (void)req; (void)req_len;
}

bool mv8686_io_rw_extended(MV8686State *c, bool write, uint8_t fn,
                           uint32_t addr, uint8_t *buf, uint32_t len)
{
    if (fn != 1) {
        mv_trace("cmd53 on unexpected function %u", fn);
        return false;
    }

    if (addr == MV8686_IOPORT_ADDR) {
        if (write && c->dl_state == MV8686_DL_HELPER) {
            /* helper chunks: [le32 chunk size][data]; zero size ends */
            uint32_t chunk = len >= 4 ?
                (buf[0] | (buf[1] << 8) | (buf[2] << 16) | (buf[3] << 24)) : 0;
            if (chunk == 0) {
                /* Bootstrapper booted. AppleMRVL868x::readEEPROM waits for
                 * RD_BASE to equal its fixed 16-byte request size. */
                if (mv8686_wifi_enabled() && c->eeprom_delivered) {
                    /* Recovery reload: Apple only reads the EEPROM on its
                     * first cold probe.  loadMainProgram() polls RD_BASE for
                     * the 0x800 download request size right after the helper
                     * boots. */
                    c->dl_state = MV8686_DL_MAIN;
                    c->fn1[FN1_RD_BASE] = 0x00;
                    c->fn1[FN1_RD_BASE + 1] = 0x08;
                    mv_trace("helper booted (%u bytes); warm reload skips "
                             "EEPROM stage", c->helper_bytes);
                } else if (mv8686_wifi_enabled()) {
                    c->dl_state = MV8686_EEPROM_CMD;
                    c->fn1[FN1_RD_BASE] = MV8686_EEPROM_CMD_LEN;
                    c->fn1[FN1_RD_BASE + 1] = 0;
                    mv_trace("helper booted (%u bytes); RD_BASE=%u for EEPROM cmd",
                             c->helper_bytes, MV8686_EEPROM_CMD_LEN);
                } else {
                    /* graceful: report a firmware-download request size so
                     * readEEPROM's ready-status wait times out without a
                     * panic, matching the pre-bring-up behavior */
                    c->dl_state = MV8686_DL_HELPER;
                    c->fn1[FN1_RD_BASE] = 0x00;
                    c->fn1[FN1_RD_BASE + 1] = 0x08;
                    mv_trace("helper booted (%u bytes); Wi-Fi bring-up "
                             "disabled by IPOD_MV_WIFI=0",
                             c->helper_bytes);
                }
            } else {
                c->helper_bytes += MIN(chunk, len - 4);
            }
            return true;
        }
        if (write && c->dl_state == MV8686_EEPROM_CMD) {
            /* the 16-byte EEPROM read request from the helper */
            mv_trace_hex("eeprom request", buf, len);
            mv8686_stage_eeprom(c, buf, len);
            return true;
        }
        if (write && c->dl_state == MV8686_DL_MAIN) {
            c->main_bytes += len;
            /* accept the image blindly; announce firmware-ready through
             * the scratch registers the host will poll when it finishes */
            c->fn1[FN1_SCRATCH] = MV_FIRMWARE_OK & 0xff;
            c->fn1[FN1_SCRATCH + 1] = MV_FIRMWARE_OK >> 8;
            return true;
        }
        if (!write && c->dl_state == MV8686_EEPROM_READ) {
            /* deliver the staged EEPROM image */
            uint32_t n = MIN(len, c->eeprom_len);
            memset(buf, 0, len);
            memcpy(buf, c->eeprom, n);
            mv_trace("eeprom read: delivered %u of %u bytes", n, len);
            c->eeprom_delivered = true;
            /* EEPROM done; move on to the main firmware download */
            c->dl_state = MV8686_DL_MAIN;
            c->fn1[FN1_RD_BASE] = 0x00;
            c->fn1[FN1_RD_BASE + 1] = 0x08;   /* req_size 0x800 */
            return true;
        }
        if (write) {
            uint16_t pkt_len, type;
            mv_trace_hex("cmd53 tx", buf, len);
            if (len < 4) {
                return false;
            }
            pkt_len = le16(buf);
            type = le16(buf + 2);
            if (pkt_len < 4 || pkt_len > len) {
                pkt_len = len;
            }
            switch (type) {
            case MVMS_CMD:
                mv8686_handle_cmd(c, buf + 4, pkt_len - 4);
                break;
            case MVMS_DAT:
                mv8686_handle_tx_data(c, buf + 4, pkt_len - 4);
                break;
            default:
                mv_trace("tx packet with unhandled type %u", type);
                break;
            }
            /* The card consumed the host buffer.  This is a distinct
             * interrupt cause from a command response and matters for
             * fire-and-forget commands such as DEEP_SLEEP. */
            c->dnld_pending = true;
            mv8686_update_int(c);
        } else {
            MV8686Packet *p = c->rx_head;
            if (!p) {
                mv_trace("cmd53 rx with empty queue");
                memset(buf, 0, len);
                return true;
            }
            memset(buf, 0, len);
            memcpy(buf, p->data, MIN(p->len, len));
            mv_trace_hex("cmd53 rx", buf, MIN(p->len, len));
            c->rx_head = p->next;
            if (!c->rx_head) {
                c->rx_tail = NULL;
            }
            g_free(p);
            mv8686_update_int(c);
        }
        return true;
    }

    /* extended access to the fn1 register window */
    for (uint32_t i = 0; i < len; i++) {
        uint32_t reg = addr + i;
        if (reg >= sizeof(c->fn1)) {
            break;
        }
        if (write) {
            c->fn1[reg] = buf[i];
        } else {
            buf[i] = c->fn1[reg];
        }
    }
    return true;
}

/* --- fn0 / enumeration ---------------------------------------------- */

static uint8_t mv8686_fn0_read(MV8686State *c, uint32_t reg)
{
    if (reg < 0x100) {
        if (reg == CCCR_IOR) {
            /* every enabled function is instantly ready */
            return c->cccr[CCCR_IOE];
        }
        if (reg == CCCR_INT_PEND) {
            /* Bit n reports an interrupt pending from function n.  The
             * S5L8900 controller raises its card-interrupt IRQ first, then
             * AppleMRVL868x reads this register to decide whether it should
             * inspect function 1's H_INT_STATUS. */
            return (c->rx_head || c->dnld_pending) ? (1 << 1) : 0;
        }
        return c->cccr[reg];
    }
    if (reg >= 0x100 && reg < 0x200) {
        return c->fbr1[reg - 0x100];
    }
    if (reg >= MV8686_CIS0_ADDR &&
        reg < MV8686_CIS0_ADDR + sizeof(mv8686_cis0)) {
        return mv8686_cis0[reg - MV8686_CIS0_ADDR];
    }
    if (reg >= MV8686_CIS1_ADDR &&
        reg < MV8686_CIS1_ADDR + sizeof(mv8686_cis1)) {
        return mv8686_cis1[reg - MV8686_CIS1_ADDR];
    }
    return 0;
}

static void mv8686_fn0_write(MV8686State *c, uint32_t reg, uint8_t data)
{
    switch (reg) {
    case CCCR_IOE:
    case CCCR_IEN:
    case CCCR_BUS_IFACE:
    case CCCR_FN0_BLK:
    case CCCR_FN0_BLK + 1:
    case CCCR_POWER:
        c->cccr[reg] = data;
        break;
    case CCCR_IO_ABORT:
        if (data & 0x08) {
            /* RES resets function 1.  Apple uses this to recover the chip;
             * leaving the mailbox live makes the following helper image
             * look like a stream of malformed runtime packets. */
            mv8686_reset(c);
        }
        break;
    default:
        if (reg >= 0x100 && reg < 0x200) {
            uint32_t fbr = reg - 0x100;
            if (fbr == FBR_BLKSIZE || fbr == FBR_BLKSIZE + 1) {
                c->fbr1[fbr] = data;
            }
        }
        break;
    }
}

static uint8_t mv8686_fn1_read(MV8686State *c, uint32_t reg)
{
    uint8_t value;

    if (reg >= sizeof(c->fn1)) {
        return 0;
    }
    /* the host polls scratch for 0xFEDC after the main download; seeing
     * the second byte read completes the firmware boot handshake */
    if (c->dl_state == MV8686_DL_MAIN && c->main_bytes &&
        reg == FN1_SCRATCH + 1) {
        c->dl_state = MV8686_FW_READY;
        mv_trace("firmware boot handshake complete (main %u bytes)",
                 c->main_bytes);
    }
    value = c->fn1[reg];
    if (reg == FN1_H_INT_STATUS) {
        /* Apple reads the cause register once per card interrupt.  The
         * download-ready edge is consumed by that read; upload remains
         * asserted until its packet is drained from the mailbox. */
        c->dnld_pending = false;
        mv8686_update_int(c);
    }
    return value;
}

static void mv8686_fn1_write(MV8686State *c, uint32_t reg, uint8_t data)
{
    if (reg >= sizeof(c->fn1)) {
        return;
    }
    switch (reg) {
    case FN1_CONFIG:
        c->fn1[reg] = data;
        if (c->deep_sleep && (data & 0x02) && /* HOST_POWER_UP */
            !timer_pending(c->wake_timer)) {
            /* Real firmware needs time to wake.  More importantly, do not
             * raise DS_AWAKE from inside the guest's CMD52 write: Apple sets
             * its wake-wait state after that write returns. */
            timer_mod(c->wake_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      NANOSECONDS_PER_SECOND);
        }
        break;
    case FN1_H_INT_STATUS:
        /* write-to-clear from the host; recompute from queue state */
        mv8686_update_int(c);
        break;
    default:
        c->fn1[reg] = data;
        break;
    }
}

static uint32_t mv8686_cmd52(MV8686State *c, uint32_t arg)
{
    bool write = (arg >> 31) & 1;
    uint8_t fn = (arg >> 28) & 0x7;
    bool raw = (arg >> 27) & 1;
    uint32_t reg = (arg >> 9) & 0x1ffff;
    uint8_t data = arg & 0xff;
    uint8_t out = 0;

    if (write) {
        if (fn == 0) {
            mv8686_fn0_write(c, reg, data);
        } else if (fn == 1) {
            mv8686_fn1_write(c, reg, data);
        }
        if (raw) {
            out = fn == 0 ? mv8686_fn0_read(c, reg)
                          : mv8686_fn1_read(c, reg);
        } else {
            out = data;
        }
    } else {
        if (fn == 0) {
            out = mv8686_fn0_read(c, reg);
        } else if (fn == 1) {
            out = mv8686_fn1_read(c, reg);
        }
    }
    /* R5: flags byte (no errors, state=CMD) << 8 | data */
    return (0x10 << 8) | out;
}

uint32_t mv8686_exec_cmd(MV8686State *c, uint8_t cmd_idx, uint32_t arg)
{
    switch (cmd_idx) {
    case 5:
        /* The inquiry CMD5 (arg 0) only occurs while (re-)enumerating the
         * card.  AppleMRVL868x's recovery path (invokeTheHandOfGod) power
         * cycles the chip and re-enumerates without touching IO_ABORT, so
         * treat the probe as the power-on edge and return the card to its
         * awaiting-bootstrapper state. */
        if (arg == 0) {
            mv8686_reset(c);
        }
        /* R4: C=1, one I/O function, no memory, OCR */
        return (1u << 31) | (1u << 28) | MV8686_OCR;
    case 3:
        /* R6: new RCA | card status bits */
        return (MV8686_RCA << 16);
    case 7:
        c->selected = true;
        /* R1b: state=stby, ready */
        return 0x00000700;
    case 52:
        return mv8686_cmd52(c, arg);
    default:
        return 0;
    }
}

void mv8686_reset(MV8686State *c)
{
    MV8686Packet *p = c->rx_head;
    uint8_t mac[sizeof(c->mac)];
    bool have_mac;

    memcpy(mac, c->mac, sizeof(mac));
    have_mac = memcmp(mac, (uint8_t[sizeof(mac)]) { 0 }, sizeof(mac)) != 0;

    while (p) {
        MV8686Packet *next = p->next;
        g_free(p);
        p = next;
    }

    void (*set_card_irq)(void *, int) = c->set_card_irq;
    void *irq_opaque = c->irq_opaque;
    void (*send_frame)(void *, const uint8_t *, size_t) = c->send_frame;
    void *net_opaque = c->net_opaque;
    QEMUTimer *wake_timer = c->wake_timer;
    bool eeprom_delivered = c->eeprom_delivered;

    memset(c, 0, sizeof(*c));
    c->set_card_irq = set_card_irq;
    c->irq_opaque = irq_opaque;
    c->send_frame = send_frame;
    c->net_opaque = net_opaque;
    c->wake_timer = wake_timer;
    c->eeprom_delivered = eeprom_delivered;
    if (!c->wake_timer) {
        c->wake_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                     mv8686_wake_timer, c);
    } else {
        timer_del(c->wake_timer);
    }

    c->cccr[CCCR_REVISION] = 0x11;   /* CCCR 1.1 / SDIO 1.1 */
    c->cccr[CCCR_SD_SPEC] = 0x00;
    c->cccr[CCCR_CARD_CAP] = 0x03;   /* SDC | SMB (multiblock) */
    c->cccr[CCCR_CIS_PTR] = MV8686_CIS0_ADDR & 0xff;
    c->cccr[CCCR_CIS_PTR + 1] = (MV8686_CIS0_ADDR >> 8) & 0xff;
    c->cccr[CCCR_CIS_PTR + 2] = (MV8686_CIS0_ADDR >> 16) & 0xff;
    c->cccr[CCCR_POWER] = 0x01;      /* SMPC */

    c->fbr1[FBR_IFACE_CODE] = 0x07;  /* standard interface code: WLAN */
    c->fbr1[FBR_CIS_PTR] = MV8686_CIS1_ADDR & 0xff;
    c->fbr1[FBR_CIS_PTR + 1] = (MV8686_CIS1_ADDR >> 8) & 0xff;
    c->fbr1[FBR_CIS_PTR + 2] = (MV8686_CIS1_ADDR >> 16) & 0xff;
    c->fbr1[FBR_BLKSIZE] = 0x00;
    c->fbr1[FBR_BLKSIZE + 1] = 0x02; /* default fn1 block size 512 */

    c->fn1[FN1_IOPORT0] = MV8686_IOPORT_ADDR & 0xff;
    c->fn1[FN1_IOPORT1] = (MV8686_IOPORT_ADDR >> 8) & 0xff;
    c->fn1[FN1_IOPORT2] = (MV8686_IOPORT_ADDR >> 16) & 0xff;
    c->fn1[FN1_STATUS] = 0x0f;    /* IO/CIS/UL/DL all ready */
    c->fn1[FN1_H_INT_STATUS] = H_INT_DNLD;
    c->fn1[FN1_FW_STATUS] = MV_FIRMWARE_OK & 0xff;
    c->fn1[FN1_FW_STATUS + 1] = MV_FIRMWARE_OK >> 8;

    if (have_mac) {
        memcpy(c->mac, mac, sizeof(c->mac));
    } else {
        /* locally administered MAC, stable across boots */
        c->mac[0] = 0x02; c->mac[1] = 0x1a; c->mac[2] = 0x11;
        c->mac[3] = 0xe0; c->mac[4] = 0x00; c->mac[5] = 0x01;
    }
}

void mv8686_cleanup(MV8686State *c)
{
    MV8686Packet *p = c->rx_head;

    while (p) {
        MV8686Packet *next = p->next;
        g_free(p);
        p = next;
    }
    c->rx_head = c->rx_tail = NULL;
    if (c->wake_timer) {
        timer_free(c->wake_timer);
        c->wake_timer = NULL;
    }
}
