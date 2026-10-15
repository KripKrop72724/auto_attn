#include "hikvision_discovery.h"
#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

bool hik_discovery_ipv4(const char *text, uint32_t *value)
{
    if (!text || !value || !*text) return false;
    uint32_t result = 0;
    const char *p = text;
    for (unsigned part = 0; part < 4; part++) {
        if (*p < '0' || *p > '9') return false;
        const char *begin = p;
        unsigned octet = 0, digits = 0;
        while (*p >= '0' && *p <= '9') {
            if (++digits > 3) return false;
            octet = octet * 10 + (unsigned)(*p++ - '0');
        }
        if (octet > 255 || (digits > 1 && *begin == '0')) return false;
        result = (result << 8) | octet;
        if (part < 3) { if (*p++ != '.') return false; }
        else if (*p) return false;
    }
    *value = result;
    return true;
}
static bool private_address(uint32_t ip)
{
    return (ip >> 24) == 10 || (ip >> 20) == 0xac1 || (ip >> 16) == 0xc0a8;
}
bool hik_discovery_local_address(uint32_t candidate, uint32_t own,
    uint32_t mask, uint32_t gateway)
{
    uint32_t hosts = ~mask;
    return private_address(own) && private_address(candidate) && mask &&
        hosts >= 3 && !(hosts & (hosts + 1)) &&
        (candidate & mask) == (own & mask) && candidate != own &&
        candidate != gateway && (candidate & hosts) && (candidate & hosts) != hosts;
}
static bool text_field(const char *xml, const char *tag, char *out, size_t capacity)
{
    char open[40], close[40];
    int a = snprintf(open, sizeof(open), "<%s>", tag);
    int b = snprintf(close, sizeof(close), "</%s>", tag);
    if (a <= 0 || b <= 0 || (size_t)a >= sizeof(open) || (size_t)b >= sizeof(close)) return false;
    const char *begin = strstr(xml, open);
    if (!begin || strstr(begin + a, open)) return false;
    begin += a;
    const char *end = strstr(begin, close);
    if (!end || strstr(end + b, close)) return false;
    while (begin < end && isspace((unsigned char)*begin)) begin++;
    while (end > begin && isspace((unsigned char)end[-1])) end--;
    size_t n = (size_t)(end - begin);
    if (!n || n >= capacity || memchr(begin, '<', n) || memchr(begin, '&', n)) return false;
    memcpy(out, begin, n); out[n] = 0;
    return true;
}
bool hik_discovery_candidate(const char *xml, size_t length, const char *uuid,
    const char *serial, uint32_t own, uint32_t mask, uint32_t gateway,
    char address[16])
{
    if (!xml || !uuid || !*uuid || !serial || !*serial || !address ||
        !length || length > HIK_DISCOVERY_PACKET_MAX || memchr(xml, 0, length) ||
        xml[length] != 0 || strstr(xml, "<!")) return false;
    const char *root = xml;
    while (isspace((unsigned char)*root)) root++;
    if (!strncmp(root, "<?xml ", 6)) {
        root = strstr(root, "?>"); if (!root) return false;
        root += 2; while (isspace((unsigned char)*root)) root++;
    }
    if (strncmp(root, "<ProbeMatch", 11) ||
        (root[11] != '>' && !isspace((unsigned char)root[11]))) return false;
    const char *end = strstr(root, "</ProbeMatch>");
    if (!end || strstr(root + 11, "<ProbeMatch")) return false;
    end += 13; while (isspace((unsigned char)*end)) end++;
    if (*end) return false;
    char reply_uuid[48], advertised_serial[160], candidate[16];
    if (!text_field(root, "Uuid", reply_uuid, sizeof(reply_uuid)) || strcasecmp(reply_uuid, uuid) ||
        !text_field(root, "DeviceSN", advertised_serial, sizeof(advertised_serial)) ||
        !text_field(root, "IPv4Address", candidate, sizeof(candidate))) return false;
    size_t expected = strlen(serial), advertised = strlen(advertised_serial);
    /* SADP can prefix the ISAPI serial with model/build information. This is
     * only a hint: the authenticated ISAPI serial must still match exactly. */
    if (strcmp(advertised_serial, serial) && (expected < 8 || advertised < expected ||
        strcmp(advertised_serial + advertised - expected, serial))) return false;
    uint32_t ip;
    if (!hik_discovery_ipv4(candidate, &ip) || !hik_discovery_local_address(ip, own, mask, gateway)) return false;
    memcpy(address, candidate, strlen(candidate) + 1);
    return true;
}
