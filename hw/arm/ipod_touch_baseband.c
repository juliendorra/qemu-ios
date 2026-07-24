#include "hw/arm/ipod_touch_baseband.h"
#include "qemu/module.h"
#include "qemu/timer.h"

/*
 * IT_BASEBAND_TRACE=<path> logs every byte AppleBaseband writes to the
 * S-Gold2 stub and every response the stub queues, with guest-visible
 * timestamps. "1"/"stderr" log to stderr. The trace is the ground truth
 * for reconstructing the init handshake (see scripts/sgold2d.py for the
 * external, hot-reloadable variant of this stub).
 */
static FILE *sgold2_trace_file(void)
{
    static FILE *fp;
    static bool checked;

    if (!checked) {
        checked = true;
        const char *path = getenv("IT_BASEBAND_TRACE");
        if (path && *path) {
            if (!strcmp(path, "1") || !strcmp(path, "stderr")) {
                fp = stderr;
            } else {
                fp = fopen(path, "a");
            }
        }
    }
    return fp;
}

static void sgold2_trace(const char *dir, const uint8_t *buf, int len)
{
    FILE *fp = sgold2_trace_file();

    if (!fp) {
        return;
    }
    int64_t now = qemu_clock_get_us(QEMU_CLOCK_VIRTUAL);
    fprintf(fp, "[%3lld.%06lld] %s ", now / 1000000LL, now % 1000000LL, dir);
    for (int i = 0; i < len; i++) {
        fprintf(fp, "%02x ", buf[i]);
    }
    fprintf(fp, " |");
    for (int i = 0; i < len; i++) {
        fputc(buf[i] >= 0x20 && buf[i] < 0x7f ? buf[i] : '.', fp);
    }
    fprintf(fp, "|\n");
    fflush(fp);
}

static void sgold2_flush(Chardev *chr)
{
    SGold2State *s = SGOLD2_CHARDEV(chr);
    int len = qemu_chr_be_can_write(chr);

    if (len > s->outlen) {
        len = s->outlen;
    }
    if (!len) {
        return;
    }

    qemu_chr_be_write(chr, s->outbuf, len);
    s->outlen -= len;
    if (s->outlen) {
        memmove(s->outbuf, s->outbuf + len, s->outlen);
    }
}

static void sgold2_queue(SGold2State *s, const char *resp)
{
    int len = strlen(resp);

    if (s->outlen + len > sizeof(s->outbuf)) {
        return; // queue full -> drop the response
    }
    memcpy(s->outbuf + s->outlen, resp, len);
    s->outlen += len;
    sgold2_trace("<-", (const uint8_t *)resp, len);
    sgold2_flush(CHARDEV(s));
}

/*
 * IT_BASEBAND_RULES=<path> replaces the hardcoded responses below with a
 * text table, so response hypotheses iterate by editing a file and
 * relaunching QEMU -- no rebuild -- while keeping the in-MMIO instant
 * reply timing that the external-socket modem (scripts/sgold2d.py)
 * cannot provide (its ~ms asynchrony perturbs the IOIpodUSBDevice::start
 * race; see DEVICE_BRINGUP_PLAYBOOK.md).
 *
 * Format, one rule per line, first match wins:
 *     <prefix>\t<response>
 *     default\t<response>          # unmatched AT lines ("-" = stay silent)
 *     # comment / blank lines ignored
 * <prefix> matches case-insensitively at the start of the AT line.
 * <response> understands \r \n \t \\ escapes, "-" for silent, and the
 * tokens {int} (atoi of the text after the prefix) and {rest} (raw text
 * after the prefix).
 */
typedef struct {
    char *match;
    char *response;              /* unescaped; NULL = silent match */
} SGold2Rule;

static SGold2Rule *sgold2_rules;
static int sgold2_num_rules;
static char *sgold2_default_resp;    /* NULL = "\r\nOK\r\n"; "" = silent */
static bool sgold2_use_rules;

static char *sgold2_unescape(const char *in)
{
    char *out = g_malloc(strlen(in) + 1);
    char *w = out;

    for (const char *r = in; *r; r++) {
        if (*r == '\\' && r[1]) {
            r++;
            switch (*r) {
            case 'r': *w++ = '\r'; break;
            case 'n': *w++ = '\n'; break;
            case 't': *w++ = '\t'; break;
            default:  *w++ = *r;   break;
            }
        } else {
            *w++ = *r;
        }
    }
    *w = '\0';
    return out;
}

