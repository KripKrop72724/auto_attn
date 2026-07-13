#include "add_connector.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#if __has_include("zone_lite_config.h")
#include "zone_lite_config.h"
#else
#include "zone_lite_config.example.h"
#endif

#ifndef ZONE_LITE_ADD_ENABLED
#define ZONE_LITE_ADD_ENABLED 0
#endif
#ifndef ZONE_LITE_ADD_WS_URL
#define ZONE_LITE_ADD_WS_URL ""
#endif
#ifndef ZONE_LITE_ADD_CONNECTOR_ID
#define ZONE_LITE_ADD_CONNECTOR_ID ""
#endif
#ifndef ZONE_LITE_ADD_DEVICE_TOKEN
#define ZONE_LITE_ADD_DEVICE_TOKEN ""
#endif
#ifndef ZONE_LITE_ADD_HEARTBEAT_SECONDS
#define ZONE_LITE_ADD_HEARTBEAT_SECONDS 15
#endif
#ifndef ZONE_LITE_ADD_RECONNECT_MS
#define ZONE_LITE_ADD_RECONNECT_MS 30000
#endif

#define ADD_COMMAND_QUEUE_DEPTH 8
#define ADD_SEND_TIMEOUT_MS 5000
#define ADD_MAX_INBOUND_BYTES 8192
#define ADD_ACK_TIMEOUT_MS 15000
#define ADD_OUTBOX_RETRY_MS 5000
#define ADD_TRANSPORT_RECOVERY_MS 45000
#define ADD_TRANSPORT_RESTART_GUARD_MS 45000
#define ADD_OUTBOX_LINE_BYTES 4096
#define ADD_OUTBOX_MAX_BYTES (4 * 1024 * 1024)
#define ADD_OUTBOX_PATH "/storage/add_pending.jsonl"
#define ADD_OUTBOX_TMP_PATH "/storage/add_pending.tmp"
#define ADD_OUTBOX_BACKUP_PATH "/storage/add_pending.bak"

static const char *TAG = "add_connector";
static esp_websocket_client_handle_t s_client;
static QueueHandle_t s_commands;
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_send_lock;
static SemaphoreHandle_t s_outbox_lock;
static SemaphoreHandle_t s_ack_sem;
static add_zkt_telemetry_t s_zkt;
static char s_activity[64] = "BOOTING";
static char s_boot_id[48];
static uint64_t s_sequence;
static bool s_started;
static bool s_connected;
static bool s_connected_edge;
static bool s_ack_matched;
static char s_waiting_ack[80];
static uint32_t s_outbox_depth;
static int64_t s_disconnected_since_ms;
static int64_t s_last_transport_restart_ms;

static int64_t monotonic_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static char *allocate_outbox_line_buffer(void)
{
    char *line = heap_caps_malloc(
        ADD_OUTBOX_LINE_BYTES,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!line) {
        line = malloc(ADD_OUTBOX_LINE_BYTES);
    }
    return line;
}

static size_t valid_utf8_sequence_length(const unsigned char *value, size_t remaining)
{
    if (remaining == 0) return 0;
    unsigned char first = value[0];
    if (first <= 0x7f) return 1;
    if (first >= 0xc2 && first <= 0xdf && remaining >= 2 &&
        value[1] >= 0x80 && value[1] <= 0xbf) {
        return 2;
    }
    if (remaining >= 3 && value[2] >= 0x80 && value[2] <= 0xbf) {
        if (first == 0xe0 && value[1] >= 0xa0 && value[1] <= 0xbf) return 3;
        if (first >= 0xe1 && first <= 0xec && value[1] >= 0x80 && value[1] <= 0xbf) return 3;
        if (first == 0xed && value[1] >= 0x80 && value[1] <= 0x9f) return 3;
        if (first >= 0xee && first <= 0xef && value[1] >= 0x80 && value[1] <= 0xbf) return 3;
    }
    if (remaining >= 4 && value[2] >= 0x80 && value[2] <= 0xbf &&
        value[3] >= 0x80 && value[3] <= 0xbf) {
        if (first == 0xf0 && value[1] >= 0x90 && value[1] <= 0xbf) return 4;
        if (first >= 0xf1 && first <= 0xf3 && value[1] >= 0x80 && value[1] <= 0xbf) return 4;
        if (first == 0xf4 && value[1] >= 0x80 && value[1] <= 0x8f) return 4;
    }
    return 0;
}

