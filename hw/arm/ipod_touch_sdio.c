#include "hw/arm/ipod_touch_sdio.h"
#include "hw/core/irq.h"
#include "qemu/cutils.h"
#include "qemu/sockets.h"
#include "system/dma.h"

/* Opt-in stage-0 protocol tracing (SLEEP_WAKE_INVESTIGATION.md, Wi-Fi plan).
 * Enabled with IPOD_SDIO_TRACE=1; rate-limited so an access storm cannot
 * flood the host log. Not compiled out: the check is one cached branch. */
#define SDIO_TRACE_LINE_LIMIT 60000

#define ETH_HEADER_LEN 14
#define ETHERTYPE_IPV4 0x0800
#define IP_PROTOCOL_TCP 6
#define IP_PROTOCOL_UDP 17
#define HTTPS_PORT 443
#define DNS_PORT 53

static uint16_t net_be16(const uint8_t *p)
{
    return ((uint16_t)p[0] << 8) | p[1];
}

static void net_put_be16(uint8_t *p, uint16_t value)
{
    p[0] = value >> 8;
    p[1] = value & 0xff;
}

static uint32_t net_checksum_add(uint32_t sum, const uint8_t *data,
                                 size_t length)
{
    while (length >= 2) {
        sum += net_be16(data);
        data += 2;
        length -= 2;
    }
    if (length) {
        sum += (uint16_t)data[0] << 8;
    }
    return sum;
}