static void sgold2_load_rules(void)
{
    static bool checked;

    if (checked) {
        return;
    }
    checked = true;
    const char *path = getenv("IT_BASEBAND_RULES");
    if (!path || !*path) {
        return;
    }
    FILE *fp = fopen(path, "r");
    if (!fp) {
        fprintf(stderr, "sgold2: cannot open IT_BASEBAND_RULES %s\n", path);
        exit(1);
    }
    char linebuf[1024];
    while (fgets(linebuf, sizeof(linebuf), fp)) {
        char *nl = strpbrk(linebuf, "\r\n");
        if (nl) {
            *nl = '\0';
        }
        if (!linebuf[0] || linebuf[0] == '#') {
            continue;
        }
        char *tab = strchr(linebuf, '\t');
        if (!tab) {
            fprintf(stderr, "sgold2: bad rules line (no TAB): %s\n", linebuf);
            exit(1);
        }
        *tab = '\0';
        const char *resp_raw = tab + 1;
        char *resp = strcmp(resp_raw, "-") == 0 ? NULL
                                                : sgold2_unescape(resp_raw);
        if (strcmp(linebuf, "default") == 0) {
            sgold2_default_resp = resp ? resp : g_strdup("");
            continue;
        }
        sgold2_rules = g_realloc(sgold2_rules,
                                 (sgold2_num_rules + 1) * sizeof(SGold2Rule));
        sgold2_rules[sgold2_num_rules].match = g_strdup(linebuf);
        sgold2_rules[sgold2_num_rules].response = resp;
        sgold2_num_rules++;
    }
    fclose(fp);
    sgold2_use_rules = true;
    fprintf(stderr, "sgold2: loaded %d rules from %s\n",
            sgold2_num_rules, path);
}

static void sgold2_queue_template(SGold2State *s, const char *tmpl,
                                  const char *rest)
{
    char resp[1024];
    size_t n = 0;

    for (const char *r = tmpl; *r && n < sizeof(resp) - 1; ) {
        if (strncmp(r, "{int}", 5) == 0) {
            n += snprintf(resp + n, sizeof(resp) - n, "%d", atoi(rest));
            r += 5;
        } else if (strncmp(r, "{rest}", 6) == 0) {
            n += snprintf(resp + n, sizeof(resp) - n, "%s", rest);
            r += 6;
        } else {
            resp[n++] = *r++;
        }
    }
    resp[n < sizeof(resp) ? n : sizeof(resp) - 1] = '\0';
    sgold2_queue(s, resp);
}

static bool sgold2_process_line_rules(SGold2State *s, const char *line)
{
    for (int i = 0; i < sgold2_num_rules; i++) {
        SGold2Rule *rule = &sgold2_rules[i];
        size_t mlen = strlen(rule->match);

        if (strncasecmp(line, rule->match, mlen) == 0) {
            if (rule->response) {
                sgold2_queue_template(s, rule->response, line + mlen);
            }
            return true;
        }
    }
    if (sgold2_default_resp) {
        if (sgold2_default_resp[0]) {
            sgold2_queue(s, sgold2_default_resp);
        }
        return true;
    }
    sgold2_queue(s, "\r\nOK\r\n");
    return true;
}

