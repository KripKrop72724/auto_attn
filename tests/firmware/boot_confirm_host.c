/* Host harness for boot_confirm.c: when an unconfirmed image rolls back. */
#include <assert.h>
#include <stdio.h>

#include "boot_confirm.h"

static boot_confirm_t clock;
static boot_confirm_input_t input;

static boot_confirm_verdict_t run(uint32_t seconds, uint32_t step)
{
    boot_confirm_verdict_t verdict = BOOT_CONFIRM_WAIT;
    for (uint32_t done = 0; done < seconds && verdict == BOOT_CONFIRM_WAIT; done += step)
        verdict = boot_confirm_step(&clock, step, &input);
    return verdict;
}

static void reset(void) { clock = (boot_confirm_t){0}; }

int main(void)
{
    /* A healthy image confirms at once. */
    input = (boot_confirm_input_t){.healthy = true, .environment_ready = true};
    assert(boot_confirm_step(&clock, 5, &input) == BOOT_CONFIRM_ACCEPT && clock.active_seconds == 5);

    /* Unhealthy with ADD and the terminal reachable: 900 accountable
     * seconds, then roll back. */
    reset();
    input = (boot_confirm_input_t){.environment_ready = true};
    assert(run(BOOT_CONFIRM_ACTIVE_SECONDS - 5, 5) == BOOT_CONFIRM_WAIT);
    assert(boot_confirm_step(&clock, 5, &input) == BOOT_CONFIRM_EXPIRED);
    assert(clock.active_seconds == BOOT_CONFIRM_ACTIVE_SECONDS && !clock.paused_seconds);

    /* A site outage during the OTA: a healthy-looking image waiting on Wi-Fi,
     * ADD or the terminal does not spend its budget... */
    reset();
    input = (boot_confirm_input_t){0};
    assert(run(3 * 60 * 60, 5) == BOOT_CONFIRM_WAIT);
    assert(!clock.active_seconds && clock.paused_seconds == 3 * 60 * 60);
    /* ...and once the site is back it still gets the full budget. */
    input.environment_ready = true;
    assert(run(BOOT_CONFIRM_ACTIVE_SECONDS - 5, 5) == BOOT_CONFIRM_WAIT);
    input.healthy = true;
    assert(boot_confirm_step(&clock, 5, &input) == BOOT_CONFIRM_ACCEPT);

    /* A local failure counts even while the site is down: the image cannot
     * hide a broken store or worker behind an outage. */
    reset();
    input = (boot_confirm_input_t){.local_failure = true};
    assert(run(BOOT_CONFIRM_ACTIVE_SECONDS, 5) == BOOT_CONFIRM_EXPIRED);
    assert(clock.active_seconds == BOOT_CONFIRM_ACTIVE_SECONDS && !clock.paused_seconds);

    /* A flapping site: only the accountable stretches add up. */
    reset();
    for (unsigned cycle = 0; cycle < 8; ++cycle) {
        input = (boot_confirm_input_t){.environment_ready = true};
        assert(run(100, 5) == BOOT_CONFIRM_WAIT);
        input = (boot_confirm_input_t){0};
        assert(run(600, 5) == BOOT_CONFIRM_WAIT);
    }
    assert(clock.active_seconds == 800 && clock.paused_seconds == 4800);
    input.environment_ready = true;
    assert(run(100, 5) == BOOT_CONFIRM_EXPIRED && clock.active_seconds == 900);

    /* The hard cap bounds an image that never becomes accountable, for
     * example one that breaks Wi-Fi, and comes before a late health. */
    reset();
    input = (boot_confirm_input_t){0};
    assert(run(BOOT_CONFIRM_MAX_SECONDS - 5, 5) == BOOT_CONFIRM_WAIT);
    input.healthy = true;
    assert(boot_confirm_step(&clock, 5, &input) == BOOT_CONFIRM_CAPPED);

    /* A clock jump cannot overflow the counters. */
    reset();
    input = (boot_confirm_input_t){0};
    assert(boot_confirm_step(&clock, UINT32_MAX, &input) == BOOT_CONFIRM_CAPPED);
    assert(boot_confirm_step(&clock, UINT32_MAX, &input) == BOOT_CONFIRM_CAPPED);
    assert(clock.elapsed_seconds == UINT32_MAX && clock.paused_seconds == UINT32_MAX);
    assert(boot_confirm_step(NULL, 5, &input) == BOOT_CONFIRM_WAIT);
    puts("boot confirmation host tests passed");
    return 0;
}