static uint16_t net_checksum_finish(uint32_t sum)
{
    uint16_t result;

    while (sum >> 16) {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    result = ~sum;
    return result ? result : 0xffff;
}

static void ipod_https_fix_checksums(uint8_t *ip, size_t ip_length,
                                     size_t header_length)
{
    uint8_t *tcp = ip + header_length;
    size_t tcp_length = ip_length - header_length;
    uint32_t sum = 0;

    net_put_be16(ip + 10, 0);
    net_put_be16(ip + 10,
                 net_checksum_finish(net_checksum_add(0, ip, header_length)));

    net_put_be16(tcp + 16, 0);
    sum = net_checksum_add(sum, ip + 12, 8); /* source + destination */
    sum += IP_PROTOCOL_TCP;
    sum += tcp_length;
    sum = net_checksum_add(sum, tcp, tcp_length);
    net_put_be16(tcp + 16, net_checksum_finish(sum));
}

static uint16_t ipod_https_proxy_port(void)
{
    static int initialized;
    static uint16_t port;

    if (!initialized) {
        const char *setting = g_getenv("IPOD_HTTPS_PROXY_PORT");
        char *end = NULL;
        uint64_t value = setting ? g_ascii_strtoull(setting, &end, 10) : 0;

        if (setting && setting[0] && end && !end[0] && value > 0 &&
            value <= UINT16_MAX - IPOD_HTTPS_HOST_COUNT) {
            port = value;
            fprintf(stderr,
                    "[ipod-https] transparent guest TCP :443 interception "
                    "enabled on local port %u\n", port);
        } else if (setting && setting[0]) {
            fprintf(stderr, "[ipod-https] invalid proxy port %s; disabled\n",
                    setting);
        }
        initialized = 1;
    }
    return port;
}

static uint16_t ipod_https_control_port(void)
{
    static int initialized;
    static uint16_t port;

    if (!initialized) {
        const char *setting = g_getenv("IPOD_HTTPS_PROXY_CONTROL_PORT");
        char *end = NULL;
        uint64_t value = setting ? g_ascii_strtoull(setting, &end, 10) : 0;

        if (setting && setting[0] && end && !end[0] && value > 0 &&
            value <= UINT16_MAX) {
            port = value;
        } else if (!setting && ipod_https_proxy_port() > 1) {
            port = ipod_https_proxy_port() - 1;
        } else if (setting && setting[0]) {
            fprintf(stderr, "[ipod-https] invalid control port %s\n", setting);
        }
        initialized = 1;
    }
    return port;
}

static bool ipod_https_ipv4_udp(const uint8_t *frame, size_t frame_size,
                                const uint8_t **ip_out,
                                const uint8_t **udp_out,
                                size_t *udp_length_out)
{
    const uint8_t *ip;
    const uint8_t *udp;
    size_t ip_length;
    size_t header_length;
    size_t udp_length;

    if (frame_size < ETH_HEADER_LEN + 28 ||
        net_be16(frame + 12) != ETHERTYPE_IPV4) {
        return false;
    }
    ip = frame + ETH_HEADER_LEN;
    header_length = (ip[0] & 0x0f) * 4;
    ip_length = net_be16(ip + 2);
    if ((ip[0] >> 4) != 4 || header_length < 20 ||
        ip_length < header_length + 8 ||
        ETH_HEADER_LEN + ip_length > frame_size ||
        ip[9] != IP_PROTOCOL_UDP || (net_be16(ip + 6) & 0x3fff)) {
        return false;
    }
    udp = ip + header_length;
    udp_length = net_be16(udp + 4);
    if (udp_length < 8 || header_length + udp_length > ip_length) {
        return false;
    }
    *ip_out = ip;
    *udp_out = udp;
    *udp_length_out = udp_length;
    return true;
}

static bool ipod_https_dns_name(const uint8_t *dns, size_t dns_length,
                                size_t *offset, char *hostname,
                                size_t hostname_size)
{
    size_t cursor = *offset;
    size_t consumed = cursor;
    size_t output = 0;
    unsigned jumps = 0;
    bool jumped = false;

    while (cursor < dns_length) {
        uint8_t label_length = dns[cursor++];

        if ((label_length & 0xc0) == 0xc0) {
            size_t pointer;

            if (cursor >= dns_length || ++jumps > 16) {
                return false;
            }
            pointer = ((label_length & 0x3f) << 8) | dns[cursor++];
            if (!jumped) {
                consumed = cursor;
            }
            if (pointer >= dns_length) {
                return false;
            }
            cursor = pointer;
            jumped = true;
            continue;
        }
        if (label_length & 0xc0) {
            return false;
        }
        if (!label_length) {
            if (!jumped) {
                consumed = cursor;
            }
            break;
        }
        if (label_length > 63 || cursor + label_length > dns_length ||
            output + label_length + (output ? 1 : 0) >= hostname_size) {
            return false;
        }
        if (output) {
            hostname[output++] = '.';
        }
        for (unsigned i = 0; i < label_length; i++) {
            unsigned char character = dns[cursor++];

            if (!(g_ascii_isalnum(character) || character == '-' ||
                  character == '_')) {
                return false;
            }
            hostname[output++] = g_ascii_tolower(character);
        }
        if (!jumped) {
            consumed = cursor;
        }
    }
    if (!output || cursor > dns_length) {
        return false;
    }
    hostname[output] = '\0';
    *offset = consumed;
    return true;
}

static IPodHTTPSHost *ipod_https_remember_host(IPodTouchSDIOState *s,
                                               const uint8_t *address,
                                               const char *hostname)
{
    IPodHTTPSHost *oldest = &s->https_hosts[0];

    for (unsigned i = 0; i < IPOD_HTTPS_HOST_COUNT; i++) {
        IPodHTTPSHost *host = &s->https_hosts[i];

        if (host->valid && !memcmp(host->original_ip, address, 4)) {
            pstrcpy(host->hostname, sizeof(host->hostname), hostname);
            host->last_used = ++s->https_host_clock;
            return host;
        }
        if (!host->valid) {
            oldest = host;
            break;
        }
        if (host->last_used < oldest->last_used) {
            oldest = host;
        }
    }
    memset(oldest, 0, sizeof(*oldest));
    oldest->valid = true;
    memcpy(oldest->original_ip, address, 4);
    pstrcpy(oldest->hostname, sizeof(oldest->hostname), hostname);
    oldest->last_used = ++s->https_host_clock;
    return oldest;
}

static void ipod_https_remember_alias(IPodTouchSDIOState *s,
                                       const char *target, const char *alias)
{
    IPodHTTPSAlias *oldest = &s->https_aliases[0];

    if (!strcmp(target, alias)) {
        return;
    }
    for (unsigned i = 0; i < IPOD_HTTPS_ALIAS_COUNT; i++) {
        IPodHTTPSAlias *entry = &s->https_aliases[i];

        if (entry->valid && !strcmp(entry->target, target)) {
            pstrcpy(entry->alias, sizeof(entry->alias), alias);
            entry->last_used = ++s->https_alias_clock;
            return;
        }
        if (!entry->valid) {
            oldest = entry;
            break;
        }
        if (entry->last_used < oldest->last_used) {
            oldest = entry;
        }
    }
    memset(oldest, 0, sizeof(*oldest));
    oldest->valid = true;
    pstrcpy(oldest->target, sizeof(oldest->target), target);
    pstrcpy(oldest->alias, sizeof(oldest->alias), alias);
    oldest->last_used = ++s->https_alias_clock;
}

static const char *ipod_https_preferred_hostname(IPodTouchSDIOState *s,
                                                  const char *hostname)
{
    const char *current = hostname;

    for (unsigned depth = 0; depth < 16; depth++) {
        const char *next = NULL;

        for (unsigned i = 0; i < IPOD_HTTPS_ALIAS_COUNT; i++) {
            IPodHTTPSAlias *entry = &s->https_aliases[i];

            if (entry->valid && !strcmp(entry->target, current)) {
                next = entry->alias;
                entry->last_used = ++s->https_alias_clock;
                break;
            }
        }
        if (!next || !strcmp(next, current)) {
            break;
        }
        current = next;
    }
    return current;
}

static void ipod_https_observe_dns_response(IPodTouchSDIOState *s,
                                            const uint8_t *frame,
                                            size_t frame_size)
{
    const uint8_t *ip;
    const uint8_t *udp;
    const uint8_t *dns;
    size_t udp_length;
    size_t dns_length;
    size_t offset = 12;
    char hostname[IPOD_HTTPS_HOSTNAME_MAX + 1];
    uint16_t question_count;
    uint16_t answer_count;

    if (!ipod_https_proxy_port() ||
        !ipod_https_ipv4_udp(frame, frame_size, &ip, &udp, &udp_length) ||
        net_be16(udp) != DNS_PORT) {
        return;
    }
    dns = udp + 8;
    dns_length = udp_length - 8;
    if (dns_length < 12 || !(dns[2] & 0x80)) {
        return;
    }
    question_count = net_be16(dns + 4);
    answer_count = net_be16(dns + 6);
    if (!question_count ||
        !ipod_https_dns_name(dns, dns_length, &offset, hostname,
                             sizeof(hostname)) ||
        offset + 4 > dns_length) {
        return;
    }
    offset += 4;
    for (unsigned question = 1; question < question_count; question++) {
        char owner[IPOD_HTTPS_HOSTNAME_MAX + 1];

        if (!ipod_https_dns_name(dns, dns_length, &offset, owner,
                                 sizeof(owner)) ||
            offset + 4 > dns_length) {
            return;
        }
        offset += 4;
    }
    for (unsigned answer = 0; answer < answer_count; answer++) {
        char ignored[IPOD_HTTPS_HOSTNAME_MAX + 1];
        uint16_t type;
        uint16_t class;
        uint16_t data_length;

        if (!ipod_https_dns_name(dns, dns_length, &offset, ignored,
                                 sizeof(ignored)) ||
            offset + 10 > dns_length) {
            return;
        }
        type = net_be16(dns + offset);
        class = net_be16(dns + offset + 2);
        data_length = net_be16(dns + offset + 8);
        offset += 10;
        if (offset + data_length > dns_length) {
            return;
        }
        if (type == 5 && class == 1) {
            char target[IPOD_HTTPS_HOSTNAME_MAX + 1];
            size_t cname_offset = offset;

            if (ipod_https_dns_name(dns, dns_length, &cname_offset, target,
                                    sizeof(target)) &&
                cname_offset <= offset + data_length) {
                ipod_https_remember_alias(
                    s, target, ipod_https_preferred_hostname(s, hostname));
            }
        } else if (type == 1 && class == 1 && data_length == 4) {
            const char *preferred =
                ipod_https_preferred_hostname(s, hostname);
            IPodHTTPSHost *host =
                ipod_https_remember_host(s, dns + offset, preferred);
            unsigned slot = host - s->https_hosts;

            fprintf(stderr,
                    "[ipod-https] DNS %s -> %u.%u.%u.%u (slot %u)\n",
                    preferred, dns[offset], dns[offset + 1], dns[offset + 2],
                    dns[offset + 3], slot + 1);
        }
        offset += data_length;
    }
}

static IPodHTTPSHost *ipod_https_find_host(IPodTouchSDIOState *s,
                                           const uint8_t *address)
{
    for (unsigned i = 0; i < IPOD_HTTPS_HOST_COUNT; i++) {
        IPodHTTPSHost *host = &s->https_hosts[i];

        if (host->valid && !memcmp(host->original_ip, address, 4)) {
            host->last_used = ++s->https_host_clock;
            return host;
        }
    }
    return NULL;
}

static void ipod_https_notify_proxy(uint16_t proxy_port,
                                    const char *hostname,
                                    const uint8_t *address)
{
    struct sockaddr_in destination = { 0 };
    uint16_t control_port = ipod_https_control_port();
    char message[384];
    int length;
    int fd;

    if (!control_port || !hostname) {
        return;
    }
    length = snprintf(message, sizeof(message), "%u\t%s\t%u.%u.%u.%u\n",
                      proxy_port, hostname, address[0], address[1], address[2],
                      address[3]);
    if (length <= 0 || length >= sizeof(message)) {
        return;
    }
    fd = qemu_socket(PF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        return;
    }
    destination.sin_family = AF_INET;
    destination.sin_port = htons(control_port);
    destination.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sendto(fd, message, length, 0, (struct sockaddr *)&destination,
           sizeof(destination));
    close(fd);
}

static bool ipod_https_ipv4_tcp(uint8_t *frame, size_t frame_size,
                                uint8_t **ip_out, uint8_t **tcp_out,
                                size_t *ip_length_out,
                                size_t *ip_header_length_out)
{
    uint8_t *ip;
    size_t ip_length;
    size_t header_length;

    if (frame_size < ETH_HEADER_LEN + 20 ||
        net_be16(frame + 12) != ETHERTYPE_IPV4) {
        return false;
    }
    ip = frame + ETH_HEADER_LEN;
    header_length = (ip[0] & 0x0f) * 4;
    ip_length = net_be16(ip + 2);
    if ((ip[0] >> 4) != 4 || header_length < 20 ||
        ip_length < header_length + 20 ||
        ETH_HEADER_LEN + ip_length > frame_size ||
        ip[9] != IP_PROTOCOL_TCP || (net_be16(ip + 6) & 0x3fff)) {
        return false;
    }
    *ip_out = ip;
    *tcp_out = ip + header_length;
    *ip_length_out = ip_length;
    *ip_header_length_out = header_length;
    return true;
}

static IPodHTTPSFlow *ipod_https_find_flow(IPodTouchSDIOState *s,
                                           const uint8_t *guest_ip,
                                           uint16_t guest_port,
                                           bool allocate)
{
    IPodHTTPSFlow *oldest = &s->https_flows[0];

    for (unsigned i = 0; i < IPOD_HTTPS_FLOW_COUNT; i++) {
        IPodHTTPSFlow *flow = &s->https_flows[i];

        if (flow->valid && flow->guest_port == guest_port &&
            !memcmp(flow->guest_ip, guest_ip, sizeof(flow->guest_ip))) {
            flow->last_used = ++s->https_flow_clock;
            return flow;
        }
        if (!flow->valid) {
            oldest = flow;
            break;
        }
        if (flow->last_used < oldest->last_used) {
            oldest = flow;
        }
    }
    if (!allocate) {
        return NULL;
    }
    memset(oldest, 0, sizeof(*oldest));
    oldest->valid = true;
    oldest->guest_port = guest_port;
    memcpy(oldest->guest_ip, guest_ip, sizeof(oldest->guest_ip));
    oldest->last_used = ++s->https_flow_clock;
    return oldest;
}

static uint8_t *ipod_https_redirect_outgoing(IPodTouchSDIOState *s,
                                             const uint8_t *input,
                                             size_t size)
{
    static const uint8_t proxy_ip[4] = { 10, 0, 2, 2 };
    uint16_t proxy_port = ipod_https_proxy_port();
    g_autofree uint8_t *candidate = NULL;
    uint8_t *ip, *tcp;
    size_t ip_length, header_length;
    IPodHTTPSFlow *flow;
    IPodHTTPSHost *host;

    if (!proxy_port) {
        return NULL;
    }
    candidate = g_memdup2(input, size);
    if (!ipod_https_ipv4_tcp(candidate, size, &ip, &tcp, &ip_length,
                             &header_length) ||
        net_be16(tcp + 2) != HTTPS_PORT ||
        !memcmp(ip + 16, proxy_ip, sizeof(proxy_ip))) {
        return NULL;
    }

    flow = ipod_https_find_flow(s, ip + 12, net_be16(tcp), true);
    memcpy(flow->original_ip, ip + 16, sizeof(flow->original_ip));
    host = ipod_https_find_host(s, flow->original_ip);
    if (host) {
        unsigned slot = host - s->https_hosts;

        flow->proxy_port = proxy_port + slot + 1;
    } else {
        flow->proxy_port = proxy_port;
    }
    if (tcp[13] & 0x02) { /* SYN */
        fprintf(stderr,
                "[ipod-https] redirect %u.%u.%u.%u:%u -> 127.0.0.1:%u%s%s\n",
                flow->original_ip[0], flow->original_ip[1],
                flow->original_ip[2], flow->original_ip[3], HTTPS_PORT,
                flow->proxy_port, host ? " host=" : "",
                host ? host->hostname : "");
        if (host) {
            ipod_https_notify_proxy(flow->proxy_port, host->hostname,
                                    flow->original_ip);
        }
    }
    memcpy(ip + 16, proxy_ip, sizeof(proxy_ip));
    net_put_be16(tcp + 2, flow->proxy_port);
    ipod_https_fix_checksums(ip, ip_length, header_length);
    return g_steal_pointer(&candidate);
}

static uint8_t *ipod_https_redirect_incoming(IPodTouchSDIOState *s,
                                             const uint8_t *input,
                                             size_t size)
{
    static const uint8_t proxy_ip[4] = { 10, 0, 2, 2 };
    uint16_t proxy_port = ipod_https_proxy_port();
    g_autofree uint8_t *candidate = NULL;
    uint8_t *ip, *tcp;
    size_t ip_length, header_length;
    IPodHTTPSFlow *flow;

    if (!proxy_port) {
        return NULL;
    }
    candidate = g_memdup2(input, size);
    if (!ipod_https_ipv4_tcp(candidate, size, &ip, &tcp, &ip_length,
                             &header_length) ||
        memcmp(ip + 12, proxy_ip, sizeof(proxy_ip)) ||
        net_be16(tcp) < proxy_port ||
        net_be16(tcp) > proxy_port + IPOD_HTTPS_HOST_COUNT) {
        return NULL;
    }
    flow = ipod_https_find_flow(s, ip + 16, net_be16(tcp + 2), false);
    if (!flow) {
        return NULL;
    }
    if (net_be16(tcp) != flow->proxy_port) {
        return NULL;
    }
    memcpy(ip + 12, flow->original_ip, sizeof(flow->original_ip));
    net_put_be16(tcp, HTTPS_PORT);
    ipod_https_fix_checksums(ip, ip_length, header_length);
    return g_steal_pointer(&candidate);
}

static ssize_t ipod_touch_sdio_receive(NetClientState *nc,
                                       const uint8_t *buf, size_t size)
{
    IPodTouchSDIOState *s = qemu_get_nic_opaque(nc);

    ipod_https_observe_dns_response(s, buf, size);
    g_autofree uint8_t *redirected =
        ipod_https_redirect_incoming(s, buf, size);

    mv8686_receive_frame(&s->card, redirected ? redirected : buf, size);
    return size;
}

static NetClientInfo ipod_touch_sdio_net_info = {
    .type = NET_CLIENT_DRIVER_NIC,
    .size = sizeof(NICState),
    .receive = ipod_touch_sdio_receive,
};

static void ipod_touch_sdio_send_frame(void *opaque, const uint8_t *buf,
                                       size_t size)
{
    IPodTouchSDIOState *s = opaque;
    g_autofree uint8_t *redirected =
        ipod_https_redirect_outgoing(s, buf, size);

    if (s->nic) {
        qemu_send_packet(qemu_get_queue(s->nic),
                         redirected ? redirected : buf, size);
    }
}

static bool sdio_trace_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        const char *env = getenv("IPOD_SDIO_TRACE");
        enabled = (env && env[0] && strcmp(env, "0") != 0) ? 1 : 0;
    }
    return enabled;
}

