#include "zkt_storage_owner.h"
#include "zkt_journal_crypto.h"
#include "zkt_journal_state.h"
#include "zkt_custody_wire.h"
#include "queue_store.h"
#include <dirent.h>
#include <errno.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/platform_util.h"
#include "nvs.h"

typedef struct {
    zj_mailbox_t mailbox;
    zj_store_t store;
    zj_state_t state;
    zj_crypto_key_t key;
    zj_metadata_t metadata;
    zj_owner_health_t health;
    char prefix[112];
    int nvs_error;
    uint64_t retry_at_us;
    char wire_scratch[ZJ_CUSTODY_PAYLOAD_MAX];
} owner_t;
static owner_t *owner;
static SemaphoreHandle_t mailbox_lock;
static TaskHandle_t owner_task;

static bool enter(void)
{
    return mailbox_lock && xSemaphoreTake(mailbox_lock, pdMS_TO_TICKS(100)) == pdTRUE;
}
static int state_read(void *context, const char *name, uint8_t *out, size_t length)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zkt_journal", NVS_READONLY, &handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK) { o->nvs_error = status; return -1; }
    size_t actual = length;
    status = nvs_get_blob(handle, name, out, &actual);
    nvs_close(handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK || actual != length) {
        o->nvs_error = status == ESP_OK ? ESP_ERR_INVALID_SIZE : status;
        return -1;
    }
    return 1;
}
static bool state_write(void *context, const char *name, const uint8_t *bytes, size_t length)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zkt_journal", NVS_READWRITE, &handle);
    if (status == ESP_OK) {
        status = nvs_set_blob(handle, name, bytes, length);
        if (status == ESP_OK) status = nvs_commit(handle);
        nvs_close(handle);
    }
    if (status != ESP_OK) o->nvs_error = status;
    return status == ESP_OK;
}
static bool state_random(void *context, uint8_t *out, size_t length)
{
    (void)context;
    esp_fill_random(out, length);
    return true;
}
static bool journal_absent(void *context)
{
    owner_t *o = context;
    char directory[112];
    const char *base = strrchr(o->prefix, '/');
    if (!base || !base[1]) return false;
    size_t length = (size_t)(base - o->prefix);
    memcpy(directory, o->prefix, length);
    directory[length] = 0;
    if (!length) strcpy(directory, "/");
    ++base;
    DIR *dir = opendir(directory);
    if (!dir) return false;
    bool absent = true;
    struct dirent *entry;
    for (;;) {
        errno = 0;
        entry = readdir(dir);
        if (!entry) { if (errno) absent = false; break; }
        if (!strncmp(entry->d_name, base, strlen(base))) absent = false;
    }
    if (closedir(dir) != 0) absent = false;
    return absent;
}
static bool admitted(void *context, size_t bytes)
{
    (void)context;
    /* APPEND already holds the shared budget lock and an admission covering
     * both possible segment metadata and this record. No competing producer
     * can consume that budget before the write finishes. */
    return bytes <= ZJ_META_BYTES + ZJ_RECORD_MAX;
}
static zj_result_t recover(owner_t *o)
{
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now < o->retry_at_us) return ZJ_IO;
    if (!qs_local_read_begin()) return ZJ_IO;
    o->nvs_error = 0;
    zj_state_port_t state_port = {state_read, state_write, state_random, journal_absent, o};
    zj_result_t result = zj_state_open(&o->state, state_port, o->metadata.terminal_serial);
    if (result == ZJ_OK && !zj_state_identity(&o->state, o->key.master, o->metadata.capture_epoch))
        result = ZJ_CORRUPT;
    if (result == ZJ_OK) {
        zj_store_port_t port = {zj_state_checkpoint_load, zj_state_checkpoint_commit,
            zj_state_reserve, admitted, &o->state, zj_crypto_port(&o->key)};
        result = zj_store_open(&o->store, o->prefix, &o->metadata, o->state.limit, port);
    }
    qs_local_end(true, 0);
    o->retry_at_us = result == ZJ_OK ? 0 : now + 5000000;
    if (result != ZJ_OK) {
        o->store.ready = false;
        mbedtls_platform_zeroize(&o->key, sizeof(o->key));
    }
    return result;
}
static void execute(owner_t *o, const zj_request_t *request, zj_reply_t *reply)
{
    memset(reply, 0, sizeof(*reply));
    o->nvs_error = 0;
    o->store.last_errno = 0;
    o->store.last_operation = NULL;
    if (!o->store.ready || !o->state.ready) {
        reply->result = recover(o);
        if (reply->result != ZJ_OK) return;
    }
    bool writing = request->operation == ZJ_APPEND;
    bool locked = writing ? qs_local_begin(QS_ADMIT_LIVE, ZJ_META_BYTES + ZJ_RECORD_MAX) : qs_local_read_begin();
    if (!locked) {
        o->store.last_errno = errno;
        o->store.last_operation = errno == EBUSY ? "storage_lock" : "storage_admission";
        reply->result = errno == ENOSPC ? ZJ_FULL : ZJ_IO;
        return;
    }
    switch (request->operation) {
        case ZJ_APPEND:
            reply->result = zj_store_append(&o->store, &request->input.observation, &reply->capture_sequence);
            break;
        case ZJ_SETTLE: {
            zj_custody_expected_t actual;
            reply->result = zj_store_peek(&o->store, &reply->item);
            if (reply->result != ZJ_OK) break;
            if (!zj_custody_encode(&reply->item, o->store.port.crypto, o->wire_scratch,
                                   sizeof(o->wire_scratch), &actual)) reply->result = ZJ_INVALID;
            else if (memcmp(actual.observation_id, request->input.settlement.observation_id, sizeof(actual.observation_id)) ||
                     memcmp(actual.payload_digest, request->input.settlement.payload_digest, sizeof(actual.payload_digest)))
                reply->result = ZJ_STALE;
            else reply->result = zj_store_settle(&o->store, &request->input.settlement.token,
                                                 request->input.settlement.receipt_digest);
            mbedtls_platform_zeroize(o->wire_scratch, sizeof(o->wire_scratch));
            break;
        }
        case ZJ_PEEK: reply->result = zj_store_peek(&o->store, &reply->item); break;
        case ZJ_RECLAIM: reply->result = zj_store_reclaim_step(&o->store); break;
        default: reply->result = ZJ_INVALID; break;
    }
    bool write_failed = writing && (reply->result == ZJ_IO || reply->result == ZJ_UNCERTAIN);
    qs_local_end(!write_failed, o->store.last_errno);
}
static void task(void *context)
{
    owner_t *o = context;
    zj_request_t request;
    zj_reply_t reply;
    for (;;) {
        uint64_t ticket = 0;
        while (!enter()) vTaskDelay(pdMS_TO_TICKS(1));
        o->health.sampled_uptime_us = (uint64_t)esp_timer_get_time();
        bool work = zj_mailbox_begin(&o->mailbox, &request, &ticket);
        bool repair = !work && (!o->store.ready || !o->state.ready) &&
            o->health.sampled_uptime_us >= o->retry_at_us;
        if (work || repair) {
            o->health.operation_running = true;
            o->health.recovering = repair || !o->store.ready || !o->state.ready;
            if (work) o->health.operation = request.operation;
            o->health.operation_started_us = o->health.sampled_uptime_us;
        }
        xSemaphoreGive(mailbox_lock);
        if (!work && !repair) { ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(200)); continue; }
        if (work) execute(o, &request, &reply);
        else {
            memset(&reply, 0, sizeof(reply));
            reply.result = recover(o);
        }
        uint64_t finished = (uint64_t)esp_timer_get_time();
        while (!enter()) vTaskDelay(pdMS_TO_TICKS(1));
        if (work) (void)zj_mailbox_finish(&o->mailbox, ticket, &reply);
        o->health.progress_uptime_us = finished;
        o->health.sampled_uptime_us = finished;
        uint64_t elapsed = finished - o->health.operation_started_us;
        if (elapsed > o->health.max_operation_us) o->health.max_operation_us = elapsed;
        o->health.operation_running = false;
        o->health.recovering = false;
        o->health.ready = o->store.ready && o->state.ready;
        o->health.last_result = reply.result;
        if (reply.result != ZJ_OK && reply.result != ZJ_EMPTY && reply.result != ZJ_STALE) {
            ++o->health.failures;
            o->health.filesystem_error = o->store.last_errno;
            o->health.nvs_error = o->nvs_error;
            o->health.failed_operation = o->store.last_operation;
        }
        xSemaphoreGive(mailbox_lock);
        mbedtls_platform_zeroize(&request, sizeof(request));
        mbedtls_platform_zeroize(&reply, sizeof(reply));
    }
}

bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata)
{
    if (owner || !prefix || !metadata || !strrchr(prefix, '/') || strlen(prefix) >= sizeof(owner->prefix)) return false;
    uint8_t validated[ZJ_META_BYTES];
    if (!zj_metadata_encode(metadata, validated)) return false;
    mailbox_lock = xSemaphoreCreateMutex();
    if (!mailbox_lock) return false;
    owner = heap_caps_calloc(1, sizeof(*owner), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!owner) {
        vSemaphoreDelete(mailbox_lock);
        mailbox_lock = NULL;
        return false;
    }
    strcpy(owner->prefix, prefix);
    owner->metadata = *metadata;
    zj_mailbox_init(&owner->mailbox);
    owner->health.started = true;
    if (xTaskCreate(task, "zkt_storage", 12288, owner, 5, &owner_task) != pdPASS) {
        heap_caps_free(owner);
        owner = NULL;
        vSemaphoreDelete(mailbox_lock);
        mailbox_lock = NULL;
        return false;
    }
    return true;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    if (ticket) *ticket = 0;
    if (!owner || !enter()) return false;
    bool ok = zj_mailbox_submit(&owner->mailbox, request, ticket);
    xSemaphoreGive(mailbox_lock);
    if (ok) xTaskNotifyGive(owner_task);
    return ok;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    if (complete) *complete = false;
    if (!owner || !enter()) return false;
    bool ok = zj_mailbox_poll(&owner->mailbox, ticket, reply, complete);
    xSemaphoreGive(mailbox_lock);
    return ok;
}
bool zj_owner_abandon(uint64_t ticket)
{
    if (!owner || !enter()) return false;
    bool ok = zj_mailbox_abandon(&owner->mailbox, ticket);
    xSemaphoreGive(mailbox_lock);
    return ok;
}
bool zj_owner_health(zj_owner_health_t *health)
{
    if (!owner || !health || !enter()) return false;
    *health = owner->health;
    health->completed = owner->mailbox.completed;
    health->refused = owner->mailbox.refused;
    health->occupied = owner->mailbox.occupied;
    health->high_watermark = owner->mailbox.high_watermark;
    xSemaphoreGive(mailbox_lock);
    return true;
}
