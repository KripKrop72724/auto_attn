#include "zkt_ota_guard.h"
#include "zkt_storage_owner.h"
#include "queue_store.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
#include "esp_spiffs.h"
#include "storage_exception.h"
#include "storage_repair.h"
#endif
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include <dirent.h>
#include <errno.h>
#include <string.h>

static uint64_t pending_ticket;

static const char *legacy_evidence(void)
{
    /* Legacy versions cannot start the journal owner. Even an empty/orphaned
     * namespace is evidence to investigate, never permission to downgrade. */
    nvs_handle_t handle;
    esp_err_t result = nvs_open("zkt_journal", NVS_READONLY, &handle);
    if (result == ESP_OK) {
        nvs_close(handle);
        return "JOURNAL_LEGACY_EVIDENCE_PRESENT";
    }
    if (result != ESP_ERR_NVS_NOT_FOUND) return "JOURNAL_READER_PROOF_UNAVAILABLE";
#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
    /* ZKT 2.7.7 (M5): with the VFS unmounted this boot (the boot repair kept
     * it off, or it would not mount) the queue store never started, so this
     * comes before its lock: the boot repair's own survey listed every
     * object header, hidden ones too. This image never creates a journal
     * file, so that survey stays true for the boot. No survey, no OTA. */
    if (!esp_spiffs_mounted(NULL)) {
        bool journal_names = false;
        if (!storage_repair_survey(&journal_names)) return "JOURNAL_OTA_STORAGE_UNAVAILABLE";
        return journal_names ? "JOURNAL_LEGACY_EVIDENCE_PRESENT" : NULL;
    }
#endif
    if (!qs_local_read_begin()) return "JOURNAL_OTA_STORAGE_UNAVAILABLE";
    const char *error = NULL;
    DIR *directory = opendir(ZJ_DEVICE_DIRECTORY);
    if (!directory) error = "JOURNAL_OTA_STORAGE_UNAVAILABLE";
    else {
        unsigned count = 0;
        for (;;) {
            errno = 0;
            struct dirent *entry = readdir(directory);
            if (!entry) {
                if (errno) error = "JOURNAL_OTA_STORAGE_UNAVAILABLE";
                break;
            }
            if (!strncmp(entry->d_name, ZJ_DEVICE_BASENAME, sizeof(ZJ_DEVICE_BASENAME) - 1)) {
                error = "JOURNAL_LEGACY_EVIDENCE_PRESENT";
                break;
            }
            if (++count > 1024U) { error = "JOURNAL_OTA_DIRECTORY_LIMIT"; break; }
        }
        if (closedir(directory)) error = "JOURNAL_OTA_STORAGE_UNAVAILABLE";
    }
    qs_local_end(true, 0); /* Read failure must not become attendance-write loss. */
    return error;
}

const char *zj_ota_before_download(uint32_t address, uint32_t size, const char *version)
{
    if (!version || !version[0] || strnlen(version, 32) >= 32) return "JOURNAL_OTA_TARGET_UNSUPPORTED";
    const esp_app_desc_t *app = esp_app_get_description();
    if (!app || strcmp(app->project_name, "zone_lite")) return "JOURNAL_OTA_IMAGE_UNKNOWN";
    if (!strcmp(app->version, ZJ_WRITER_VERSION)) return "JOURNAL_ROLLBACK_SLOT_PROTECTED";
#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
    /* ZKT 2.7.7, the OTA floor (critique 1): once this device's redo area or
     * reclaim journal holds anything (or the image confirmed under the storage
     * exception, or the store stays unmounted), an image that cannot read the
     * redo area would mount a store whose custody may be in a record and
     * erase it. Checked before any other evidence, the survey fallback
     * included. */
    if (storage_repair_ota_floor() && !sx_version_redo_aware(version)) return "STORAGE_REDO_FLOOR";
#endif
    if (strcmp(app->version, ZJ_BRIDGE_VERSION)) {
        if (!strcmp(version, ZJ_WRITER_VERSION)) return "JOURNAL_OTA_BRIDGE_REQUIRED";
        return legacy_evidence();
    }
    if (strcmp(version, ZJ_WRITER_VERSION)) return "JOURNAL_OTA_TARGET_UNSUPPORTED";

    /* If a previous timeout could not release its ticket, never queue another
     * check or reuse its result for a different assignment. Accepted owner
     * work still completes; no task or held lock is forcibly restarted. */
    if (pending_ticket) {
        if (!zj_owner_abandon(pending_ticket)) return "JOURNAL_OTA_CHECK_PENDING";
        pending_ticket = 0;
    }
    zj_request_t request = {.operation = ZJ_OTA_CHECK,
        .input.ota = {.address = address, .size = size}};
    memcpy(request.input.ota.version, version, strlen(version) + 1);
    if (!zj_owner_submit(&request, &pending_ticket) || !pending_ticket) {
        pending_ticket = 0;
        return "JOURNAL_OTA_CHECK_UNAVAILABLE";
    }
    int64_t deadline = esp_timer_get_time() + 5000000;
    do {
        zj_reply_t reply;
        bool complete = false;
        if (zj_owner_poll(pending_ticket, &reply, &complete) && complete) {
            pending_ticket = 0;
            if (reply.result == ZJ_OK && reply.compatibility == ZJ_COMPAT_OK) return NULL;
            return reply.compatibility == ZJ_COMPAT_OK ? "JOURNAL_OTA_CHECK_FAILED" : zj_compat_error(reply.compatibility);
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    } while (esp_timer_get_time() < deadline);
    if (zj_owner_abandon(pending_ticket)) pending_ticket = 0;
    return "JOURNAL_OTA_CHECK_TIMEOUT";
}

#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
/* ZKT 2.7.7 (M5): the confirm-first exception's OTA term, read once per boot.
 * A full directory listing under the queue store's lock every five seconds
 * would compete with live capture; nothing this image does can change the
 * answer (it creates no journal file and no journal namespace), so a
 * definite answer is kept and only a busy store is asked again, at most once
 * a minute. */
bool zj_ota_precheck_cached(void)
{
    static bool known, passed;
    static int64_t retry_at_us;
    if (known) return passed;
    int64_t now = esp_timer_get_time();
    if (now < retry_at_us) return false;
    const esp_app_desc_t *app = esp_app_get_description();
    const char *error = app ? zj_ota_before_download(0, 0, app->version) : "JOURNAL_OTA_IMAGE_UNKNOWN";
    if (error && !strcmp(error, "JOURNAL_OTA_STORAGE_UNAVAILABLE")) {
        retry_at_us = now + 60LL * 1000000LL;
        return false;
    }
    known = true;
    passed = error == NULL;
    return passed;
}
#endif
