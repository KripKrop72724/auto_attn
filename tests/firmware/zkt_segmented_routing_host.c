#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#include <string.h>
#define ZONE_LITE_QUEUE_OWNER 1
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct { durable_queue_t queue; int *mutex; qs_admission_t admission; } lane_t;
static int lane_lock, budget_mutex;
static int *budget_lock = &budget_mutex;
static lane_t lanes[QS_COUNT];
static dq_audit_t recovery_audits[QS_COUNT];
static qs_health_t health;
static char storage_generation[33] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static bool required, owner_context;
static unsigned routed, accesses;
bool zj_runtime_checkpoint_required(void) { return required; }
bool zj_owner_is_current_task(void) { return owner_context; }
bool storage_upgrade_segmented_writes(void) { return true; }
static bool lock(qs_lane_t lane)
{ assert((unsigned)lane < QS_COUNT && !lane_lock); lane_lock = 1; return true; }
static int xSemaphoreTake(int *mutex, unsigned wait)
{ (void)wait; assert(!*mutex); *mutex = 1; return pdTRUE; }
static void xSemaphoreGive(int *mutex) { assert(*mutex); *mutex = 0; }
static bool ensure_storage_generation(void)
{ assert(budget_mutex && (owner_context || !required)); return true; }
static dq_result_t reopen(lane_t *lane)
{ (void)lane; assert(lane_lock && budget_mutex && (owner_context || !required)); return DQ_OK; }
static void record_queue_result(dq_result_t result, const char *operation, bool write)
{ assert(result == DQ_OK); (void)operation; (void)write; }
dq_result_t dq_append(durable_queue_t *queue, const void *data, size_t length)
{ (void)queue; assert(data && length); ++accesses; return DQ_OK; }
dq_result_t dq_peek(durable_queue_t *queue, void *data, size_t capacity, size_t *length, dq_token_t *token)
{ (void)queue; assert(data && capacity && length && token); ++accesses; *length = 1; return DQ_OK; }
dq_result_t dq_settle(durable_queue_t *queue, const dq_token_t *token)
{ (void)queue; assert(token); ++accesses; return DQ_OK; }
dq_result_t zq_append(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy)
{ assert(lane == QS_LIVE && data && length == 1 && policy == QS_ADMIT_LIVE); ++routed; return DQ_PENDING; }
dq_result_t zq_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token)
{ assert(lane == QS_LIVE && data && capacity == 1 && length && token); ++routed; return DQ_PENDING; }
dq_result_t zq_settle(qs_lane_t lane, const dq_token_t *token)
{ assert(lane == QS_LIVE && token); ++routed; return DQ_PENDING; }
bool zq_snapshot(qs_lane_t lane, uint32_t *depth)
{ assert(lane == QS_LIVE && depth); ++routed; return false; }
bool zq_generation(char output[33]) { assert(output); ++routed; return false; }
#include "segmented_routing_actual.inc"
int main(void)
{
    for (unsigned i = 0; i < QS_COUNT; ++i) { lanes[i].mutex = &lane_lock; lanes[i].queue.ready = true; }
    for (unsigned pass = 0; pass < 4; ++pass) {
        required = pass >= 2; owner_context = (pass & 1) != 0;
        bool route = required && !owner_context;
        routed = accesses = 0;
        char byte, generation[33]; size_t length = 0; dq_token_t token = {.end = 1}; uint32_t depth;
        assert(qs_generation(generation) == !route);
        assert(qs_append_with_policy(QS_LIVE, "x", 1, QS_ADMIT_LIVE) == (route ? DQ_PENDING : DQ_OK));
        assert(qs_peek(QS_LIVE, &byte, 1, &length, &token) == (route ? DQ_PENDING : DQ_OK));
        assert(qs_settle(QS_LIVE, &token) == (route ? DQ_PENDING : DQ_OK));
        assert(qs_snapshot(QS_LIVE, &depth) == !route);
        assert(routed == (route ? 5U : 0U) && accesses == (route ? 0U : 3U));
        assert(!lane_lock && !budget_mutex);
    }
    required = owner_context = true;
    health.persistence_verified = false; health.persistence_recheck_required = true;
    assert(qs_append_with_policy(QS_LIVE, "x", 1, QS_ADMIT_LIVE) == DQ_OK);
    assert(!health.persistence_verified && health.persistence_recheck_required);
}
