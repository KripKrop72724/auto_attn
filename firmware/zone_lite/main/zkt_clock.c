#include "zkt_clock.h"

#define PAKISTAN_UTC_OFFSET_SECONDS (5 * 60 * 60)

static bool valid_local_time(const struct tm *local)
{
    if (!local || local->tm_year < 100 || local->tm_year > 199 ||
        local->tm_mon < 0 || local->tm_mon > 11 ||
        local->tm_mday < 1 || local->tm_mday > 31 ||
        local->tm_hour < 0 || local->tm_hour > 23 ||
        local->tm_min < 0 || local->tm_min > 59 ||
        local->tm_sec < 0 || local->tm_sec > 59) return false;
    struct tm copy = *local;
    copy.tm_isdst = 0;
    time_t roundtrip = mktime(&copy); /* Firmware sets TZ=UTC0 at boot. */
    return roundtrip != (time_t)-1 &&
        copy.tm_year == local->tm_year && copy.tm_mon == local->tm_mon &&
        copy.tm_mday == local->tm_mday && copy.tm_hour == local->tm_hour &&
        copy.tm_min == local->tm_min && copy.tm_sec == local->tm_sec;
}

bool zkt_clock_pack_pst(time_t utc_epoch, uint32_t *packed)
{
    if (!packed) return false;
    int64_t shifted = (int64_t)utc_epoch + PAKISTAN_UTC_OFFSET_SECONDS;
    if ((int64_t)(time_t)shifted != shifted) return false;
    time_t local_epoch = (time_t)shifted;
    struct tm local;
    if (!gmtime_r(&local_epoch, &local) || !valid_local_time(&local)) return false;
    uint32_t value = (uint32_t)(local.tm_year - 100);
    value = value * 12 + (uint32_t)local.tm_mon;
    value = value * 31 + (uint32_t)(local.tm_mday - 1);
    value = value * 24 + (uint32_t)local.tm_hour;
    value = value * 60 + (uint32_t)local.tm_min;
    value = value * 60 + (uint32_t)local.tm_sec;
    *packed = value;
    return true;
}

bool zkt_clock_pst_to_utc(const struct tm *local, time_t *utc_epoch)
{
    if (!utc_epoch || !valid_local_time(local)) return false;
    struct tm copy = *local;
    copy.tm_isdst = 0;
    time_t local_epoch = mktime(&copy);
    int64_t shifted = (int64_t)local_epoch - PAKISTAN_UTC_OFFSET_SECONDS;
    if ((int64_t)(time_t)shifted != shifted) return false;
    *utc_epoch = (time_t)shifted;
    return true;
}
