#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <sys/socket.h>

/*
 * Accept only IPv4 traffic whose TCP connection terminates on the dedicated
 * setup address. The ESP-IDF HTTP server uses an IPv6 dual-stack listener when
 * IPv6 is enabled, so IPv4 clients can be reported as IPv4-mapped IPv6 peers.
 */
bool setup_portal_socket_guard_allows(
    const struct sockaddr *local,
    socklen_t local_length,
    const struct sockaddr *peer,
    socklen_t peer_length,
    uint32_t *peer_ip_network_order);
