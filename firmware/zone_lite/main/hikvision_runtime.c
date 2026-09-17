#include "hikvision_runtime.h"
#include "hikvision_http.h"
#include "hikvision_api.h"
#include "nvs.h"
#include "zone_config.h"
#include "queue_store.h"
#include "add_connector.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "mbedtls/sha256.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <stdatomic.h>

static const char *TAG = "hikvision";
static atomic_uint current_events, replay_events, source_failures, stream_error;
static atomic_bool reachable, stream_open;
static atomic_long last_message, last_poll;
static atomic_uint poll_error, poll_cursor;
static atomic_bool history_required = true;
static SemaphoreHandle_t request_lock;
static int64_t next_poll_due;
/* A single encrypted NVS blob commits the cursor and its source binding. Queue
 * persistence always precedes this write. A failed commit replays observations. */
typedef struct {
    uint32_t version, serial;
    unsigned char binding[32], anchor[32];
} poll_checkpoint_t;
static bool checkpoint_load(poll_checkpoint_t *state, bool *present)
{
    const zone_config_t *cfg = zone_config_get();
    if (!cfg->hik_source_epoch[0]) return false;
    char binding[256];
    int n = snprintf(binding, sizeof(binding), "%s\n%s", cfg->hik_expected_serial, cfg->hik_source_epoch);
    if (n < 0 || (size_t)n >= sizeof(binding)) return false;
    memset(state, 0, sizeof(*state)); state->version = 1;
    mbedtls_sha256((unsigned char *)binding, (size_t)n, state->binding, 0);
    *present = false;
    nvs_handle_t h;
    esp_err_t err = nvs_open("hik_poll", NVS_READONLY, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) return true;
    if (err != ESP_OK) return false;
    poll_checkpoint_t stored; size_t size = sizeof(stored);
    err = nvs_get_blob(h, "checkpoint", &stored, &size); nvs_close(h);
    if (err == ESP_ERR_NVS_NOT_FOUND) return true;
    if (err != ESP_OK || size != sizeof(stored) || stored.version != 1 ||
        stored.serial > 3000000000U || memcmp(stored.binding, state->binding, 32)) return false;
    *state = stored; *present = true; return true;
}
static bool checkpoint_save(const poll_checkpoint_t *state)
{
    nvs_handle_t h;
    if (nvs_open("hik_poll", NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(h, "checkpoint", state, sizeof(*state));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h); return err == ESP_OK;
}
static bool preserve(void *context, const char *body, size_t length)
{
    const char *channel = context;
    if (!length || memchr(body, 0, length)) return false;
    const zone_config_t *cfg = zone_config_get();
    cJSON *raw = cJSON_ParseWithLength(body, length);
    cJSON *event = cJSON_GetObjectItemCaseSensitive(raw, "AccessControllerEvent");
    if (event && !strcmp(channel, "STREAM")) {
        atomic_store(&reachable, true);
        atomic_store(&stream_open, true);
        atomic_store(&last_message, time(NULL));
        cJSON *current = cJSON_GetObjectItemCaseSensitive(event, "currentEvent");
        if (cJSON_IsTrue(current)) atomic_fetch_add(&current_events, 1);
        if (cJSON_IsFalse(current)) atomic_fetch_add(&replay_events, 1);
    }
    /* Qualified device emits JSON. XML/unknown metadata is retained as text,
     * never mistaken for attendance or silently discarded. */
    cJSON *root = cJSON_CreateObject();
    if (!root) { cJSON_Delete(raw); return false; }
    unsigned char digest[32];
    char hex[65];
    mbedtls_sha256((const unsigned char *)body, length, digest, 0);
    for (unsigned i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    bool ok = cJSON_AddNumberToObject(root, "schema_version", 1) &&
        cJSON_AddStringToObject(root, "source_protocol", "hikvision-isapi-v1") &&
        cJSON_AddStringToObject(root, "terminal_serial", cfg->hik_expected_serial) &&
        cJSON_AddStringToObject(root, "source_epoch", cfg->hik_source_epoch) &&
        cJSON_AddStringToObject(root, "channel", channel) &&
        cJSON_AddStringToObject(root, "observation_sha256", hex) &&
        cJSON_AddNumberToObject(root, "captured_epoch", (double)time(NULL));
    /* Hash binds the original bytes, not a reserialized JSON representation. */
    if (ok) ok = cJSON_AddStringToObject(root, "raw", body) != NULL;
    char *payload = ok ? cJSON_PrintUnformatted(root) : NULL;
    cJSON_Delete(raw);
    cJSON_Delete(root);
    ok = payload && strlen(payload) < DQ_MAX_RECORD_BYTES &&
        qs_append_with_policy(QS_HIK_SOURCE, payload, strlen(payload),
            (!strcmp(channel, "STREAM") || !strcmp(channel, "POLL")) ? QS_ADMIT_LIVE : QS_ADMIT_HISTORICAL) == DQ_OK;
    free(payload);
    if (!ok) atomic_fetch_add(&source_failures, 1);
    return ok;
}
typedef struct { unsigned char digest[32]; unsigned count; bool store; } page_context_t;
static bool poll_record(void *context, const char *body, size_t length)
{
    page_context_t *page = context;
    if (page->store && !preserve("POLL", body, length)) return false;
    mbedtls_sha256((const unsigned char *)body, length, page->digest, 0);
    page->count++;
    return true;
}
static void poll_task(void *arg)
{
    (void)arg;
    poll_checkpoint_t cursor = {0};
    bool loaded = false, present = false;
    for (;;) {
        xSemaphoreTake(request_lock, portMAX_DELAY);
        int64_t started = esp_timer_get_time();
        next_poll_due = started + 5000000;
        hik_result_t result = HIK_OK;
        if (!loaded) {
            loaded = checkpoint_load(&cursor, &present);
            if (!loaded) result = HIK_CUSTODY;
        }
        if (result == HIK_OK) result = hik_http_verify_identity();
        uint32_t begin = cursor.serial + 1;
        if (result == HIK_OK && !present) {
            uint32_t first, last, count;
            result = hik_history_bounds(&first, &last, &count);
            /* First install starts at the recent tail. Earlier retained history
             * remains explicitly outstanding for the independent full job. */
            if (result == HIK_OK) begin = count ? (last - first >= 19 ? last - 19 : first) : 1;
        }
        if (result == HIK_OK && present && cursor.serial) {
            hik_search_t anchor; hik_search_init(&anchor, cursor.serial, cursor.serial);
            page_context_t check = {0};
            result = hik_history_page(&anchor, poll_record, &check);
            if (result == HIK_OK && (check.count != 1 || memcmp(check.digest, cursor.anchor, 32)))
                result = HIK_BINDING; /* retention loss or ambiguous reset/reuse */
        }
        if (result == HIK_OK && begin <= 3000000000U) {
            hik_search_t search; hik_search_init(&search, begin, 3000000000U);
            page_context_t page = {.store = true};
            result = hik_history_page(&search, poll_record, &page);
            if (result == HIK_OK && (page.count || !present)) {
                poll_checkpoint_t next = cursor;
                if (page.count) {
                    next.serial = search.previous_serial;
                    memcpy(next.anchor, page.digest, sizeof(next.anchor));
                }
                if (!checkpoint_save(&next)) result = HIK_CUSTODY;
                else { cursor = next; present = true; }
            }
        } else if (result == HIK_OK) result = HIK_BINDING;
        atomic_store(&reachable, result == HIK_OK);
        atomic_store(&poll_error, (unsigned)result);
        if (result == HIK_OK) {
            atomic_store(&last_poll, time(NULL));
            atomic_store(&poll_cursor, cursor.serial);
        } else ESP_LOGW(TAG, "Poll incomplete, reason=%u", (unsigned)result);
        xSemaphoreGive(request_lock);
        int64_t remaining = 5000000 - (esp_timer_get_time() - started);
        /* Start-to-start cadence, never overlapping requests or busy retrying. */
        vTaskDelay(pdMS_TO_TICKS(remaining > 1000 ? (uint32_t)(remaining / 1000) : 1));
    }
}
static void history_task(void *arg)
{
    (void)arg;
    char *assignment = malloc(2048);
    if (!assignment) { vTaskDelete(NULL); return; }
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        if (xSemaphoreTake(request_lock, pdMS_TO_TICKS(50)) != pdTRUE) continue;
        if (next_poll_due - esp_timer_get_time() < 2000000 ||
            !add_connector_take_hikvision_assignment(assignment)) {
            xSemaphoreGive(request_lock); continue;
        }
        cJSON *message = cJSON_Parse(assignment);
        cJSON *serial = cJSON_GetObjectItemCaseSensitive(message, "terminal_serial");
        cJSON *epoch = cJSON_GetObjectItemCaseSensitive(message, "source_epoch");
        cJSON *request = cJSON_GetObjectItemCaseSensitive(message, "request");
        const zone_config_t *cfg = zone_config_get();
        hik_result_t result = HIK_BINDING;
        cJSON *response = NULL;
        if (cJSON_IsString(serial) && cJSON_IsString(epoch) &&
            !strcmp(serial->valuestring, cfg->hik_expected_serial) &&
            !strcmp(epoch->valuestring, cfg->hik_source_epoch)) {
            result = hik_http_verify_identity();
            if (result == HIK_OK) result = hik_history_response(request, &response);
        }
        xSemaphoreGive(request_lock);
        if (result == HIK_OK) {
            cJSON_DeleteItemFromObjectCaseSensitive(message, "type");
            cJSON_DeleteItemFromObjectCaseSensitive(message, "request");
            cJSON_AddItemToObject(message, "response", response); response = NULL;
            char *body = cJSON_PrintUnformatted(message);
            if (body) {
                /* ADD commits page evidence and source checkpoint before ACK.
                 * A lost receipt is retried under the same ADD-owned token. */
                (void)add_connector_send_payload_acknowledged("hikvision_history_page", body, 10000);
                free(body);
            }
        } else ESP_LOGW(TAG, "History request incomplete, reason=%u", (unsigned)result);
        cJSON_Delete(response); cJSON_Delete(message);
    }
}
void hikvision_append_telemetry(cJSON *payload)
{
    const zone_config_t *cfg = zone_config_get();
    cJSON_DeleteItemFromObjectCaseSensitive(payload, "zkt");
    cJSON_DeleteItemFromObjectCaseSensitive(payload, "comm_key_management");
    cJSON_AddBoolToObject(payload, "comm_key_management", false);
    cJSON *terminal = cJSON_AddObjectToObject(payload, "terminal");
    cJSON_AddNumberToObject(terminal, "schema_version", 2);
    cJSON_AddStringToObject(terminal, "vendor", "hikvision");
    cJSON_AddStringToObject(terminal, "protocol", "isapi");
    cJSON_AddStringToObject(terminal, "serial", cfg->hik_expected_serial);
    cJSON_AddStringToObject(terminal, "ip_address", cfg->hik_host);
    cJSON_AddStringToObject(terminal, "capability_profile", cfg->hik_profile);
    cJSON_AddStringToObject(terminal, "qualification_state", "NOT_QUALIFIED");
    cJSON_AddBoolToObject(terminal, "online", atomic_load(&reachable));
    cJSON_AddStringToObject(terminal, "connection_state", atomic_load(&reachable) ? "ONLINE" : "OFFLINE");
    cJSON_AddStringToObject(terminal, "capture_mode", "poll");
    cJSON_AddNumberToObject(terminal, "poll_interval_seconds", 5);
    cJSON_AddNumberToObject(terminal, "last_successful_poll_epoch", atomic_load(&last_poll));
    cJSON_AddNumberToObject(terminal, "poll_error", atomic_load(&poll_error));
    cJSON_AddNumberToObject(terminal, "durable_poll_cursor", atomic_load(&poll_cursor));
    cJSON_AddBoolToObject(terminal, "full_history_required", atomic_load(&history_required));
    cJSON_AddBoolToObject(terminal, "stream_open", atomic_load(&stream_open));
    cJSON_AddNumberToObject(terminal, "stream_error", atomic_load(&stream_error));
    cJSON_AddNumberToObject(terminal, "current_event_count", atomic_load(&current_events));
    cJSON_AddNumberToObject(terminal, "replay_event_count", atomic_load(&replay_events));
    cJSON_AddNumberToObject(terminal, "last_stream_message_epoch", atomic_load(&last_message));
    cJSON_AddNumberToObject(terminal, "source_storage_failures", atomic_load(&source_failures));
    uint32_t depth;
    if (qs_snapshot(QS_HIK_SOURCE, &depth)) cJSON_AddNumberToObject(terminal, "source_queue_depth", depth);
}
static void source_uploader(void *arg)
{
    (void)arg;
    char *payload = NULL;
    for (;;) {
        if (!payload) payload = malloc(DQ_MAX_RECORD_BYTES + 1);
        if (!payload) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        size_t length = 0;
        dq_token_t token;
        dq_result_t state = qs_peek(QS_HIK_SOURCE, payload, DQ_MAX_RECORD_BYTES, &length, &token);
        if (state == DQ_OK) {
            payload[length] = 0;
            if (add_connector_send_payload_acknowledged("hikvision_observation", payload, 10000)) {
                if (qs_settle(QS_HIK_SOURCE, &token) != DQ_OK)
                    ESP_LOGW(TAG, "Source evidence receipt is awaiting durable checkpoint");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(state == DQ_OK ? 50 : 1000));
    }
}
void hikvision_gateway_task(void *argument)
{
    (void)argument;
    TaskHandle_t stream = NULL, uploader = NULL, history = NULL;
    request_lock = xSemaphoreCreateMutex();
    if (!request_lock) { ESP_LOGE(TAG, "Request worker allocation failed"); vTaskDelete(NULL); return; }
    for (;;) {
        if (!stream && xTaskCreate(poll_task, "hik_poll", 8192, NULL, 5, &stream) != pdPASS) stream = NULL;
        if (!history && xTaskCreate(history_task, "hik_history", 8192, NULL, 3, &history) != pdPASS) history = NULL;
        if (!uploader && xTaskCreate(source_uploader, "hik_evidence", 8192, NULL, 4, &uploader) != pdPASS) uploader = NULL;
        add_connector_set_activity("HIKVISION_POLL_5S");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
