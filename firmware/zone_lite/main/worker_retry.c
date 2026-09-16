#include "worker_retry.h"
bool worker_retry_allow(worker_retry_t *s, uint32_t now)
{
    if (!s) return false;
    if (s->count == 3 && (uint32_t)(now - s->attempts[s->next]) < 600000U) return false;
    s->attempts[s->next] = now;
    s->next = (s->next + 1U) % 3U;
    if (s->count < 3) ++s->count;
    if (s->total != UINT32_MAX) ++s->total;
    return true;
}