static void G_GNUC_PRINTF(1, 2) sdio_trace(const char *fmt, ...)
{
    static unsigned lines;
    va_list ap;

    if (!sdio_trace_enabled()) {
        return;
    }
    if (lines >= SDIO_TRACE_LINE_LIMIT) {
        if (lines == SDIO_TRACE_LINE_LIMIT) {
            fprintf(stderr, "[sdio] trace line limit reached; suppressing\n");
            lines++;
        }
        return;
    }
    lines++;
    fprintf(stderr, "[sdio] ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n");
}

static void sdio_update_irq(IPodTouchSDIOState *s)
{
    qemu_set_irq(s->irq, (s->irq_reg & s->irq_mask) != 0);
}

static void sdio_card_irq(void *opaque, int level)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(opaque);

    if (level) {
        s->irq_reg |= SDIO_IRQ_CARD_INT;
    }
    /* deassert only through the guest's write-one-to-clear */
    sdio_update_irq(s);
}

static void sdio_exec_cmd53(IPodTouchSDIOState *s)
{
    uint32_t arg = s->arg;
    bool write = (arg >> 31) & 1;
    uint8_t fn = (arg >> 28) & 0x7;
    bool block_mode = (arg >> 27) & 1;
    uint32_t addr = (arg >> 9) & 0x1ffff;
    uint32_t count = arg & 0x1ff;
    uint32_t len;
    g_autofree uint8_t *buf = NULL;

    if (block_mode) {
        if (count == 0) {
            count = s->numblk;
        }
        len = count * s->blklen;
    } else {
        len = count ? count : 512;
    }
    if (len == 0 || len > 0x10000) {
        sdio_trace("CMD53 with unusable length %u", len);
        return;
    }

    buf = g_malloc0(len);
    if (write) {
        dma_memory_read(&address_space_memory, s->baddr, buf, len,
                        MEMTXATTRS_UNSPECIFIED);
        mv8686_io_rw_extended(&s->card, true, fn, addr, buf, len);
    } else {
        mv8686_io_rw_extended(&s->card, false, fn, addr, buf, len);
        dma_memory_write(&address_space_memory, s->baddr, buf, len,
                         MEMTXATTRS_UNSPECIFIED);
    }

    sdio_trace("CMD53 %s fn=%u addr=0x%05x len=%u dma=0x%08x",
               write ? "write" : "read", fn, addr, len, s->baddr);

    /* the transfer completes immediately: data-done interrupt */
    s->irq_reg |= SDIO_IRQ_DATA_DONE;
    sdio_update_irq(s);
}

