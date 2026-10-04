#pragma once
#include "queue_store.h"
#include "zkt_journal_store.h"

#define ZQ_CHUNK_BYTES 512U
#define ZQ_DEADLINE_US 10000000ULL
typedef enum { ZQ_APPEND_BEGIN, ZQ_APPEND_CHUNK, ZQ_APPEND_COMMIT,
    ZQ_PEEK_BEGIN, ZQ_PEEK_CHUNK, ZQ_SETTLE, ZQ_SNAPSHOT, ZQ_GENERATION,
    ZQ_RECOVER, ZQ_PROBE } zq_operation_t;
typedef struct {
    uint64_t deadline_us, transfer;
    dq_token_t token;
    uint32_t offset, total;
    uint16_t length;
    uint8_t operation, lane, policy;
    uint8_t bytes[ZQ_CHUNK_BYTES];
} zq_request_t;
typedef struct {
    dq_result_t result;
    uint64_t transfer;
    dq_token_t token;
    uint32_t total, depth;
    uint16_t length;
    bool verified;
    char generation[33];
    uint8_t bytes[ZQ_CHUNK_BYTES];
} zq_reply_t;
typedef struct {
    uint64_t next_transfer, append_transfer, read_transfer;
    uint64_t append_deadline, read_deadline;
    uint32_t append_used, append_total, read_length;
    uint8_t append_lane, append_policy, read_lane;
    dq_token_t read_token;
    /* Allocated with the owner in PSRAM, never on a caller/task stack. */
    uint8_t append_bytes[DQ_MAX_RECORD_BYTES], read_bytes[DQ_MAX_RECORD_BYTES];
} zq_store_t;

static inline bool zq_request_valid(const zq_request_t *r)
{
    if (!r || !r->deadline_us || r->operation > ZQ_PROBE || r->lane >= QS_COUNT ||
        r->policy > QS_ADMIT_RECOVERY || r->length > ZQ_CHUNK_BYTES)
        return false;
    switch (r->operation) {
        case ZQ_APPEND_BEGIN: return !r->transfer && !r->length && !r->offset &&
            r->total && r->total <= DQ_MAX_RECORD_BYTES;
        case ZQ_APPEND_CHUNK: return r->transfer && r->length &&
            r->offset <= DQ_MAX_RECORD_BYTES - r->length;
        case ZQ_APPEND_COMMIT: return r->transfer && !r->length;
        case ZQ_PEEK_BEGIN: return !r->transfer && !r->length;
        case ZQ_PEEK_CHUNK: return r->transfer && !r->length && r->offset < DQ_MAX_RECORD_BYTES;
        case ZQ_SETTLE: return !r->length && r->token.end > r->token.offset;
        default: return !r->length && !r->transfer;
    }
}
/* Storage task only. Each request copies at most 512 bytes or executes one
 * existing bounded queue operation. No network, borrowed buffers or handles
 * survive the call. The original durable queue/checkpoint ABI is unchanged. */
void zq_store_execute(zq_store_t *store, uint64_t now_us,
                      const zq_request_t *request, zq_reply_t *reply);

dq_result_t zq_append(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy);
dq_result_t zq_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token);
dq_result_t zq_settle(qs_lane_t lane, const dq_token_t *token);
bool zq_snapshot(qs_lane_t lane, uint32_t *depth);
bool zq_generation(char output[33]);
bool zq_recover(void);
bool zq_probe(void);