static char *sanitize_utf8_alloc(const char *value, uint32_t *invalid_bytes)
{
    if (!value) value = "";
    size_t input_len = strlen(value);
    char *safe = malloc(input_len + 1);
    if (!safe) return NULL;
    const unsigned char *input = (const unsigned char *)value;
    size_t read_at = 0;
    size_t write_at = 0;
    *invalid_bytes = 0;
    while (read_at < input_len) {
        size_t sequence = valid_utf8_sequence_length(input + read_at, input_len - read_at);
        if (sequence == 0) {
            safe[write_at++] = '?';
            read_at++;
            (*invalid_bytes)++;
            continue;
        }
        memcpy(safe + write_at, input + read_at, sequence);
        write_at += sequence;
        read_at += sequence;
    }
    safe[write_at] = '\0';
    return safe;
}

static void mark_transport_disconnected(void)
{
    bool wake_waiter = false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        wake_waiter = s_connected || s_waiting_ack[0] != '\0';
        s_connected = false;
        s_waiting_ack[0] = '\0';
        s_ack_matched = false;
        if (s_disconnected_since_ms == 0) {
            s_disconnected_since_ms = monotonic_ms();
        }
        xSemaphoreGive(s_lock);
    }
    if (wake_waiter && s_ack_sem) {
        xSemaphoreGive(s_ack_sem);
    }
}

static void iso_utc(time_t value, char out[32])
{
    struct tm tm_value = {0};
    gmtime_r(&value, &tm_value);
    strftime(out, 32, "%Y-%m-%dT%H:%M:%SZ", &tm_value);
}

static void json_add_epoch(cJSON *object, const char *name, int64_t epoch)
{
    if (epoch <= 0) {
        cJSON_AddNullToObject(object, name);
        return;
    }
    char value[32];
    iso_utc((time_t)epoch, value);
    cJSON_AddStringToObject(object, name, value);
}

static bool send_root_locked(cJSON *root)
{
    if (!s_client || !s_connected || !root || !esp_websocket_client_is_connected(s_client)) {
        mark_transport_disconnected();
        return false;
    }
    char *text = cJSON_PrintUnformatted(root);
    if (!text) {
        return false;
    }
    int sent = esp_websocket_client_send_text(
        s_client,
        text,
        (int)strlen(text),
        pdMS_TO_TICKS(ADD_SEND_TIMEOUT_MS));
    bool ok = sent == (int)strlen(text);
    free(text);
    if (!ok) {
        mark_transport_disconnected();
    }
    return ok;
}

