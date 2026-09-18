#pragma once
#include "durable_queue.h"
#include <string.h>

/* One NVS blob is the atomic unit: cursor, generation and chain must never
 * originate from separately committed keys. Zero padding before checksumming. */
#define RUNTIME_CHECKPOINT_VERSION 1U
typedef struct {
    uint32_t version, generation;
    int64_t truth_epoch, history_sweep, lease_expiry;
    uint32_t zkt_ip, history_failures, source_cursor, source_generation;
    int32_t attendance_count, history_year, history_month, oldest_year, oldest_month, restart_day;
    uint16_t lease_uid;
    uint8_t history_schema, history_pending, history_failed, lease_active, source_certified;
    char truth_version[32], source_chain[65];
    uint32_t crc;
} runtime_checkpoint_t;

static inline bool runtime_checkpoint_valid(const runtime_checkpoint_t *state)
{
    if (!state || state->version != RUNTIME_CHECKPOINT_VERSION || !state->generation ||
        state->history_pending > 1 || state->history_failed > 1 ||
        state->lease_active > 1 || state->source_certified > 1 ||
        !memchr(state->truth_version, 0, sizeof(state->truth_version)) ||
        state->source_chain[64] != 0 ||
        state->crc != dq_crc32(state, offsetof(runtime_checkpoint_t, crc))) return false;
    for (unsigned i = 0; i < 64; i++) {
        char c = state->source_chain[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}