static void sgold2_process_line(SGold2State *s)
{
    const char *line = s->line;

    if (strncasecmp(line, "at", 2) != 0) {
        return; // not an AT command; a real modem would stay silent too
    }

    sgold2_load_rules();
    if (sgold2_use_rules) {
        sgold2_process_line_rules(s, line);
        return;
    }

    if (strncasecmp(line, "at+xdrv=9,1,", 12) == 0) {
        // baseband NVRAM read: report an empty store so enumeration ends
        char resp[64];
        int idx = atoi(line + 12);
        snprintf(resp, sizeof(resp), "\r\n+XDRV: 9,1,0,%d,NULL\r\n\r\nOK\r\n", idx);
        sgold2_queue(s, resp);
        return;
    }

    if (strncasecmp(line, "at+xdrv=4,", 10) == 0) {
        // device 4 is the vibrator (no GPIO on M68AP - it lives behind the baseband)
        fprintf(stderr, "sgold2: vibrator command: %s\n", line);
        sgold2_queue(s, "\r\nOK\r\n");
        return;
    }

    if (strncasecmp(line, "at+cops?", 8) == 0) {
        // pretend we are registered with a carrier
        sgold2_queue(s, "\r\n+COPS: 0,0,\"QEMU\",0\r\n\r\nOK\r\n");
        return;
    }

    if (strncasecmp(line, "at+xcallstat=", 13) == 0) {
        s->xcallstat_enabled = (line[13] == '1');
        sgold2_queue(s, "\r\nOK\r\n");
        return;
    }

    if (strncasecmp(line, "atd", 3) == 0) {
        fprintf(stderr, "sgold2: dial request: %s\n", line);
        sgold2_queue(s, "\r\nOK\r\n");
        if (s->xcallstat_enabled) {
            // report the call as immediately active
            sgold2_queue(s, "\r\n+XCALLSTAT: 1,0\r\n");
        }
        return;
    }

    // at, at+ipr=..., at+xdrv=0,... (audio), at+chld, at+cmut, ...
    sgold2_queue(s, "\r\nOK\r\n");
}

/*
 * IT_BASEBAND_H5=1: after at+xtransportmode ("Enabling h5 (snooped
 * at+xtransportmode)" -- string in the kernel AppleReliableSerialLayer
 * kext), CommCenter drives the baseband link with the H5 / BCSP
 * "Three-wire UART" transport (a documented Bluetooth-family protocol):
 *
 *   SLIP framing  : 0xC0 delimiter, 0xDB escape (DB DC = literal C0,
 *                   DB DD = literal DB); RFC 1055.
 *   4-byte header : b0 = SEQ(0-2) ACK(3-5) CRC-present(6) reliable(7)
 *                   b1 = type(0-3) payload-len-low(4-7)
 *                   b2 = payload-len-high(0-7)   (12-bit length)
 *                   b3 = ~(b0+b1+b2) & 0xff      (header checksum)
 *   type 0xf      = Link Control; payload = the link-establishment magic
 *                   SYNC 01 7e / SYNC-RESP 02 7d / CONFIG 03 fc /
 *                   CONFIG-RESP 04 7b (all verified byte-exact on the wire,
 *                   see IPHONE_2G_BRINGUP_HANDOFF.md runs 11-12).
 *
 * A bare echo (the earlier IT_BASEBAND_FRAME_ECHO probe) never advanced the
 * state machine because bouncing SYNC back is not SYNC-RESP. This responds
 * per the H5 link-establishment rules: SYNC->SYNC-RESP, CONFIG->CONFIG-RESP,
 * enough to bring CommCenter's link up. Frame bytes bypass the AT accumulator.
 */
static bool sgold2_h5_enabled(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *env = getenv("IT_BASEBAND_H5");
        cached = env && *env && strcmp(env, "0") != 0;
    }
    return cached;
}

/* SLIP-encode payload into dst (which already holds a leading 0xC0); returns
 * total bytes written including both delimiters. */
static int sgold2_slip_wrap(uint8_t *dst, const uint8_t *src, int n)
{
    int w = 0;
    dst[w++] = 0xC0;
    for (int i = 0; i < n; i++) {
        if (src[i] == 0xC0) {
            dst[w++] = 0xDB; dst[w++] = 0xDC;
        } else if (src[i] == 0xDB) {
            dst[w++] = 0xDB; dst[w++] = 0xDD;
        } else {
            dst[w++] = src[i];
        }
    }
    dst[w++] = 0xC0;
    return w;
}

/* Build + queue an H5 Link-Control frame carrying `payload`. */
static void sgold2_h5_send_link(SGold2State *s, const uint8_t *payload, int n)
{
    uint8_t pkt[4 + 8];
    pkt[0] = 0x00;                         /* seq0 ack0, unreliable, no CRC */
    pkt[1] = ((n & 0x0f) << 4) | 0x0f;     /* len-low | type 0xf (link ctrl) */
    pkt[2] = (n >> 4) & 0xff;              /* len-high */
    pkt[3] = (~(pkt[0] + pkt[1] + pkt[2])) & 0xff;
    memcpy(pkt + 4, payload, n);

    uint8_t framed[2 + 2 * (4 + 8)];
    int fn = sgold2_slip_wrap(framed, pkt, 4 + n);
    sgold2_trace("<H", framed, fn);
    if (s->outlen + fn <= sizeof(s->outbuf)) {
        memcpy(s->outbuf + s->outlen, framed, fn);
        s->outlen += fn;
        sgold2_flush(CHARDEV(s));
    }
}

