#pragma once
#include "bld1_repair.h"
#include <time.h>

/* Operator-requested temporary exception, NOT model qualification. */
static inline bool bld1_write_exception(const uint8_t mac[6], const char *family,
    const char *zone, const char *device, const char *serial, const char *profile,
    time_t now)
{
    return now >= 1791990000 && now < 1792080000 &&
        bld1_repair_matches(mac, family, zone, device, serial) &&
        !strcmp(profile, "ds-k1t341amf-v3.2.30-20210406-unverified-v1");
}
