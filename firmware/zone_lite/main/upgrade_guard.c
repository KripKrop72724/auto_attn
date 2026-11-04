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
    static const char hil_268[] =
        "fecc5df0223a3c7c8b019a445bcf829bc8d09dd93e920aecc8446908fadeadc6";
    static const char hil_269[] =
        "ad71339fef6926b21a21a05c1e1c4e30a936e0df5be6160871c7283841ad91b8";
    static const char hil_2610[] =
        "a95370b1487d9c1454dfb932c41432451f24ef69d0abc1292a5c0262c17c243c";
    static const char hil_2612[] =
        "3f9028126c8dde9816486783a27a9802ed41179cd9f8caf9935deb79a0057fc1";
    static const char hil_2613[] =
        "c7be4171153333563f9ed6e43f91376d42dfb9d3ca2faefda2ca6d1ac87b2def";
    static const char hil_2614[] =
        "7d0381fc68a01b34ab6989dfed2b5a1a93f39c293bfd0b7db1734b0871fa6469";
    static const char fleet_2615[] =
        "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6";
    /* 2.7.2: the image SLICTOWER-3FL runs, so 3FL upgrades in place. */
    static const char platform_272[] =
        "e908496e0695d8c29e96a6f2db3840060eeab526c4012e29a3deef826f7c8eea";
    /* 2.7.3: the panic-trace HIL image, so its devices upgrade in place. */
    static const char platform_273[] =
        "98855248bb4325103b5253ab4b15f4e939adf3ace89c208d463b09ec9ca21a6a";
    /* 2.7.4: the storage-inventory HIL image, so its devices upgrade in place. */
    static const char platform_274[] =
        "3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac";
    /* 2.7.5: the boot storage repair HIL image, so its devices upgrade in place. */
    static const char platform_275[] =
        "430e2daf29dd99ff006a476ff584e4201493d661cee67e4b4f9ba15545b17a40";
    /* 2.7.6: the last-resort reformat HIL image, so its devices upgrade in place. */
    static const char platform_276[] =
        "9d7050768f1cb716e9bc11f9b7b4d20b423b3eccbe1531809130457998e96f0c";
    /* 2.7.7: the confirm-first exception HIL image (Peshawar-02, 13FL, Swat),
     * so its devices upgrade in place. */
    static const char platform_277[] =
        "6d9ee63dd7e5917c83a3ada14685405c0081073d593a7901487eb2a3fd2c3bc1";
    /* 2.7.8: the production image (AVAILABLE 29 Oct 2026), so the whole
     * fleet upgrades in place. */
    static const char platform_278[] =
        "b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e";
    /* 2.8.0: the secure-pull image (HIL_ONLY 30 Oct 2026), so its devices
     * upgrade in place. */
    static const char platform_280[] =
        "21ac81b292b463cf7fa1bf0d6ce364a49493951836ad962a19dcae049f40ed76";
    /* 2.8.1: the rollback-slot gate image (AVAILABLE 4 Nov 2026), so its
     * devices upgrade in place. */
    static const char platform_281[] =
        "21238fa36df844642545e8c2f6bcfec03be37470890b776a600c39c0cdd1f5f0";
    const char *expected = !strcmp(version, "2.4.12") ? baseline_2412 :
        !strcmp(version, "2.5.2") ? baseline_252 :
        !strcmp(version, "2.6.6") ? hil_266 :
        !strcmp(version, "2.6.7") ? hil_267 :
        !strcmp(version, "2.6.8") ? hil_268 :
        !strcmp(version, "2.6.9") ? hil_269 :
        !strcmp(version, "2.6.10") ? hil_2610 :
        !strcmp(version, "2.6.12") ? hil_2612 :
        !strcmp(version, "2.6.13") ? hil_2613 :
        !strcmp(version, "2.6.14") ? hil_2614 :
        !strcmp(version, "2.6.15") ? fleet_2615 :
        !strcmp(version, "2.7.2") ? platform_272 :
        !strcmp(version, "2.7.3") ? platform_273 :
        !strcmp(version, "2.7.4") ? platform_274 :
        !strcmp(version, "2.7.5") ? platform_275 :
        !strcmp(version, "2.7.6") ? platform_276 :
        !strcmp(version, "2.7.7") ? platform_277 :
        !strcmp(version, "2.7.8") ? platform_278 :
        !strcmp(version, "2.8.0") ? platform_280 :
        !strcmp(version, "2.8.1") ? platform_281 : NULL;
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

ug_slot_verdict_t ug_direct_slot_verdict(ug_ota_state_t running, ug_ota_state_t other, bool qualified)
{
    if (qualified) return UG_SLOT_PREDECESSOR;
    /* An unconfirmed image can still roll back to the other slot. */
    if (running != UG_OTA_VALID) return UG_SLOT_UNQUALIFIED;
    /* Entries ESP-IDF 5.5.3's bootloader never selects: it aborts every
     * PENDING_VERIFY entry before selection and skips INVALID and ABORTED;
     * esp_ota_begin erases the inactive entry before the first write. */
    switch (other) {
    case UG_OTA_NO_ENTRY: return UG_SLOT_NONE_NO_ENTRY;
    case UG_OTA_INVALID: return UG_SLOT_NONE_INVALID;
    case UG_OTA_ABORTED: return UG_SLOT_NONE_ABORTED;
    case UG_OTA_PENDING_VERIFY: return UG_SLOT_NONE_PENDING_VERIFY;
    default: return UG_SLOT_UNQUALIFIED; /* NEW, VALID, UNDEFINED, unknown: selectable */
    }
}

const char *ug_slot_verdict_name(ug_slot_verdict_t verdict)
{
    switch (verdict) {
    case UG_SLOT_PREDECESSOR: return "PREDECESSOR";
    case UG_SLOT_NONE_NO_ENTRY: return "NONE_NO_ENTRY";
    case UG_SLOT_NONE_INVALID: return "NONE_INVALID";
    case UG_SLOT_NONE_ABORTED: return "NONE_ABORTED";
    case UG_SLOT_NONE_PENDING_VERIFY: return "NONE_PENDING_VERIFY";
    case UG_SLOT_UNQUALIFIED: break;
    }
    return "UNQUALIFIED";
}

const char *ug_ota_state_name(ug_ota_state_t state)
{
    switch (state) {
    case UG_OTA_NO_ENTRY: return "NO_ENTRY";
    case UG_OTA_NEW: return "NEW";
    case UG_OTA_PENDING_VERIFY: return "PENDING_VERIFY";
    case UG_OTA_VALID: return "VALID";
    case UG_OTA_INVALID: return "INVALID";
    case UG_OTA_ABORTED: return "ABORTED";
    case UG_OTA_UNDEFINED: return "UNDEFINED";
    case UG_OTA_UNKNOWN: break;
    }
    return "UNKNOWN";
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
