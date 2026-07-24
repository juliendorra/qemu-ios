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

static void sgold2_process_line(SGold2State *s)
{
    const char *line = s->line;

    if (strncasecmp(line, "at", 2) != 0) {
        return; // not an AT command; a real modem would stay silent too
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

static int sgold2_chr_write(Chardev *chr, const uint8_t *buf, int len)
{
    SGold2State *s = SGOLD2_CHARDEV(chr);

    sgold2_trace("->", buf, len);
    for (int i = 0; i < len; i++) {
        uint8_t byte = buf[i];
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
