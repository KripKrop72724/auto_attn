#include "storage_recovery.h"

#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE

#include "storage_recovery_core.h"

#include <dirent.h>
#include <errno.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_spiffs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"

#include "add_connector.h"
#include "durable_queue.h"
#include "legacy_queue.h"
#include "ota_manager.h"
#include "upgrade_guard.h"
#include "zone_config.h"
#include "zone_storage_paths.h"

#define SR_BLOCKED_PATH ZONE_STORAGE_BASE "/blocked_identity.jsonl"
#define SR_BLOCKED_BACKUP_PATH ZONE_STORAGE_BASE "/blocked_recovery.bak"
#define SR_BLOCKED_TMP_PATH ZONE_STORAGE_BASE "/blocked_recovery.tmp"
#define SR_ACKED_PATH ZONE_STORAGE_BASE "/acked_uids.txt"
#define SR_QUEUE "blocked_legacy"
#define SR_ADD_WAIT_MS (20LL * 60 * 1000)
/* Bounded below the OTA manager's recovery deadline so the run always ends
 * by itself and the rollback report is sent from a settled state. */
#define SR_RUN_LIMIT_MS (8LL * 60 * 60 * 1000)
#define SR_MAX_CONSECUTIVE_FAILURES 40U
#define SR_MAX_BACKOFF_SECONDS 15U
#define SR_INVENTORY_MAX_FILES 40U

static const char *TAG = "storage_recovery";

/* The only connectors this image may change. ADD scope enforces the same set;
 * the device repeats the check so a misrouted image cannot alter storage. */
static const struct {
    const char *connector_id;
    uint8_t mac[6];
} k_targets[] = {
    {"bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", {0xe0, 0x72, 0xa1, 0xd7, 0x05, 0xc4}},
    {"233dac02-eb1b-4598-a876-e3a7b1ecfd54", {0xe0, 0x72, 0xa1, 0xd5, 0x08, 0xa0}},
};

static atomic_int s_state = STORAGE_RECOVERY_IDLE;
static atomic_bool s_stop_requested;
static char s_code[48] = "STORAGE_RECOVERY_NOT_STARTED";
static bool s_mounted;

typedef struct {
    int64_t deadline_ms;
    uint32_t consecutive_failures;
} sr_glue_t;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static void log_line(void *context, const char *level, const char *code, const char *message)
{
    (void)context;
    ESP_LOGI(TAG, "%s %s", code, message);
    (void)add_connector_log(level, "storage", code, message);
}

static void finish(const char *code)
{
    strlcpy(s_code, code, sizeof(s_code));
    atomic_store(&s_state, STORAGE_RECOVERY_DONE);
}

bool storage_recovery_mount(void)
{
    if (s_mounted) return true;
    esp_vfs_spiffs_conf_t conf = {
        .base_path = ZONE_STORAGE_BASE,
        .partition_label = NULL,
        .max_files = 16,
        // Never format: the retained rows are the only reason this image exists.
        .format_if_mount_failed = false,
    };
    s_mounted = esp_vfs_spiffs_register(&conf) == ESP_OK;
    if (!s_mounted) ESP_LOGE(TAG, "Could not mount retained storage; recovery will refuse");
    return s_mounted;
}

