#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Non-cumulative, inclusive millisecond buckets, schema version 1. The final
 * bucket has no qualifying upper bound. Freeze on exhaustion rather than wrap
 * or silently discard a slow sample while continuing to publish percentiles. */
#define ZJ_LATENCY_BUCKETS 13U
static const uint32_t zj_latency_upper_ms[ZJ_LATENCY_BUCKETS] = {
    0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000, 15000, UINT32_MAX};
typedef struct {
    uint32_t buckets[ZJ_LATENCY_BUCKETS], samples, max_ms;
    bool saturated;
} zj_capture_latency_t;

static inline void zj_capture_latency_record(zj_capture_latency_t *value, uint32_t duration_ms)
{
    if (value->samples == UINT32_MAX || value->saturated) { value->saturated = true; return; }
    unsigned bucket = 0;
    while (duration_ms > zj_latency_upper_ms[bucket]) ++bucket;
    ++value->buckets[bucket];
    ++value->samples;
    if (duration_ms > value->max_ms) value->max_ms = duration_ms;
}
