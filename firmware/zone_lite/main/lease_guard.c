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