static const char *check_preconditions(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    if (!app || strcmp(app->project_name, "zone_lite") || strcmp(app->version, STORAGE_RECOVERY_VERSION))
        return "STORAGE_RECOVERY_BUILD_MISMATCH";
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) return "STORAGE_RECOVERY_IDENTITY";
    const char *connector = zone_config_get()->connector_id;
    bool target = false;
    for (size_t i = 0; i < sizeof(k_targets) / sizeof(k_targets[0]); ++i)
        target = target || (!strcmp(connector, k_targets[i].connector_id) && !memcmp(mac, k_targets[i].mac, 6));
    if (!target) return "STORAGE_RECOVERY_TARGET_MISMATCH";
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
    bool slots = running && previous && running->address != previous->address &&
        (running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1) &&
        (previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || previous->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1);
    if (!slots) return "STORAGE_RECOVERY_SLOT_LAYOUT";
    esp_ota_img_states_t state;
    // Only an unconfirmed image returns to its predecessor if anything resets.
    if (esp_ota_get_state_partition(running, &state) != ESP_OK || state != ESP_OTA_IMG_PENDING_VERIFY)
        return "STORAGE_RECOVERY_NOT_PENDING_VERIFY";
    esp_app_desc_t description;
    uint8_t digest[32];
    if (esp_ota_get_partition_description(previous, &description) != ESP_OK ||
        strcmp(description.project_name, "zone_lite") || strcmp(description.version, "2.5.2") ||
        esp_partition_get_sha256(previous, digest) != ESP_OK || !ug_direct_predecessor_matches("2.5.2", digest))
        return "STORAGE_RECOVERY_ROLLBACK_IMAGE";
    if (esp_ota_get_state_partition(previous, &state) != ESP_OK || state != ESP_OTA_IMG_VALID)
        return "STORAGE_RECOVERY_ROLLBACK_STATE";
    if (!ota_manager_storage_recovery_authorized()) return "STORAGE_RECOVERY_NOT_AUTHORIZED";
    return NULL;
}

static void log_inventory(const char *code)
{
    size_t total = 0, used = 0;
    char message[200];
    if (esp_spiffs_info(NULL, &total, &used) == ESP_OK)
        snprintf(message, sizeof(message), "Retained storage uses %u of %u bytes", (unsigned)used, (unsigned)total);
    else
        snprintf(message, sizeof(message), "Retained storage usage could not be measured");
    log_line(NULL, "INFO", code, message);
    DIR *directory = opendir(ZONE_STORAGE_BASE);
    if (!directory) {
        log_line(NULL, "WARN", code, "Retained storage directory could not be listed");
        return;
    }
    unsigned listed = 0;
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL && listed < SR_INVENTORY_MAX_FILES) {
        char path[300];
        struct stat st;
        snprintf(path, sizeof(path), "%s/%s", ZONE_STORAGE_BASE, entry->d_name);
        if (stat(path, &st) != 0) continue;
        snprintf(message, sizeof(message), "File %.96s is %ld bytes", entry->d_name, (long)st.st_size);
        log_line(NULL, "INFO", code, message);
        listed++;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    closedir(directory);
}

static sr_send_t send_row(void *context, const char *generation, const char *record_id,
                          const char *bytes, size_t length, const char *serial, const char *reason)
{
    sr_glue_t *glue = context;
    if (atomic_load(&s_stop_requested) || now_ms() > glue->deadline_ms) return SR_SEND_STOP;
    if (!add_connector_is_connected()) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        return SR_SEND_RETRY;
    }
    if (add_connector_transfer_queue_evidence(SR_QUEUE, generation, record_id, bytes, length, serial, reason)) {
        glue->consecutive_failures = 0;
        return SR_SEND_ACKED;
    }
    if (++glue->consecutive_failures >= SR_MAX_CONSECUTIVE_FAILURES) return SR_SEND_STOP;
    uint32_t backoff = glue->consecutive_failures < SR_MAX_BACKOFF_SECONDS
        ? glue->consecutive_failures : SR_MAX_BACKOFF_SECONDS;
    vTaskDelay(pdMS_TO_TICKS(backoff * 1000U));
    return SR_SEND_RETRY;
}

/* Mirror lq_reclaim: clear the 2.6.x blocked-lane cursor before unlinking so a
 * later image can never apply an old offset to a different file. */