static void sdio_exec_cmd(IPodTouchSDIOState *s)
{
    uint8_t idx = s->cmd & 0x3f;

    if (idx == 53) {
        s->resp0 = 0x00001000; /* R5: no errors */
        sdio_exec_cmd53(s);
    } else {
        s->resp0 = mv8686_exec_cmd(&s->card, idx, s->arg);
    }
    s->resp1 = 0;
    s->resp2 = 0;
    s->resp3 = 0;
    s->dsta |= SDIO_DSTA_READY | SDIO_DSTA_CMD_COMPLETE;

    if (idx != 53) {
        sdio_trace("CMD%u arg=0x%08x -> resp 0x%08x (cmd52 fn=%u reg=0x%05x %s)",
                   idx, s->arg, s->resp0,
                   (s->arg >> 28) & 0x7, (s->arg >> 9) & 0x1ffff,
                   (s->arg & (1u << 31)) ? "write" : "read");
    }
}

static void ipod_touch_sdio_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    IPodTouchSDIOState *s = (struct IPodTouchSDIOState *) opaque;

    sdio_trace("W off=0x%03x size=%u val=0x%08" PRIx64, (uint32_t)addr, size, value);

    switch(addr) {
        case SDIO_CTRL:
            s->ctrl = value;
            break;
        case SDIO_DCTRL:
            s->dctrl = value;
            break;
        case SDIO_CMD:
            s->cmd = value;
            if(value & (1 << 31)) {
                sdio_exec_cmd(s);
            }
            break;
        case SDIO_ARGU:
            s->arg = value;
            break;
        case SDIO_STAC:
            /* the guest writes back the DSTA bits it consumed */
            s->dsta &= ~value;
            break;
        case SDIO_CLKDIV:
            s->clkdiv = value;
            break;
        case SDIO_CSR:
            s->csr = value;
            break;
        case SDIO_IRQ:
            /* write-one-to-clear */
            s->irq_reg &= ~value;
            sdio_update_irq(s);
            break;
        case SDIO_IRQMASK:
            s->irq_mask = value;
            sdio_update_irq(s);
            break;
        case SDIO_BADDR:
            s->baddr = value;
            break;
        case SDIO_BLKLEN:
            s->blklen = value;
            break;
        case SDIO_NUMBLK:
            s->numblk = value;
            break;
        default:
            if (addr / 4 < ARRAY_SIZE(s->unknown_regs)) {
                s->unknown_regs[addr / 4] = value;
            }
            break;
    }
}

