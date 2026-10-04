#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

/* The current decoder contract covers complete Gregorian dates, 2000-2099.
 * Pure field validation is shared with framing; it needs no wall clock or TZ. */
static inline bool zkt_clock_fields_valid(unsigned year, unsigned month, unsigned day,
    unsigned hour, unsigned minute, unsigned second)
{
    static const unsigned char days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (year < 2000 || year > 2099 || month < 1 || month > 12 ||
        hour > 23 || minute > 59 || second > 59) return false;
    unsigned maximum = days[month - 1];
    if (month == 2 && year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ++maximum;
    return day >= 1 && day <= maximum;
}

/* ZKT's packed clock stores local wall time without a timezone field. */
bool zkt_clock_pack_pst(time_t utc_epoch, uint32_t *packed);
bool zkt_clock_pst_to_utc(const struct tm *local, time_t *utc_epoch);
