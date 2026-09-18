#include "hikvision_runtime.h"
#include "hikvision_http.h"
#include "hikvision_api.h"
#include "hikvision_clock.h"
#include "hikvision_commands.h"
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
static atomic_uint poll_error, poll_cursor, poll_count, last_poll_interval_ms, history_page_count;
static atomic_uint poll_stage, poll_http_status, poll_duration_ms, poll_failures;
static const char *poll_stages[] = {"checkpoint", "identity", "history_bounds", "events", "checkpoint_commit"};
static atomic_bool history_required = true;
static atomic_bool profiles_active;
static atomic_bool uploader_started, uploader_waiting, uploader_buffer_ready;
static atomic_uint uploader_tick, poll_tick, history_tick;
static atomic_bool checkpoint_ready, runtime_workers_started;
typedef struct { int64_t device_epoch, sampled_epoch, sampled_us; bool valid; } clock_sample_t;
static clock_sample_t clock_sample;
static portMUX_TYPE clock_mux = portMUX_INITIALIZER_UNLOCKED;
static clock_sample_t read_clock_sample(void)
{
    portENTER_CRITICAL(&clock_mux);
    clock_sample_t sample = clock_sample;
    portEXIT_CRITICAL(&clock_mux);
    if (esp_timer_get_time() - sample.sampled_us > 120000000) sample.valid = false;
    return sample;
}
static void sample_terminal_clock(void)
{
    char response[2048]; size_t length = 0;
    int64_t started = esp_timer_get_time();
    hik_result_t result = hik_http_request(HTTP_METHOD_GET, "/ISAPI/System/time", NULL,
        response, sizeof(response), &length);
    clock_sample_t sample = {.sampled_epoch = time(NULL), .sampled_us = esp_timer_get_time()};
    if (result == HIK_OK && length < sizeof(response)) {
        response[length] = 0;
        char *begin = strstr(response, "<localTime>");
        char *end = begin ? strstr(begin + 11, "</localTime>") : NULL;
        if (begin && end && !strstr(end + 12, "<localTime>") && end - (begin + 11) <= 25) {
            char value[26]; size_t size = (size_t)(end - (begin + 11));
            memcpy(value, begin + 11, size); value[size] = 0;
            sample.valid = hik_clock_parse(value, &sample.device_epoch) &&
                sample.sampled_epoch >= 1577836800 && sample.sampled_us - started <= 2000000;
        }
    }
    portENTER_CRITICAL(&clock_mux);
    clock_sample = sample;
    portEXIT_CRITICAL(&clock_mux);
    char message[160];
    snprintf(message, sizeof(message), "Terminal clock %s; drift=%lld seconds; request=%u",
        sample.valid ? "sampled" : "unverified", (long long)(sample.valid ? sample.device_epoch - sample.sampled_epoch : 0),
        (unsigned)result);
    ESP_LOGI(TAG, "%s", message);
    (void)add_connector_log(sample.valid ? "INFO" : "WARN", "hikvision", "HIK_CLOCK_SAMPLE", message);
}
static void append_clock_evidence(cJSON *root)
{
    clock_sample_t sample = read_clock_sample();
    if (!sample.valid) return;
    cJSON *clock = cJSON_AddObjectToObject(root, "clock_sample");
    if (clock) {
        cJSON_AddNumberToObject(clock, "device_epoch", (double)sample.device_epoch);
        cJSON_AddNumberToObject(clock, "sampled_epoch", (double)sample.sampled_epoch);
    }
}
static SemaphoreHandle_t request_lock, source_upload_lock;
static QueueHandle_t profile_commands;
/* Protected by request_lock: at most one bounded background page per poll. */
static bool background_slot;
static bool take_background_slot(void)
{
    bool available = background_slot;
    background_slot = false;
    return available;
}
static uint32_t poll_delay_ms(int64_t elapsed_us)
{
    int64_t remaining = 2000000 - elapsed_us;
    /* Slow terminals must still allow the 200ms profile / 100ms history worker
     * to acquire one slot. Requests never overlap; two seconds is the target,
     * not a reason to starve all background progress after an overrun. */
    return remaining > 250000 ? (uint32_t)(remaining / 1000) : 250;
}
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
    if (ok) append_clock_evidence(root);
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
static const char *hik_reason(hik_result_t result)
{
    static const char *reasons[] = {"ok", "invalid_configuration", "network_timeout_or_unreachable",
        "authentication_rejected", "http_error", "response_too_large", "identity_or_history_changed",
        "invalid_response", "durable_storage_failed"};
    return (unsigned)result < sizeof(reasons) / sizeof(reasons[0]) ? reasons[result] : "unknown";
}
typedef struct {
    unsigned char digest[32];
    const char *channel;
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
    if (!preserve(page->channel ? page->channel : "POLL", body, length)) return false;
    mbedtls_sha256((const unsigned char *)body, length, page->digest, 0);
    page->count++;
    return true;
}
static void poll_task(void *arg)
{
    (void)arg;
    poll_checkpoint_t cursor = {0};
    bool loaded = false, present = false;
    int64_t clock_checked_us = -60000000, last_report_us = -60000000, last_start_us = 0;
    hik_result_t previous_result = HIK_CONFIGURATION;
    led_status_t previous_led = LED_STATUS_BOOTING;
    for (;;) {
        xSemaphoreTake(request_lock, portMAX_DELAY);
        int64_t started = esp_timer_get_time();
        if (last_start_us) atomic_store(&last_poll_interval_ms, (unsigned)((started - last_start_us) / 1000));
        last_start_us = started;
        atomic_fetch_add(&poll_count, 1);
        background_slot = false;
        hik_result_t result = HIK_OK;
        unsigned stage = 0;
        if (!loaded) {
            loaded = checkpoint_load(&cursor, &present);
            if (!loaded) result = HIK_CUSTODY;
            else { atomic_store(&poll_cursor, cursor.serial); atomic_store(&checkpoint_ready, true); }
        }
        if (result == HIK_OK) { stage = 1; result = hik_http_verify_identity(); }
        if (result == HIK_OK && started - clock_checked_us >= 60000000) {
            sample_terminal_clock();
            clock_checked_us = started;
        }
        /* Include the committed anchor in the same ordered source page as new
         * records. Validate it before any new evidence enters the queue. This
         * keeps reset/reuse protection without a second slow history search on
         * every empty poll, which can starve profiles and reconciliation. */
        uint32_t begin = present && cursor.serial ? cursor.serial : 1;
        if (result == HIK_OK && !present) {
            uint32_t first, last, count;
            stage = 2;
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
            stage = 3;
            result = hik_history_page(&search, poll_record, &page);
            if (page.anchor_conflict || (result == HIK_OK && page.expected_anchor && !page.anchor_seen))
                result = HIK_BINDING;
            if (result == HIK_OK && (page.count || !present)) {
                poll_checkpoint_t next = cursor;
                if (page.count) {
                    next.serial = search.previous_serial;
                    memcpy(next.anchor, page.digest, sizeof(next.anchor));
                }
                stage = 4;
                if (!checkpoint_save(&next)) result = HIK_CUSTODY;
                else { cursor = next; present = true; }
            }
        } else if (result == HIK_OK) result = HIK_BINDING;
        atomic_store(&poll_stage, stage);
        atomic_store(&poll_http_status, stage ? hik_http_last_status() : 0);
        atomic_store(&poll_duration_ms, (unsigned)((esp_timer_get_time() - started) / 1000));
        if (result == HIK_OK) atomic_store(&poll_failures, 0);
        else atomic_fetch_add(&poll_failures, 1);
        atomic_store(&reachable, result == HIK_OK);
        atomic_store(&poll_error, (unsigned)result);
        uint32_t depth = 0;
        bool known = qs_snapshot(QS_HIK_SOURCE, &depth);
        qs_health_t storage = qs_health();
        uint32_t worker_age = (uint32_t)(esp_timer_get_time() / 1000) - atomic_load(&uploader_tick);
        bool workers_ready = add_connector_delivery_healthy() && atomic_load(&uploader_started) && atomic_load(&uploader_buffer_ready) && worker_age <= 90000;
        led_status_t status = LED_STATUS_BACKLOG;
        if (result == HIK_OK) {
            atomic_store(&last_poll, time(NULL));
            atomic_store(&poll_cursor, cursor.serial);
            bool durable = storage.observed && storage.available && storage.recovery_complete &&
                storage.persistence_verified && !storage.last_error;
            status = known && !depth && add_connector_is_connected() && durable && workers_ready
                ? LED_STATUS_HEALTHY : LED_STATUS_BACKLOG;
        } else ESP_LOGW(TAG, "Poll incomplete, reason=%u", (unsigned)result);
        led_status_set(status);
        if (result != previous_result || status != previous_led || started - last_report_us >= 60000000) {
            char message[384];
            snprintf(message, sizeof(message), "Poll %s; reason=%s; stage=%s; http=%u; failures=%u; terminal=%s; duration_ms=%lu; cursor=%lu; queued=%lu; storage=%s; source_worker=%s",
                result == HIK_OK ? "OK" : "retrying", hik_reason(result), poll_stages[stage], atomic_load(&poll_http_status),
                atomic_load(&poll_failures), zone_config_get()->hik_host,
                (unsigned long)((esp_timer_get_time() - started) / 1000), (unsigned long)cursor.serial,
                (unsigned long)depth, storage.persistence_verified && !storage.last_error ? "verified" : "unverified",
                workers_ready ? "running" : "waiting");
            ESP_LOGI(TAG, "%s", message);
            if (add_connector_log(result == HIK_OK ? "INFO" : "WARN", "hikvision", "HIK_POLL_STATUS", message)) {
                previous_result = result; previous_led = status; last_report_us = started;
            }
        }
        atomic_store(&poll_tick, (uint32_t)(esp_timer_get_time() / 1000));
        background_slot = true;
        xSemaphoreGive(request_lock);
        vTaskDelay(pdMS_TO_TICKS(poll_delay_ms(esp_timer_get_time() - started)));
    }
}
/* Low-rate independent audit. It preserves retained records through the same
 * durable source queue without advancing the live poll checkpoint or claiming
 * full-history/Oracle certification. A page is checkpointed only after custody. */
