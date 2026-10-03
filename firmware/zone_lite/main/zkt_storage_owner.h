#pragma once
#include "zkt_storage_mailbox.h"

typedef struct {
    bool started, ready, operation_running, recovering, checkpoint_recovery_pending;
    bool compatibility_checked, writer_allowed;
    zj_compat_result_t compatibility;
    zj_operation_t operation;
    zj_result_t last_result;
    uint64_t sampled_uptime_us, operation_started_us, progress_uptime_us;
    uint64_t completed, refused, failures, max_operation_us;
    unsigned occupied, high_watermark;
    int filesystem_error, nvs_error;
    const char *failed_operation;
} zj_owner_health_t;

/* Call once, after encrypted NVS, SPIFFS/legacy budget coordination and the
 * Wi-Fi entropy source are initialized. Only a qualified reader/writer gate
 * may enable this task. Starting it is NOT a compatibility certificate.
 * This module exclusively owns journal files and its NVS namespace; legacy
 * and catalog migration must separately transfer their operations here. */
bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata);
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket);
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *complete);
bool zj_owner_abandon(uint64_t ticket);
bool zj_owner_health(zj_owner_health_t *health);
