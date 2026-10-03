#include "queue_store.h"
#include "storage_upgrade.h"
#include "storage_budget.h"
#include <errno.h>
#include <dirent.h>
#include <string.h>
#include "esp_random.h"
#include "esp_timer.h"
#include <stdio.h>
#include <unistd.h>
#include "esp_spiffs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

typedef struct { durable_queue_t queue; SemaphoreHandle_t mutex; qs_lane_t lane; qs_admission_t admission; } lane_t;
static lane_t lanes[QS_COUNT];
static SemaphoreHandle_t budget_lock;
static qs_health_t health;
static storage_budget_t budget;
static char storage_generation[33];
static const char *names[] = {"ql", "qb", "qo", "qi", "qr", "qe", "qh"};
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
static dq_audit_t recovery_audits[QS_COUNT];
static uint8_t recovery_buffer[DQ_MAX_RECORD_BYTES];
#endif
static bool ensure_storage_generation(void)
{
    if (storage_generation[0]) return true;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("durable_queue", NVS_READWRITE, &handle);
    if (result != ESP_OK) return false;
    char value[33] = {0};
    size_t length = sizeof(value);
    result = nvs_get_str(handle, "instance", value, &length);
    if (result == ESP_ERR_NVS_NOT_FOUND) {
        // Never attach a fresh identity to existing, unaccounted segments.
        DIR *dir = opendir("/storage");
        if (!dir) { nvs_close(handle); return false; }
        struct dirent *entry;
        bool orphaned = false;
        errno = 0;
        while ((entry = readdir(dir)) != NULL) {
            for (unsigned i = 0; i < QS_COUNT; ++i) {
                if (strncmp(entry->d_name, names[i], strlen(names[i])) == 0) orphaned = true;
            }
        }
        if (errno) orphaned = true;
        if (closedir(dir) != 0) orphaned = true;
        if (orphaned) { nvs_close(handle); return false; }
        uint8_t random[16];
        esp_fill_random(random, sizeof(random));
        for (unsigned i = 0; i < sizeof(random); ++i) snprintf(value + 2 * i, 3, "%02x", random[i]);
        result = nvs_set_str(handle, "instance", value);
        if (result == ESP_OK) result = nvs_commit(handle);
    }
    nvs_close(handle);
    if (result != ESP_OK || strlen(value) != 32 || strspn(value, "0123456789abcdef") != 32) return false;
    memcpy(storage_generation, value, sizeof(storage_generation));
    return true;
}