static bool send_payload(
    const char *type,
    const char *payload_json,
    bool wait_for_ack,
    char message_id_out[80])
{
    if (!ZONE_LITE_ADD_ENABLED || !type || !payload_json) {
        return false;
    }
    uint32_t sanitized_bytes = 0;
    char *safe_payload_json = sanitize_utf8_alloc(payload_json, &sanitized_bytes);
    if (!safe_payload_json) {
        return false;
    }
    cJSON *payload = cJSON_Parse(safe_payload_json);
    free(safe_payload_json);
    if (!payload || !cJSON_IsObject(payload)) {
        cJSON_Delete(payload);
        return false;
    }
    if (sanitized_bytes > 0) {
        ESP_LOGW(
            TAG,
            "Sanitized %u invalid UTF-8 byte(s) before ADD send type=%s",
            (unsigned)sanitized_bytes,
            type);
    }
    if (xSemaphoreTake(s_send_lock, pdMS_TO_TICKS(ADD_SEND_TIMEOUT_MS)) != pdTRUE) {
        cJSON_Delete(payload);
        return false;
    }
    if (!s_client || !s_connected) {
        xSemaphoreGive(s_send_lock);
        cJSON_Delete(payload);
        return false;
    }

    char message_id[80];
    uint64_t seq;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        xSemaphoreGive(s_send_lock);
        cJSON_Delete(payload);
        return false;
    }
    seq = ++s_sequence;
    snprintf(message_id, sizeof(message_id), "%s-%llu", s_boot_id, (unsigned long long)seq);
    xSemaphoreGive(s_lock);

    time_t now;
    time(&now);
    char sent_at[32];
    iso_utc(now, sent_at);
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "schema_version", "1");
    cJSON_AddStringToObject(root, "message_id", message_id);
    cJSON_AddStringToObject(root, "connector_id", ZONE_LITE_ADD_CONNECTOR_ID);
    cJSON_AddStringToObject(root, "boot_id", s_boot_id);
    cJSON_AddNumberToObject(root, "seq", (double)seq);
    cJSON_AddStringToObject(root, "sent_at", sent_at);
    cJSON_AddStringToObject(root, "type", type);
    cJSON_AddItemToObject(root, "payload", payload);
    if (wait_for_ack) {
        while (xSemaphoreTake(s_ack_sem, 0) == pdTRUE) {
        }
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            strlcpy(s_waiting_ack, message_id, sizeof(s_waiting_ack));
            s_ack_matched = false;
            xSemaphoreGive(s_lock);
        } else {
            cJSON_Delete(root);
            xSemaphoreGive(s_send_lock);
            return false;
        }
    }
    bool ok = send_root_locked(root);
    cJSON_Delete(root);
    if (message_id_out) strlcpy(message_id_out, message_id, 80);
    if (!ok && wait_for_ack && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (strcmp(s_waiting_ack, message_id) == 0) s_waiting_ack[0] = '\0';
        xSemaphoreGive(s_lock);
    }
    xSemaphoreGive(s_send_lock);
    return ok;
}

bool add_connector_send_payload(const char *type, const char *payload_json)
{
    return send_payload(type, payload_json, false, NULL);
}

static void parse_inbound(const char *data, size_t len)
{
    if (len == 0 || len > ADD_MAX_INBOUND_BYTES) {
        return;
    }
    char *copy = calloc(1, len + 1);
    if (!copy) {
        return;
    }
    memcpy(copy, data, len);
    cJSON *root = cJSON_Parse(copy);
    free(copy);
    if (!root) {
        return;
    }
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (cJSON_IsString(type) && strcmp(type->valuestring, "ack") == 0) {
        cJSON *message_id = cJSON_GetObjectItemCaseSensitive(root, "message_id");
        if (cJSON_IsString(message_id) && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (strcmp(s_waiting_ack, message_id->valuestring) == 0) {
                s_waiting_ack[0] = '\0';
                s_ack_matched = true;
                xSemaphoreGive(s_ack_sem);
            }
            xSemaphoreGive(s_lock);
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) && strcmp(type->valuestring, "error") == 0) {
        cJSON *code = cJSON_GetObjectItemCaseSensitive(root, "code");
        cJSON *message_type = cJSON_GetObjectItemCaseSensitive(root, "message_type");
        cJSON *message_id = cJSON_GetObjectItemCaseSensitive(root, "message_id");
        ESP_LOGW(
            TAG,
            "ADD rejected outbound message code=%s type=%s",
            cJSON_IsString(code) ? code->valuestring : "UNKNOWN",
            cJSON_IsString(message_type) ? message_type->valuestring : "UNKNOWN");
        if (cJSON_IsString(message_id) && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (strcmp(s_waiting_ack, message_id->valuestring) == 0) {
                s_waiting_ack[0] = '\0';
                s_ack_matched = false;
                xSemaphoreGive(s_ack_sem);
            }
            xSemaphoreGive(s_lock);
        }
        cJSON_Delete(root);
        return;
    }
    cJSON *command_id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
    cJSON *command_type = cJSON_GetObjectItemCaseSensitive(root, "command_type");
    cJSON *payload = cJSON_GetObjectItemCaseSensitive(root, "payload");
    if (!cJSON_IsString(type) || strcmp(type->valuestring, "command") != 0 ||
        !cJSON_IsString(command_id) || !cJSON_IsString(command_type) || !cJSON_IsObject(payload)) {
        cJSON_Delete(root);
        return;
    }
    add_command_t command = {0};
    strlcpy(command.command_id, command_id->valuestring, sizeof(command.command_id));
    strlcpy(command.command_type, command_type->valuestring, sizeof(command.command_type));
    cJSON *value = cJSON_GetObjectItemCaseSensitive(payload, "uid");
    if (cJSON_IsString(value)) strlcpy(command.uid, value->valuestring, sizeof(command.uid));
    value = cJSON_GetObjectItemCaseSensitive(payload, "name");
    if (cJSON_IsString(value)) {
        strlcpy(command.name, value->valuestring, sizeof(command.name));
        command.has_name = true;
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "lease_id");
    if (cJSON_IsString(value)) strlcpy(command.lease_id, value->valuestring, sizeof(command.lease_id));
    value = cJSON_GetObjectItemCaseSensitive(payload, "privilege");
    if (cJSON_IsNumber(value)) {
        command.privilege = value->valueint;
        command.has_privilege = true;
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "duration_seconds");
    if (cJSON_IsNumber(value)) command.duration_seconds = value->valueint;
    if (xQueueSend(s_commands, &command, 0) != pdTRUE) {
        ESP_LOGW(TAG, "Command queue full; rejecting %s", command.command_id);
        add_connector_command_update(
            command.command_id,
            "FAILED",
            "COMMAND_QUEUE_FULL",
            "Connector is busy; retry this command.",
            "{}");
    } else {
        add_connector_command_update(command.command_id, "ACKNOWLEDGED", NULL, NULL, "{}");
    }
    cJSON_Delete(root);
}

