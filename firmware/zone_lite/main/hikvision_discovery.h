#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HIK_DISCOVERY_PACKET_MAX 4096
/* IPv4 values use canonical host order (192.168.1.2 == 0xc0a80102). */
bool hik_discovery_ipv4(const char *text, uint32_t *value);
bool hik_discovery_local_address(uint32_t candidate, uint32_t own,
    uint32_t mask, uint32_t gateway);
/* xml contains length payload bytes followed by a NUL terminator. */
bool hik_discovery_candidate(const char *xml, size_t length, const char *uuid,
    const char *serial, uint32_t own, uint32_t mask, uint32_t gateway,
    char address[16]);
/* Caller holds the terminal request lock. Only read-only discovery/identity
 * requests are sent; a host is saved only after authenticated serial proof. */
bool hikvision_discovery_step(void);
