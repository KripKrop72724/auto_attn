#include "hikvision_runtime.h"
#include "hikvision_http.h"
#include "hikvision_api.h"
#include "nvs.h"
#include "zone_config.h"
#include "queue_store.h"
#include "add_connector.h"
#include "led_status.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
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
static atomic_bool profiles_active;
static atomic_bool uploader_started, uploader_waiting, uploader_buffer_ready;
static atomic_uint uploader_tick;
static SemaphoreHandle_t request_lock;
static QueueHandle_t profile_commands;
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
typedef struct {
    unsigned char digest[32];
    const unsigned char *expected_anchor;
    unsigned count;
    bool anchor_seen, anchor_conflict;
} page_context_t;
static bool poll_record(void *context, const char *body, size_t length)
{
    page_context_t *page = context;
    if (page->expected_anchor && !page->anchor_seen) {
        unsigned char digest[32];
        mbedtls_sha256((const unsigned char *)body, length, digest, 0);
        page->anchor_conflict = memcmp(digest, page->expected_anchor, 32) != 0;
        page->anchor_seen = !page->anchor_conflict;
        return page->anchor_seen;
    }
    if (!preserve("POLL", body, length)) return false;
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
        /* Include the committed anchor in the same ordered source page as new
         * records. Validate it before any new evidence enters the queue. This
         * keeps reset/reuse protection without a second slow history search on
         * every empty poll, which can starve profiles and reconciliation. */
        uint32_t begin = present && cursor.serial ? cursor.serial : 1;
        if (result == HIK_OK && !present) {
            uint32_t first, last, count;
            result = hik_history_bounds(&first, &last, &count);
            /* First install starts at the recent tail. Earlier retained history
             * remains explicitly outstanding for the independent full job. */
            if (result == HIK_OK) begin = count ? (last - first >= 19 ? last - 19 : first) : 1;
        }
        if (result == HIK_OK && begin <= 3000000000U) {
            hik_search_t search; hik_search_init(&search, begin, 3000000000U);
            page_context_t page = {
                .expected_anchor = present && cursor.serial ? cursor.anchor : NULL,
            };
            result = hik_history_page(&search, poll_record, &page);
            if (page.anchor_conflict || (result == HIK_OK && page.expected_anchor && !page.anchor_seen))
                result = HIK_BINDING;
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
            uint32_t depth = 0;
            bool known = qs_snapshot(QS_HIK_SOURCE, &depth);
            led_status_set(known && !depth && add_connector_is_connected()
                ? LED_STATUS_HEALTHY : LED_STATUS_BACKLOG);
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
        if (atomic_load(&profiles_active) || next_poll_due <= esp_timer_get_time() ||
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
static bool collect_profile(void *context, const char *body, size_t length)
{
    if (!length || length > 4096 || memchr(body, 0, length)) return false;
    cJSON *value = cJSON_CreateString(body);
    if (!value) return false;
    if (!cJSON_AddItemToArray(context, value)) { cJSON_Delete(value); return false; }
    return true;
}
static void profile_task(void *arg)
{
    (void)arg;
    TickType_t wait = 0;
    for (;;) {
        add_command_t command = {0};
        bool requested = xQueueReceive(profile_commands, &command, wait) == pdTRUE;
        if (requested && command.expires_epoch > 0 && time(NULL) >= command.expires_epoch) {
            if (add_connector_command_update(command.command_id, "EXPIRED", "COMMAND_EXPIRED",
                    "Profile refresh expired before execution", "{}"))
                (void)add_connector_command_complete(command.command_id);
            else add_connector_command_retry(command.command_id);
            wait = pdMS_TO_TICKS(30000); continue;
        }
        hik_search_t scan; hik_search_init(&scan, 1, 1);
        atomic_store(&profiles_active, true);
        char snapshot_id[33]; memcpy(snapshot_id, scan.search_id, sizeof(snapshot_id));
        bool failed = false;
        for (unsigned phase = 1; phase <= 2 && !failed; phase++) {
            if (phase == 2) hik_search_init(&scan, 1, 1);
            while (!scan.complete && !failed) {
                vTaskDelay(pdMS_TO_TICKS(200));
                if (!add_connector_is_connected()) { failed = true; break; }
                if (xSemaphoreTake(request_lock, pdMS_TO_TICKS(50)) != pdTRUE) continue;
                if (next_poll_due <= esp_timer_get_time()) {
                    xSemaphoreGive(request_lock); continue;
                }
                cJSON *body = cJSON_CreateObject();
                cJSON *rows = body ? cJSON_AddArrayToObject(body, "records") : NULL;
                uint32_t position = scan.position;
                hik_result_t result = rows ? hik_http_verify_identity() : HIK_NETWORK;
                if (result == HIK_OK) result = hik_user_page(&scan, collect_profile, rows);
                xSemaphoreGive(request_lock);
                bool valid = result == HIK_OK &&
                    cJSON_AddStringToObject(body, "snapshot_id", snapshot_id) &&
                    cJSON_AddStringToObject(body, "terminal_serial", zone_config_get()->hik_expected_serial) &&
                    cJSON_AddNumberToObject(body, "phase", phase) &&
                    cJSON_AddNumberToObject(body, "position", position) &&
                    cJSON_AddNumberToObject(body, "total", scan.total);
                char *payload = valid ? cJSON_PrintUnformatted(body) : NULL;
                /* No local publication on partial reads. ADD stages encrypted
                 * pages and publishes only two complete matching inventories. */
                failed = !payload || !add_connector_send_payload_acknowledged(
                    "hikvision_profile_page", payload, 10000);
                free(payload); cJSON_Delete(body);
            }
        }
        atomic_store(&profiles_active, false);
        if (requested) {
            bool stored = add_connector_command_update(command.command_id,
                failed ? "RETRYING" : "SUCCEEDED", failed ? "HIK_PROFILE_REFRESH_PENDING" : NULL,
                failed ? "Complete matching profile scans are still required" : NULL,
                failed ? "{}" : "{\"complete\":true,\"stable\":true}");
            if (!failed && stored) (void)add_connector_command_complete(command.command_id);
            else add_connector_command_retry(command.command_id);
        }
        wait = pdMS_TO_TICKS(failed ? 30000 : 900000);
    }
}
void hikvision_append_telemetry(cJSON *payload)
{
    const zone_config_t *cfg = zone_config_get();
    cJSON *diagnostics = cJSON_GetObjectItemCaseSensitive(payload, "diagnostics");
    cJSON *workers = cJSON_GetObjectItemCaseSensitive(diagnostics, "workers");
    cJSON *worker = cJSON_IsArray(workers) ? cJSON_CreateObject() : NULL;
    if (worker) {
        int64_t now = esp_timer_get_time() / 1000;
        uint32_t age = (uint32_t)now - atomic_load(&uploader_tick);
        bool started = atomic_load(&uploader_started);
        bool ok = cJSON_AddStringToObject(worker, "name", "hikvision_source") &&
            cJSON_AddStringToObject(worker, "state", !started ? "STOPPED" : age > 90000 ? "FAULT" :
                !atomic_load(&uploader_buffer_ready) ? "WAITING_RESOURCE" :
                atomic_load(&uploader_waiting) ? "WAITING_NETWORK" : "RUNNING") &&
            (!started || cJSON_AddNumberToObject(worker, "last_activity_uptime_ms", (double)(now - age)));
        if (!ok || !cJSON_AddItemToArray(workers, worker)) cJSON_Delete(worker);
    }
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
        atomic_store(&uploader_started, true);
        atomic_store(&uploader_tick, (uint32_t)(esp_timer_get_time() / 1000));
        if (!payload) payload = malloc(DQ_MAX_RECORD_BYTES + 1);
        atomic_store(&uploader_buffer_ready, payload != NULL);
        if (!payload) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        size_t length = 0;
        dq_token_t token;
        dq_result_t state = qs_peek(QS_HIK_SOURCE, payload, DQ_MAX_RECORD_BYTES, &length, &token);
        if (state == DQ_OK) {
            payload[length] = 0;
            atomic_store(&uploader_waiting, true);
            if (add_connector_send_payload_acknowledged("hikvision_observation", payload, 10000)) {
                if (qs_settle(QS_HIK_SOURCE, &token) != DQ_OK)
                    ESP_LOGW(TAG, "Source evidence receipt is awaiting durable checkpoint");
            }
            atomic_store(&uploader_waiting, false);
        }
        vTaskDelay(pdMS_TO_TICKS(state == DQ_OK ? 50 : 1000));
    }
}
void hikvision_gateway_task(void *argument)
{
    (void)argument;
    TaskHandle_t stream = NULL, uploader = NULL, history = NULL, profiles = NULL;
    request_lock = xSemaphoreCreateMutex();
    profile_commands = xQueueCreate(1, sizeof(add_command_t));
    if (!request_lock || !profile_commands) { ESP_LOGE(TAG, "Request worker allocation failed"); vTaskDelete(NULL); return; }
    for (;;) {
        if (!stream && xTaskCreate(poll_task, "hik_poll", 8192, NULL, 5, &stream) != pdPASS) stream = NULL;
        if (!history && xTaskCreate(history_task, "hik_history", 8192, NULL, 3, &history) != pdPASS) history = NULL;
        if (!profiles && xTaskCreate(profile_task, "hik_profiles", 8192, NULL, 2, &profiles) != pdPASS) profiles = NULL;
        if (!uploader && xTaskCreate(source_uploader, "hik_evidence", 8192, NULL, 4, &uploader) != pdPASS) uploader = NULL;
        if (!stream || !history || !profiles || !uploader) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        if (profiles && uxQueueSpacesAvailable(profile_commands)) {
            add_command_t command;
            if (add_connector_take_command(&command)) {
                if (!strcmp(command.command_type, "REFRESH_USERS")) {
                    if (xQueueSend(profile_commands, &command, 0) != pdTRUE)
                        add_connector_command_retry(command.command_id);
                } else {
                    if (add_connector_command_update(command.command_id, "FAILED", "HIK_COMMAND_UNAVAILABLE",
                            "This command is not enabled in the current Hikvision image", "{}"))
                        (void)add_connector_command_complete(command.command_id);
                    else add_connector_command_retry(command.command_id);
                }
            }
        }
        add_connector_set_activity("HIKVISION_POLL_5S");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