static void websocket_event(void *arg, esp_event_base_t base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *event = event_data;
    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            s_connected = true;
            s_connected_edge = true;
            s_disconnected_since_ms = 0;
            xSemaphoreGive(s_lock);
        }
        ESP_LOGI(TAG, "ADD live control channel connected");
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        mark_transport_disconnected();
        ESP_LOGW(TAG, "ADD live control channel disconnected; client will reconnect");
        break;
    case WEBSOCKET_EVENT_DATA:
        if (event && event->op_code == 0x8) {
            uint16_t close_code = 0;
            if (event->data_len >= 2) {
                const uint8_t *bytes = (const uint8_t *)event->data_ptr;
                close_code = ((uint16_t)bytes[0] << 8) | bytes[1];
            }
            int reason_len = event->data_len > 2 ? event->data_len - 2 : 0;
            if (reason_len > 120) reason_len = 120;
            ESP_LOGW(
                TAG,
                "ADD WebSocket close frame code=%u reason=%.*s",
                close_code,
                reason_len,
                event->data_len > 2 ? event->data_ptr + 2 : "");
            mark_transport_disconnected();
        } else if (event && event->op_code == 0x1 && event->payload_offset == 0 &&
            event->data_len == event->payload_len) {
            parse_inbound(event->data_ptr, event->data_len);
        }
        break;
    case WEBSOCKET_EVENT_ERROR:
        mark_transport_disconnected();
        ESP_LOGW(TAG, "ADD WebSocket transport error");
        break;
    default:
        break;
    }
}