static uint64_t ipod_touch_sdio_read(void *opaque, hwaddr addr, unsigned size)
{
    IPodTouchSDIOState *s = (struct IPodTouchSDIOState *) opaque;
    uint64_t ret = 0;

    switch (addr) {
        case SDIO_CTRL:
            ret = s->ctrl;
            break;
        case SDIO_DCTRL:
            ret = s->dctrl;
            break;
        case SDIO_CMD:
            ret = s->cmd;
            break;
        case SDIO_ARGU:
            ret = s->arg;
            break;
        case SDIO_DSTA:
            /* the controller is always ready for the next command */
            ret = s->dsta | SDIO_DSTA_READY;
            break;
        case SDIO_RESP0:
            ret = s->resp0;
            break;
        case SDIO_RESP1:
            ret = s->resp1;
            break;
        case SDIO_RESP2:
            ret = s->resp2;
            break;
        case SDIO_RESP3:
            ret = s->resp3;
            break;
        case SDIO_CLKDIV:
            ret = s->clkdiv;
            break;
        case SDIO_CSR:
            ret = s->csr;
            break;
        case SDIO_IRQ:
            ret = s->irq_reg;
            break;
        case SDIO_IRQMASK:
            ret = s->irq_mask;
            break;
        case SDIO_BADDR:
            ret = s->baddr;
            break;
        case SDIO_BLKLEN:
            ret = s->blklen;
            break;
        case SDIO_NUMBLK:
            ret = s->numblk;
            break;
        case SDIO_REMBLK:
            ret = 0; /* every programmed block has been transferred */
            break;
        default:
            if (addr / 4 < ARRAY_SIZE(s->unknown_regs)) {
                ret = s->unknown_regs[addr / 4];
            }
            break;
    }

    sdio_trace("R off=0x%03x size=%u -> 0x%08" PRIx64, (uint32_t)addr, size, ret);
    return ret;
}

