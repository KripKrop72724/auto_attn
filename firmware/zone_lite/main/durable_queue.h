#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DQ_SEGMENT_BYTES (64U * 1024U)
#define DQ_MAX_RECORD_BYTES 8192U
#define DQ_CHECKPOINT_VERSION 2U
typedef enum { DQ_OK, DQ_EMPTY, DQ_FULL, DQ_IO, DQ_CORRUPT, DQ_STALE, DQ_BUFFER_SMALL, DQ_PENDING } dq_result_t;
typedef struct {
    uint32_t version, generation, read_segment, read_offset;
    uint32_t write_segment, write_offset, next_sequence, depth, crc;
} dq_checkpoint_t;
typedef struct {
    /* load: 1 found, 0 never initialized, -1 unavailable/corrupt. */
    int (*load)(void *context, dq_checkpoint_t *checkpoint);
    bool (*commit)(void *context, const dq_checkpoint_t *checkpoint);
    bool (*admit)(void *context, size_t bytes);
    void *context;
} dq_port_t;
typedef struct {
    char prefix[128];
    dq_port_t port;
    dq_checkpoint_t checkpoint;
    bool ready;
} durable_queue_t;
typedef struct { uint32_t segment, offset, end, sequence, crc; } dq_token_t;
typedef struct {
    uint32_t generation, segment, offset, remaining, sequence;
    bool started, complete;
} dq_audit_t;

/* Validate one pending record per call without committing or retiring anything.
 * Caller owns the queue lock and releases it between DQ_PENDING calls. Changes
 * to the authoritative checkpoint restart the audit conservatively. */
dq_result_t dq_audit_step(const durable_queue_t *queue, dq_audit_t *audit,
                          void *buffer, size_t capacity);

uint32_t dq_crc32(const void *data, size_t length);
dq_result_t dq_open(durable_queue_t *queue, const char *prefix, dq_port_t port);
dq_result_t dq_append(durable_queue_t *queue, const void *data, size_t length);
dq_result_t dq_peek(durable_queue_t *queue, void *data, size_t capacity,
                    size_t *length, dq_token_t *token);
/* Caller must verify the matching durable destination receipt before settle.
 * All calls require the owning queue's mutex, never held during networking. */
dq_result_t dq_settle(durable_queue_t *queue, const dq_token_t *token);
