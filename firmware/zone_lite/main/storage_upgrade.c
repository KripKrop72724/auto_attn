#include "storage_upgrade.h"
#include "upgrade_guard.h"
#include "zkt_journal_compat.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_secure_boot.h"
#include "nvs.h"

#ifndef ZONE_LITE_SEGMENTED_WRITES
#define ZONE_LITE_SEGMENTED_WRITES 0
#endif
#ifndef ZONE_LITE_DIRECT_LEGACY_UPGRADE
#define ZONE_LITE_DIRECT_LEGACY_UPGRADE 0
#endif
#if ZONE_LITE_SEGMENTED_WRITES && ZONE_LITE_DIRECT_LEGACY_UPGRADE
#error "Direct legacy upgrades cannot enable segmented writes"
#endif
const char *storage_upgrade_contract(void)
{
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    return "ZONE_HIKVISION_STORAGE_CONTRACT_V1:SOURCE:READ=2:LANES=7F";
#elif defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
    return "ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1:VERSION=2.6.22:FACTORY_TRIAL=1";
#elif defined(ZONE_LITE_JOURNAL_REPLACEMENT_BRIDGE) && ZONE_LITE_JOURNAL_REPLACEMENT_BRIDGE
    return "ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1:VERSION=" ZJ_BRIDGE_VERSION;
#elif defined(ZONE_LITE_JOURNAL_BRIDGE_IMAGE) && ZONE_LITE_JOURNAL_BRIDGE_IMAGE
    return "ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1";
#elif defined(ZONE_LITE_JOURNAL_WRITER_IMAGE) && ZONE_LITE_JOURNAL_WRITER_IMAGE
#if defined(ZONE_LITE_QUALIFIED_READER_MATRIX) && ZONE_LITE_QUALIFIED_READER_MATRIX
    return ZJ_READER_MATRIX_MARKER;
#else
    return "ZONE_STORAGE_CONTRACT_V4:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:BRIDGE=2.6.17";
#endif
#elif defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
    /* One-shot custody transfer that always returns to the exact signed 2.5.2. */
    return "ZONE_STORAGE_CONTRACT_V2:RECOVERY:READ=2:LANES=3F:BASE=2.5.2";
#elif ZONE_LITE_DIRECT_LEGACY_UPGRADE
    return "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13,2.6.14,2.6.15,2.7.2,2.7.3,2.7.4,2.7.5,2.7.6,2.7.7,2.7.8,2.8.0,2.8.1";
#elif ZONE_LITE_SEGMENTED_WRITES
    return "ZONE_STORAGE_CONTRACT_V1:SEGMENTED:READ=2:LANES=3F:COMPAT=2.5.4";
#else
    return "ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4";
#endif
}
static bool ready, segmented;
static const char *error_code = "STORAGE_UPGRADE_NOT_CHECKED";
static const char *slot_verdict = "";
static const char *slot_other = "";
static bool failed(const char *code) { error_code = code; ready = segmented = false; return false; }

const char *storage_upgrade_rollback_slot(void) { return slot_verdict; }
const char *storage_upgrade_rollback_slot_state(void) { return slot_other; }

#if ZONE_LITE_DIRECT_LEGACY_UPGRADE
static ug_ota_state_t slot_state(const esp_partition_t *partition)
{
    esp_ota_img_states_t state;
    esp_err_t result = esp_ota_get_state_partition(partition, &state);
    if (result == ESP_ERR_NOT_FOUND) return UG_OTA_NO_ENTRY;
    if (result != ESP_OK) return UG_OTA_UNKNOWN;
    switch (state) {
    case ESP_OTA_IMG_NEW: return UG_OTA_NEW;
    case ESP_OTA_IMG_PENDING_VERIFY: return UG_OTA_PENDING_VERIFY;
    case ESP_OTA_IMG_VALID: return UG_OTA_VALID;
    case ESP_OTA_IMG_INVALID: return UG_OTA_INVALID;
    case ESP_OTA_IMG_ABORTED: return UG_OTA_ABORTED;
    case ESP_OTA_IMG_UNDEFINED: return UG_OTA_UNDEFINED;
    default: return UG_OTA_UNKNOWN;
    }
}
#endif

