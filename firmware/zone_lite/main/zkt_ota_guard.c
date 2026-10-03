#include "zkt_ota_guard.h"
#include "zkt_storage_owner.h"
#include "queue_store.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
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