static void heartbeat_task(void *arg)
{
    (void)arg;
    while (true) {
        if (add_connector_is_connected()) {
            add_zkt_telemetry_t zkt;
            char activity[sizeof(s_activity)];
            if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                zkt = s_zkt;
                strlcpy(activity, s_activity, sizeof(activity));
                xSemaphoreGive(s_lock);
            } else {
                memset(&zkt, 0, sizeof(zkt));
                strlcpy(activity, "STATE_LOCK_BUSY", sizeof(activity));
            }
            wifi_ap_record_t ap = {0};
            int rssi = esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
            cJSON *payload = cJSON_CreateObject();
            cJSON_AddStringToObject(payload, "firmware_version", "zone-lite-2.0.1");
            cJSON_AddNumberToObject(payload, "config_version", 2);
            cJSON_AddNumberToObject(payload, "uptime_seconds", (double)(esp_timer_get_time() / 1000000));
            cJSON_AddNumberToObject(payload, "rssi", rssi);
            cJSON_AddNumberToObject(payload, "free_heap", esp_get_free_heap_size());
            cJSON_AddNumberToObject(payload, "outbox_depth", add_connector_outbox_depth());
            cJSON_AddStringToObject(payload, "current_activity", activity);
            cJSON_AddStringToObject(payload, "led_state", zkt.online ? "HEALTHY" : zkt.connection_state);
            cJSON *zkt_json = cJSON_AddObjectToObject(payload, "zkt");
            cJSON_AddBoolToObject(zkt_json, "online", zkt.online);
            cJSON_AddStringToObject(zkt_json, "connection_state", zkt.connection_state[0] ? zkt.connection_state : "UNKNOWN");
            cJSON_AddStringToObject(zkt_json, "ip_address", zkt.ip_address);
            cJSON_AddStringToObject(zkt_json, "serial", zkt.serial);
            cJSON_AddStringToObject(zkt_json, "model", zkt.model);
            cJSON_AddStringToObject(zkt_json, "platform", zkt.platform);
            if (zkt.device_time[0]) cJSON_AddStringToObject(zkt_json, "device_time", zkt.device_time);
            time_t now; time(&now); char sampled[32]; iso_utc(now, sampled);
            cJSON_AddStringToObject(zkt_json, "device_time_sampled_at", sampled);
            cJSON_AddStringToObject(zkt_json, "transition_reason", zkt.transition_reason);
            cJSON_AddNumberToObject(zkt_json, "user_count", zkt.user_count);
            cJSON_AddNumberToObject(zkt_json, "attendance_count", zkt.attendance_count);
            cJSON_AddNumberToObject(zkt_json, "consecutive_failures", zkt.consecutive_failures);
            cJSON_AddNumberToObject(zkt_json, "consecutive_successes", zkt.consecutive_successes);
            cJSON_AddNumberToObject(zkt_json, "flap_count_15m", zkt.flap_count_15m);
            cJSON_AddNumberToObject(zkt_json, "probe_latency_ms", zkt.probe_latency_ms);
            cJSON_AddNumberToObject(zkt_json, "user_record_size", zkt.user_record_size);
            json_add_epoch(zkt_json, "backoff_until", zkt.backoff_until_epoch);
            json_add_epoch(zkt_json, "stability_since", zkt.stability_since_epoch);
            json_add_epoch(zkt_json, "last_reconcile_at", zkt.last_reconcile_epoch);
            json_add_epoch(zkt_json, "next_restart_at", zkt.next_restart_epoch);
            char *json = cJSON_PrintUnformatted(payload);
            cJSON_Delete(payload);
            if (json) {
                (void)add_connector_send_payload("heartbeat", json);
                free(json);
            }
        } else if (s_client) {
            int64_t now_ms = monotonic_ms();
            int64_t disconnected_since = 0;
            if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                if (s_disconnected_since_ms == 0) s_disconnected_since_ms = now_ms;
                disconnected_since = s_disconnected_since_ms;
                xSemaphoreGive(s_lock);
            }
            if (disconnected_since > 0 &&
                now_ms - disconnected_since >= ADD_TRANSPORT_RECOVERY_MS &&
                now_ms - s_last_transport_restart_ms >= ADD_TRANSPORT_RESTART_GUARD_MS) {
                s_last_transport_restart_ms = now_ms;
                ESP_LOGW(TAG, "ADD transport remained offline; restarting WebSocket client");
                (void)esp_websocket_client_stop(s_client);
                vTaskDelay(pdMS_TO_TICKS(250));
                if (esp_websocket_client_start(s_client) != ESP_OK) {
                    ESP_LOGE(TAG, "ADD WebSocket client restart failed");
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_ADD_HEARTBEAT_SECONDS * 1000));
    }
}

static uint32_t count_outbox_rows(void)
{
    FILE *file = fopen(ADD_OUTBOX_PATH, "r");
    if (!file) return 0;
    char *line = allocate_outbox_line_buffer();
    if (!line) {
        fclose(file);
        ESP_LOGE(TAG, "Could not allocate ADD outbox scan buffer");
        return 0;
    }
    uint32_t count = 0;
    while (fgets(line, ADD_OUTBOX_LINE_BYTES, file)) {
        if (line[0] != '\0') count++;
    }
    free(line);
    fclose(file);
    return count;
}

