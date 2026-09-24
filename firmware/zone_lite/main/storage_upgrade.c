#include "storage_upgrade.h"
#include "upgrade_guard.h"
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
#elif ZONE_LITE_DIRECT_LEGACY_UPGRADE
    return "ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2";
#elif ZONE_LITE_SEGMENTED_WRITES
    return "ZONE_STORAGE_CONTRACT_V1:SEGMENTED:READ=2:LANES=3F:COMPAT=2.5.4";
#else
    return "ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4";
#endif
}
static bool ready, segmented;
static const char *error_code = "STORAGE_UPGRADE_NOT_CHECKED";
static bool failed(const char *code) { error_code = code; ready = segmented = false; return false; }

bool storage_upgrade_init(void)
{
    ready = segmented = false;
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
        if (!ota_slots || esp_ota_get_partition_description(previous, &description) != ESP_OK ||
            strcmp(description.project_name, "zone_lite") ||
            esp_partition_get_sha256(previous, digest) != ESP_OK ||
            !ug_direct_predecessor_matches(description.version, digest))
            return failed("STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED");
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
