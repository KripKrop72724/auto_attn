#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#include <string.h>
#define ZONE_LITE_QUEUE_OWNER 1
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define MALLOC_CAP_SPIRAM 1024U
#define MALLOC_CAP_8BIT 4U
typedef struct { durable_queue_t queue; int *mutex; } lane_t;
static int lane_lock, budget_mutex;
static int *budget_lock = &budget_mutex;
static lane_t lanes[QS_COUNT];
static qs_health_t health;
static dq_audit_t recovery_audits[QS_COUNT];
static uint8_t scratch[DQ_MAX_RECORD_BYTES];
static uint8_t *recovery_buffer;
static bool required, started, owner_context;
static unsigned routed, accesses, allocations;
static int failed_lane = -1;
bool zj_runtime_checkpoint_required(void) { return required; }
bool zj_owner_started(void) { return started; }
bool zj_owner_is_current_task(void) { return owner_context; }
bool zq_recover(void) { ++routed; return false; }
static bool lock(qs_lane_t lane)
{ assert((unsigned)lane < QS_COUNT && !lane_lock); lane_lock = 1; return true; }
static int xSemaphoreTake(int *mutex, unsigned wait)
{ (void)wait; assert(!*mutex); *mutex = 1; return pdTRUE; }
static void xSemaphoreGive(int *mutex) { assert(*mutex); *mutex = 0; }
static void *heap_caps_malloc(size_t bytes, unsigned caps)
{
    assert(lane_lock && budget_mutex && bytes == sizeof(scratch));
    assert(caps == (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) && !allocations);
    ++allocations; return scratch;
}
static dq_result_t reopen(lane_t *lane) { assert(lane->queue.ready); return DQ_OK; }
static void record_queue_result(dq_result_t result, const char *operation, bool write)
{ (void)operation; assert(!write); if (result != DQ_OK) health.last_error = EIO; }
dq_result_t dq_audit_step(const durable_queue_t *queue, dq_audit_t *audit, void *buffer, size_t capacity)
{
    assert(lane_lock && budget_mutex && buffer == recovery_buffer && capacity == DQ_MAX_RECORD_BYTES);
    ++accesses;
    if (failed_lane >= 0 && queue == &lanes[failed_lane].queue) return DQ_IO;
    if (!audit->started || audit->generation != queue->checkpoint.generation) {
        *audit = (dq_audit_t){.started = true, .generation = queue->checkpoint.generation};
        return DQ_PENDING;
    }
    audit->complete = true; return DQ_OK;
}
#include "segmented_recovery_actual.inc"
int main(void)
{
    for (unsigned i = 0; i < QS_COUNT; ++i) {
        lanes[i].mutex = &lane_lock; lanes[i].queue.ready = true;
        lanes[i].queue.checkpoint.generation = 5;
    }
    required = true;
    /* Single app-task bootstrap keeps its old full-lane pass. */
    assert(!qs_recover_step() && accesses == QS_COUNT);
    assert(qs_recover_step() && accesses == 2 * QS_COUNT);
    started = true;
    assert(!qs_recover_step() && routed == 1 && accesses == 2 * QS_COUNT);
    owner_context = true;
    memset(recovery_audits, 0, sizeof(recovery_audits)); accesses = 0;
    for (unsigned pass = 0; pass < 2 * QS_COUNT; ++pass) {
        assert(qs_recover_step() == (pass == 2 * QS_COUNT - 1));
        assert(accesses == pass + 1 && !lane_lock && !budget_mutex);
    }
    unsigned before = accesses;
    for (unsigned pass = 0; pass < QS_COUNT; ++pass) assert(qs_recover_step());
    assert(accesses == before); /* No repeated scans for unchanged proof. */
    ++lanes[2].queue.checkpoint.generation;
    assert(!qs_recover_step()); /* Another lane cannot certify the changed one. */
    for (unsigned pass = 0; pass < 2 * QS_COUNT; ++pass) (void)qs_recover_step();
    assert(qs_recover_step() && accesses == before + 2);
    health.legacy.error = EIO;
    assert(!qs_recover_step() && !health.recovery_complete);
    health.legacy.error = 0;
    assert(qs_recover_step()); /* Segmented checks cannot override legacy incidents. */
    memset(recovery_audits, 0, sizeof(recovery_audits));
    failed_lane = 2; before = accesses;
    for (unsigned pass = 0; pass < 2 * QS_COUNT; ++pass) assert(!qs_recover_step());
    assert(accesses == before + 2 * QS_COUNT && health.last_error == EIO);
    for (unsigned i = 0; i < QS_COUNT; ++i) if (i != 2) assert(recovery_audits[i].complete);
    failed_lane = -1;
    for (unsigned pass = 0; pass < 2 * QS_COUNT; ++pass) assert(!qs_recover_step());
    assert(health.last_error == EIO); /* This component cannot clear an unrelated incident. */
    assert(allocations == 1); /* Owner/bootstrap handoff shares the bounded scratch. */
}
