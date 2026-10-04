#include "legacy_storage_health.h"
#include <errno.h>

void lf_record(lf_state_t *state, lf_lane_t lane, lf_operation_t operation,
               dq_result_t result, int error)
{
    if (!state || (unsigned)lane >= LF_LANES || (unsigned)operation >= LF_OPERATIONS) return;
    state->observed = true;
    if (result == DQ_IO || result == DQ_CORRUPT)
        state->errors[lane][operation] = result == DQ_CORRUPT ? EBADMSG : error ? error : EIO;
    else if (operation == LF_READ && (result == DQ_OK || result == DQ_EMPTY) && state->errors[lane][LF_READ]) {
        state->errors[lane][LF_READ] = 0;
        if (state->read_recoveries < UINT32_MAX) ++state->read_recoveries;
    }
}

lf_health_t lf_health(const lf_state_t *state)
{
    lf_health_t health = {0};
    if (!state) return health;
    static const char *queues[] = {"add_live", "add_bulk", "ords_pending", "identity_blocked",
        "ords_quarantine", "add_quarantine", "add_quarantine_backup"};
    static const char *operations[] = {"legacy_read", "legacy_append", "legacy_retire"};
    health.observed = state->observed;
    health.read_recoveries = state->read_recoveries;
    for (unsigned lane = 0; lane < LF_LANES; ++lane) {
        health.read_faults += state->errors[lane][LF_READ] != 0;
        health.append_faults += state->errors[lane][LF_APPEND] != 0;
        health.retire_faults += state->errors[lane][LF_RETIRE] != 0;
        for (unsigned operation = 0; operation < LF_OPERATIONS; ++operation) {
            if (!health.error && state->errors[lane][operation]) {
                health.error = state->errors[lane][operation];
                health.operation = operations[operation];
                health.queue = queues[lane];
            }
        }
    }
    return health;
}
