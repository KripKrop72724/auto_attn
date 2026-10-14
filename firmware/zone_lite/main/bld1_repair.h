#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* Deliberately device-specific. Never infer a new terminal identity or secret. */
static inline bool bld1_repair_matches(const uint8_t mac[6], const char *family,
    const char *zone, const char *device, const char *serial)
{
    static const uint8_t target[6] = {0xac, 0x27, 0x6e, 0xa3, 0x0a, 0x58};
    return !memcmp(mac, target, 6) && !strcmp(family, "hikvision") &&
        !strcmp(zone, "LF-ZONE-BLD1-01") && !strcmp(device, "1") &&
        !strcmp(serial, "G04600476");
}

/* The onsite r2 diagnostic confirmed identity; only the stored username differed.
 * Repair that field to the verified terminal account, regardless of its old value.
 * Call only AFTER all physical/provisioning identity guards have passed. */
static inline bool bld1_repair_user_needs_update(const char *user)
{
    return strcmp(user, "admin") != 0;
}
