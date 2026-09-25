#include "upgrade_guard.h"

bool ug_direct_predecessor_matches(const char *version, const uint8_t digest[32])
{
    if (!version || !digest) return false;
    /* Exact ESP application hashes of the immutable signed ADD releases. */
    static const char baseline_2412[] =
        "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589";
    static const char baseline_252[] =
        "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b";
    static const char hil_266[] =
        "69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e";
    static const char hil_267[] =
        "3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c";
    const char *expected = !strcmp(version, "2.4.12") ? baseline_2412 :
        !strcmp(version, "2.5.2") ? baseline_252 :
        !strcmp(version, "2.6.6") ? hil_266 :
        !strcmp(version, "2.6.7") ? hil_267 : NULL;
    if (!expected) return false;
    static const char hex[] = "0123456789abcdef";
    for (unsigned i = 0; i < 32; ++i) {
        const char *high = strchr(hex, expected[2 * i]);
        const char *low = strchr(hex, expected[2 * i + 1]);
        if (!high || !low || digest[i] != (uint8_t)(((high - hex) << 4) | (low - hex)))
            return false;
    }
    return true;
}

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
