#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

/* ZKT's packed clock stores local wall time without a timezone field. */
bool zkt_clock_pack_pst(time_t utc_epoch, uint32_t *packed);
bool zkt_clock_pst_to_utc(const struct tm *local, time_t *utc_epoch);