bool qs_generation(char output[33])
{
    if (!output || !budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = ensure_storage_generation();
    if (ok) memcpy(output, storage_generation, sizeof(storage_generation));
    xSemaphoreGive(budget_lock);
    return ok;
}

static int load(void *arg, dq_checkpoint_t *checkpoint)
{
    lane_t *lane = arg;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("durable_queue", NVS_READONLY, &handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK) return -1;
    size_t size = sizeof(*checkpoint);
    status = nvs_get_blob(handle, names[lane->lane], checkpoint, &size);
    nvs_close(handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    return status == ESP_OK && size == sizeof(*checkpoint) ? 1 : -1;
}
static bool commit(void *arg, const dq_checkpoint_t *checkpoint)
{
    lane_t *lane = arg;
    nvs_handle_t handle;
    if (nvs_open("durable_queue", NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t status = nvs_set_blob(handle, names[lane->lane], checkpoint, sizeof(*checkpoint));
    if (status == ESP_OK) status = nvs_commit(handle);
    nvs_close(handle);
    return status == ESP_OK;
}
static bool measure(void)
{
    health.available = esp_spiffs_info(NULL, &health.total_bytes, &health.used_bytes) == ESP_OK && health.total_bytes;
    if (!health.available) return false;
    (void)storage_budget_admit(&budget, health.total_bytes, health.used_bytes, 0, SB_RECOVERY);
    health.bulk_paused = budget.bulk_paused;
    health.admission_reserve_bytes = budget.live_reserve + budget.recovery_reserve;
    return true;
}
static bool admit(void *arg, size_t bytes)
{
    lane_t *lane = arg;
    if (!measure()) return false;
    sb_class_t kind = lane->admission == QS_ADMIT_HISTORICAL ? SB_HISTORICAL :
        lane->admission == QS_ADMIT_LIVE ? SB_LIVE : SB_RECOVERY;
    return storage_budget_admit(&budget, health.total_bytes, health.used_bytes, bytes, kind);
}
bool qs_local_read_begin(void)
{
    if (!budget_lock) { errno = EINVAL; return false; }
    if (xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        errno = EBUSY;
        return false;
    }
    return true;
}
bool qs_local_begin(qs_admission_t policy, size_t bytes)
{
    if ((unsigned)policy > QS_ADMIT_OPTIONAL_HISTORICAL || !budget_lock) {
        errno = EINVAL;
        return false;
    }
    // Historical catalog writes are outside the live attendance path. Give
    // a concurrent queue fsync time to release the shared admission lock.
    int wait_ms = policy == QS_ADMIT_HISTORICAL || policy == QS_ADMIT_OPTIONAL_HISTORICAL ? 10000 : 1000;
    uint32_t wait = pdMS_TO_TICKS(wait_ms);
    if (xSemaphoreTake(budget_lock, wait) != pdTRUE) {
        errno = EBUSY;
        return false;
    }
    bool admitted = qs_local_admit_locked(policy, bytes);
    if (!admitted) xSemaphoreGive(budget_lock);
    return admitted;
}
bool qs_local_admit_locked(qs_admission_t policy, size_t bytes)
{
    if ((unsigned)policy > QS_ADMIT_OPTIONAL_HISTORICAL) { errno = EINVAL; return false; }
    bool measured = measure();
    sb_class_t kind = policy == QS_ADMIT_HISTORICAL || policy == QS_ADMIT_OPTIONAL_HISTORICAL ? SB_HISTORICAL :
        policy == QS_ADMIT_LIVE ? SB_LIVE : SB_RECOVERY;
    bool admitted = measured && storage_budget_admit(&budget,
        health.total_bytes, health.used_bytes, bytes, kind);
    health.bulk_paused = budget.bulk_paused;
    if (!admitted) {
        health.failures++;
        health.admission_rejections++;
        health.last_operation = measured ? "capacity_admission" : "filesystem_info";
        int error = measured ? ENOSPC : EIO;
        // Only the optional authenticated catalog may fall back to memory.
        // Refused attendance and recovery writes must still block boot proof.
        if (!measured || policy != QS_ADMIT_OPTIONAL_HISTORICAL) health.last_error = error;
        errno = error;
    }
    return admitted;
}

void qs_local_end(bool persisted, int captured_error)
{
    if (!persisted) {
        health.failures++;
        health.write_failures++;
        health.last_operation = "local_write_commit";
        health.last_error = captured_error ? captured_error : EIO;
    }
    xSemaphoreGive(budget_lock);
}

static bool lock(qs_lane_t lane)
{
    return (unsigned)lane < QS_COUNT && lanes[lane].mutex &&
        xSemaphoreTake(lanes[lane].mutex, pdMS_TO_TICKS(1000)) == pdTRUE;
}
static dq_result_t reopen(lane_t *lane)
{
    if (lane->queue.ready) return DQ_OK;
    char prefix[32];
    snprintf(prefix, sizeof(prefix), "/storage/%s", names[lane->lane]);
    dq_port_t port = {load, commit, admit, lane};
    return dq_open(&lane->queue, prefix, port);
}
/* Caller owns the budget lock. A successful unrelated operation must not
 * clear a latched storage fault; only a complete recovery check may do so. */
static void record_queue_result(dq_result_t result, const char *operation, bool writing)
{
    if (result == DQ_OK || result == DQ_EMPTY || result == DQ_STALE ||
        result == DQ_BUFFER_SMALL || result == DQ_PENDING) return;
    health.failures++;
    if (result == DQ_FULL) health.admission_rejections++;
    else if (writing) health.write_failures++;
    else health.read_failures++;
    health.last_operation = result == DQ_FULL ? "capacity_admission" : operation;
    health.last_error = result == DQ_FULL ? ENOSPC : result == DQ_CORRUPT ? EBADMSG : (errno ? errno : EIO);
}
bool qs_init(void)
{
    if (!budget_lock) budget_lock = xSemaphoreCreateMutex();
    if (!budget_lock) return false;
    bool ok = true;
    for (unsigned i = 0; i < QS_COUNT; i++) {
        lane_t *lane = &lanes[i]; lane->lane = (qs_lane_t)i;
        if (!lane->mutex) lane->mutex = xSemaphoreCreateMutex();
        if (!lock((qs_lane_t)i)) { ok = false; continue; }
        if (xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
            errno = 0;
            dq_result_t result = ensure_storage_generation() ? reopen(lane) : DQ_IO;
            record_queue_result(result, "segment_recovery", false);
            if (result != DQ_OK) ok = false;
            xSemaphoreGive(budget_lock);
        } else ok = false;
        xSemaphoreGive(lane->mutex);
    }
    if (xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
        health.recovery_complete = ok;
#else
        /* A valid checkpoint is not proof that its pending records survived. */
        health.recovery_complete = false;
        memset(recovery_audits, 0, sizeof(recovery_audits));
#endif
        xSemaphoreGive(budget_lock);
    } else ok = false;
    return ok;
}
bool qs_recover_step(void)
{
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    return false; /* Hikvision retains its existing independent recovery path. */
#else
    bool complete = true;
    for (unsigned i = 0; i < QS_COUNT; ++i) {
        if (!lock((qs_lane_t)i)) return false;
        if (!budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
            xSemaphoreGive(lanes[i].mutex); return false;
        }
        errno = 0;
        dq_result_t result = reopen(&lanes[i]);
        if (result == DQ_OK && !recovery_audits[i].complete)
            result = dq_audit_step(&lanes[i].queue, &recovery_audits[i],
                recovery_buffer, sizeof(recovery_buffer));
        if (result != DQ_OK) complete = false;
        if (result != DQ_PENDING) record_queue_result(result, "segment_verify", false);
        xSemaphoreGive(budget_lock);
        xSemaphoreGive(lanes[i].mutex);
    }
    if (budget_lock && xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        health.recovery_complete = complete && !health.last_error;
        complete = health.recovery_complete;
        xSemaphoreGive(budget_lock);
    } else complete = false;
    return complete;
#endif
}
bool qs_verify_persistence(void)
{
    if (!storage_upgrade_ready() || !budget_lock ||
        xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    /* A probe can only resolve its own failure. Queue corruption, refused
     * attendance and failed local writes need their own recovery evidence. */
    if (!health.recovery_complete || health.last_error) {
        xSemaphoreGive(budget_lock); return false;
    }
    if (health.persistence_verified && !health.persistence_probe_error) {
        xSemaphoreGive(budget_lock); return true;
    }
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
    static int64_t retry_at_us;
    int64_t now_us = esp_timer_get_time();
    if (health.persistence_probe_error && now_us < retry_at_us) {
        xSemaphoreGive(budget_lock); return false;
    }
#endif
    unsigned char expected[32], actual[32];
    esp_fill_random(expected, sizeof(expected));
    const char *path = "/storage/persistence-probe";
    const char *stage = "persistence_measure";
    int nvs_error = ESP_OK;
    int file_error = 0;
    errno = 0;
    bool ok = measure();
    if (!ok) file_error = EIO;
    if (ok) {
        stage = "persistence_capacity";
        ok = storage_budget_admit(&budget, health.total_bytes, health.used_bytes, 4096, SB_RECOVERY);
        if (!ok) file_error = ENOSPC;
    }
    FILE *f = NULL;
    if (ok) {
        stage = "persistence_open_write";
        f = fopen(path, "wb");
        ok = f != NULL;
        if (!ok) file_error = errno;
    }
    if (ok) {
        stage = "persistence_write";
        ok = fwrite(expected, 1, sizeof(expected), f) == sizeof(expected);
        if (!ok) file_error = errno;
    }
    if (ok) {
        stage = "persistence_sync";
        ok = fflush(f) == 0 && fsync(fileno(f)) == 0;
        if (!ok) file_error = errno;
    }
    if (f && fclose(f) != 0 && ok) {
        stage = "persistence_close_write";
        ok = false;
        file_error = errno;
    }
    f = NULL;
    if (ok) {
        stage = "persistence_open_read";
        f = fopen(path, "rb");
        ok = f != NULL;
        if (!ok) file_error = errno;
    }
    if (ok) {
        stage = "persistence_read";
        ok = fread(actual, 1, sizeof(actual), f) == sizeof(actual) &&
            fgetc(f) == EOF && !ferror(f) && !memcmp(actual, expected, sizeof(actual));
        if (!ok) file_error = errno;
    }
    if (f && fclose(f) != 0 && ok) {
        stage = "persistence_close_read";
        ok = false;
        file_error = errno;
    }
    if (ok) {
        stage = "persistence_unlink";
        ok = unlink(path) == 0;
        if (!ok) file_error = errno;
    }
    nvs_handle_t h;
    if (ok) {
        stage = "persistence_nvs_open_write";
        nvs_error = nvs_open("durable_queue", NVS_READWRITE, &h);
        ok = nvs_error == ESP_OK;
        if (ok) {
            stage = "persistence_nvs_set";
            nvs_error = nvs_set_blob(h, "write_proof", expected, sizeof(expected));
            ok = nvs_error == ESP_OK;
            if (ok) {
                stage = "persistence_nvs_commit";
                nvs_error = nvs_commit(h);
                ok = nvs_error == ESP_OK;
            }
            nvs_close(h);
        }
    }
    if (ok) {
        stage = "persistence_nvs_open_read";
        nvs_error = nvs_open("durable_queue", NVS_READONLY, &h);
        ok = nvs_error == ESP_OK;
        if (ok) {
            size_t size = sizeof(actual);
            stage = "persistence_nvs_read";
            nvs_error = nvs_get_blob(h, "write_proof", actual, &size);
            ok = nvs_error == ESP_OK && size == sizeof(actual) &&
                !memcmp(actual, expected, size);
            nvs_close(h);
        }
    }
    health.persistence_verified = ok;
    if (ok) {
        health.persistence_probe_failures = 0;
        health.persistence_probe_error = 0;
        health.persistence_probe_operation = NULL;
    }
    else {
        if (health.persistence_probe_total_failures < UINT32_MAX) health.persistence_probe_total_failures++;
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
        if (health.persistence_probe_failures < 3) health.persistence_probe_failures++;
        // A single interrupted write is not yet an established durability
        // failure. Keep boot proof withheld and retry twice before latching.
        if (health.persistence_probe_failures >= 3) {
            errno = file_error ? file_error : EIO;
            record_queue_result(DQ_IO, stage, true);
            if (nvs_error != ESP_OK) health.last_error = nvs_error;
        }
#else
        if (health.persistence_probe_failures < UINT32_MAX) health.persistence_probe_failures++;
        health.persistence_probe_operation = stage;
        health.persistence_probe_error = nvs_error != ESP_OK ? nvs_error : file_error ? file_error : EIO;
        /* Do not put a recoverable probe into last_error: that would prevent
         * both the recovery audit and this exact proof from running again.
         * Back off repeated flash writes; retain the total after recovery. */
        unsigned shift = health.persistence_probe_failures < 6 ? health.persistence_probe_failures : 6;
        unsigned seconds = 1U << shift;
        if (seconds > 60) seconds = 60;
        retry_at_us = esp_timer_get_time() + (int64_t)seconds * 1000000;
#endif
    }
    xSemaphoreGive(budget_lock);
    return ok;
}
dq_result_t qs_append(qs_lane_t lane, const void *data, size_t length)
{
    qs_admission_t policy = lane == QS_BULK || lane == QS_BLOCKED ? QS_ADMIT_HISTORICAL :
        lane == QS_RECEIPTS || lane == QS_EVIDENCE ? QS_ADMIT_RECOVERY : QS_ADMIT_LIVE;
    return qs_append_with_policy(lane, data, length, policy);
}
dq_result_t qs_append_with_policy(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy)
{
    bool writable = storage_upgrade_segmented_writes();
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    if (lane == QS_HIK_SOURCE) writable = storage_upgrade_ready();
#endif
    if (!writable || (unsigned)policy > QS_ADMIT_RECOVERY) return DQ_IO;
    if (!lock(lane)) return DQ_IO;
    lanes[lane].admission = policy;
    if (!budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        xSemaphoreGive(lanes[lane].mutex); return DQ_IO;
    }
    errno = 0;
    dq_result_t result = ensure_storage_generation() ? reopen(&lanes[lane]) : DQ_IO;
    if (result == DQ_OK) result = dq_append(&lanes[lane].queue, data, length);
    if (result == DQ_OK && !health.persistence_probe_error) health.persistence_verified = true;
    record_queue_result(result, "segment_append", true);
    xSemaphoreGive(budget_lock);
    xSemaphoreGive(lanes[lane].mutex);
    return result;
}
dq_result_t qs_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token)
{
    /* A catalog commit may hold the shared budget lock for longer than this
     * reader's slice. Contention preserves the queue and must be retried; only
     * a failed storage operation is evidence of lost local durability. */
    if (!lock(lane))
        return (unsigned)lane < QS_COUNT && lanes[lane].mutex ? DQ_PENDING : DQ_IO;
    if (!budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        xSemaphoreGive(lanes[lane].mutex);
        return budget_lock ? DQ_PENDING : DQ_IO;
    }
    errno = 0;
    dq_result_t result = ensure_storage_generation() ? reopen(&lanes[lane]) : DQ_IO;
    if (result == DQ_OK) result = dq_peek(&lanes[lane].queue, data, capacity, length, token);
    record_queue_result(result, "segment_read", false);
    xSemaphoreGive(budget_lock);
    xSemaphoreGive(lanes[lane].mutex);
    return result;
}
dq_result_t qs_settle(qs_lane_t lane, const dq_token_t *token)
{
    if (!lock(lane)) return DQ_IO;
    if (!budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        xSemaphoreGive(lanes[lane].mutex); return DQ_IO;
    }
    errno = 0;
    dq_result_t result = ensure_storage_generation() ? reopen(&lanes[lane]) : DQ_IO;
    if (result == DQ_OK) result = dq_settle(&lanes[lane].queue, token);
    record_queue_result(result, "segment_settle", true);
    xSemaphoreGive(budget_lock);
    xSemaphoreGive(lanes[lane].mutex);
    return result;
}
bool qs_snapshot(qs_lane_t lane, uint32_t *depth)
{
    if (!depth || !lock(lane)) return false;
    bool ready = lanes[lane].queue.ready;
    if (ready) *depth = lanes[lane].queue.checkpoint.depth;
    xSemaphoreGive(lanes[lane].mutex);
    return ready;
}
qs_health_t qs_health(void)
{
    qs_health_t snapshot = {0};
    if (budget_lock && xSemaphoreTake(budget_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        (void)measure(); snapshot = health; snapshot.observed = true; xSemaphoreGive(budget_lock);
    }
    return snapshot;
}
