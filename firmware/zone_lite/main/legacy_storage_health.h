#pragma once
#include "durable_queue.h"

/* Fixed inventory, no record contents or unbounded allocation. Only the exact
 * bridge/writer adapters report here while holding the shared storage lock. */
typedef enum { LF_ADD_LIVE, LF_ADD_BULK, LF_ORDS_PENDING, LF_IDENTITY_BLOCKED,
    LF_ORDS_QUARANTINE, LF_ADD_QUARANTINE, LF_ADD_QUARANTINE_BACKUP, LF_LANES } lf_lane_t;
typedef enum { LF_READ, LF_APPEND, LF_RETIRE, LF_OPERATIONS } lf_operation_t;
typedef struct { int errors[LF_LANES][LF_OPERATIONS]; uint32_t read_recoveries; bool observed; } lf_state_t;
typedef struct {
    uint32_t read_faults, append_faults, retire_faults, read_recoveries;
    bool observed;
    int error;
    const char *operation, *queue;
} lf_health_t;

/* A complete peek, including required restoration/checkpoint validation, can
 * clear only this lane's read incident. PENDING, STALE and capacity refusal
 * are not recovery evidence. Append/retirement faults require future explicit
 * custody/checkpoint recovery proof; later successful I/O cannot clear them. */
void lf_record(lf_state_t *, lf_lane_t, lf_operation_t, dq_result_t, int captured_error);
lf_health_t lf_health(const lf_state_t *);
