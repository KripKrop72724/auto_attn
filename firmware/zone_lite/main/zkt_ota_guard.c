#include "zkt_ota_guard.h"
#include "zkt_storage_owner.h"
#include "queue_store.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
#include "esp_spiffs.h"
#include "storage_exception.h"
#include "storage_repair.h"
#include "zkt_legacy_evidence.h"
#include "cJSON.h"
#include "esp_log.h"
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#endif
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include <dirent.h>
#include <errno.h>
#include <string.h>

static uint64_t pending_ticket;

#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
/* ZKT 2.7.7: bridge-era leftovers (zkt_legacy_evidence.h). The OTA task
 * builds the inventory; the heartbeat reads the last one. */
static const char *TAG = "zkt_ota_guard";
static portMUX_TYPE s_legacy_lock = portMUX_INITIALIZER_UNLOCKED;
static zle_inventory_t s_legacy;
static zle_verdict_t s_legacy_verdict;
static bool s_legacy_known;

static void legacy_nvs(zle_inventory_t *inventory)
{
    nvs_handle_t handle;
    esp_err_t result = nvs_open("zkt_journal", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return;
    inventory->namespace_present = true;
    if (result != ESP_OK) { inventory->root = ZLE_ROOT_UNREADABLE; return; }
    /* The root holds the journal key: read, classify, erase. */
    uint8_t root[ZLE_ROOT_BYTES + 1];
    size_t length = sizeof(root);
    result = nvs_get_blob(handle, "root", root, &length);
    if (result == ESP_ERR_NVS_NOT_FOUND) inventory->root = ZLE_ROOT_ABSENT;
    else if (result == ESP_ERR_NVS_INVALID_LENGTH) inventory->root = ZLE_ROOT_INVALID;
    else if (result != ESP_OK) inventory->root = ZLE_ROOT_UNREADABLE;
    else inventory->root = zle_root_classify(root, length);
    volatile uint8_t *wipe = root;
    for (size_t i = 0; i < sizeof(root); ++i) wipe[i] = 0;
    nvs_iterator_t it = NULL;
    result = nvs_entry_find_in_handle(handle, NVS_TYPE_ANY, &it);
    unsigned walked = 0;
    while (result == ESP_OK && walked++ < 64U) {
        nvs_entry_info_t info;
        if (nvs_entry_info(it, &info) != ESP_OK) { result = ESP_FAIL; break; }
        zle_note_key(inventory, info.key);
        result = nvs_entry_next(&it);
    }
    nvs_release_iterator(it);
    inventory->namespace_listed = result == ESP_ERR_NVS_NOT_FOUND;
    nvs_close(handle);
}

#define LEGACY_NAME_BYTES 40U

/* Every zktj* name of the store, each read in full (at most ZLE_FILES_MAX,
 * each at most one segment). Only the directory walk holds the queue store's
 * read lock, as before 2.7.7: live appends wait at most 1 s for it (review of
 * 26 October, D6/N1). Sizes and contents are read after it is released -
 * nothing writes a journal file while this image's journal runtime is
 * disabled - and only when they can change the verdict (an ADD root, an
 * unknown key or a running journal already refuses). Returns the pre-2.7.7
 * errors for a store that cannot be listed. */
static const char *legacy_files(zle_inventory_t *inventory)
{
    char (*names)[LEGACY_NAME_BYTES] = calloc(ZLE_FILES_MAX, LEGACY_NAME_BYTES);
    uint8_t *buffer = names ? malloc(1024) : NULL;
    if (!buffer || !qs_local_read_begin()) {
        free(buffer);
        free(names);
        return "JOURNAL_OTA_STORAGE_UNAVAILABLE";
    }
    int64_t started = esp_timer_get_time();
    const char *error = NULL;
    uint32_t kept = 0, seen = 0;
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
            if (++count > 1024U) { error = "JOURNAL_OTA_DIRECTORY_LIMIT"; break; }
            if (strncmp(entry->d_name, ZJ_DEVICE_BASENAME, sizeof(ZJ_DEVICE_BASENAME) - 1)) continue;
            ++seen;
            /* Past the limit, or a name too long to open: counted, never read
             * (FILE_LIMIT or FILE_UNREADABLE refuses). */
            if (kept < ZLE_FILES_MAX && strlen(entry->d_name) < LEGACY_NAME_BYTES)
                strlcpy(names[kept++], entry->d_name, LEGACY_NAME_BYTES);
            else zle_note_file(inventory, entry->d_name, 0, false, 0);
        }
        if (closedir(directory)) error = "JOURNAL_OTA_STORAGE_UNAVAILABLE";
    }
    qs_local_end(true, 0); /* Read failure must not become attendance-write loss. */
    int64_t walked = esp_timer_get_time();
    bool read = !error && zle_contents_matter(inventory, seen);
    for (uint32_t i = 0; !error && i < kept; ++i) {
        char path[sizeof(ZJ_DEVICE_DIRECTORY) + LEGACY_NAME_BYTES];
        struct stat st;
        uint64_t size = 0, read_bytes = 0;
        bool read_ok = false;
        snprintf(path, sizeof(path), "%s/%s", ZJ_DEVICE_DIRECTORY, names[i]);
        if (!stat(path, &st) && st.st_size >= 0) {
            size = (uint64_t)st.st_size;
            if (!read) {
                /* The verdict refuses whatever the file holds: sized, not read. */
                read_ok = true;
                read_bytes = size;
            } else if (size <= ZLE_FILE_BYTES_MAX) {
                FILE *file = fopen(path, "rb");
                read_ok = file != NULL;
                while (read_ok) {
                    size_t got = fread(buffer, 1, 1024, file);
                    read_bytes += got;
                    if (got < 1024) { read_ok = !ferror(file); break; }
                    if (read_bytes > ZLE_FILE_BYTES_MAX) break;
                }
                if (file && fclose(file)) read_ok = false;
            }
        }
        zle_note_file(inventory, names[i], size, read_ok, read_bytes);
    }
    free(buffer);
    free(names);
    inventory->store_listed = error == NULL;
    ESP_LOGI(TAG, "journal evidence: %lu zktj name(s), walk %lld ms under the queue lock, %s %lld ms",
             (unsigned long)seen, (long long)((walked - started) / 1000), read ? "read" : "sized",
             (long long)((esp_timer_get_time() - walked) / 1000));
    return error;
}

