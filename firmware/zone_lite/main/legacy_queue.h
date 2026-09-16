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
    bool ready;
} legacy_queue_t;
typedef struct {
    uint32_t generation, offset, end, crc;
    bool evidence_required; /* A fragment must never be interpreted as attendance. */
} lq_token_t;

/* The owner holds the storage lock for these bounded local operations only.
 * Writers may append between peek and settle. Never rewrite the source file.
 * Opening verifies the complete consumed prefix with constant memory. */
dq_result_t lq_open(legacy_queue_t *, const char *, lq_port_t);
dq_result_t lq_peek(legacy_queue_t *, char *, size_t, lq_token_t *);
dq_result_t lq_settle(legacy_queue_t *, const lq_token_t *);
/* Call only after matching durable custody of the exact bytes. */
dq_result_t lq_settle_evidence(legacy_queue_t *, const lq_token_t *);
dq_result_t lq_reclaim(legacy_queue_t *);
