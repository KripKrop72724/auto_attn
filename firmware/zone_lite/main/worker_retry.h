#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    uint32_t attempts[3], total;
    uint8_t count, next;
} worker_retry_t;
/* Strict sliding window: never more than three attempts in any ten minutes. */
bool worker_retry_allow(worker_retry_t *state, uint32_t now_ms);
