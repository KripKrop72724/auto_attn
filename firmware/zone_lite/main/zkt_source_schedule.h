#pragma once
#include <stdbool.h>
#include <stdint.h>

/* ADD-owned attendance cannot inherit the legacy fifteen-minute audit timer.
 * One bounded source range is attempted per due session turn. No catch-up loop
 * or task is created; live packets and administrative expiry run first. */
typedef struct {
    uint64_t last_completed_ms, next_attempt_ms;
    uint32_t retry_ms;
    bool completed, exhausted;
} zts_schedule_t;

static inline bool zts_due(const zts_schedule_t *s, uint64_t now, bool writer_ready, bool connected)
{
    return writer_ready && connected && !s->exhausted &&
        (!s->completed || (now >= s->last_completed_ms && now >= s->next_attempt_ms));
}

static inline void zts_completed(zts_schedule_t *s, uint64_t now, bool success, uint32_t random)
{
    if (s->completed && now < s->last_completed_ms) s->exhausted = true;
    s->retry_ms = success ? 0 : !s->retry_ms ? 2000 : s->retry_ms >= 29500 ? 59000 : s->retry_ms * 2;
    uint32_t delay = success ? 2000 + random % 251U : s->retry_ms + random % 1001U;
    s->exhausted |= UINT64_MAX - now < delay;
    s->last_completed_ms = now;
    s->next_attempt_ms = s->exhausted ? UINT64_MAX : now + delay;
    s->completed = true;
}
