#include "upgrade_guard.h"

bool ug_capability_valid(const ug_capability_t *c)
{
    if (!c || c->version != UG_CAPABILITY_VERSION || c->queue_format != DQ_CHECKPOINT_VERSION ||
        c->reader_mask != UG_ALL_QUEUE_READERS ||
        !memchr(c->application_version, 0, sizeof(c->application_version)) ||
        strcmp(c->application_version, UG_COMPAT_VERSION) ||
        c->crc != dq_crc32(c, offsetof(ug_capability_t, crc))) return false;
    uint8_t nonzero = 0;
    for (unsigned i = 0; i < sizeof(c->application_digest); ++i) nonzero |= c->application_digest[i];
    return nonzero != 0;
}
bool ug_predecessor_matches(const ug_capability_t *c, const char *version,
                            const uint8_t digest[32], bool secure_boot, bool previous_ota_slot)
{
    return secure_boot && previous_ota_slot && version && digest && ug_capability_valid(c) &&
        !strcmp(version, c->application_version) && !memcmp(digest, c->application_digest, 32);
}