static const MemoryRegionOps ipod_touch_sdio_ops = {
    .read = ipod_touch_sdio_read,
    .write = ipod_touch_sdio_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void ipod_touch_sdio_reset(DeviceState *dev)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    memset(&s->ctrl, 0,
           offsetof(IPodTouchSDIOState, card) -
           offsetof(IPodTouchSDIOState, ctrl));
    mv8686_reset(&s->card);
    memcpy(s->card.mac, s->conf.macaddr.a, sizeof(s->card.mac));
}

static void ipod_touch_sdio_init(Object *obj)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &ipod_touch_sdio_ops, s, TYPE_IPOD_TOUCH_SDIO, 4096);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    s->card.set_card_irq = sdio_card_irq;
    s->card.irq_opaque = s;
    s->card.send_frame = ipod_touch_sdio_send_frame;
    s->card.net_opaque = s;
    mv8686_reset(&s->card);
}

static void ipod_touch_sdio_realize(DeviceState *dev, Error **errp)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    qemu_macaddr_default_if_unset(&s->conf.macaddr);
    memcpy(s->card.mac, s->conf.macaddr.a, sizeof(s->card.mac));
    s->nic = qemu_new_nic(&ipod_touch_sdio_net_info, &s->conf,
                          object_get_typename(OBJECT(dev)), dev->id,
                          &dev->mem_reentrancy_guard, s);
    qemu_format_nic_info_str(qemu_get_queue(s->nic), s->conf.macaddr.a);
}

static void ipod_touch_sdio_unrealize(DeviceState *dev)
{
    IPodTouchSDIOState *s = IPOD_TOUCH_SDIO(dev);

    if (s->nic) {
        qemu_del_nic(s->nic);
        s->nic = NULL;
    }
    mv8686_cleanup(&s->card);
}

static const Property ipod_touch_sdio_properties[] = {
    DEFINE_NIC_PROPERTIES(IPodTouchSDIOState, conf),
};

static void ipod_touch_sdio_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = ipod_touch_sdio_realize;
    dc->unrealize = ipod_touch_sdio_unrealize;
    device_class_set_props(dc, ipod_touch_sdio_properties);
    device_class_set_legacy_reset(dc, ipod_touch_sdio_reset);
}

static const TypeInfo ipod_touch_sdio_type_info = {
    .name = TYPE_IPOD_TOUCH_SDIO,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(IPodTouchSDIOState),
    .instance_init = ipod_touch_sdio_init,
    .class_init = ipod_touch_sdio_class_init,
};

static void ipod_touch_sdio_register_types(void)
{
    type_register_static(&ipod_touch_sdio_type_info);
}

type_init(ipod_touch_sdio_register_types)