static const char *legacy_evidence(void)
{
    zle_inventory_t inventory = {0};
    /* zkt_journal_runtime.c's mode(): only the bridge and writer versions
     * start the journal; every other image's runtime is DISABLED. */
    const esp_app_desc_t *app = esp_app_get_description();
    inventory.runtime_disabled = app && !strcmp(app->project_name, "zone_lite") &&
        strcmp(app->version, ZJ_BRIDGE_VERSION) && strcmp(app->version, ZJ_WRITER_VERSION);
    inventory.owner_started = zj_owner_started();
    legacy_nvs(&inventory);
    const char *error = NULL;
    /* ZKT 2.7.7 (M5): with the VFS unmounted this boot (the boot repair kept
     * it off, or it would not mount) the queue store never started: the boot
     * repair's survey listed every object header, hidden ones too, and any
     * journal name there still refuses (its files cannot be read now). */
    if (!esp_spiffs_mounted(NULL)) {
        bool journal_names = false;
        if (!storage_repair_survey(&journal_names)) error = "JOURNAL_OTA_STORAGE_UNAVAILABLE";
        else if (journal_names) error = "JOURNAL_LEGACY_EVIDENCE_PRESENT";
        else inventory.store_listed = true;
    } else {
        error = legacy_files(&inventory);
    }
    zle_verdict_t verdict = zle_decide(&inventory);
    if (!error) error = verdict.error;
    else if (verdict.state != ZLE_REFUSED || !verdict.error) verdict = (zle_verdict_t){ZLE_REFUSED, error, "STORE_UNREADABLE"};
    taskENTER_CRITICAL(&s_legacy_lock);
    s_legacy = inventory;
    s_legacy_verdict = verdict;
    s_legacy_known = true;
    taskEXIT_CRITICAL(&s_legacy_lock);
    return error;
}

bool zj_ota_append_legacy_evidence(cJSON *storage)
{
    if (!storage) return false;
    zle_inventory_t inventory;
    zle_verdict_t verdict;
    taskENTER_CRITICAL(&s_legacy_lock);
    bool known = s_legacy_known;
    inventory = s_legacy;
    verdict = s_legacy_verdict;
    taskEXIT_CRITICAL(&s_legacy_lock);
    if (!known) return true;
    cJSON *out = cJSON_CreateObject();
    bool ok = out && cJSON_AddStringToObject(out, "state", zle_state_name(verdict.state)) &&
        cJSON_AddStringToObject(out, "reason", verdict.reason ? verdict.reason : "UNKNOWN") &&
        cJSON_AddBoolToObject(out, "namespace", inventory.namespace_present) &&
        cJSON_AddStringToObject(out, "authority", inventory.namespace_present ? zle_root_name(inventory.root) : "ABSENT") &&
        cJSON_AddNumberToObject(out, "keys", inventory.keys) &&
        cJSON_AddBoolToObject(out, "reader_proof", inventory.reader_proof) &&
        cJSON_AddNumberToObject(out, "files", inventory.files) &&
        cJSON_AddNumberToObject(out, "bytes", inventory.bytes) &&
        cJSON_AddNumberToObject(out, "unreadable", inventory.unreadable) &&
        cJSON_AddBoolToObject(out, "runtime_disabled", inventory.runtime_disabled);
    if (ok && cJSON_AddItemToObject(storage, "journal_evidence", out)) return true;
    cJSON_Delete(out);
    return false;
}
#else
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
#endif

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
