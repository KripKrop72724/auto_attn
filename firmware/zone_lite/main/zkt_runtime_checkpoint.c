#include "zkt_runtime_checkpoint.h"
#include "zkt_storage_owner.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include <stdatomic.h>
#include <string.h>

#define CHECKPOINT_WAIT_US 5000000ULL
static atomic_flag caller_busy = ATOMIC_FLAG_INIT;
static uint64_t pending_ticket;
static runtime_checkpoint_t pending_facts;

bool zj_runtime_checkpoint_required(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    return app && !strcmp(app->project_name, "zone_lite") &&
        (!strcmp(app->version, ZJ_BRIDGE_VERSION) || !strcmp(app->version, ZJ_WRITER_VERSION));
}

static bool same_facts(const runtime_checkpoint_t *left, const runtime_checkpoint_t *right)
{
    runtime_checkpoint_t a = *left, b = *right;
    a.generation = b.generation = 0;
    a.crc = b.crc = 0;
    return !memcmp(&a, &b, sizeof(a));
}

static bool collect(uint64_t deadline, runtime_checkpoint_t *confirmed)
{
    do {
        zj_reply_t reply;
        bool complete = false;
        if (zj_owner_poll(pending_ticket, &reply, &complete) && complete) {
            pending_ticket = 0;
            bool valid = reply.result == ZJ_OK && runtime_checkpoint_valid(&reply.runtime_checkpoint) &&
                same_facts(&pending_facts, &reply.runtime_checkpoint);
            if (valid) *confirmed = reply.runtime_checkpoint;
            memset(&pending_facts, 0, sizeof(pending_facts));
            return valid;
        }
        if ((uint64_t)esp_timer_get_time() >= deadline) break;
        vTaskDelay(pdMS_TO_TICKS(20));
    } while ((uint64_t)esp_timer_get_time() < deadline);
    /* Accepted work can still be inside NVS. Keep its bounded slot and exact
     * facts, including a lease obligation, until completion is observed. */
    return false;
}

bool zj_runtime_checkpoint_save(const runtime_checkpoint_t *proposed, runtime_checkpoint_t *confirmed)
{
    if (!confirmed) return false;
    memset(confirmed, 0, sizeof(*confirmed));
    if (!runtime_checkpoint_valid(proposed) || !zj_runtime_checkpoint_required() ||
        atomic_flag_test_and_set_explicit(&caller_busy, memory_order_acquire)) return false;
    bool saved = false;
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - CHECKPOINT_WAIT_US) goto done;
    uint64_t deadline = now + CHECKPOINT_WAIT_US;
    if (pending_ticket && !collect(deadline, confirmed)) goto done;
    if ((uint64_t)esp_timer_get_time() >= deadline) goto done;
    zj_request_t request = {.operation = ZJ_RUNTIME_CHECKPOINT,
        .input.runtime_checkpoint = {.state = *proposed, .deadline_us = deadline}};
    if (!zj_owner_submit(&request, &pending_ticket)) {
        pending_ticket = 0;
        goto done;
    }
    pending_facts = *proposed;
    saved = collect(deadline, confirmed);
done:
    atomic_flag_clear_explicit(&caller_busy, memory_order_release);
    return saved;
}

zj_result_t zj_runtime_checkpoint_commit(const runtime_checkpoint_t *proposed,
    uint64_t deadline_us, runtime_checkpoint_t *confirmed, int *nvs_error)
{
    if (!confirmed || !nvs_error) return ZJ_INVALID;
    memset(confirmed, 0, sizeof(*confirmed));
    *nvs_error = 0;
    if (!runtime_checkpoint_valid(proposed) || !deadline_us) return ZJ_INVALID;
    if ((uint64_t)esp_timer_get_time() >= deadline_us) return ZJ_STALE;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zone_lite", NVS_READWRITE, &handle);
    if (status != ESP_OK) { *nvs_error = status; return ZJ_IO; }
    runtime_checkpoint_t current = {0}, next = *proposed;
    size_t length = sizeof(current);
    status = nvs_get_blob(handle, "runtime_v1", &current, &length);
    zj_result_t result = ZJ_IO;
    if (status == ESP_ERR_NVS_NOT_FOUND) {
        /* Previously observed state cannot disappear and become a new root. */
        if (proposed->generation != 1) { result = ZJ_CORRUPT; goto done; }
        next.generation = 1;
    } else if (status != ESP_OK) {
        *nvs_error = status;
        goto done;
    } else if (length != sizeof(current) || !runtime_checkpoint_valid(&current) ||
               current.history_schema != proposed->history_schema) {
        result = ZJ_CORRUPT;
        goto done;
    } else {
        if (current.generation == UINT32_MAX) { result = ZJ_FULL; goto done; }
        if (current.generation < proposed->generation - 1) { result = ZJ_STALE; goto done; }
        /* A previous commit may have succeeded despite a failed response.
         * This sole writer always advances from the actual retained blob. */
        next.generation = current.generation + 1;
    }
    if ((uint64_t)esp_timer_get_time() >= deadline_us) { result = ZJ_STALE; goto done; }
    next.crc = dq_crc32(&next, offsetof(runtime_checkpoint_t, crc));
    status = nvs_set_blob(handle, "runtime_v1", &next, sizeof(next));
    if (status == ESP_OK) status = nvs_commit(handle);
    if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
    length = sizeof(current);
    status = nvs_get_blob(handle, "runtime_v1", &current, &length);
    if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
    if (length != sizeof(current) || !runtime_checkpoint_valid(&current) || memcmp(&next, &current, sizeof(next))) {
        result = ZJ_UNCERTAIN;
        goto done;
    }
    *confirmed = current;
    result = ZJ_OK;
done:
    nvs_close(handle);
    return result;
}
