#include "local_recovery.h"

local_recovery_action_t local_recovery_step(local_recovery_t *state, const local_recovery_input_t *input)
{
    if (!state || !input) return LOCAL_RECOVERY_WAIT;
    /* Every new incident needs a verification that starts after it. */
    if (!input->active || input->incidents != state->incidents) {
        state->incidents = input->incidents;
        state->quiet = 0;
        state->request = 0;
        return LOCAL_RECOVERY_WAIT;
    }
    if (!state->request) {
        if (state->quiet < LOCAL_RECOVERY_QUIET_STEPS) ++state->quiet;
        return state->quiet < LOCAL_RECOVERY_QUIET_STEPS ? LOCAL_RECOVERY_WAIT : LOCAL_RECOVERY_REQUEST;
    }
    bool verified = (int32_t)(input->verified_through - state->request) >= 0;
    return verified && input->storage_healthy && input->workers_healthy ? LOCAL_RECOVERY_CLEAR
                                                                        : LOCAL_RECOVERY_WAIT;
}

void local_recovery_requested(local_recovery_t *state, uint32_t request)
{
    if (state) state->request = request;
}
