#include "zkt_segmented_owner.h"
#include <string.h>

static uint64_t transfer(zq_store_t *s)
{
    /* Refuse exhaustion; an old copied request can never acquire a new buffer. */
    return s->next_transfer == UINT64_MAX ? 0 : ++s->next_transfer;
}
static void expire(zq_store_t *s, uint64_t now)
{
    if (s->append_transfer && now >= s->append_deadline) {
        s->append_transfer = 0;
        memset(s->append_bytes, 0, sizeof(s->append_bytes));
    }
    if (s->read_transfer && now >= s->read_deadline) {
        s->read_transfer = 0;
        memset(s->read_bytes, 0, sizeof(s->read_bytes));
    }
}
void zq_store_execute(zq_store_t *s, uint64_t now, const zq_request_t *r, zq_reply_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out)); out->result = DQ_IO;
    if (!s || !zq_request_valid(r)) return;
    expire(s, now);
    if (now >= r->deadline_us) { out->result = DQ_PENDING; return; }
    switch (r->operation) {
        case ZQ_APPEND_BEGIN:
            /* A later producer may replace only RAM; it cannot retire or
             * overwrite any previously accepted durable queue record. */
            s->append_transfer = transfer(s);
            memset(s->append_bytes, 0, sizeof(s->append_bytes));
            s->append_used = 0; s->append_total = r->total;
            s->append_lane = r->lane; s->append_policy = r->policy;
            s->append_deadline = r->deadline_us;
            out->transfer = s->append_transfer;
            out->result = out->transfer ? DQ_OK : DQ_FULL;
            break;
        case ZQ_APPEND_CHUNK:
        case ZQ_APPEND_COMMIT:
            if (s->append_transfer != r->transfer || s->append_lane != r->lane ||
                s->append_policy != r->policy || s->append_deadline != r->deadline_us) {
                out->result = DQ_STALE; break;
            }
            if (r->operation == ZQ_APPEND_CHUNK) {
                if (r->offset != s->append_used || r->length > s->append_total - s->append_used) {
                    out->result = DQ_STALE; break;
                }
                memcpy(s->append_bytes + s->append_used, r->bytes, r->length);
                s->append_used += r->length;
                out->transfer = s->append_transfer; out->total = s->append_used; out->result = DQ_OK;
            } else {
                if (s->append_used != s->append_total) { out->result = DQ_STALE; break; }
                /* Burn the RAM transfer even on uncertain append failure.
                 * Retrying complete caller bytes can replay an event, but
                 * cannot commit this transfer twice after a lost result. */
                s->append_transfer = 0;
                out->result = qs_append_with_policy((qs_lane_t)r->lane,
                    s->append_bytes, s->append_total, (qs_admission_t)r->policy);
                memset(s->append_bytes, 0, sizeof(s->append_bytes));
            }
            break;
        case ZQ_PEEK_BEGIN: {
            s->read_transfer = 0; s->read_length = 0;
            memset(s->read_bytes, 0, sizeof(s->read_bytes));
            size_t length = 0;
            out->result = qs_peek((qs_lane_t)r->lane, s->read_bytes,
                                  sizeof(s->read_bytes), &length, &s->read_token);
            if (out->result != DQ_OK) break;
            if (!length || length > sizeof(s->read_bytes)) { out->result = DQ_CORRUPT; break; }
            s->read_transfer = transfer(s);
            if (!s->read_transfer) { out->result = DQ_FULL; break; }
            s->read_length = (uint32_t)length; s->read_lane = r->lane; s->read_deadline = r->deadline_us;
            out->transfer = s->read_transfer; out->total = s->read_length; out->token = s->read_token;
            break;
        }
        case ZQ_PEEK_CHUNK:
            if (s->read_transfer != r->transfer || s->read_lane != r->lane ||
                s->read_deadline != r->deadline_us || r->offset >= s->read_length) {
                out->result = DQ_STALE; break;
            }
            out->length = (uint16_t)(s->read_length - r->offset > ZQ_CHUNK_BYTES ?
                ZQ_CHUNK_BYTES : s->read_length - r->offset);
            memcpy(out->bytes, s->read_bytes + r->offset, out->length);
            out->transfer = s->read_transfer; out->total = s->read_length;
            out->token = s->read_token; out->result = DQ_OK;
            break;
        case ZQ_SETTLE: out->result = qs_settle((qs_lane_t)r->lane, &r->token); break;
        case ZQ_SNAPSHOT:
            out->verified = qs_snapshot((qs_lane_t)r->lane, &out->depth);
            out->result = out->verified ? DQ_OK : DQ_PENDING; break;
        case ZQ_GENERATION:
            out->verified = qs_generation(out->generation);
            out->result = out->verified ? DQ_OK : DQ_IO; break;
        case ZQ_RECOVER:
            out->verified = qs_recover_step(); out->result = out->verified ? DQ_OK : DQ_PENDING; break;
        case ZQ_PROBE:
            out->verified = qs_verify_persistence(); out->result = out->verified ? DQ_OK : DQ_PENDING; break;
    }
}
