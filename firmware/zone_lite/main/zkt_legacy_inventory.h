#pragma once
#include "zkt_segmented_owner.h"

/* A local absence proof, not an employee/Oracle disposition or a persisted
 * migration certificate. Only the owner updates it under the mailbox lock.
 * Six ZKT segmented queues plus two ADD, two attendance and three quarantine
 * generations participate. Hikvision's family-specific lane is excluded. */
#define ZQ_INVENTORY_LANES (QS_HIK_SOURCE + 7U)
#define ZQ_INVENTORY_REQUIRED ((1U << ZQ_INVENTORY_LANES) - 1U)
#define ZQ_INVENTORY_QUARANTINE (7U << (QS_HIK_SOURCE + 4U))
_Static_assert(ZQ_INVENTORY_LANES < 32U, "Inventory mask must contain every legacy domain");
typedef struct {
    uint32_t empty_mask;
    uint64_t generation;
    bool exhausted;
} zq_inventory_t;

static inline void zq_inventory_invalidate(zq_inventory_t *s)
{
    s->empty_mask = 0;
    if (s->generation == UINT64_MAX) s->exhausted = true;
    else ++s->generation;
}
static inline uint32_t zq_inventory_bit(const zq_request_t *r)
{
    if (!zq_request_valid(r)) return 0;
    if (r->domain == ZQ_SEGMENTED)
        return r->lane < QS_HIK_SOURCE ? 1U << r->lane : 0;
    unsigned offset = r->domain == ZQ_ADD_LEGACY ? QS_HIK_SOURCE :
        r->domain == ZQ_ATTENDANCE_LEGACY ? QS_HIK_SOURCE + 2U : QS_HIK_SOURCE + 4U;
    return 1U << (offset + r->lane);
}
static inline bool zq_inventory_mutates(const zq_request_t *r)
{
    return r->operation <= ZQ_APPEND_COMMIT || r->operation == ZQ_SETTLE;
}
static inline void zq_inventory_admitted(zq_inventory_t *s, const zq_request_t *r)
{
    /* Invalidate at RAM admission too: a queued/timed-out producer may still
     * append after the caller has stopped waiting. Unknown is never zero. */
    if (!zq_inventory_bit(r) || zq_inventory_mutates(r)) zq_inventory_invalidate(s);
}
static inline void zq_inventory_begin(zq_inventory_t *s, const zq_request_t *r)
{
    uint32_t bit = zq_inventory_bit(r);
    if (!bit || zq_inventory_mutates(r)) { zq_inventory_invalidate(s); return; }
    if (r->operation == ZQ_PEEK_BEGIN || r->operation == ZQ_SNAPSHOT) {
        /* A first read/recovery of a non-quarantine domain can preserve a
         * corrupt tail in quarantine. Recheck those dependent generations.
         * A previously verified empty domain's cached read adds no new bytes. */
        if (!(s->empty_mask & bit) && r->domain != ZQ_QUARANTINE)
            s->empty_mask &= ~ZQ_INVENTORY_QUARANTINE;
        s->empty_mask &= ~bit;
    }
}
static inline void zq_inventory_complete(zq_inventory_t *s, const zq_request_t *r, const zq_reply_t *out)
{
    uint32_t bit = zq_inventory_bit(r);
    /* The periodic segmented audit and persistence probe do not add or retire
     * attendance. Successful checks preserve existing absence evidence; they
     * cannot establish it. A pending/failed/unverified check still revokes the
     * whole proof, including when no queue producer has run. */
    bool check = r->operation == ZQ_RECOVER || r->operation == ZQ_PROBE;
    if (!bit || (out->result != DQ_OK && out->result != DQ_EMPTY) ||
        (check && (out->result != DQ_OK || !out->verified))) {
        zq_inventory_invalidate(s); return;
    }
    if ((r->operation == ZQ_PEEK_BEGIN && out->result == DQ_EMPTY) ||
        (r->operation == ZQ_SNAPSHOT && r->domain == ZQ_SEGMENTED && out->verified && !out->depth))
        s->empty_mask |= bit;
}
static inline bool zq_inventory_empty(const zq_inventory_t *s)
{
    return s->generation && !s->exhausted && s->empty_mask == ZQ_INVENTORY_REQUIRED;
}
