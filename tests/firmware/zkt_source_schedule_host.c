#include "zkt_source_schedule.h"
#include <assert.h>
#include <stdio.h>

int main(void)
{
    zts_schedule_t schedule = {0};
    assert(zts_due(&schedule, 0, true, true));
    assert(!zts_due(&schedule, 0, false, true));
    assert(!zts_due(&schedule, 0, true, false));
    for (uint32_t random = 0; random < 3000; ++random) {
        schedule = (zts_schedule_t){0};
        zts_completed(&schedule, UINT32_MAX - 100, true, random);
        assert(schedule.next_attempt_ms >= (uint64_t)UINT32_MAX + 1900);
        assert(schedule.next_attempt_ms <= (uint64_t)UINT32_MAX + 2150);
        assert(!zts_due(&schedule, schedule.next_attempt_ms - 1, true, true));
        assert(zts_due(&schedule, schedule.next_attempt_ms, true, true));
        assert(!zts_due(&schedule, schedule.next_attempt_ms, true, false));
    }
    schedule = (zts_schedule_t){0};
    uint64_t now = 1000;
    for (unsigned failure = 0; failure < 100; ++failure) {
        zts_completed(&schedule, now, false, UINT32_MAX - failure);
        uint64_t delay = schedule.next_attempt_ms - now;
        assert(delay >= 2000 && delay <= 60000);
        if (failure >= 5) assert(schedule.retry_ms == 59000);
        now = schedule.next_attempt_ms;
    }
    zts_completed(&schedule, now, true, 250);
    assert(!schedule.retry_ms && schedule.next_attempt_ms == now + 2250);
    assert(!zts_due(&schedule, now - 1, true, true));
    zts_completed(&schedule, now - 1, true, 0);
    assert(schedule.exhausted && !zts_due(&schedule, UINT64_MAX, true, true));
    schedule = (zts_schedule_t){0};
    zts_completed(&schedule, UINT64_MAX - 1999, true, 0);
    assert(schedule.exhausted && !zts_due(&schedule, UINT64_MAX, true, true));
    puts("journal source schedule: bounded polls, outages, retries, jitter and counter limits passed");
}
