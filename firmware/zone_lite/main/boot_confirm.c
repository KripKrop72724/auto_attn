#include "boot_confirm.h"

#include <stddef.h>

static void add_seconds(uint32_t *total, uint32_t seconds)
{
    *total = UINT32_MAX - *total < seconds ? UINT32_MAX : *total + seconds;
}

boot_confirm_verdict_t boot_confirm_step(boot_confirm_t *state, uint32_t seconds, const boot_confirm_input_t *input)
{
    if (!state || !input) return BOOT_CONFIRM_WAIT;
    add_seconds(&state->elapsed_seconds, seconds);
    add_seconds(input->environment_ready || input->local_failure ? &state->active_seconds : &state->paused_seconds,
                seconds);
    /* The cap comes first so that no state, not even health that ADD never
     * acknowledges, can hold an unconfirmed image forever. */
    if (state->elapsed_seconds >= BOOT_CONFIRM_MAX_SECONDS) return BOOT_CONFIRM_CAPPED;
    if (input->healthy) return BOOT_CONFIRM_ACCEPT;
    return state->active_seconds >= BOOT_CONFIRM_ACTIVE_SECONDS ? BOOT_CONFIRM_EXPIRED : BOOT_CONFIRM_WAIT;
}

const char *boot_confirm_verdict_name(boot_confirm_verdict_t verdict)
{
    switch (verdict) {
    case BOOT_CONFIRM_ACCEPT: return "ACCEPT";
    case BOOT_CONFIRM_EXPIRED: return "EXPIRED";
    case BOOT_CONFIRM_CAPPED: return "CAPPED";
    default: return "WAIT";
    }
}
