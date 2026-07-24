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
 * IT_BASEBAND_FRAME_ECHO=1: after at+xtransportmode CommCenter abandons AT
 * lines for 0xC0-delimited AppleReliableSerialLayer frames (link probe
 * C0 00 2F 00 D0 01 7E C0 retried at ~13 Hz -- see
 * IPHONE_2G_BRINGUP_HANDOFF.md run 10). The protocol is undocumented; as a
 * first probe this echoes every complete frame straight back and logs it,
 * so a change in the guest's retry pattern tells us the frame reached the
 * right layer. Frame bytes bypass the AT line accumulator.
 */
static bool sgold2_frame_echo_enabled(void)
{
    static int cached = -1;

    if (cached < 0) {
        const char *env = getenv("IT_BASEBAND_FRAME_ECHO");
        cached = env && *env && strcmp(env, "0") != 0;
    }
    return cached;
}

static bool sgold2_frame_byte(SGold2State *s, uint8_t byte)
{
    if (!sgold2_frame_echo_enabled()) {
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
            uint8_t echo[2 + sizeof(s->frame)];
            echo[0] = 0xC0;
            memcpy(echo + 1, s->frame, s->frame_len);
            echo[1 + s->frame_len] = 0xC0;
            sgold2_trace("<E", echo, 2 + s->frame_len);
            if (s->outlen + 2 + s->frame_len <= sizeof(s->outbuf)) {
                memcpy(s->outbuf + s->outlen, echo, 2 + s->frame_len);
                s->outlen += 2 + s->frame_len;
                sgold2_flush(CHARDEV(s));
            }
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