static void restore_outbox_if_needed(void)
{
    struct stat pending = {0};
    struct stat backup = {0};
    bool has_pending = stat(ADD_OUTBOX_PATH, &pending) == 0;
    bool has_backup = stat(ADD_OUTBOX_BACKUP_PATH, &backup) == 0;
    if (!has_pending && has_backup) {
        if (rename(ADD_OUTBOX_BACKUP_PATH, ADD_OUTBOX_PATH) == 0) {
            ESP_LOGW(TAG, "Recovered ADD attendance outbox after interrupted compaction");
            has_pending = true;
        }
    }
    if (has_pending) {
        (void)remove(ADD_OUTBOX_BACKUP_PATH);
    }
    (void)remove(ADD_OUTBOX_TMP_PATH);
}

static bool remove_first_outbox_row_locked(void)
{
    FILE *in = fopen(ADD_OUTBOX_PATH, "r");
    if (!in) return false;
    FILE *out = fopen(ADD_OUTBOX_TMP_PATH, "w");
    if (!out) {
        fclose(in);
        return false;
    }
    char *line = allocate_outbox_line_buffer();
    if (!line) {
        fclose(in);
        fclose(out);
        (void)remove(ADD_OUTBOX_TMP_PATH);
        ESP_LOGE(TAG, "Could not allocate ADD outbox compaction buffer");
        return false;
    }
    bool skipped = false;
    bool ok = true;
    while (fgets(line, ADD_OUTBOX_LINE_BYTES, in)) {
        if (!skipped) {
            skipped = true;
            continue;
        }
        if (fputs(line, out) == EOF) {
            ok = false;
            break;
        }
    }
    if (ferror(in)) ok = false;
    if (fflush(out) != 0 || fsync(fileno(out)) != 0) ok = false;
    fclose(in);
    fclose(out);
    free(line);
    if (!ok || !skipped) {
        (void)remove(ADD_OUTBOX_TMP_PATH);
        return false;
    }
    (void)remove(ADD_OUTBOX_BACKUP_PATH);
    if (rename(ADD_OUTBOX_PATH, ADD_OUTBOX_BACKUP_PATH) != 0) {
        (void)remove(ADD_OUTBOX_TMP_PATH);
        return false;
    }
    if (rename(ADD_OUTBOX_TMP_PATH, ADD_OUTBOX_PATH) != 0) {
        (void)rename(ADD_OUTBOX_BACKUP_PATH, ADD_OUTBOX_PATH);
        return false;
    }
    (void)remove(ADD_OUTBOX_BACKUP_PATH);
    if (s_outbox_depth > 0) s_outbox_depth--;
    return true;
}

bool add_connector_enqueue_attendance(const char *payload_json)
{
    if (!ZONE_LITE_ADD_ENABLED) return true;
    if (!s_outbox_lock || !payload_json) return false;
    cJSON *payload = cJSON_Parse(payload_json);
    if (!payload || !cJSON_IsObject(payload)) {
        cJSON_Delete(payload);
        return false;
    }
    cJSON *record = cJSON_CreateObject();
    cJSON_AddStringToObject(record, "type", "attendance_batch");
    cJSON_AddItemToObject(record, "payload", payload);
    char *line = cJSON_PrintUnformatted(record);
    cJSON_Delete(record);
    if (!line || strlen(line) + 2 >= ADD_OUTBOX_LINE_BYTES) {
        free(line);
        return false;
    }
    bool ok = false;
    if (xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
        struct stat st = {0};
        off_t current = stat(ADD_OUTBOX_PATH, &st) == 0 ? st.st_size : 0;
        if (current + (off_t)strlen(line) + 1 <= ADD_OUTBOX_MAX_BYTES) {
            FILE *file = fopen(ADD_OUTBOX_PATH, "a");
            if (file) {
                ok = fprintf(file, "%s\n", line) > 0 && fflush(file) == 0 && fsync(fileno(file)) == 0;
                fclose(file);
                if (ok) s_outbox_depth++;
            }
        } else {
            ESP_LOGE(TAG, "ADD attendance outbox is full; preserving existing rows");
        }
        xSemaphoreGive(s_outbox_lock);
    }
    free(line);
    return ok;
}

