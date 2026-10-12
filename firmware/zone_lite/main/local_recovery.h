#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Decides when a latched LOCAL_FAILURE may clear. Never on a timer: storage
 * must pass a verification requested after the latest incident, and the
 * delivery workers must be healthy. A verification is requested only after
 * LOCAL_RECOVERY_QUIET_STEPS steps without a new incident, so a recurring
 * incident cannot make the store probe flash on every step. Pure logic; the
 * caller owns the I/O. */
#define LOCAL_RECOVERY_QUIET_STEPS 10U
typedef struct {
    uint32_t incidents; /* incident count seen at the last step */
    uint32_t quiet;     /* steps since the incident count last changed */
    uint32_t request;   /* storage verification request; 0 when none */
} local_recovery_t;

typedef struct {
    bool active;               /* the LED latch is on */
    uint32_t incidents;        /* LOCAL_FAILURE incidents so far (wrapping) */
    uint32_t verified_through; /* latest verification request that passed */
    bool storage_healthy;      /* verified now: no fault, audited, probe passed */
    bool workers_healthy;      /* delivery workers running with fresh ticks */
} local_recovery_input_t;

typedef enum {
    LOCAL_RECOVERY_WAIT,
    LOCAL_RECOVERY_REQUEST, /* request a fresh verification, then report it */
    LOCAL_RECOVERY_CLEAR,   /* clear the latch if no incident followed `incidents` */
} local_recovery_action_t;

local_recovery_action_t local_recovery_step(local_recovery_t *state, const local_recovery_input_t *input);
/* Record the request made for LOCAL_RECOVERY_REQUEST; request 0 means the
 * store was busy, so the next step asks again. */
void local_recovery_requested(local_recovery_t *state, uint32_t request);