static bool before_retire(void *context)
{
    (void)context;
    atomic_store(&s_state, STORAGE_RECOVERY_RETIRING);
    nvs_handle_t handle;
    esp_err_t err = nvs_open("legacy_queues", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) return true;
    if (err != ESP_OK) return false;
    lq_checkpoint_t checkpoint = {0};
    size_t size = sizeof(checkpoint);
    err = nvs_get_blob(handle, "blocked", &checkpoint, &size);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) return true;
    bool valid = err == ESP_OK && size == sizeof(checkpoint) &&
        (checkpoint.version == 1 || checkpoint.version == 2) && checkpoint.generation &&
        checkpoint.generation != UINT32_MAX &&
        checkpoint.crc == dq_crc32(&checkpoint, offsetof(lq_checkpoint_t, crc));
    if (!valid) {
        // An already-corrupt cursor is held by later images regardless of this file.
        log_line(NULL, "WARN", "STORAGE_RECOVERY_CURSOR_UNCHANGED",
                 "The existing blocked-lane cursor is not valid and was left unchanged");
        return true;
    }
    lq_checkpoint_t next = checkpoint;
    next.version = 1;
    next.offset = 0;
    next.prefix_crc = 0;
    next.generation++;
    next.crc = dq_crc32(&next, offsetof(lq_checkpoint_t, crc));
    if (nvs_open("legacy_queues", NVS_READWRITE, &handle) != ESP_OK) return false;
    err = nvs_set_blob(handle, "blocked", &next, sizeof(next));
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    return err == ESP_OK;
}

void storage_recovery_run(void)
{
    atomic_store(&s_state, STORAGE_RECOVERY_RUNNING);
    const char *refusal = check_preconditions();
    if (!refusal && !storage_recovery_mount()) refusal = "STORAGE_RECOVERY_MOUNT";
    int64_t started = now_ms();
    while (!add_connector_is_connected()) {
        if (atomic_load(&s_stop_requested) || now_ms() - started > SR_ADD_WAIT_MS) {
            finish(refusal ? refusal : "STORAGE_RECOVERY_ADD_UNAVAILABLE");
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (refusal) {
        log_line(NULL, "ERROR", refusal, "Storage recovery refused before any change; returning to 2.5.2");
        finish(refusal);
        return;
    }
    log_line(NULL, "INFO", "STORAGE_RECOVERY_STARTED",
             "One-shot recovery started; blocked rows move to ADD custody before any local removal");
    log_inventory("STORAGE_RECOVERY_INVENTORY_BEFORE");
    sr_glue_t glue = {.deadline_ms = now_ms() + SR_RUN_LIMIT_MS};
    sr_ports_t ports = {.context = &glue, .send = send_row, .log = log_line, .before_retire = before_retire};
    const char *sources[] = {SR_BLOCKED_PATH, SR_BLOCKED_BACKUP_PATH, SR_BLOCKED_TMP_PATH};
    sr_outcome_t outcome;
    sr_transfer_and_retire(sources, sizeof(sources) / sizeof(sources[0]), SR_ACKED_PATH, &ports, &outcome);
    char message[220];
    snprintf(message, sizeof(message),
             "Recovery %s: files %lu rows %lu malformed %lu bytes %llu uids %lu recorded %lu retries %lu",
             sr_result_name(outcome.result), (unsigned long)outcome.files, (unsigned long)outcome.records,
             (unsigned long)outcome.malformed, (unsigned long long)outcome.bytes, (unsigned long)outcome.uids,
             (unsigned long)outcome.uids_appended, (unsigned long)outcome.retries);
    bool clean = outcome.result == SR_COMPLETE || outcome.result == SR_NOTHING_TO_DO;
    log_line(NULL, clean ? "INFO" : "WARN", outcome.code, message);
    log_inventory("STORAGE_RECOVERY_INVENTORY_AFTER");
    finish(outcome.code);
}

storage_recovery_state_t storage_recovery_state(void)
{
    return (storage_recovery_state_t)atomic_load(&s_state);
}

const char *storage_recovery_code(void) { return s_code; }

void storage_recovery_request_stop(void) { atomic_store(&s_stop_requested, true); }

#endif
