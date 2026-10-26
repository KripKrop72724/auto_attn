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
/* The NVS checkpoint format, shared with the boot storage repair: version 1
 * (cursor on a row boundary) or 2 (inside an evidence row), a nonzero
 * generation and dq_crc32 of the fields before crc. */
bool lq_checkpoint_valid(const lq_checkpoint_t *checkpoint);
void lq_checkpoint_seal(lq_checkpoint_t *checkpoint);
/* The successor that reads every retained byte again from offset 0, as
 * lq_reclaim commits before it removes a settled file. False only when the
 * generation would wrap. */
bool lq_checkpoint_cleared(const lq_checkpoint_t *previous, lq_checkpoint_t *next);
typedef struct {
    char path[128];
    lq_checkpoint_t checkpoint;
    lq_port_t port;
    bool ready, recovering, empty_cached, checkpoint_corrupt;
    uint32_t recovery_offset, recovery_crc;
    /* ZKT 2.7.8: the last DQ_IO of lq_open_step or lq_peek came from a seek
     * or read of the opened file (its errno is the read's), not from the
     * open, the close or the checkpoint store. */
    bool io_read;
} legacy_queue_t;
typedef struct {
    uint32_t generation, offset, end, crc;
    bool evidence_required; /* A fragment must never be interpreted as attendance. */
    bool checkpoint_evidence; /* Exact corrupt checkpoint, never file content. */
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
/* Opt-in recovery for an owner with durable ADD evidence transfer. Return the
 * original checkpoint as opaque bytes without advancing/clearing anything.
 * A matching custody acknowledgement permits lq_settle_evidence to reset the
 * cursor to zero. Every still-retained byte is then replayed; none is skipped. */
dq_result_t lq_checkpoint_evidence(legacy_queue_t *, void *, size_t, lq_token_t *);
dq_result_t lq_settle(legacy_queue_t *, const lq_token_t *);
/* Call only after matching durable custody of the exact bytes. */
dq_result_t lq_settle_evidence(legacy_queue_t *, const lq_token_t *);
dq_result_t lq_reclaim(legacy_queue_t *);
/* Opt-in empty-cache use by a single storage owner. The owner invalidates this
 * before EVERY append or restored generation, including failed/partial writes. */
static inline void lq_invalidate_empty(legacy_queue_t *q) { if (q) q->empty_cached=false; }