static void outbox_task(void *arg)
{
    (void)arg;
    char *line = allocate_outbox_line_buffer();
    if (!line) {
        ESP_LOGE(TAG, "Could not allocate ADD outbox worker buffer");
        vTaskDelete(NULL);
        return;
    }
    while (true) {
        bool have_row = false;
        if (add_connector_is_connected() && add_connector_outbox_depth() > 0 &&
            xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
            FILE *file = fopen(ADD_OUTBOX_PATH, "r");
            if (file) {
                have_row = fgets(line, ADD_OUTBOX_LINE_BYTES, file) != NULL;
                fclose(file);
            }
            xSemaphoreGive(s_outbox_lock);
        }
        if (!have_row) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        line[strcspn(line, "\r\n")] = '\0';
        cJSON *record = cJSON_Parse(line);
        cJSON *type = record ? cJSON_GetObjectItemCaseSensitive(record, "type") : NULL;
        cJSON *payload = record ? cJSON_GetObjectItemCaseSensitive(record, "payload") : NULL;
        char *payload_json = cJSON_IsObject(payload) ? cJSON_PrintUnformatted(payload) : NULL;
        if (!cJSON_IsString(type) || !payload_json) {
            cJSON_Delete(record);
            free(payload_json);
            ESP_LOGE(TAG, "Dropping a corrupt ADD outbox row so later attendance can drain");
            if (xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
                (void)remove_first_outbox_row_locked();
                xSemaphoreGive(s_outbox_lock);
            }
            continue;
        }
        char message_id[80] = {0};
        bool sent = send_payload(type->valuestring, payload_json, true, message_id);
        cJSON_Delete(record);
        free(payload_json);
        bool awakened = sent && xSemaphoreTake(s_ack_sem, pdMS_TO_TICKS(ADD_ACK_TIMEOUT_MS)) == pdTRUE;
        bool acknowledged = false;
        if (awakened && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            acknowledged = s_ack_matched;
            s_ack_matched = false;
            xSemaphoreGive(s_lock);
        }
        if (acknowledged && xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
            if (!remove_first_outbox_row_locked()) {
                ESP_LOGE(TAG, "Could not compact acknowledged ADD attendance outbox");
            }
            xSemaphoreGive(s_outbox_lock);
        } else {
            if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                if (strcmp(s_waiting_ack, message_id) == 0) s_waiting_ack[0] = '\0';
                xSemaphoreGive(s_lock);
            }
            vTaskDelay(pdMS_TO_TICKS(ADD_OUTBOX_RETRY_MS));
        }
    }
}

void add_connector_init(void)
{
    if (!ZONE_LITE_ADD_ENABLED || s_started) {
        return;
    }
    s_lock = xSemaphoreCreateMutex();
    s_send_lock = xSemaphoreCreateMutex();
    s_outbox_lock = xSemaphoreCreateMutex();
    s_ack_sem = xSemaphoreCreateBinary();
    s_commands = xQueueCreate(ADD_COMMAND_QUEUE_DEPTH, sizeof(add_command_t));
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(
        s_boot_id,
        sizeof(s_boot_id),
        "%02x%02x%02x%02x%02x%02x-%08lx",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], (unsigned long)esp_random());
    strlcpy(s_zkt.connection_state, "BOOTING", sizeof(s_zkt.connection_state));
    s_started = s_lock && s_send_lock && s_outbox_lock && s_ack_sem && s_commands;
}

