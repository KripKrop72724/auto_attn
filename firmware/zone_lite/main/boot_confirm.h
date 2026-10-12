#pragma once

#include <stdbool.h>
#include <stdint.h>

/* How long a new image may take to prove itself before it rolls back.
 *
 * The active budget counts only time in which the image is accountable:
 * ADD and the terminal are reachable, or the image itself shows a local
 * failure. A healthy-looking image waiting on a site's Wi-Fi, ADD or
 * terminal is paused, so an outage during an OTA no longer causes a
 * rollback. The hard cap bounds every case, including an image that breaks
 * Wi-Fi or the terminal link and so never becomes accountable. Pure logic;
 * the caller owns the clock and the rollback. */
#define BOOT_CONFIRM_ACTIVE_SECONDS 900U
#define BOOT_CONFIRM_MAX_SECONDS (4U * 60U * 60U)

typedef struct {
    uint32_t active_seconds;  /* counted toward the budget */
    uint32_t paused_seconds;  /* a healthy-looking image waiting on the site */
    uint32_t elapsed_seconds; /* since confirmation began */
} boot_confirm_t;

typedef struct {
    bool healthy;           /* full boot health, ready to confirm */
    bool local_failure;     /* positive evidence that the image fails locally */
    bool environment_ready; /* ADD connected and the terminal reachable */
} boot_confirm_input_t;

typedef enum {
    BOOT_CONFIRM_WAIT,
    BOOT_CONFIRM_ACCEPT,
    BOOT_CONFIRM_EXPIRED, /* the active budget is spent: roll back */
    BOOT_CONFIRM_CAPPED,  /* the hard cap is reached: roll back */
} boot_confirm_verdict_t;

/* Account `seconds` since the previous step, then decide. */
boot_confirm_verdict_t boot_confirm_step(boot_confirm_t *state, uint32_t seconds, const boot_confirm_input_t *input);
const char *boot_confirm_verdict_name(boot_confirm_verdict_t verdict);
