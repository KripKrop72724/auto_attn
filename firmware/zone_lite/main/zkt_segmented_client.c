#include "zkt_segmented_owner.h"
#include "zkt_storage_owner.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdatomic.h>
#include <string.h>

typedef struct { atomic_flag busy; uint64_t ticket; } client_t;
/* One retained reply per class, plus independent read/write buffers in the
 * owner. Metadata and retirement cannot overwrite an in-progress copy. */
static client_t clients[3] = {
    {.busy = ATOMIC_FLAG_INIT}, {.busy = ATOMIC_FLAG_INIT}, {.busy = ATOMIC_FLAG_INIT}};
static bool collect(client_t *client, uint64_t deadline, zj_reply_t *reply)
{
    do {
        bool complete = false;
        if (zj_owner_poll(client->ticket, reply, &complete) && complete) {
            client->ticket = 0; return true;
        }
        if ((uint64_t)esp_timer_get_time() >= deadline) return false;
        vTaskDelay(pdMS_TO_TICKS(2));
    } while (true);
}
static bool begin(client_t *client, uint64_t *deadline)
{
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - ZQ_DEADLINE_US ||
        atomic_flag_test_and_set_explicit(&client->busy, memory_order_acquire)) return false;
    *deadline = now + ZQ_DEADLINE_US;
    zj_reply_t previous;
    if (!client->ticket || collect(client, *deadline, &previous)) return true;
    atomic_flag_clear_explicit(&client->busy, memory_order_release); return false;
}
static dq_result_t call(client_t *client, const zq_request_t *input, zq_reply_t *out)
{
    if (client->ticket || (uint64_t)esp_timer_get_time() >= input->deadline_us) return DQ_PENDING;
    zj_request_t request = {.operation = ZJ_SEGMENTED_QUEUE, .input.segmented = *input};
    if (!zj_owner_submit(&request, &client->ticket)) return DQ_PENDING;
    zj_reply_t reply;
    if (!collect(client, input->deadline_us, &reply)) return DQ_PENDING;
    if (reply.result != ZJ_OK) return DQ_IO;
    *out = reply.segmented;
    return out->result;
}
static void end(client_t *client)
{ atomic_flag_clear_explicit(&client->busy, memory_order_release); }
static dq_result_t append(zq_domain_t domain, unsigned lane, const void *data, size_t length, qs_admission_t policy)
{
    if (lane >= zq_domain_lanes(domain) || (unsigned)policy > QS_ADMIT_RECOVERY ||
        !data || !length || length > DQ_MAX_RECORD_BYTES) return DQ_IO;
    client_t *client = &clients[0]; uint64_t deadline;
    if (!begin(client, &deadline)) return DQ_PENDING;
    zq_request_t in = {.operation = ZQ_APPEND_BEGIN, .lane = (uint8_t)lane, .domain = (uint8_t)domain,
        .policy = (uint8_t)policy, .total = (uint32_t)length, .deadline_us = deadline};
    zq_reply_t out;
    dq_result_t result = call(client, &in, &out);
    if (result != DQ_OK) goto done;
    if (!out.transfer) { result = DQ_CORRUPT; goto done; }
    in.transfer = out.transfer; in.operation = ZQ_APPEND_CHUNK;
    while (in.offset < length) {
        in.length = (uint16_t)(length - in.offset > ZQ_CHUNK_BYTES ? ZQ_CHUNK_BYTES : length - in.offset);
        memcpy(in.bytes, (const uint8_t *)data + in.offset, in.length);
        result = call(client, &in, &out);
        if (result != DQ_OK) goto done;
        if (out.transfer != in.transfer || out.total != in.offset + in.length) { result = DQ_CORRUPT; goto done; }
        in.offset += in.length;
    }
    in.operation = ZQ_APPEND_COMMIT; in.length = 0;
    result = call(client, &in, &out);
done:
    end(client); return result;
}
static bool same_legacy_token(const lq_token_t *a, const lq_token_t *b)
{
    return a->generation == b->generation && a->offset == b->offset &&
        a->end == b->end && a->crc == b->crc && a->evidence_required == b->evidence_required &&
        a->checkpoint_evidence == b->checkpoint_evidence;
}
static dq_result_t peek(zq_domain_t domain, unsigned lane, void *data, size_t capacity,
                       size_t *length, dq_token_t *token, lq_token_t *legacy_token)
{
    if (length) *length = 0;
    if (token) memset(token, 0, sizeof(*token));
    if (legacy_token) memset(legacy_token, 0, sizeof(*legacy_token));
    if (lane >= zq_domain_lanes(domain) || !data || !capacity || !length ||
        (domain != ZQ_SEGMENTED ? !legacy_token : !token)) return DQ_IO;
    client_t *client = &clients[1]; uint64_t deadline;
    if (!begin(client, &deadline)) return DQ_PENDING;
    zq_request_t in = {.operation = ZQ_PEEK_BEGIN, .lane = (uint8_t)lane,
        .domain = (uint8_t)domain, .deadline_us = deadline};
    zq_reply_t out;
    dq_result_t result = call(client, &in, &out);
    if (result != DQ_OK) goto done;
    if (!out.transfer || !out.total || out.total > DQ_MAX_RECORD_BYTES) { result = DQ_CORRUPT; goto done; }
    if (out.total > capacity) { result = DQ_BUFFER_SMALL; goto done; }
    in.operation = ZQ_PEEK_CHUNK; in.transfer = out.transfer;
    uint32_t total = out.total; dq_token_t copied = out.token;
    lq_token_t legacy_copied = out.legacy_token;
    while (in.offset < total) {
        result = call(client, &in, &out);
        if (result != DQ_OK) goto done;
        size_t expected = total - in.offset > ZQ_CHUNK_BYTES ? ZQ_CHUNK_BYTES : total - in.offset;
        bool token_matches = domain != ZQ_SEGMENTED ? same_legacy_token(&out.legacy_token, &legacy_copied) :
            !memcmp(&out.token, &copied, sizeof(copied));
        if (out.transfer != in.transfer || out.total != total || out.length != expected || !token_matches) {
            result = DQ_CORRUPT; goto done;
        }
        memcpy((uint8_t *)data + in.offset, out.bytes, out.length);
        in.offset += out.length;
    }
    *length = total;
    if (domain != ZQ_SEGMENTED) *legacy_token = legacy_copied;
    else *token = copied;
done:
    if (result != DQ_OK && in.offset) memset(data, 0, in.offset);
    end(client); return result;
}
dq_result_t zq_append(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy)
{ return append(ZQ_SEGMENTED, (unsigned)lane, data, length, policy); }
dq_result_t zq_legacy_append(unsigned lane, const void *data, size_t length, qs_admission_t policy)
{ return append(ZQ_ADD_LEGACY, lane, data, length, policy); }
dq_result_t zq_attendance_legacy_append(unsigned lane, const void *data, size_t length, qs_admission_t policy)
{ return append(ZQ_ATTENDANCE_LEGACY, lane, data, length, policy); }
dq_result_t zq_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token)
{ return peek(ZQ_SEGMENTED, (unsigned)lane, data, capacity, length, token, NULL); }
dq_result_t zq_legacy_peek(unsigned lane, void *data, size_t capacity, size_t *length, lq_token_t *token)
{ return peek(ZQ_ADD_LEGACY, lane, data, capacity, length, NULL, token); }
dq_result_t zq_evidence_peek(unsigned lane, void *data, size_t capacity, size_t *length, lq_token_t *token)
{ return peek(ZQ_QUARANTINE, lane, data, capacity, length, NULL, token); }
dq_result_t zq_attendance_legacy_peek(unsigned lane, void *data, size_t capacity, size_t *length, lq_token_t *token)
{ return peek(ZQ_ATTENDANCE_LEGACY, lane, data, capacity, length, NULL, token); }
static dq_result_t simple(zq_request_t *in, zq_reply_t *out)
{
    client_t *client = &clients[2];
    if (!begin(client, &in->deadline_us)) return DQ_PENDING;
    dq_result_t result = call(client, in, out);
    end(client); return result;
}
dq_result_t zq_settle(qs_lane_t lane, const dq_token_t *token)
{
    if ((unsigned)lane >= QS_COUNT || !token || token->end <= token->offset) return DQ_IO;
    zq_request_t in = {.operation = ZQ_SETTLE, .lane = (uint8_t)lane, .token = *token};
    zq_reply_t out;
    return simple(&in, &out);
}
dq_result_t zq_legacy_settle(unsigned lane, const lq_token_t *token, bool custody)
{
    if (lane >= 2 || !token || token->end <= token->offset) return DQ_IO;
    zq_request_t in = {.operation = ZQ_SETTLE, .lane = (uint8_t)lane, .domain = ZQ_ADD_LEGACY,
        .legacy_token = *token, .custody = custody};
    zq_reply_t out;
    return simple(&in, &out);
}
dq_result_t zq_evidence_settle(unsigned lane, const lq_token_t *token)
{
    if (lane >= 3 || !token || token->end <= token->offset) return DQ_IO;
    zq_request_t in = {.operation = ZQ_SETTLE, .lane = (uint8_t)lane, .domain = ZQ_QUARANTINE,
        .legacy_token = *token, .custody = true};
    zq_reply_t out;
    return simple(&in, &out);
}
dq_result_t zq_attendance_legacy_settle(unsigned lane, const lq_token_t *token, bool custody)
{
    if (lane >= 2 || !token || token->end <= token->offset) return DQ_IO;
    zq_request_t in = {.operation = ZQ_SETTLE, .lane = (uint8_t)lane, .domain = ZQ_ATTENDANCE_LEGACY,
        .legacy_token = *token, .custody = custody};
    zq_reply_t out;
    return simple(&in, &out);
}
bool zq_snapshot(qs_lane_t lane, uint32_t *depth)
{
    if ((unsigned)lane >= QS_COUNT || !depth) return false;
    zq_request_t in = {.operation = ZQ_SNAPSHOT, .lane = (uint8_t)lane}; zq_reply_t out;
    bool ok = simple(&in, &out) == DQ_OK && out.verified;
    if (ok) *depth = out.depth;
    return ok;
}
bool zq_generation(char output[33])
{
    if (!output) return false;
    output[0] = 0;
    zq_request_t in = {.operation = ZQ_GENERATION}; zq_reply_t out;
    bool ok = simple(&in, &out) == DQ_OK && out.verified && out.generation[32] == 0 &&
        strspn(out.generation, "0123456789abcdef") == 32;
    if (ok) memcpy(output, out.generation, 33);
    return ok;
}
bool zq_recover(void)
{
    zq_request_t in = {.operation = ZQ_RECOVER}; zq_reply_t out;
    return simple(&in, &out) == DQ_OK && out.verified;
}
bool zq_probe(void)
{
    zq_request_t in = {.operation = ZQ_PROBE}; zq_reply_t out;
    return simple(&in, &out) == DQ_OK && out.verified;
}
