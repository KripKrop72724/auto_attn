#pragma once
#include "durable_queue.h"

typedef struct {
    uint32_t version, generation, offset, prefix_crc, crc;
} lq_checkpoint_t;
typedef struct {
    int (*load)(void *, lq_checkpoint_t *);
    bool (*commit)(void *, const lq_checkpoint_t *);
    void *context;
} lq_port_t;
typedef struct {
    char path[128];
    lq_checkpoint_t checkpoint;
    lq_port_t port;
    bool ready, recovering;
    uint32_t recovery_offset, recovery_crc;
} legacy_queue_t;
typedef struct {
    uint32_t generation, offset, end, crc;
    bool evidence_required; /* A fragment must never be interpreted as attendance. */
} lq_token_t;

/* The owner holds the storage lock for these bounded local operations only.
 * Writers may append between peek and settle. Never rewrite the source file.
 * Opening verifies the complete consumed prefix with constant memory. */
dq_result_t lq_open(legacy_queue_t *, const char *, lq_port_t);
/* Queue must be zero-initialized before first use. Each call verifies at most
 * 8 KiB and closes its file before returning DQ_PENDING. Release the owner lock
 * and yield between pending calls. No source checkpoint is changed. */
#define LQ_RECOVERY_SLICE_BYTES 8192U
dq_result_t lq_open_step(legacy_queue_t *, const char *, lq_port_t);
dq_result_t lq_peek(legacy_queue_t *, char *, size_t, lq_token_t *);
dq_result_t lq_settle(legacy_queue_t *, const lq_token_t *);
/* Call only after matching durable custody of the exact bytes. */
dq_result_t lq_settle_evidence(legacy_queue_t *, const lq_token_t *);
dq_result_t lq_reclaim(legacy_queue_t *);
