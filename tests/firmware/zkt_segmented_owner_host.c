#include "zkt_segmented_owner.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static zq_store_t store;
static durable_queue_t queues[QS_COUNT];
static dq_checkpoint_t checkpoints[QS_COUNT];
static unsigned appends, reads, settles;
static bool full, fail_checkpoint;
static int load(void *context, dq_checkpoint_t *out)
{
    unsigned lane = (unsigned)(uintptr_t)context;
    if (!checkpoints[lane].version) return 0;
    *out = checkpoints[lane]; return 1;
}
static bool commit(void *context, const dq_checkpoint_t *in)
{
    if (fail_checkpoint) { fail_checkpoint = false; return false; }
    checkpoints[(unsigned)(uintptr_t)context] = *in; return true;
}
static bool admit(void *context, size_t length) { (void)context; (void)length; return !full; }
static durable_queue_t *queue(qs_lane_t lane)
{
    assert((unsigned)lane < QS_COUNT);
    if (!queues[lane].ready) {
        char prefix[32]; snprintf(prefix, sizeof(prefix), "segmented-%u", (unsigned)lane);
        assert(dq_open(&queues[lane], prefix, (dq_port_t){load, commit, admit, (void *)(uintptr_t)lane}) == DQ_OK);
    }
    return &queues[lane];
}
dq_result_t qs_append_with_policy(qs_lane_t lane, const void *bytes, size_t length, qs_admission_t policy)
{ ++appends; assert((unsigned)policy <= QS_ADMIT_RECOVERY); return dq_append(queue(lane), bytes, length); }
dq_result_t qs_peek(qs_lane_t lane, void *bytes, size_t capacity, size_t *length, dq_token_t *token)
{ ++reads; return dq_peek(queue(lane), bytes, capacity, length, token); }
dq_result_t qs_settle(qs_lane_t lane, const dq_token_t *token) { ++settles; return dq_settle(queue(lane), token); }
bool qs_snapshot(qs_lane_t lane, uint32_t *depth) { *depth = queue(lane)->checkpoint.depth; return true; }
bool qs_generation(char out[33]) { memset(out, 'a', 32); out[32] = 0; return true; }
bool qs_recover_step(void) { return true; }
bool qs_verify_persistence(void) { return !full; }

