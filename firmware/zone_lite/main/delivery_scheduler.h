#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Two live lanes, two bulk lanes, receipts and evidence. Legacy and new
 * generations share service, so migration cannot starve either generation. */
#define DS_LANES 6
typedef struct {
    uint64_t retry_at[DS_LANES];
    uint32_t backoff_ms[DS_LANES];
    unsigned live_attempts, live_next, bulk_next, proof_next, background_next;
} delivery_scheduler_t;
int ds_pick(const delivery_scheduler_t *, unsigned ready_mask, uint64_t now_ms, bool background_due);
void ds_attempted(delivery_scheduler_t *, unsigned lane);
void ds_complete(delivery_scheduler_t *, unsigned lane, uint64_t now_ms, bool successful, uint32_t jitter_ms);