bool storage_upgrade_init(void)
{
    ready = segmented = false;
    slot_verdict = slot_other = "";
    const esp_app_desc_t *running = esp_app_get_description();
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    /* Hikvision starts with its own source lane; it never upgrades ZKT's
     * legacy lanes to segmented writers or fabricates a ZKT predecessor proof.
     * qs_init still validates/replays every existing queue before use. */
    if (!running || strcmp(running->project_name, "zone_lite_hikvision"))
        return failed("STORAGE_APPLICATION_UNKNOWN");
    if (!esp_secure_boot_enabled()) return failed("STORAGE_SECURE_BOOT_REQUIRED");
    error_code = ""; ready = true; return true;
#endif
    if (!running || strcmp(running->project_name, "zone_lite")) return failed("STORAGE_APPLICATION_UNKNOWN");
#if defined(ZONE_LITE_JOURNAL_BRIDGE_IMAGE) && ZONE_LITE_JOURNAL_BRIDGE_IMAGE
    /* The bridge initially preserves the legacy writer. Its separate journal
     * runtime requires secure boot, encrypted NVS and its actual reader proof;
     * no new authority is granted by this packaging marker. */
    if (strcmp(running->version, ZJ_BRIDGE_VERSION) || ZONE_LITE_SEGMENTED_WRITES || ZONE_LITE_DIRECT_LEGACY_UPGRADE)
        return failed("STORAGE_BRIDGE_BUILD_MISMATCH");
    if (!esp_secure_boot_enabled()) return failed("STORAGE_SECURE_BOOT_REQUIRED");
#endif
#if defined(ZONE_LITE_JOURNAL_WRITER_IMAGE) && ZONE_LITE_JOURNAL_WRITER_IMAGE
    if (strcmp(running->version, "2.7.0") || ZONE_LITE_SEGMENTED_WRITES || ZONE_LITE_DIRECT_LEGACY_UPGRADE)
        return failed("STORAGE_WRITER_BUILD_MISMATCH");
    if (!esp_secure_boot_enabled()) return failed("STORAGE_SECURE_BOOT_REQUIRED");
    /* Reader proof and the exact retained rollback slot are checked by the
     * journal runtime before any journal writer can start. */
#endif
#if ZONE_LITE_DIRECT_LEGACY_UPGRADE
    {
        if (strcmp(running->version, UG_DIRECT_VERSION)) return failed("STORAGE_DIRECT_VERSION_MISMATCH");
        if (!esp_secure_boot_enabled()) return failed("STORAGE_SECURE_BOOT_REQUIRED");
        const esp_partition_t *current = esp_ota_get_running_partition();
        const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
        esp_app_desc_t description;
        uint8_t digest[32];
        bool ota_slots = current && previous && current->address != previous->address &&
            (current->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || current->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1) &&
            (previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1);
        if (!ota_slots) return failed("STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED");
        bool qualified = esp_ota_get_partition_description(previous, &description) == ESP_OK &&
            !strcmp(description.project_name, "zone_lite") &&
            esp_partition_get_sha256(previous, digest) == ESP_OK &&
            ug_direct_predecessor_matches(description.version, digest);
        /* 2.8.1: also ready when this image is VALID and the other slot can
         * never be booted (after a rollback or an interrupted download). */
        ug_ota_state_t other = slot_state(previous);
        ug_slot_verdict_t verdict = ug_direct_slot_verdict(slot_state(current), other, qualified);
        slot_verdict = ug_slot_verdict_name(verdict);
        slot_other = ug_ota_state_name(other);
        if (verdict == UG_SLOT_UNQUALIFIED) return failed("STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED");
        ready = true; error_code = ""; return true;
    }
#endif
    if (!ZONE_LITE_SEGMENTED_WRITES && strcmp(running->version, UG_COMPAT_VERSION)) {
        error_code = ""; ready = true; return true;
    }
    if (!esp_secure_boot_enabled()) return failed("STORAGE_SECURE_BOOT_REQUIRED");
    if (ZONE_LITE_SEGMENTED_WRITES && strcmp(running->version, UG_CANDIDATE_VERSION))
        return failed("STORAGE_CANDIDATE_VERSION_MISMATCH");
    ug_capability_t capability = {0};
    nvs_handle_t handle;
    if (nvs_open("queue_upgrade", ZONE_LITE_SEGMENTED_WRITES ? NVS_READONLY : NVS_READWRITE, &handle) != ESP_OK)
        return failed("STORAGE_COMPATIBILITY_NVS_UNAVAILABLE");
    esp_err_t result;
    if (ZONE_LITE_SEGMENTED_WRITES) {
        size_t length = sizeof(capability);
        result = nvs_get_blob(handle, "reader_v1", &capability, &length);
        nvs_close(handle);
        if (result != ESP_OK || length != sizeof(capability) || !ug_capability_valid(&capability))
            return failed("STORAGE_COMPATIBILITY_PROOF_MISSING");
        const esp_partition_t *current = esp_ota_get_running_partition();
        const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
        esp_app_desc_t description;
        uint8_t digest[32];
        bool ota_slots = current && previous && current->address != previous->address &&
            (current->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || current->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1) &&
            (previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1);
        if (!ota_slots || esp_ota_get_partition_description(previous, &description) != ESP_OK ||
            strcmp(description.project_name, "zone_lite") || esp_partition_get_sha256(previous, digest) != ESP_OK ||
            !ug_predecessor_matches(&capability, description.version, digest, true, ota_slots))
            return failed("STORAGE_ROLLBACK_IMAGE_MISMATCH");
        segmented = true;
    } else {
        const esp_partition_t *current = esp_ota_get_running_partition();
        capability.version = UG_CAPABILITY_VERSION;
        capability.queue_format = DQ_CHECKPOINT_VERSION;
        capability.reader_mask = UG_ALL_QUEUE_READERS;
        memcpy(capability.application_version, UG_COMPAT_VERSION, sizeof(UG_COMPAT_VERSION));
        if (!current || esp_partition_get_sha256(current, capability.application_digest) != ESP_OK) {
            nvs_close(handle); return failed("STORAGE_COMPATIBILITY_DIGEST_FAILED");
        }
        capability.crc = dq_crc32(&capability, offsetof(ug_capability_t, crc));
        result = nvs_set_blob(handle, "reader_v1", &capability, sizeof(capability));
        if (result == ESP_OK) result = nvs_commit(handle);
        nvs_close(handle);
        if (result != ESP_OK) return failed("STORAGE_COMPATIBILITY_COMMIT_FAILED");
    }
    ready = true; error_code = ""; return true;
}
bool storage_upgrade_ready(void) { return ready; }
bool storage_upgrade_segmented_writes(void) { return ready && segmented; }
const char *storage_upgrade_error(void) { return error_code; }
