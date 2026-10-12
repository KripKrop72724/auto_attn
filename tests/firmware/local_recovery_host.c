/* Host harness for local_recovery.c: when a latched LOCAL_FAILURE may clear. */
#include <assert.h>
#include <stdio.h>

#include "local_recovery.h"

static local_recovery_t state;
static local_recovery_input_t input;

static local_recovery_action_t step(void) { return local_recovery_step(&state, &input); }

/* Steps until the supervisor asks for a verification; returns how many. */
static unsigned until_request(void)
{
    for (unsigned steps = 1; steps <= 100; ++steps)
        if (step() == LOCAL_RECOVERY_REQUEST) return steps;
    assert(!"never requested");
    return 0;
}

int main(void)
{
    /* No latch: nothing to do. */
    assert(step() == LOCAL_RECOVERY_WAIT);

    /* A latched incident is verified only after a quiet period without new
     * incidents, so a recurring one never makes the store probe flash on
     * every step. */
    input = (local_recovery_input_t){.active = true, .incidents = 1, .verified_through = 7,
                                     .storage_healthy = true, .workers_healthy = true};
    assert(step() == LOCAL_RECOVERY_WAIT); /* the incident count just changed */
    for (unsigned recurrence = 2; recurrence < 50; ++recurrence) {
        input.incidents = recurrence;
        for (unsigned quiet = 0; quiet < LOCAL_RECOVERY_QUIET_STEPS - 1; ++quiet)
            assert(step() == LOCAL_RECOVERY_WAIT);
    }
    assert(until_request() == 2);

    /* A busy store (request 0) is asked again on the next step. */
    local_recovery_requested(&state, 0);
    assert(step() == LOCAL_RECOVERY_REQUEST);
    local_recovery_requested(&state, 8);

    /* An older verification never counts, however healthy storage looks. */
    assert(step() == LOCAL_RECOVERY_WAIT);

    /* The requested verification passed, but storage or workers are not
     * healthy right now. */
    input.verified_through = 8;
    input.storage_healthy = false;
    assert(step() == LOCAL_RECOVERY_WAIT);
    input.storage_healthy = true;
    input.workers_healthy = false;
    assert(step() == LOCAL_RECOVERY_WAIT);
    input.workers_healthy = true;
    assert(step() == LOCAL_RECOVERY_CLEAR);

    /* A new incident before the clear needs its own, later verification. */
    input.incidents = 50;
    assert(step() == LOCAL_RECOVERY_WAIT);
    assert(until_request() == LOCAL_RECOVERY_QUIET_STEPS);
    local_recovery_requested(&state, 9);
    assert(step() == LOCAL_RECOVERY_WAIT);
    input.verified_through = 9;
    assert(step() == LOCAL_RECOVERY_CLEAR);

    /* Once the latch is off the request is forgotten. */
    input.active = false;
    assert(step() == LOCAL_RECOVERY_WAIT);
    input.active = true;
    input.incidents = 51;
    assert(step() == LOCAL_RECOVERY_WAIT);
    assert(until_request() == LOCAL_RECOVERY_QUIET_STEPS);

    /* Request and incident counters wrap (requests skip 0). */
    local_recovery_requested(&state, UINT32_MAX);
    input.verified_through = UINT32_MAX - 1;
    assert(step() == LOCAL_RECOVERY_WAIT);
    input.verified_through = 1;
    assert(step() == LOCAL_RECOVERY_CLEAR);
    input.incidents = 0; /* 2^32 incidents later */
    assert(step() == LOCAL_RECOVERY_WAIT);
    assert(until_request() == LOCAL_RECOVERY_QUIET_STEPS);
    puts("local recovery host tests passed");
    return 0;
}