typedef struct {
    uint32_t version, first, last, cursor, scanned, completed_epoch;
    unsigned char binding[32], anchor[32];
} light_checkpoint_t;
static light_checkpoint_t light_checkpoint;
static bool light_loaded;
static int64_t light_next_us = 60000000;
static atomic_uint light_state, light_scanned, light_cursor, light_completed, light_error;
enum { LIGHT_WAITING, LIGHT_SCANNING, LIGHT_DELIVERY, LIGHT_TERMINAL, LIGHT_RETRY, LIGHT_COMPLETE, LIGHT_BLOCKED };
static const char *light_names[] = {"WAITING", "SCANNING", "WAITING_DELIVERY", "WAITING_TERMINAL", "RETRYING", "COMPLETE", "BLOCKED"};
static bool light_store(const light_checkpoint_t *state)
{
    nvs_handle_t h;
    if (nvs_open("hik_light", NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(h, "checkpoint", state, sizeof(*state));
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h); return err == ESP_OK;
}
static bool light_load(void)
{
    poll_checkpoint_t live; bool present;
    if (!checkpoint_load(&live, &present)) return false;
    light_checkpoint_t state = {.version = 1};
    memcpy(state.binding, live.binding, 32);
    nvs_handle_t h;
    esp_err_t err = nvs_open("hik_light", NVS_READONLY, &h);
    if (err != ESP_ERR_NVS_NOT_FOUND) {
        if (err != ESP_OK) return false;
        light_checkpoint_t stored; size_t size = sizeof(stored);
        err = nvs_get_blob(h, "checkpoint", &stored, &size); nvs_close(h);
        if (err != ESP_ERR_NVS_NOT_FOUND) {
            if (err != ESP_OK || size != sizeof(stored) || stored.version != 1 ||
                memcmp(stored.binding, state.binding, 32) || stored.last > 3000000000U ||
                stored.cursor > stored.last || (stored.last && (!stored.first || stored.first > stored.last))) return false;
            state = stored;
        }
    }
    light_checkpoint = state;
    atomic_store(&light_scanned, state.scanned);
    atomic_store(&light_cursor, state.cursor);
    atomic_store(&light_completed, state.completed_epoch);
    return true;
}
static void light_report(unsigned state, hik_result_t result)
{
    unsigned previous = atomic_exchange(&light_state, state);
    unsigned old_error = atomic_exchange(&light_error, result);
    if (previous == state && old_error == (unsigned)result) return;
    char message[224];
    snprintf(message, sizeof(message), "Light audit %s; reason=%s; range=%lu..%lu; saved_cursor=%lu; preserved=%lu; live polling remains active",
        light_names[state], hik_reason(result), (unsigned long)light_checkpoint.first,
        (unsigned long)light_checkpoint.last, (unsigned long)light_checkpoint.cursor,
        (unsigned long)light_checkpoint.scanned);
    ESP_LOGI(TAG, "%s", message);
    (void)add_connector_log(result == HIK_OK ? "INFO" : "WARN", "hikvision", "HIK_LIGHT_RECONCILE", message);
}
static void light_step(void)
{
    int64_t now = esp_timer_get_time();
    if (now < light_next_us) return;
    light_next_us = now + 30000000; /* at most one page every 30 seconds */
    if (!light_loaded) {
        if (!light_load()) { light_report(LIGHT_BLOCKED, HIK_CUSTODY); return; }
        light_loaded = true;
    }
    if (!atomic_load(&reachable)) { light_report(LIGHT_TERMINAL, HIK_NETWORK); return; }
    uint32_t depth;
    if (!add_connector_is_connected() || !qs_snapshot(QS_HIK_SOURCE, &depth) || depth >= 40) {
        light_report(LIGHT_DELIVERY, HIK_OK); return;
    }
    light_checkpoint_t next = light_checkpoint;
    time_t epoch = time(NULL);
    if (!next.last) {
        /* Persisted completion survives reboot; bad wall time never starts a
         * rapid loop. Monotonic page pacing remains in force during recovery. */
        if (next.completed_epoch && (epoch < next.completed_epoch || epoch - next.completed_epoch < 21600)) {
            light_report(LIGHT_COMPLETE, HIK_OK); return;
        }
        uint32_t first, last, count;
        hik_result_t result = hik_history_bounds(&first, &last, &count);
        if (result != HIK_OK) { light_report(LIGHT_RETRY, result); return; }
        uint32_t live_cursor = atomic_load(&poll_cursor);
        if (count && (!live_cursor || first > live_cursor)) { light_report(LIGHT_WAITING, HIK_OK); return; }
        next.first = first; next.last = last < live_cursor ? last : live_cursor;
        next.cursor = next.scanned = 0; memset(next.anchor, 0, 32);
        if (!count) { next.first = next.last = 0; next.completed_epoch = epoch; }
        if (!light_store(&next)) { light_report(LIGHT_RETRY, HIK_CUSTODY); return; }
        light_checkpoint = next;
        atomic_store(&light_scanned, 0); atomic_store(&light_cursor, 0);
        if (!count) { atomic_store(&light_completed, epoch); light_report(LIGHT_COMPLETE, HIK_OK); return; }
        light_report(LIGHT_SCANNING, HIK_OK);
        return;
    }
    hik_search_t search; hik_search_init(&search, next.cursor ? next.cursor : next.first, next.last);
    page_context_t page = {.channel = "HISTORY", .expected_anchor = next.cursor ? next.anchor : NULL};
    hik_result_t result = hik_history_page(&search, poll_record, &page);
    if (page.anchor_conflict || (result == HIK_OK && page.expected_anchor && !page.anchor_seen)) result = HIK_BINDING;
    if (result != HIK_OK) { light_report(result == HIK_BINDING ? LIGHT_BLOCKED : LIGHT_RETRY, result); return; }
    if (page.count) { next.cursor = search.previous_serial; memcpy(next.anchor, page.digest, 32); next.scanned += page.count; }
    if (search.complete) { next.first = next.last = next.cursor = 0; next.completed_epoch = epoch; }
    if (!light_store(&next)) { light_report(LIGHT_RETRY, HIK_CUSTODY); return; }
    light_checkpoint = next;
    atomic_store(&light_scanned, next.scanned); atomic_store(&light_cursor, next.cursor);
    atomic_store(&light_completed, next.completed_epoch);
    light_report(search.complete ? LIGHT_COMPLETE : LIGHT_SCANNING, HIK_OK);
}

static void history_task(void *arg)
{
    (void)arg;
    char *assignment = malloc(2048);
    if (!assignment) { vTaskDelete(NULL); return; }
    for (;;) {
        atomic_store(&history_tick, (uint32_t)(esp_timer_get_time() / 1000));
        vTaskDelay(pdMS_TO_TICKS(100));
        if (xSemaphoreTake(request_lock, pdMS_TO_TICKS(50)) != pdTRUE) continue;
        if (atomic_load(&profiles_active) || !background_slot) {
            xSemaphoreGive(request_lock); continue;
        }
        if (!add_connector_take_hikvision_assignment(assignment)) {
            if (esp_timer_get_time() >= light_next_us) {
                (void)take_background_slot();
                light_step();
            }
            xSemaphoreGive(request_lock); continue;
        }
        (void)take_background_slot();
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
                bool accepted = add_connector_send_payload_acknowledged("hikvision_history_page", body, 10000);
                if (accepted) atomic_fetch_add(&history_page_count, 1);
                (void)add_connector_log(accepted ? "INFO" : "WARN", "hikvision",
                    accepted ? "HIK_HISTORY_PAGE_ACCEPTED" : "HIK_HISTORY_RECEIPT_PENDING",
                    accepted ? "Reconciliation page accepted by ADD; live polling remains active"
                             : "Reconciliation receipt pending; live polling remains active");
                free(body);
            }
        } else {
            ESP_LOGW(TAG, "History request incomplete, reason=%u", (unsigned)result);
            char message[80]; snprintf(message, sizeof(message), "History request retrying; reason=%s", hik_reason(result));
            (void)add_connector_log("WARN", "hikvision", "HIK_HISTORY_RETRY", message);
        }
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
        atomic_store(&profiles_active, true);
        if (requested && strcmp(command.command_type, "REFRESH_USERS")) {
            for (;;) {
                vTaskDelay(pdMS_TO_TICKS(200));
                if (!add_connector_is_connected()) break;
                if (xSemaphoreTake(request_lock, pdMS_TO_TICKS(50)) != pdTRUE) continue;
                if (!take_background_slot()) { xSemaphoreGive(request_lock); continue; }
                cJSON *receipt = NULL;
                hik_result_t result = command.expires_epoch > 0 && time(NULL) >= command.expires_epoch
                    ? HIK_CONFIGURATION : hik_profile_command(&command, &receipt);
                xSemaphoreGive(request_lock);
                char *body = receipt ? cJSON_PrintUnformatted(receipt) : NULL;
                bool done = result == HIK_OK && body;
                bool rejected = result == HIK_BINDING || result == HIK_CONFIGURATION;
                bool sent = add_connector_command_update(command.command_id,
                    done ? "SUCCEEDED" : rejected ? "FAILED" : "RETRYING",
                    done ? NULL : rejected ? "HIK_PROFILE_PRECONDITION_FAILED" : "HIK_PROFILE_VERIFICATION_PENDING",
                    done ? NULL : rejected ? "Terminal identity, profile state, or requested fields failed validation; refresh before retrying" : "Terminal readback remains pending",
                    body ? body : "{}");
                free(body); cJSON_Delete(receipt);
                if ((done || rejected) && sent) (void)add_connector_command_complete(command.command_id);
                else add_connector_command_retry(command.command_id);
                requested = false;
                break;
            }
            if (requested) { add_connector_command_retry(command.command_id); requested = false; }
        }
        hik_search_t scan; hik_search_init(&scan, 1, 1);
        char snapshot_id[33]; memcpy(snapshot_id, scan.search_id, sizeof(snapshot_id));
        bool failed = false;
        for (unsigned phase = 1; phase <= 2 && !failed; phase++) {
            if (phase == 2) hik_search_init(&scan, 1, 1);
            while (!scan.complete && !failed) {
                vTaskDelay(pdMS_TO_TICKS(200));
                if (!add_connector_is_connected()) { failed = true; break; }
                if (xSemaphoreTake(request_lock, pdMS_TO_TICKS(50)) != pdTRUE) continue;
                if (!take_background_slot()) {
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
        uint32_t tick = atomic_load(&uploader_tick);
        int64_t now = esp_timer_get_time() / 1000;
        uint32_t age = (uint32_t)now - tick;
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
    append_clock_evidence(terminal);
    cJSON_AddStringToObject(terminal, "vendor", "hikvision");
    cJSON_AddStringToObject(terminal, "protocol", "isapi");
    cJSON_AddStringToObject(terminal, "serial", cfg->hik_expected_serial);
    cJSON_AddStringToObject(terminal, "ip_address", cfg->hik_host);
    cJSON_AddStringToObject(terminal, "capability_profile", cfg->hik_profile);
    cJSON_AddStringToObject(terminal, "qualification_state", "NOT_QUALIFIED");
    cJSON_AddBoolToObject(terminal, "online", atomic_load(&reachable));
    cJSON_AddStringToObject(terminal, "connection_state", atomic_load(&reachable) ? "ONLINE" : "OFFLINE");
    cJSON_AddStringToObject(terminal, "capture_mode", "poll");
    cJSON_AddNumberToObject(terminal, "poll_interval_seconds", 2);
    cJSON_AddNumberToObject(terminal, "poll_count", atomic_load(&poll_count));
    cJSON_AddNumberToObject(terminal, "last_poll_interval_ms", atomic_load(&last_poll_interval_ms));
    cJSON_AddNumberToObject(terminal, "history_page_count", atomic_load(&history_page_count));
    cJSON *light = cJSON_AddObjectToObject(terminal, "light_reconcile");
    if (light) {
        cJSON_AddStringToObject(light, "state", light_names[atomic_load(&light_state)]);
        cJSON_AddStringToObject(light, "policy", "retained_history_20_records_30s_repeat_6h");
        cJSON_AddNumberToObject(light, "scanned", atomic_load(&light_scanned));
        cJSON_AddNumberToObject(light, "cursor", atomic_load(&light_cursor));
        cJSON_AddNumberToObject(light, "last_completed_epoch", atomic_load(&light_completed));
        cJSON_AddNumberToObject(light, "error", atomic_load(&light_error));
    }
    cJSON_AddNumberToObject(terminal, "profile_command_version", 2);
    cJSON_AddNumberToObject(terminal, "last_successful_poll_epoch", atomic_load(&last_poll));
    cJSON_AddNumberToObject(terminal, "poll_error", atomic_load(&poll_error));
    cJSON_AddStringToObject(terminal, "poll_stage", poll_stages[atomic_load(&poll_stage)]);
    cJSON_AddNumberToObject(terminal, "poll_http_status", atomic_load(&poll_http_status));
    cJSON_AddNumberToObject(terminal, "poll_duration_ms", atomic_load(&poll_duration_ms));
    cJSON_AddNumberToObject(terminal, "consecutive_poll_failures", atomic_load(&poll_failures));
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
    /* The common heartbeat started before queue/worker sampling. Stamp its
     * uptime after collection so a later worker tick is not in its future. */
    cJSON *uptime = cJSON_GetObjectItemCaseSensitive(payload, "uptime_seconds");
    if (cJSON_IsNumber(uptime)) cJSON_SetNumberValue(uptime, (esp_timer_get_time() / 1000000));
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
        xSemaphoreTake(source_upload_lock, portMAX_DELAY);
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
        xSemaphoreGive(source_upload_lock);
        vTaskDelay(pdMS_TO_TICKS(state == DQ_OK ? 50 : 1000));
    }
}
void hikvision_gateway_task(void *argument)
{
    (void)argument;
    TaskHandle_t stream = NULL, uploader = NULL, history = NULL, profiles = NULL;
    request_lock = xSemaphoreCreateMutex();
    source_upload_lock = xSemaphoreCreateMutex();
    profile_commands = xQueueCreate(1, sizeof(add_command_t));
    if (!request_lock || !source_upload_lock || !profile_commands) { ESP_LOGE(TAG, "Request worker allocation failed"); vTaskDelete(NULL); return; }
    for (;;) {
        if (!stream && xTaskCreate(poll_task, "hik_poll", 8192, NULL, 5, &stream) != pdPASS) stream = NULL;
        if (!history && xTaskCreate(history_task, "hik_history", 8192, NULL, 3, &history) != pdPASS) history = NULL;
        if (!profiles && xTaskCreate(profile_task, "hik_profiles", 8192, NULL, 2, &profiles) != pdPASS) profiles = NULL;
        if (!uploader && xTaskCreate(source_uploader, "hik_evidence", 8192, NULL, 4, &uploader) != pdPASS) uploader = NULL;
        atomic_store(&runtime_workers_started, stream && history && profiles && uploader);
        if (!stream || !history || !profiles || !uploader) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        if (profiles && uxQueueSpacesAvailable(profile_commands)) {
            add_command_t command;
            if (add_connector_take_command(&command)) {
                if (!strcmp(command.command_type, "REFRESH_USERS") ||
                    !strcmp(command.command_type, "CREATE_USER") ||
                    !strcmp(command.command_type, "UPDATE_USER") ||
                    !strcmp(command.command_type, "DELETE_USER")) {
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
        add_connector_set_activity("HIKVISION_POLL_2S");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

/* Called only by the OTA task immediately before esp_restart. Retain both
 * mutexes until reset: terminal writes/pages and source settlement have ended,
 * and no new operation can race the reboot. A durable backlog may replay after
 * boot; external terminal reachability is not a restart requirement. */
bool hikvision_claim_ota_restart(void)
{
    if (!request_lock || !source_upload_lock || !atomic_load(&checkpoint_ready) ||
        !atomic_load(&runtime_workers_started)) return false;
    if (xSemaphoreTake(request_lock, 0) != pdTRUE) return false;
    if (xSemaphoreTake(source_upload_lock, 0) != pdTRUE) {
        xSemaphoreGive(request_lock);
        return false;
    }
    qs_health_t storage = qs_health();
    if (!storage.observed || !storage.available || !storage.recovery_complete ||
        !storage.persistence_verified || storage.last_error) {
        xSemaphoreGive(source_upload_lock);
        xSemaphoreGive(request_lock);
        return false;
    }
    return true;
}

/* OTA proves the installed application and recovered custody independently of
 * a powered-off external terminal. Capture faults remain reported to ADD. */
bool hikvision_boot_health_ready(void)
{
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    qs_health_t storage = qs_health();
    return atomic_load(&runtime_workers_started) && atomic_load(&checkpoint_ready) &&
        atomic_load(&poll_tick) && now - atomic_load(&poll_tick) <= 90000U &&
        atomic_load(&history_tick) && now - atomic_load(&history_tick) <= 90000U &&
        atomic_load(&uploader_started) && atomic_load(&uploader_buffer_ready) &&
        now - atomic_load(&uploader_tick) <= 90000U &&
        storage.observed && storage.available && storage.recovery_complete &&
        storage.persistence_verified && !storage.last_error;
}