void add_connector_start(void)
{
    if (!s_started || s_client || ZONE_LITE_ADD_WS_URL[0] == '\0' ||
        ZONE_LITE_ADD_CONNECTOR_ID[0] == '\0' || ZONE_LITE_ADD_DEVICE_TOKEN[0] == '\0') {
        return;
    }
    char headers[512];
    snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-ADD-Connector-Id: %s\r\n", ZONE_LITE_ADD_DEVICE_TOKEN, ZONE_LITE_ADD_CONNECTOR_ID);
    esp_websocket_client_config_t config = {
        .uri = ZONE_LITE_ADD_WS_URL,
        .headers = headers,
        .subprotocol = "add-device-v1",
        .network_timeout_ms = 10000,
        .reconnect_timeout_ms = ZONE_LITE_ADD_RECONNECT_MS,
        .ping_interval_sec = 20,
        .disable_auto_reconnect = false,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    s_client = esp_websocket_client_init(&config);
    if (!s_client) {
        ESP_LOGE(TAG, "Could not initialize ADD WebSocket client");
        return;
    }
    esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, websocket_event, NULL);
    if (esp_websocket_client_start(s_client) != ESP_OK) {
        ESP_LOGE(TAG, "Could not start ADD WebSocket client");
        esp_websocket_client_destroy(s_client);
        s_client = NULL;
        return;
    }
    if (xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
        restore_outbox_if_needed();
        s_outbox_depth = count_outbox_rows();
        xSemaphoreGive(s_outbox_lock);
    }
    xTaskCreate(heartbeat_task, "add_heartbeat", 8192, NULL, 4, NULL);
    xTaskCreate(outbox_task, "add_outbox", 8192, NULL, 4, NULL);
}

bool add_connector_is_connected(void)
{
    bool connected = false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        connected = s_connected;
        xSemaphoreGive(s_lock);
    }
    if (connected && (!s_client || !esp_websocket_client_is_connected(s_client))) {
        mark_transport_disconnected();
        connected = false;
    }
    return connected;
}

bool add_connector_consume_connected_edge(void)
{
    bool edge = false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        edge = s_connected_edge;
        s_connected_edge = false;
        xSemaphoreGive(s_lock);
    }
    return edge;
}

uint32_t add_connector_outbox_depth(void)
{
    uint32_t depth = 0;
    if (s_outbox_lock && xSemaphoreTake(s_outbox_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        depth = s_outbox_depth;
        xSemaphoreGive(s_outbox_lock);
    }
    return depth;
}

void add_connector_set_activity(const char *activity)
{
    if (!s_lock || !activity) return;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        strlcpy(s_activity, activity, sizeof(s_activity));
        xSemaphoreGive(s_lock);
    }
}

void add_connector_set_zkt(const add_zkt_telemetry_t *telemetry)
{
    if (!s_lock || !telemetry) return;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        s_zkt = *telemetry;
        xSemaphoreGive(s_lock);
    }
}

bool add_connector_take_command(add_command_t *out)
{
    return s_commands && out && xQueueReceive(s_commands, out, 0) == pdTRUE;
}

bool add_connector_command_update(
    const char *command_id,
    const char *status,
    const char *error_code,
    const char *error_message,
    const char *result_json)
{
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "command_id", command_id ? command_id : "");
    cJSON_AddStringToObject(payload, "status", status ? status : "FAILED");
    cJSON *result = cJSON_Parse(result_json ? result_json : "{}");
    cJSON_AddItemToObject(payload, "result", result && cJSON_IsObject(result) ? result : cJSON_CreateObject());
    if (result && !cJSON_IsObject(result)) cJSON_Delete(result);
    if (error_code) cJSON_AddStringToObject(payload, "error_code", error_code);
    if (error_message) cJSON_AddStringToObject(payload, "error_message", error_message);
    char *json = cJSON_PrintUnformatted(payload);
    cJSON_Delete(payload);
    bool ok = json && add_connector_send_payload("command_update", json);
    free(json);
    return ok;
}

bool add_connector_log(
    const char *level,
    const char *subsystem,
    const char *code,
    const char *message)
{
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "level", level ? level : "INFO");
    cJSON_AddStringToObject(payload, "subsystem", subsystem ? subsystem : "firmware");
    if (code) cJSON_AddStringToObject(payload, "code", code);
    cJSON_AddStringToObject(payload, "message", message ? message : "");
    cJSON_AddItemToObject(payload, "context", cJSON_CreateObject());
    char *json = cJSON_PrintUnformatted(payload);
    cJSON_Delete(payload);
    bool ok = json && add_connector_send_payload("log", json);
    free(json);
    return ok;
}
