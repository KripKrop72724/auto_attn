#include "queue_store.h"
#include "storage_budget.h"
#include <errno.h>
#include <stdio.h>
#include "esp_spiffs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

typedef struct { durable_queue_t queue; SemaphoreHandle_t mutex; qs_lane_t lane; qs_admission_t admission; } lane_t;
static lane_t lanes[QS_COUNT];
static SemaphoreHandle_t budget_lock;
static qs_health_t health;
static storage_budget_t budget;
static const char *names[] = {"ql", "qb", "qo", "qi", "qr", "qe"};
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
bool qs_init(void)
{
    if (!budget_lock) budget_lock = xSemaphoreCreateMutex();
    if (!budget_lock) return false;
    bool ok = true;
    for (unsigned i = 0; i < QS_COUNT; i++) {
        lane_t *lane = &lanes[i]; lane->lane = (qs_lane_t)i;
        if (!lane->mutex) lane->mutex = xSemaphoreCreateMutex();
        if (!lock((qs_lane_t)i)) { ok = false; continue; }
        if (reopen(lane) != DQ_OK) ok = false;
        xSemaphoreGive(lane->mutex);
    }
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
    if ((unsigned)policy > QS_ADMIT_RECOVERY) return DQ_IO;
    if (!lock(lane)) return DQ_IO;
    lanes[lane].admission = policy;
    if (!budget_lock || xSemaphoreTake(budget_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        xSemaphoreGive(lanes[lane].mutex); return DQ_IO;
    }
    dq_result_t result = reopen(&lanes[lane]);
    if (result == DQ_OK) result = dq_append(&lanes[lane].queue, data, length);
    if (result != DQ_OK) { health.failures++; health.last_error = errno; }
    xSemaphoreGive(budget_lock);
    xSemaphoreGive(lanes[lane].mutex);
    return result;
}
dq_result_t qs_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token)
{
    if (!lock(lane)) return DQ_IO;
    dq_result_t result = reopen(&lanes[lane]);
    if (result == DQ_OK) result = dq_peek(&lanes[lane].queue, data, capacity, length, token);
    xSemaphoreGive(lanes[lane].mutex);
    return result;
}
dq_result_t qs_settle(qs_lane_t lane, const dq_token_t *token)
{
    if (!lock(lane)) return DQ_IO;
    dq_result_t result = reopen(&lanes[lane]);
    if (result == DQ_OK) result = dq_settle(&lanes[lane].queue, token);
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
        (void)measure(); snapshot = health; xSemaphoreGive(budget_lock);
    }
    return snapshot;
}
