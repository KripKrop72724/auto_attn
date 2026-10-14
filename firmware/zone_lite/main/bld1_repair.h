#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* Deliberately device-specific. Never infer a new terminal identity or secret. */
static inline bool bld1_repair_matches(const uint8_t mac[6], const char *family,
    const char *zone, const char *device, const char *serial, const char *user)
{
    static const uint8_t target[6] = {0xac, 0x27, 0x6e, 0xa3, 0x0a, 0x58};
    return !memcmp(mac, target, 6) && !strcmp(family, "hikvision") &&
        !strcmp(zone, "LF-ZONE-BLD1-01") && !strcmp(device, "1") &&
        !strcmp(serial, "G04600476") &&
        (!strcmp(user, "Admin") || !strcmp(user, "admin"));
}
