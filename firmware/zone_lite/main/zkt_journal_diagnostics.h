#pragma once
#include "cJSON.h"
#include "zkt_journal_boot.h"
#include "zkt_journal_capture.h"

typedef struct {
    bool owner_observed, transport_observed, capture_observed;
    zj_owner_health_t owner;
    zj_transport_health_t transport;
    zj_capture_health_t capture;
} zj_diagnostics_snapshot_t;

/* Called with the single boot snapshot used for runtime_profile. Reads only
 * bounded worker snapshots; never opens files or waits for network traffic.
 * On false the caller must discard the entire diagnostics object. */
bool zj_diagnostics_append(cJSON *diagnostics, const zj_boot_t *boot,
                           bool recent, bool legacy, const zj_diagnostics_snapshot_t *workers,
                           uint64_t now_ms);
