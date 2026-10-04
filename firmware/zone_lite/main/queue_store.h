#pragma once
#include "durable_queue.h"
#include "legacy_storage_health.h"

typedef enum { QS_LIVE, QS_BULK, QS_ORDS, QS_BLOCKED, QS_RECEIPTS, QS_EVIDENCE,
    QS_HIK_SOURCE, QS_COUNT } qs_lane_t;
typedef enum { QS_ADMIT_LIVE, QS_ADMIT_HISTORICAL, QS_ADMIT_RECOVERY,
    QS_ADMIT_OPTIONAL_HISTORICAL } qs_admission_t;
typedef struct {
    size_t total_bytes, used_bytes, admission_reserve_bytes;
    bool observed, available, bulk_paused, recovery_complete, persistence_verified;
    bool persistence_recheck_required; /* A recovered legacy fault needs a fresh proof. */
    uint32_t failures, write_failures, read_failures, admission_rejections;
    uint32_t persistence_probe_failures;
    uint32_t persistence_probe_total_failures;
    int persistence_probe_error;
    const char *persistence_probe_operation;
    int last_error;
    const char *last_operation;
    lf_health_t legacy;
} qs_health_t;
bool qs_init(void);
/* ZKT supervisor: bounded verification of pending segmented records. */
bool qs_recover_step(void);
/* Boot recovery must have succeeded; proves filesystem and encrypted NVS writes. */
bool qs_verify_persistence(void);
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
/* Local reads, retirement checkpoints and reclamation still need to run when
 * capacity has crossed the write ceiling. Success pairs with local_end(true,
 * 0); the caller reports read/NVS errors under its own operation category. */
bool qs_local_read_begin(void);
/* Caller already owns the shared lock. Refusal keeps it held; pair the original
 * begin with one end. Used when recovery discovers that it must preserve bytes. */
bool qs_local_admit_locked(qs_admission_t policy, size_t bytes);
/* Current health without recursively acquiring the shared admission mutex.
 * Caller must hold a successful qs_local_*_begin until qs_local_end. */
qs_health_t qs_local_health_locked(void);
void qs_local_end(bool persisted, int captured_error);
/* Owner-only legacy adapters: reports the exact lane/operation before releasing
 * the local lock. Refusals and contention are not failed persistence. */
void qs_local_end_legacy(lf_lane_t lane, lf_operation_t operation, dq_result_t result, int captured_error);
