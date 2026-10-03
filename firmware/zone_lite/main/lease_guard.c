#include "lease_guard.h"
#define LG_MIN_TIME 1767225600LL

static void recover(lg_port_t p, uint16_t uid)
{
    // Failed/uncertain elevation is still an obligation to verify revocation.
    // Keep the durable lease when either terminal verification or clearing fails.
    if (p.revoke(p.context, uid) && p.verify(p.context, uid, 0))
        (void)p.persist(p.context, uid, 0, false);
}

lg_result_t lg_grant(lg_port_t p, uint16_t uid, unsigned seconds, int64_t absolute)
{
    if (!p.now || !p.persist || !p.elevate || !p.verify || !p.revoke || !uid || !seconds || seconds > 600)
        return LG_TIME;
    int64_t before = p.now(p.context);
    if (before < LG_MIN_TIME || before > INT64_MAX - seconds) return LG_TIME;
    int64_t deadline = absolute > 0 ? absolute : before + seconds;
    if (deadline <= before) return LG_EXPIRED;
    if (deadline - before > 600) return LG_TIME;
    if (!p.persist(p.context, uid, deadline, true)) return LG_STORAGE;
    if (!p.elevate(p.context, uid) || !p.verify(p.context, uid, 14)) {
        recover(p, uid); return LG_TERMINAL;
    }
    int64_t verified = p.now(p.context);
    if (verified < before || verified > INT64_MAX - seconds) {
        recover(p, uid); return LG_TIME;
    }
    deadline = absolute > 0 ? absolute : verified + seconds;
    if (deadline <= verified) { recover(p, uid); return LG_EXPIRED; }
    if (!p.persist(p.context, uid, deadline, true)) {
        recover(p, uid); return LG_STORAGE;
    }
    return LG_OK;
}

bool lg_watch_arm(lg_watch_t *watch, uint16_t uid, int64_t deadline,
                  int64_t now, int64_t uptime)
{
    if (!watch) return false;
    *watch = (lg_watch_t){.uid = uid, .deadline_epoch = deadline, .due = true};
    if (!uid || now < LG_MIN_TIME || deadline <= now || deadline - now > 600 ||
        uptime < 0 || uptime > INT64_MAX - (deadline - now) * 1000) return false;
    *watch = (lg_watch_t){.uid = uid, .deadline_epoch = deadline,
        .deadline_ms = uptime + (deadline - now) * 1000,
        .last_epoch = now, .last_ms = uptime, .armed = true};
    return true;
}

bool lg_watch_due(lg_watch_t *watch, uint16_t uid, int64_t deadline,
                  int64_t now, int64_t uptime)
{
    if (!watch) return true;
    if (!watch->armed || watch->due || !uid || uid != watch->uid ||
        deadline != watch->deadline_epoch || now < LG_MIN_TIME ||
        now < watch->last_epoch || uptime < watch->last_ms ||
        now >= deadline || uptime >= watch->deadline_ms) {
        watch->due = true;
        return true;
    }
    watch->last_epoch = now;
    watch->last_ms = uptime;
    return false;
}
