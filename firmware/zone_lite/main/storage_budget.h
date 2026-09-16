#pragma once
#include <stdbool.h>
#include <stddef.h>

typedef enum { SB_LIVE, SB_HISTORICAL, SB_RECOVERY } sb_class_t;
typedef struct {
    bool bulk_paused;
    size_t admission_ceiling;
    size_t live_reserve;
    size_t recovery_reserve;
} storage_budget_t;
/* 60/55 hysteresis, 70% reserved-only pressure, 75% absolute ceiling.
 * Reservations are part of that ceiling, not additional capacity. */
bool storage_budget_admit(storage_budget_t *budget, size_t total, size_t used,
                          size_t bytes, sb_class_t kind);