/* Process one fully-received, still-SLIP-escaped frame. */
static void sgold2_h5_frame(SGold2State *s)
{
    /* SLIP-unescape in place. */
    uint8_t u[sizeof(s->frame)];
    int un = 0;
    for (int i = 0; i < s->frame_len; i++) {
        uint8_t b = s->frame[i];
        if (b == 0xDB && i + 1 < s->frame_len) {
            uint8_t n = s->frame[++i];
            u[un++] = (n == 0xDC) ? 0xC0 : (n == 0xDD) ? 0xDB : n;
        } else {
            u[un++] = b;
        }
    }
    if (un < 5) {
        return;                            /* too short for header + payload */
    }
    uint8_t type = u[1] & 0x0f;
    const uint8_t *pl = u + 4;
    static const uint8_t sync_resp[] = { 0x02, 0x7d };
    static const uint8_t conf_resp[] = { 0x04, 0x7b };
    if (type == 0x0f) {                    /* Link Control */
        if (pl[0] == 0x01) {               /* SYNC   -> SYNC-RESP */
            sgold2_h5_send_link(s, sync_resp, sizeof(sync_resp));
        } else if (pl[0] == 0x03) {        /* CONFIG -> CONFIG-RESP */
            sgold2_h5_send_link(s, conf_resp, sizeof(conf_resp));
        }
        /* 02 (SYNC-RESP) / 04 (CONFIG-RESP) from the guest: link is coming
         * up, nothing to answer at the link-establishment layer. */
    }
}

static bool sgold2_frame_byte(SGold2State *s, uint8_t byte)
{
    if (!sgold2_h5_enabled()) {
        return false;
    }
    if (!s->in_frame) {
        if (byte != 0xC0) {
            return false;
        }
        s->in_frame = true;
        s->frame_len = 0;
        return true;
    }
    if (byte == 0xC0) {
        if (s->frame_len > 0) {
            sgold2_trace("->", s->frame, s->frame_len);
            sgold2_h5_frame(s);
            s->frame_len = 0;
            /* stay in_frame: back-to-back frames share delimiters */
        }
        return true;
    }
    if (s->frame_len < sizeof(s->frame)) {
        s->frame[s->frame_len++] = byte;
    }
    return true;
}

static int sgold2_chr_write(Chardev *chr, const uint8_t *buf, int len)
{
    SGold2State *s = SGOLD2_CHARDEV(chr);

    sgold2_trace("->", buf, len);
    for (int i = 0; i < len; i++) {
        uint8_t byte = buf[i];
        if (sgold2_frame_byte(s, byte)) {
            continue;
        }
        if (byte == '\r' || byte == '\n') {
            if (s->line_len > 0) {
                s->line[s->line_len] = '\0';
                sgold2_process_line(s);
                s->line_len = 0;
            }
        }
        else if (s->line_len < sizeof(s->line) - 1) {
            s->line[s->line_len] = byte;
            s->line_len++;
        }
    }

    return len;
}

static void sgold2_chr_accept_input(Chardev *chr)
{
    sgold2_flush(chr);
}

static void char_sgold2_class_init(ObjectClass *oc, const void *data)
{
    ChardevClass *cc = CHARDEV_CLASS(oc);

    cc->chr_write = sgold2_chr_write;
    cc->chr_accept_input = sgold2_chr_accept_input;
}

static const TypeInfo char_sgold2_type_info = {
    .name = TYPE_CHARDEV_SGOLD2,
    .parent = TYPE_CHARDEV,
    .instance_size = sizeof(SGold2State),
    .class_init = char_sgold2_class_init,
};

static void sgold2_register_types(void)
{
    type_register_static(&char_sgold2_type_info);
}

type_init(sgold2_register_types)
