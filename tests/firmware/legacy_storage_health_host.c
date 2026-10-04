#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#include <string.h>
static lf_state_t legacy_health;
static qs_health_t health;
static unsigned held, budget_lock = 1;
static void xSemaphoreGive(unsigned lock) { assert(lock == budget_lock && held); held = 0; }
static bool measure(void) { health.available = true; return true; }
#include "legacy_health_actual.inc"
static void report(lf_lane_t lane, lf_operation_t operation, dq_result_t result, int error)
{ assert(!held); held = 1; qs_local_end_legacy(lane, operation, result, error); assert(!held); }
static qs_health_t snapshot(void)
{ assert(!held); held = 1; qs_health_t out = qs_local_health_locked(); held = 0; return out; }
int main(void)
{
    assert(!snapshot().legacy.observed);
    health.recovery_complete = health.persistence_verified = true;
    report(LF_ADD_LIVE, LF_READ, DQ_IO, ENXIO);
    qs_health_t out = snapshot();
    assert(out.last_error == ENXIO && !out.recovery_complete && !out.persistence_verified);
    assert(out.read_failures == 1 && !out.write_failures && out.legacy.read_faults == 1);
    assert(!strcmp(out.last_operation, "legacy_read") && !strcmp(out.legacy.queue, "add_live"));
    for (unsigned lane = 1; lane < LF_LANES; ++lane) report((lf_lane_t)lane, LF_READ, DQ_EMPTY, 0);
    dq_result_t refused[] = {DQ_PENDING, DQ_STALE, DQ_FULL, DQ_BUFFER_SMALL};
    for (unsigned i = 0; i < sizeof(refused) / sizeof(*refused); ++i) report(LF_ADD_LIVE, LF_READ, refused[i], EBUSY);
    report(LF_ADD_LIVE, LF_APPEND, DQ_OK, 0);
    report(LF_ADD_LIVE, LF_RETIRE, DQ_OK, 0);
    assert(snapshot().legacy.read_faults == 1 && !snapshot().legacy.read_recoveries);
    report(LF_ADD_LIVE, LF_READ, DQ_OK, 0);
    out = snapshot();
    assert(!out.last_error && !out.legacy.read_faults && out.legacy.read_recoveries == 1);
    assert(out.persistence_recheck_required);
    assert(!out.recovery_complete && !out.persistence_verified); /* Must re-run full recovery/probe. */
    report(LF_ADD_LIVE, LF_READ, DQ_EMPTY, 0);
    assert(snapshot().legacy.read_recoveries == 1);
    for (unsigned lane = 0; lane < LF_LANES; ++lane) {
        report((lf_lane_t)lane, LF_READ, DQ_CORRUPT, ENOSPC);
        report((lf_lane_t)lane, LF_APPEND, DQ_IO, ENOSPC);
        report((lf_lane_t)lane, LF_RETIRE, DQ_IO, 0);
    }
    out = snapshot();
    assert(out.legacy.read_faults == LF_LANES && out.legacy.append_faults == LF_LANES && out.legacy.retire_faults == LF_LANES);
    assert(out.last_error == EBADMSG && out.write_failures == 2 * LF_LANES);
    for (unsigned lane = 0; lane < LF_LANES; ++lane) {
        report((lf_lane_t)lane, LF_READ, DQ_EMPTY, 0);
        report((lf_lane_t)lane, LF_APPEND, DQ_OK, 0);
        report((lf_lane_t)lane, LF_RETIRE, DQ_OK, 0);
    }
    out = snapshot();
    assert(!out.legacy.read_faults && out.legacy.read_recoveries == 1 + LF_LANES);
    assert(out.legacy.append_faults == LF_LANES && out.legacy.retire_faults == LF_LANES);
    assert(out.last_error == ENOSPC && !strcmp(out.last_operation, "legacy_append"));
    health.recovery_complete = health.persistence_verified = true;
    out = snapshot(); /* A cached global success cannot hide a scoped failure. */
    assert(!out.recovery_complete && !out.persistence_verified);
    health.last_error = EPERM; health.last_operation = "unrelated_write";
    out = snapshot();
    assert(out.last_error == EPERM && !strcmp(out.last_operation, "unrelated_write") && out.legacy.error);
    legacy_health.read_recoveries = UINT32_MAX;
    report(LF_ORDS_PENDING, LF_READ, DQ_IO, EIO);
    report(LF_ORDS_PENDING, LF_READ, DQ_OK, 0);
    assert(snapshot().legacy.read_recoveries == UINT32_MAX);
    assert(snapshot().last_error == EPERM);
}
