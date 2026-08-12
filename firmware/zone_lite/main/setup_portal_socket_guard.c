#include "setup_portal_socket_guard.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <stddef.h>
#include <string.h>

#define PORTAL_IP_HOST_ORDER   UINT32_C(0xC0A8FE01)
#define PORTAL_NET_HOST_ORDER  UINT32_C(0xC0A8FE00)
#define PORTAL_MASK_HOST_ORDER UINT32_C(0xFFFFFF00)

static bool sockaddr_ipv4_network_order(
    const struct sockaddr *address,
    socklen_t length,
    uint32_t *ip_network_order)
{
    if (!address || !ip_network_order) return false;

    if (address->sa_family == AF_INET) {
        if (length < sizeof(struct sockaddr_in)) return false;
        const struct sockaddr_in *ipv4 = (const struct sockaddr_in *)address;
        *ip_network_order = ipv4->sin_addr.s_addr;
        return true;
    }

    if (address->sa_family == AF_INET6) {
        if (length < sizeof(struct sockaddr_in6)) return false;
        const struct sockaddr_in6 *ipv6 = (const struct sockaddr_in6 *)address;
        static const uint8_t mapped_prefix[12] = {
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff,
        };
        if (memcmp(ipv6->sin6_addr.s6_addr, mapped_prefix, sizeof(mapped_prefix)) != 0) {
            return false;
        }
        memcpy(ip_network_order, &ipv6->sin6_addr.s6_addr[12], sizeof(*ip_network_order));
        return true;
    }

    return false;
}

bool setup_portal_socket_guard_allows(
    const struct sockaddr *local,
    socklen_t local_length,
    const struct sockaddr *peer,
    socklen_t peer_length,
    uint32_t *peer_ip_network_order)
{
    if (peer_ip_network_order) *peer_ip_network_order = 0;
    uint32_t local_ip = 0;
    uint32_t peer_ip = 0;
    if (!sockaddr_ipv4_network_order(local, local_length, &local_ip) ||
        !sockaddr_ipv4_network_order(peer, peer_length, &peer_ip)) {
        return false;
    }

    uint32_t local_host = ntohl(local_ip);
    uint32_t peer_host = ntohl(peer_ip);
    if (local_host != PORTAL_IP_HOST_ORDER ||
        (peer_host & PORTAL_MASK_HOST_ORDER) != PORTAL_NET_HOST_ORDER) {
        return false;
    }

    if (peer_ip_network_order) *peer_ip_network_order = peer_ip;
    return true;
}
