#pragma once
#include "durable_queue.h"

typedef enum { QS_LIVE, QS_BULK, QS_ORDS, QS_BLOCKED, QS_RECEIPTS, QS_EVIDENCE,
    QS_HIK_SOURCE, QS_COUNT } qs_lane_t;
typedef enum { QS_ADMIT_LIVE, QS_ADMIT_HISTORICAL, QS_ADMIT_RECOVERY } qs_admission_t;
typedef struct {
    size_t total_bytes, used_bytes, admission_reserve_bytes;
    bool observed, available, bulk_paused, recovery_complete, persistence_verified;
    uint32_t failures, write_failures, read_failures, admission_rejections;
    int last_error;
    const char *last_operation;
} qs_health_t;
bool qs_init(void);
bool qs_generation(char output[33]);
dq_result_t qs_append(qs_lane_t lane, const void *data, size_t length);
dq_result_t qs_append_with_policy(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy);
dq_result_t qs_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token);
dq_result_t qs_settle(qs_lane_t lane, const dq_token_t *token);
bool qs_snapshot(qs_lane_t lane, uint32_t *depth);
qs_health_t qs_health(void);

/* Holds the shared filesystem admission lock across bounded local writes only.
 * Every successful begin must have exactly one end; never wait on a network
 * while admitted. All producers, including legacy writers, share this budget. */
bool qs_local_begin(qs_admission_t policy, size_t bytes);
void qs_local_end(bool persisted, int captured_error);
