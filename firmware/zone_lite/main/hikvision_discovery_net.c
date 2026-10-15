#include "hikvision_discovery.h"
#include "hikvision_http.h"
#include "zone_config.h"
#include "add_connector.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

bool hikvision_discovery_step(void)
{
    static int64_t next_attempt;
    int64_t now = esp_timer_get_time();
    if (now < next_attempt) return false;
    next_attempt = now + 30000000; /* one LAN-only inquiry per 30 seconds */
    const zone_config_t *cfg = zone_config_get();
    if (!cfg->provisioned || strcmp(cfg->firmware_family, "hikvision") || !cfg->hik_expected_serial[0]) return false;
    esp_netif_ip_info_t info;
    esp_netif_t *station = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (!station || esp_netif_get_ip_info(station, &info) != ESP_OK || !info.ip.addr) return false;
    uint32_t own = ntohl(info.ip.addr), mask = ntohl(info.netmask.addr), gateway = ntohl(info.gw.addr);
    int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) return false;
    int enabled = 1; unsigned char ttl = 1;
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 200000};
    struct in_addr local = {.s_addr = info.ip.addr};
    struct ip_mreq group = {.imr_multiaddr.s_addr = inet_addr("239.255.255.250"), .imr_interface = local};
    struct sockaddr_in bind_address = {.sin_family = AF_INET, .sin_port = htons(37020), .sin_addr.s_addr = htonl(INADDR_ANY)};
    bool ready = setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) == 0 &&
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) == 0 &&
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_IF, &local, sizeof(local)) == 0 &&
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl)) == 0 &&
        bind(fd, (struct sockaddr *)&bind_address, sizeof(bind_address)) == 0 &&
        setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &group, sizeof(group)) == 0;
    char uuid[40], probe[192];
    uint32_t a = esp_random(), b = esp_random(), c = esp_random(), d = esp_random();
    snprintf(uuid, sizeof(uuid), "%08lx-%04lx-4%03lx-%04lx-%04lx%08lx",
        (unsigned long)a, (unsigned long)(b >> 16), (unsigned long)(b & 0xfff),
        (unsigned long)((c >> 16 & 0x3fff) | 0x8000), (unsigned long)(c & 0xffff), (unsigned long)d);
    int size = snprintf(probe, sizeof(probe), "<?xml version=\"1.0\" encoding=\"utf-8\"?><Probe><Uuid>%s</Uuid><Types>inquiry</Types></Probe>", uuid);
    struct sockaddr_in destination = {.sin_family = AF_INET, .sin_port = htons(37020), .sin_addr.s_addr = group.imr_multiaddr.s_addr};
    char *packet = ready ? malloc(HIK_DISCOVERY_PACKET_MAX + 2) : NULL;
    bool sent = packet && size > 0 && (size_t)size < sizeof(probe) &&
        sendto(fd, probe, (size_t)size, 0, (struct sockaddr *)&destination, sizeof(destination)) == size;
    bool found = false;
    int64_t deadline = esp_timer_get_time() + 2000000;
    for (unsigned replies = 0; sent && replies < 64 && esp_timer_get_time() < deadline; replies++) {
        struct sockaddr_in sender; socklen_t sender_size = sizeof(sender);
        int n = recvfrom(fd, packet, HIK_DISCOVERY_PACKET_MAX + 1, 0, (struct sockaddr *)&sender, &sender_size);
        if (n <= 0) continue;
        if (n > HIK_DISCOVERY_PACKET_MAX) continue;
        packet[n] = 0;
        char candidate[16]; uint32_t address;
        if (!hik_discovery_candidate(packet, (size_t)n, uuid, cfg->hik_expected_serial,
                own, mask, gateway, candidate) || !hik_discovery_ipv4(candidate, &address) ||
            address != ntohl(sender.sin_addr.s_addr) || sender.sin_port != htons(37020)) continue;
        /* End the multicast receive window before a bounded, read-only Digest
         * identity request. Never use an advertised port, password or serial. */
        close(fd); fd = -1;
        if (hik_http_verify_identity_at(candidate, 3000) == HIK_OK) {
            char previous[64]; zone_config_copy_hik_host(previous, sizeof(previous));
            if (zone_config_save_hik_host(candidate) == ESP_OK) {
                char message[192];
                snprintf(message, sizeof(message), "Authenticated saved terminal serial at %s; previous address %s; encrypted address saved", candidate, previous);
                (void)add_connector_log("INFO", "hikvision", "HIK_IP_REDISCOVERED", message);
                found = true;
            } else {
                (void)add_connector_log("WARN", "hikvision", "HIK_IP_SAVE_PENDING", "Verified terminal address could not be committed; existing address retained");
            }
        }
        break;
    }
    free(packet);
    if (fd >= 0) close(fd);
    return found;
}