static uint64_t now = 1, next_ticket, ticket;
static zj_request_t pending;
static zj_reply_t result;
static bool executed, hold_reply, reject, nested;
static int delay_operation = -1, tamper_operation = -1;
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned ms) { now += ms * 1000U; }
bool zj_owner_submit(const zj_request_t *in, uint64_t *out)
{
    *out = 0;
    assert(in->operation == ZJ_SEGMENTED_QUEUE && !ticket && zq_request_valid(&in->input.segmented));
    if (reject) { reject = false; return false; }
    pending = *in; executed = false;
    *out = ticket = ++next_ticket;
    if (nested) { nested = false; assert(zq_append(QS_BULK, "nested", 6, QS_ADMIT_HISTORICAL) == DQ_PENDING); }
    return true;
}
bool zj_owner_poll(uint64_t input, zj_reply_t *out, bool *complete)
{
    assert(ticket && ticket == input); *complete = false;
    if (!executed) {
        memset(&result, 0, sizeof(result));
        zq_store_execute(&store, now, &pending.input.segmented, &result.segmented);
        result.result = ZJ_OK; executed = true;
        if (delay_operation == pending.input.segmented.operation) { hold_reply = true; delay_operation = -1; }
        if (tamper_operation == pending.input.segmented.operation) {
            ++result.segmented.transfer; tamper_operation = -1;
        }
    }
    if (hold_reply) return true;
    *out = result; *complete = true; ticket = 0; return true;
}
static zq_reply_t execute(zq_request_t *request)
{ zq_reply_t out; zq_store_execute(&store, now, request, &out); return out; }
int main(void)
{
    uint8_t bytes[DQ_MAX_RECORD_BYTES], copied[DQ_MAX_RECORD_BYTES];
    for (unsigned i = 0; i < sizeof(bytes); ++i) bytes[i] = (uint8_t)i;
    size_t length = 999; dq_token_t token;
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_EMPTY && !length);
    assert(zq_append(QS_LIVE, NULL, 10, QS_ADMIT_LIVE) == DQ_IO);
    assert(zq_append(QS_LIVE, bytes, sizeof(bytes) + 1, QS_ADMIT_LIVE) == DQ_IO);
    assert(zq_append((qs_lane_t)99, bytes, 1, QS_ADMIT_LIVE) == DQ_IO);
    assert(zq_append(QS_LIVE, bytes, 1, QS_ADMIT_OPTIONAL_HISTORICAL) == DQ_IO);
    reject = true; assert(zq_append(QS_LIVE, bytes, sizeof(bytes), QS_ADMIT_LIVE) == DQ_PENDING && !appends);
    nested = true;
    assert(zq_append(QS_LIVE, bytes, sizeof(bytes), QS_ADMIT_LIVE) == DQ_OK && appends == 1);
    assert(zq_peek(QS_LIVE, copied, 10, &length, &token) == DQ_BUFFER_SMALL && !length && !token.end);
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_OK && length == sizeof(bytes));
    assert(!memcmp(bytes, copied, length));
    uint32_t depth = 999;
    assert(zq_snapshot(QS_LIVE, &depth) && depth == 1);
    /* Copy corruption cannot yield a token, nor can a failed new read reuse
     * the previous record's length. Queue bytes/checkpoint are untouched. */
    tamper_operation = ZQ_PEEK_CHUNK;
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_CORRUPT && !length && !token.end);
    assert(zq_snapshot(QS_LIVE, &depth) && depth == 1 && !settles);
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_OK);
    dq_token_t wrong = token; wrong.crc ^= 1;
    assert(zq_settle(QS_LIVE, &wrong) == DQ_STALE);
    fail_checkpoint = true;
    assert(zq_settle(QS_LIVE, &token) == DQ_IO);
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_OK && !memcmp(copied, bytes, length));
    assert(zq_settle(QS_LIVE, &token) == DQ_OK);
    assert(zq_settle(QS_LIVE, &token) == DQ_STALE);
    /* Commit succeeds before its response is lost. A new caller first drains
     * that exact retained response, then submits its own different bytes. */
    delay_operation = ZQ_APPEND_COMMIT;
    assert(zq_append(QS_LIVE, "first", 5, QS_ADMIT_LIVE) == DQ_PENDING && ticket);
    assert(queue(QS_LIVE)->checkpoint.depth == 1);
    unsigned before = appends;
    assert(zq_append(QS_LIVE, "second", 6, QS_ADMIT_LIVE) == DQ_PENDING && appends == before);
    hold_reply = false;
    assert(zq_append(QS_LIVE, "second", 6, QS_ADMIT_LIVE) == DQ_OK && appends == before + 1);
    assert(zq_snapshot(QS_LIVE, &depth) && depth == 2);
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_OK && length == 5 && !memcmp(copied, "first", 5));
    delay_operation = ZQ_SETTLE;
    assert(zq_settle(QS_LIVE, &token) == DQ_PENDING && ticket && queue(QS_LIVE)->checkpoint.depth == 1);
    hold_reply = false;
    assert(zq_settle(QS_LIVE, &token) == DQ_STALE && queue(QS_LIVE)->checkpoint.depth == 1);
    assert(zq_peek(QS_LIVE, copied, sizeof(copied), &length, &token) == DQ_OK && length == 6 && !memcmp(copied, "second", 6));
    assert(zq_settle(QS_LIVE, &token) == DQ_OK);
    full = true;
    assert(zq_append(QS_LIVE, bytes, sizeof(bytes), QS_ADMIT_LIVE) == DQ_FULL);
    assert(zq_snapshot(QS_LIVE, &depth) && !depth);
    assert(zq_recover() && !zq_probe()); full = false;
    char generation[33]; assert(zq_generation(generation) && strlen(generation) == 32 && zq_probe());
    /* Direct owner requests: RAM admission never commits; old chunks cannot
     * write into a later transfer, change its lane, reset its deadline or
     * turn a partial producer into a complete append. */
    zq_request_t request = {.operation = ZQ_APPEND_BEGIN, .deadline_us = now + 100,
        .lane = QS_BULK, .policy = QS_ADMIT_HISTORICAL, .total = 5};
    zq_reply_t reply = execute(&request); assert(reply.result == DQ_OK);
    uint64_t first = reply.transfer;
    reply = execute(&request); assert(reply.result == DQ_OK && reply.transfer != first);
    uint64_t second = reply.transfer;
    request.operation = ZQ_APPEND_CHUNK; request.transfer = first; request.length = 5;
    memcpy(request.bytes, "third", 5); before = appends;
    assert(execute(&request).result == DQ_STALE && appends == before);
    request.transfer = second; request.lane = QS_LIVE; assert(execute(&request).result == DQ_STALE);
    request.lane = QS_BULK; ++request.deadline_us; assert(execute(&request).result == DQ_STALE);
    --request.deadline_us; request.operation = ZQ_APPEND_COMMIT; request.length = 0;
    assert(execute(&request).result == DQ_STALE && appends == before);
    request.operation = ZQ_APPEND_CHUNK; request.length = 5;
    assert(execute(&request).result == DQ_OK);
    request.operation = ZQ_APPEND_COMMIT; request.length = 0;
    now = request.deadline_us;
    assert(execute(&request).result == DQ_PENDING && appends == before);
    request.deadline_us = now + 100;
    assert(execute(&request).result == DQ_STALE && appends == before);
    assert(!store.append_transfer && !memcmp(store.append_bytes, (uint8_t[DQ_MAX_RECORD_BYTES]){0}, DQ_MAX_RECORD_BYTES));
    request = (zq_request_t){.operation = ZQ_APPEND_BEGIN, .deadline_us = now + 100, .total = 1};
    store.next_transfer = UINT64_MAX;
    assert(execute(&request).result == DQ_FULL && !store.append_transfer);
    puts("segmented owner copies, receipts, lost results, deadlines and capacity passed");
}
