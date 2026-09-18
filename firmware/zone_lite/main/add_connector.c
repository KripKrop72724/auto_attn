#include "add_connector.h"
#include "firmware_family.h"
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
#include "hikvision_runtime.h"
#endif
#include "evidence_receipt.h"
#include "file_transaction.h"
#include "ota_manager.h"

#include <ctype.h>
#include <stdatomic.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_spiffs.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "mbedtls/base64.h"
#include "mbedtls/gcm.h"
#include "mbedtls/sha256.h"

#include "zone_config.h"
#include "reliability.h"
#include "legacy_queue.h"
#include "queue_store.h"
#include "storage_upgrade.h"
#include "worker_retry.h"
#include "delivery_scheduler.h"
#include "nvs.h"
#include "led_status.h"

#include "zone_lite_config.example.h"

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

#define ADD_COMMAND_QUEUE_DEPTH 32
// ADD serializes COMM Key revisions per connector and keeps the authoritative
// encrypted command journal on flash.  A second 32-entry queue would reserve
// roughly 35 KiB of heap for commands that can never execute in parallel and
// needlessly increases allocation pressure during an OTA boot.
#define ADD_CONFIG_COMMAND_QUEUE_DEPTH 2
#define ADD_TRACKED_COMMAND_CAPACITY \
    (ADD_COMMAND_QUEUE_DEPTH + ADD_CONFIG_COMMAND_QUEUE_DEPTH)
#define ADD_INBOUND_QUEUE_DEPTH 96
#define ADD_SEND_TIMEOUT_MS 5000
#define ADD_MAX_INBOUND_BYTES (512 * 1024)
#define ADD_IDENTITY_CATALOG_MAX_ROWS 4096
#define ADD_IDENTITY_CATALOG_MAX_BYTES (2U * 1024U * 1024U)
#define ADD_OUTBOX_ACK_TIMEOUT_MS 10000
#define ADD_PRIORITY_ACK_TIMEOUT_MS 10000
#define ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS 12000
#define ADD_PRIORITY_HOLD_MS 3000
#define ADD_OUTBOX_RETRY_MS 5000
#define ADD_OUTBOX_RETRY_MAX_MS 60000
#define ADD_TRANSPORT_RECOVERY_MS 45000
#define ADD_TRANSPORT_RESTART_GUARD_MS 45000
#define ADD_OUTBOX_LINE_BYTES 8192
#define ADD_OUTBOX_MAX_BYTES (4 * 1024 * 1024)
#define ADD_LIVE_OUTBOX_MAX_BYTES (512 * 1024)
#define ADD_OUTBOX_COMPACT_MIN_BYTES (256 * 1024)
#define ADD_BULK_CAPACITY_WAIT_MS (10 * 60 * 1000)
#define ADD_BULK_CAPACITY_POLL_MS 250
#define ADD_OUTBOX_RECORD_OVERHEAD_BYTES 128
#define ADD_OUTBOX_PATH "/storage/add_pending.jsonl"
#define ADD_OUTBOX_TMP_PATH "/storage/add_pending.tmp"
#define ADD_OUTBOX_BACKUP_PATH "/storage/add_pending.bak"
#define ADD_OUTBOX_CURSOR_PATH "/storage/add_pending.pos"
#define ADD_OUTBOX_CURSOR_TMP_PATH "/storage/add_pending.pos.tmp"
#define ADD_LIVE_OUTBOX_PATH "/storage/add_live.jsonl"
#define ADD_LIVE_OUTBOX_TMP_PATH "/storage/add_live.tmp"
#define ADD_LIVE_OUTBOX_BACKUP_PATH "/storage/add_live.bak"
#define ADD_LIVE_OUTBOX_CURSOR_PATH "/storage/add_live.pos"
#define ADD_LIVE_OUTBOX_CURSOR_TMP_PATH "/storage/add_live.pos.tmp"
#define ADD_CORRUPT_OUTBOX_PATH "/storage/add_corrupt.jsonl"
#define ADD_CORRUPT_OUTBOX_BACKUP_PATH "/storage/add_corrupt.bak"
#define ADD_CORRUPT_OUTBOX_MAX_BYTES (128 * 1024)
#define ADD_COMMAND_INBOX_PATH "/storage/add_commands.jsonl"
#define ADD_COMMAND_INBOX_TMP_PATH "/storage/add_commands.tmp"
#define ADD_COMMAND_INBOX_BACKUP_PATH "/storage/add_commands.bak"
#define ADD_COMMAND_INBOX_MAX_BYTES (64U * 1024U)
#define ADD_COMMAND_LINE_BYTES 12288
#define ADD_IDENTITY_CATALOG_PATH "/storage/add_identities.enc"
#define ADD_IDENTITY_CATALOG_TMP_PATH "/storage/add_identities.tmp"
#define ADD_IDENTITY_CATALOG_STAGE_PATH "/storage/add_identities.stage"
#define ADD_IDENTITY_CATALOG_BACKUP_PATH "/storage/add_identities.backup"
#define ADD_IDENTITY_CATALOG_COMMIT_PATH "/storage/add_identities.commit"
#define ADD_CANCELLED_COMMANDS_PATH "/storage/add_cancelled.txt"

typedef struct {
    const char *path;
    const char *tmp_path;
    const char *backup_path;
    const char *cursor_path;
    const char *cursor_tmp_path;
    off_t max_bytes;
    off_t offset;
    uint32_t depth;
    bool depth_known;
    uint32_t ack_since_checkpoint;
    const char *label;
    SemaphoreHandle_t lock;
    legacy_queue_t legacy;
    lq_token_t pending_token;
} add_outbox_t;

typedef struct {
    char receipt_id[40];
    char batch_id[121];
    char payload_digest[65];
    char outcome[52];
    uint32_t accepted;
    uint32_t duplicates;
    uint32_t quarantined;
    bool valid;
} add_attendance_settlement_ack_t;

typedef struct {
    char *data;
    size_t length;
} add_inbound_message_t;

typedef struct {
    char uid[41];
    char user_id[64];
    char display_name[96];
    char cnic[16];
    bool shift_worker;
} add_identity_alias_t;

static const char *TAG = "add_connector";
static esp_websocket_client_handle_t s_client;
static QueueHandle_t s_commands;
static QueueHandle_t s_config_commands;
static QueueHandle_t s_reconcile_assignments;
#ifdef ZONE_LITE_HIKVISION
static QueueHandle_t s_hikvision_assignments;
#endif
static QueueHandle_t s_source_coverage;
static QueueHandle_t s_inbound_messages;
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_send_lock;
static SemaphoreHandle_t s_ack_sem;
static SemaphoreHandle_t s_ack_wait_lock;
static SemaphoreHandle_t s_command_lock;
static SemaphoreHandle_t s_catalog_lock;
static add_zkt_telemetry_t s_zkt;
static char s_activity[64] = "BOOTING";
static bool s_ota_restart_claimed;
static char s_boot_id[48];
static uint64_t s_sequence;
static bool s_started;
static bool s_connected;
static bool s_connected_edge;
static bool s_ack_matched;
static bool s_waiting_evidence;
static bool s_waiting_hikvision;
static char s_hikvision_expected[65];
static evidence_receipt_t s_evidence_expected;
static add_attendance_settlement_ack_t s_attendance_settlement_ack;
static add_reconcile_chunk_ack_t s_reconcile_chunk_ack;
static add_source_tail_ack_t s_source_tail_ack;
static char s_reconcile_last_job_id[40];
static uint32_t s_reconcile_last_generation;
static uint32_t s_reconcile_last_committed_ordinal;
static bool s_onboarding_task_started;
static bool s_command_inbox_restored;
static char s_waiting_ack[80];
static char s_running_command_id[48];
static char s_queued_command_ids[ADD_TRACKED_COMMAND_CAPACITY][48];
static size_t s_queued_command_count;
static char *s_inbound_payload;
static size_t s_inbound_payload_expected;
static size_t s_inbound_payload_received;
static uint32_t s_identity_catalog_generation;
static size_t s_identity_catalog_rows;
static char s_identity_catalog_stage_id[40];
static size_t s_identity_catalog_stage_expected;
static size_t s_identity_catalog_stage_rows;
static add_identity_alias_t *s_identity_catalog_stage_aliases;
static size_t s_identity_catalog_stage_alias_capacity;
static bool s_identity_catalog_stage_file_ok;
static add_identity_alias_t *s_identity_catalog_active_aliases;
static size_t s_identity_catalog_active_alias_rows;
static bool s_identity_catalog_active_memory_valid;
static add_outbox_t s_bulk_outbox = {
    .path = ADD_OUTBOX_PATH,
    .tmp_path = ADD_OUTBOX_TMP_PATH,
    .backup_path = ADD_OUTBOX_BACKUP_PATH,
    .cursor_path = ADD_OUTBOX_CURSOR_PATH,
    .cursor_tmp_path = ADD_OUTBOX_CURSOR_TMP_PATH,
    .max_bytes = ADD_OUTBOX_MAX_BYTES,
    .label = "reconcile",
};
static add_outbox_t s_live_outbox = {
    .path = ADD_LIVE_OUTBOX_PATH,
    .tmp_path = ADD_LIVE_OUTBOX_TMP_PATH,
    .backup_path = ADD_LIVE_OUTBOX_BACKUP_PATH,
    .cursor_path = ADD_LIVE_OUTBOX_CURSOR_PATH,
    .cursor_tmp_path = ADD_LIVE_OUTBOX_CURSOR_TMP_PATH,
    .max_bytes = ADD_LIVE_OUTBOX_MAX_BYTES,
    .label = "live",
};
static int64_t s_disconnected_since_ms;
static int64_t s_last_transport_restart_ms;
static volatile uint32_t s_priority_delivery_until_ms;

static TaskHandle_t s_outbox_task_handle;
static atomic_bool s_background_ack_waiting;
static atomic_uint_least32_t s_background_ack_since_ms;
static TaskHandle_t s_heartbeat_task_handle;
static volatile uint32_t s_outbox_tick_ms;
static volatile uint32_t s_outbox_progress_ms;
static volatile bool s_outbox_buffer_ready;
static volatile bool s_worker_start_failed;
static worker_retry_t s_outbox_retry, s_heartbeat_retry;
static volatile uint32_t s_ords_start_attempts;
static volatile bool s_outboxes_initialized;
static volatile uint32_t s_ords_worker_tick_ms;
static volatile bool s_ords_worker_started;
static volatile add_worker_operation_t s_ords_worker_operation;
static volatile add_worker_operation_t s_add_worker_operation;
static void outbox_task(void *arg);
static void heartbeat_task(void *arg);

static bool attendance_event_uid_is_valid(const char *value);

static int64_t monotonic_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static bool priority_delivery_hold_active(void)
{
    uint32_t now = (uint32_t)monotonic_ms();
    return (int32_t)(s_priority_delivery_until_ms - now) > 0;
}

static const char *firmware_version(void)
{
    static char value[96];
    if (value[0] == '\0') {
        const esp_app_desc_t *description = esp_app_get_description();
        const char *version = description && description->version[0]
            ? description->version
            : "unknown";
        snprintf(value, sizeof(value), "zone-lite-%s", version);
    }
    return value;
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

static bool evidence_identity(const cJSON *payload, evidence_receipt_t *out, bool receipt)
{
    memset(out, 0, sizeof(*out));
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(payload, "schema_version");
    if (!cJSON_IsNumber(version) || version->valuedouble != 1) return false;
    out->version = 1;
    const char *names[] = {"connector_id", "queue", "queue_generation", "record_id", "payload_digest", "receipt_id", "disposition"};
    char *destinations[] = {out->connector_id, out->queue, out->generation, out->record_id, out->digest, out->receipt_id, out->disposition};
    size_t capacities[] = {sizeof(out->connector_id), sizeof(out->queue), sizeof(out->generation), sizeof(out->record_id), sizeof(out->digest), sizeof(out->receipt_id), sizeof(out->disposition)};
    for (unsigned i = 0; i < (receipt ? 7U : 5U); ++i) {
        const cJSON *field = cJSON_GetObjectItemCaseSensitive(payload, names[i]);
        if (!cJSON_IsString(field) || !field->valuestring[0] || strlen(field->valuestring) >= capacities[i]) return false;
        strlcpy(destinations[i], field->valuestring, capacities[i]);
    }
    return strlen(out->digest) == 64;
}

/* Takes ownership of payload on every outcome. An incomplete envelope is retryable. */
static cJSON *message_envelope(cJSON *payload, const char *type, const char *message_id,
    const char *connector_id, const char *boot_id, uint64_t seq, const char *sent_at)
{
    cJSON *root = cJSON_CreateObject();
    if (!root || !cJSON_AddStringToObject(root, "schema_version", "2") ||
        !cJSON_AddStringToObject(root, "message_id", message_id) ||
        !cJSON_AddStringToObject(root, "connector_id", connector_id) ||
        !cJSON_AddStringToObject(root, "boot_id", boot_id) ||
        !cJSON_AddNumberToObject(root, "seq", (double)seq) ||
        !cJSON_AddStringToObject(root, "sent_at", sent_at) ||
        !cJSON_AddStringToObject(root, "type", type) ||
        !cJSON_AddItemToObject(root, "payload", payload)) {
        cJSON_Delete(payload);
        cJSON_Delete(root);
        return NULL;
    }
    return root;
}

static bool send_payload(
    const char *type,
    const char *payload_json,
    bool wait_for_ack,
    char message_id_out[80])
{
    if (!zone_config_get()->add_enabled || !type || !payload_json) {
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
    evidence_receipt_t expected = {0};
    bool evidence = strcmp(type, "queue_evidence") == 0;
    bool hikvision = strcmp(type, "hikvision_observation") == 0;
    cJSON *hik_hash = cJSON_GetObjectItemCaseSensitive(payload, "observation_sha256");
    if (hikvision && (!cJSON_IsString(hik_hash) || strlen(hik_hash->valuestring) != 64 || sanitized_bytes)) {
        cJSON_Delete(payload);
        return false;
    }
    if (evidence && !evidence_identity(payload, &expected, false)) {
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
    cJSON *root = message_envelope(payload, type, message_id, zone_config_get()->connector_id,
        s_boot_id, seq, sent_at);
    if (!root) {
        xSemaphoreGive(s_send_lock);
        return false;
    }
    if (wait_for_ack) {
        while (xSemaphoreTake(s_ack_sem, 0) == pdTRUE) {
        }
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            strlcpy(s_waiting_ack, message_id, sizeof(s_waiting_ack));
            s_waiting_evidence = evidence;
            s_waiting_hikvision = hikvision;
            if (hikvision) strlcpy(s_hikvision_expected, hik_hash->valuestring, sizeof(s_hikvision_expected));
            s_evidence_expected = expected;
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

static bool send_payload_and_wait_for_ack(
    const char *type,
    const char *payload_json,
    TickType_t lock_timeout,
    TickType_t ack_timeout,
    add_reconcile_chunk_ack_t *reconcile_ack_out,
    add_attendance_settlement_ack_t *attendance_ack_out)
{
    bool background = s_outbox_task_handle && xTaskGetCurrentTaskHandle() == s_outbox_task_handle;
    if (background) {
        atomic_store(&s_background_ack_since_ms, (uint32_t)monotonic_ms());
        atomic_store(&s_background_ack_waiting, true);
    } else if (atomic_load(&s_background_ack_waiting) &&
        (uint32_t)((uint32_t)monotonic_ms() - atomic_load(&s_background_ack_since_ms)) < 90000U) {
        // A higher-priority direct sender must not reacquire the ACK mutex
        // before the scheduled background attempt gets service. Returning a
        // retry leaves its local record or authoritative source pending.
        return false;
    }
    if (!s_ack_wait_lock ||
        xSemaphoreTake(s_ack_wait_lock, lock_timeout) != pdTRUE) {
        if (background) atomic_store(&s_background_ack_waiting, false);
        return false;
    }
    char message_id[80] = {0};
    bool sent = send_payload(type, payload_json, true, message_id);
    bool awakened = sent && xSemaphoreTake(s_ack_sem, ack_timeout) == pdTRUE;
    bool acknowledged = false;
    if (awakened && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        acknowledged = s_ack_matched;
        if (acknowledged && reconcile_ack_out) {
            *reconcile_ack_out = s_reconcile_chunk_ack;
        }
        if (acknowledged && attendance_ack_out) {
            *attendance_ack_out = s_attendance_settlement_ack;
            acknowledged = attendance_ack_out->valid;
        }
        s_ack_matched = false;
        xSemaphoreGive(s_lock);
    }
    if (!acknowledged && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (strcmp(s_waiting_ack, message_id) == 0) s_waiting_ack[0] = '\0';
        xSemaphoreGive(s_lock);
    }
    xSemaphoreGive(s_ack_wait_lock);
    if (background) atomic_store(&s_background_ack_waiting, false);
    return acknowledged;
}

bool add_connector_send_payload(const char *type, const char *payload_json)
{
    return send_payload(type, payload_json, false, NULL);
}

bool add_connector_send_payload_acknowledged(
    const char *type,
    const char *payload_json,
    uint32_t timeout_ms)
{
    return send_payload_and_wait_for_ack(
        type,
        payload_json,
        portMAX_DELAY,
        pdMS_TO_TICKS(timeout_ms),
        NULL,
        NULL);
}

bool add_connector_send_reconcile_chunk_acknowledged(
    const char *payload_json,
    uint32_t timeout_ms,
    add_reconcile_chunk_ack_t *ack_out)
{
    if (!ack_out) return false;
    memset(ack_out, 0, sizeof(*ack_out));
    return send_payload_and_wait_for_ack(
        "reconcile_chunk",
        payload_json,
        portMAX_DELAY,
        pdMS_TO_TICKS(timeout_ms),
        ack_out,
        NULL);
}

bool add_connector_send_source_tail_acknowledged(
    const char *payload_json,
    uint32_t timeout_ms,
    add_source_tail_ack_t *ack_out)
{
    if (!ack_out) return false;
    memset(ack_out, 0, sizeof(*ack_out));
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        memset(&s_source_tail_ack, 0, sizeof(s_source_tail_ack));
        xSemaphoreGive(s_lock);
    }
    bool acknowledged = send_payload_and_wait_for_ack(
        "source_tail_chunk",
        payload_json,
        portMAX_DELAY,
        pdMS_TO_TICKS(timeout_ms),
        NULL,
        NULL);
    if (!acknowledged || xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return false;
    }
    *ack_out = s_source_tail_ack;
    xSemaphoreGive(s_lock);
    return ack_out->valid;
}

static const char *storage_key_material(void)
{
    const zone_config_t *runtime = zone_config_get();
    return runtime->bootstrap_secret[0] ? runtime->bootstrap_secret : runtime->device_token;
}

static char *encrypt_storage_json(const char *plain)
{
    const char *material = storage_key_material();
    if (!plain || !material || material[0] == '\0') return NULL;
    size_t plain_len = strlen(plain);
    size_t raw_len = 1 + 12 + 16 + plain_len;
    unsigned char *raw = malloc(raw_len);
    if (!raw) return NULL;
    raw[0] = 1;
    for (size_t index = 0; index < 12; index += 4) {
        uint32_t value = esp_random();
        memcpy(raw + 1 + index, &value, 4);
    }
    unsigned char key[32];
    if (mbedtls_sha256((const unsigned char *)material, strlen(material), key, 0) != 0) {
        free(raw); return NULL;
    }
    mbedtls_gcm_context context;
    mbedtls_gcm_init(&context);
    int result = mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key, 256);
    if (result == 0) {
        result = mbedtls_gcm_crypt_and_tag(
            &context,
            MBEDTLS_GCM_ENCRYPT,
            plain_len,
            raw + 1,
            12,
            NULL,
            0,
            (const unsigned char *)plain,
            raw + 29,
            16,
            raw + 13);
    }
    mbedtls_gcm_free(&context);
    if (result != 0) {
        free(raw);
        return NULL;
    }
    size_t encoded_size = ((raw_len + 2) / 3) * 4 + 1;
    unsigned char *encoded = malloc(encoded_size);
    size_t encoded_len = 0;
    if (!encoded || mbedtls_base64_encode(encoded, encoded_size, &encoded_len, raw, raw_len) != 0) {
        free(raw);
        free(encoded);
        return NULL;
    }
    encoded[encoded_len] = '\0';
    free(raw);
    return (char *)encoded;
}

static char *decrypt_storage_line(const char *line)
{
    if (!line) return NULL;
    while (*line == ' ' || *line == '\t') line++;
    if (*line == '{') return strdup(line);
    const char *material = storage_key_material();
    if (!material || material[0] == '\0') return NULL;
    size_t encoded_len = strcspn(line, "\r\n");
    size_t raw_size = (encoded_len * 3) / 4 + 4;
    unsigned char *raw = malloc(raw_size);
    size_t raw_len = 0;
    if (!raw || mbedtls_base64_decode(
                    raw,
                    raw_size,
                    &raw_len,
                    (const unsigned char *)line,
                    encoded_len) != 0 ||
        raw_len < 29 || raw[0] != 1) {
        free(raw);
        return NULL;
    }
    size_t plain_len = raw_len - 29;
    unsigned char *plain = calloc(1, plain_len + 1);
    if (!plain) { free(raw); return NULL; }
    unsigned char key[32];
    if (mbedtls_sha256((const unsigned char *)material, strlen(material), key, 0) != 0) {
        free(raw); free(plain); return NULL;
    }
    mbedtls_gcm_context context;
    mbedtls_gcm_init(&context);
    int result = mbedtls_gcm_setkey(&context, MBEDTLS_CIPHER_ID_AES, key, 256);
    if (result == 0) {
        result = mbedtls_gcm_auth_decrypt(
            &context,
            plain_len,
            raw + 1,
            12,
            NULL,
            0,
            raw + 13,
            16,
            raw + 29,
            plain);
    }
    mbedtls_gcm_free(&context);
    free(raw);
    if (result != 0) {
        free(plain);
        return NULL;
    }
    return (char *)plain;
}

static int catalog_transaction_load(void *context, ft_checkpoint_t *checkpoint)
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("file_tx", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t size = sizeof(*checkpoint);
    result = nvs_get_blob(handle, "catalog", checkpoint, &size);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && size == sizeof(*checkpoint) ? 1 : -1;
}

static bool catalog_transaction_commit(void *context, const ft_checkpoint_t *checkpoint)
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("file_tx", NVS_READWRITE, &handle);
    if (result != ESP_OK) return false;
    result = nvs_set_blob(handle, "catalog", checkpoint, sizeof(*checkpoint));
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}
static const ft_port_t catalog_transaction_port = {catalog_transaction_load, catalog_transaction_commit, NULL};

static bool recover_catalog_transaction_locked(void)
{
    bool ok = ft_recover(ADD_IDENTITY_CATALOG_PATH, ADD_IDENTITY_CATALOG_COMMIT_PATH,
        ADD_IDENTITY_CATALOG_BACKUP_PATH, catalog_transaction_port);
    // Only a fully written producer stage is renamed to the canonical commit
    // path. A first installation interrupted before prepare can finish here.
    if (!ok) ok = ft_replace(ADD_IDENTITY_CATALOG_PATH, ADD_IDENTITY_CATALOG_COMMIT_PATH,
        ADD_IDENTITY_CATALOG_BACKUP_PATH, ADD_IDENTITY_CATALOG_MAX_BYTES, catalog_transaction_port);
    if (!ok) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    return ok;
}

static FILE *create_catalog_stage(const char *path)
{
    if (!qs_local_begin(QS_ADMIT_HISTORICAL, 4096)) return NULL;
    FILE *file = fopen(path, "w");
    int captured = file ? 0 : errno;
    qs_local_end(file != NULL, captured);
    if (!file) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    return file;
}

static bool write_encrypted_json_line(FILE *file, cJSON *value)
{
    char *plain = value ? cJSON_PrintUnformatted(value) : NULL;
    char *encrypted = encrypt_storage_json(plain);
    size_t bytes = encrypted ? strlen(encrypted) + 1 : 0;
    long position = file && fseek(file, 0, SEEK_END) == 0 ? ftell(file) : -1;
    bool admitted = encrypted && bytes <= DQ_MAX_RECORD_BYTES && position >= 0 &&
        (uint64_t)position <= ADD_IDENTITY_CATALOG_MAX_BYTES &&
        bytes <= ADD_IDENTITY_CATALOG_MAX_BYTES - (size_t)position &&
        qs_local_begin(QS_ADMIT_HISTORICAL, bytes);
    bool ok = admitted && fprintf(file, "%s\n", encrypted) > 0 &&
        fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (admitted) qs_local_end(ok, ok ? 0 : errno);
    if (!ok) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    free(plain);
    free(encrypted);
    return ok;
}

static void recover_identity_catalog_backup_if_active_missing(void)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        led_status_fault(LED_STATUS_LOCAL_FAILURE); return;
    }
    ft_checkpoint_t checkpoint = {0};
    int loaded = catalog_transaction_load(NULL, &checkpoint);
    bool valid = loaded == 0 || (loaded == 1 && checkpoint.version == 1 && checkpoint.generation &&
        checkpoint.phase <= 2 && checkpoint.crc == dq_crc32(&checkpoint, offsetof(ft_checkpoint_t, crc)));
    if (!valid) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    // Prepared generations require bounded-buffer content verification. Defer
    // that scan until transport and its independent heartbeat have started.
    else if (!checkpoint.phase) (void)ft_recover(ADD_IDENTITY_CATALOG_PATH,
        ADD_IDENTITY_CATALOG_COMMIT_PATH, ADD_IDENTITY_CATALOG_BACKUP_PATH, catalog_transaction_port);
    xSemaphoreGive(s_catalog_lock);
}

static bool restore_valid_identity_catalog_locked(void)
{
    if (!recover_catalog_transaction_locked()) return false;
    FILE *file = fopen(ADD_IDENTITY_CATALOG_PATH, "r");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    bool ok = file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file);
    size_t row_count = 0;
    int expected_rows = -1;
    bool legacy_catalog = false;
    if (ok) {
        char *plain = decrypt_storage_line(line);
        cJSON *metadata = plain ? cJSON_Parse(plain) : NULL;
        free(plain);
        cJSON *type = metadata
            ? cJSON_GetObjectItemCaseSensitive(metadata, "type")
            : NULL;
        cJSON *rows = metadata
            ? cJSON_GetObjectItemCaseSensitive(metadata, "rows")
            : NULL;
        cJSON *rows_count = metadata
            ? cJSON_GetObjectItemCaseSensitive(metadata, "rows_count")
            : NULL;
        legacy_catalog = cJSON_IsArray(rows);
        if (!cJSON_IsString(type) ||
            strcmp(type->valuestring, "identity_catalog") != 0) {
            ok = false;
        } else if (legacy_catalog) {
            expected_rows = cJSON_GetArraySize(rows);
            cJSON *row = NULL;
            cJSON_ArrayForEach(row, rows) {
                if (!cJSON_IsObject(row)) ok = false;
            }
            row_count = expected_rows >= 0 ? (size_t)expected_rows : 0;
        } else if (cJSON_IsNumber(rows_count)) {
            expected_rows = rows_count->valueint;
        } else {
            ok = false;
        }
        cJSON_Delete(metadata);
    }
    if (ok && (expected_rows < 0 ||
               expected_rows > ADD_IDENTITY_CATALOG_MAX_ROWS)) {
        ok = false;
    }
    while (ok && !legacy_catalog && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        cJSON *row = plain ? cJSON_Parse(plain) : NULL;
        free(plain);
        if (!cJSON_IsObject(row) ||
            row_count >= (size_t)expected_rows) {
            ok = false;
        } else {
            row_count++;
        }
        cJSON_Delete(row);
    }
    if (file && ferror(file)) ok = false;
    if (ok && row_count != (size_t)expected_rows) ok = false;
    if (file && fclose(file) != 0) ok = false;
    free(line);
    if (!ok) {
        ESP_LOGW(TAG, "No complete encrypted ADD identity catalog was restored");
        return false;
    }
    if (!s_lock || xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return false;
    }
    s_identity_catalog_rows = row_count;
    s_identity_catalog_generation = 1;
    xSemaphoreGive(s_lock);
    ESP_LOGI(
        TAG,
        "Restored validated encrypted ADD identity catalog rows=%u",
        (unsigned)row_count);
    return true;
}

static bool restore_valid_identity_catalog(void)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = restore_valid_identity_catalog_locked();
    xSemaphoreGive(s_catalog_lock);
    return ok;
}

static bool activate_identity_catalog(const char *staged_path)
{
    if (!staged_path || !recover_catalog_transaction_locked()) return false;
    // The checked transaction is idle. A leftover canonical stage is an
    // uncommitted producer result; the active/backup generations were verified.
    errno = 0;
    if (remove(ADD_IDENTITY_CATALOG_COMMIT_PATH) != 0 && errno != ENOENT) return false;
    if (rename(staged_path, ADD_IDENTITY_CATALOG_COMMIT_PATH) != 0) return false;
    bool ok = ft_replace(ADD_IDENTITY_CATALOG_PATH, ADD_IDENTITY_CATALOG_COMMIT_PATH,
        ADD_IDENTITY_CATALOG_BACKUP_PATH, ADD_IDENTITY_CATALOG_MAX_BYTES, catalog_transaction_port);
    if (!ok) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    return ok;
}

static bool persist_identity_catalog_locked(cJSON *root, size_t *row_count_out)
{
    cJSON *rows = root ? cJSON_GetObjectItemCaseSensitive(root, "rows") : NULL;
    int row_count = cJSON_IsArray(rows) ? cJSON_GetArraySize(rows) : -1;
    if (row_count < 0 || row_count > ADD_IDENTITY_CATALOG_MAX_ROWS) {
        return false;
    }
    // Persist a bounded encrypted record stream instead of materializing a
    // second catalog-sized JSON string and ciphertext buffer.  The inbound
    // payload and cJSON tree already account for the catalog once; duplicating
    // both on large terminals can exhaust internal heap before boot health can
    // acknowledge the freshly delivered catalog.
    FILE *file = create_catalog_stage(ADD_IDENTITY_CATALOG_TMP_PATH);
    cJSON *metadata = cJSON_CreateObject();
    bool ok = file && metadata;
    if (ok) {
        ok = cJSON_AddStringToObject(metadata, "schema_version", "3") &&
            cJSON_AddStringToObject(metadata, "type", "identity_catalog") &&
            cJSON_AddNumberToObject(metadata, "rows_count", row_count) &&
            write_encrypted_json_line(file, metadata);
    }
    cJSON_Delete(metadata);
    cJSON *row = NULL;
    cJSON_ArrayForEach(row, rows) {
        if (ok && !write_encrypted_json_line(file, row)) {
            ok = false;
        }
    }
    if (file && (fflush(file) != 0 || fsync(fileno(file)) != 0)) ok = false;
    if (file && fclose(file) != 0) ok = false;
    if (ok) {
        ok = activate_identity_catalog(ADD_IDENTITY_CATALOG_TMP_PATH);
    }
    if (!ok) (void)remove(ADD_IDENTITY_CATALOG_TMP_PATH);
    if (ok) {
        // Catalog readers share s_catalog_lock: a committed flash generation
        // supersedes any older volatile aliases, including tombstone updates.
        s_identity_catalog_active_memory_valid = false;
    }
    if (ok && row_count_out) {
        *row_count_out = (size_t)row_count;
    }
    return ok;
}

static bool persist_identity_catalog(cJSON *root, size_t *row_count_out)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = persist_identity_catalog_locked(root, row_count_out);
    xSemaphoreGive(s_catalog_lock);
    return ok;
}

static void reset_identity_catalog_stage(bool remove_file)
{
    if (remove_file) {
        (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
    }
    free(s_identity_catalog_stage_aliases);
    s_identity_catalog_stage_aliases = NULL;
    s_identity_catalog_stage_alias_capacity = 0;
    s_identity_catalog_stage_file_ok = false;
    s_identity_catalog_stage_id[0] = '\0';
    s_identity_catalog_stage_expected = 0;
    s_identity_catalog_stage_rows = 0;
}

static bool identity_alias_from_json(
    cJSON *row,
    add_identity_alias_t *alias)
{
    if (!cJSON_IsObject(row) || !alias) return false;
    cJSON *uid = cJSON_GetObjectItemCaseSensitive(row, "uid");
    cJSON *user_id = cJSON_GetObjectItemCaseSensitive(row, "user_id");
    cJSON *display_name =
        cJSON_GetObjectItemCaseSensitive(row, "display_name");
    cJSON *cnic = cJSON_GetObjectItemCaseSensitive(row, "cnic");
    cJSON *shift_worker =
        cJSON_GetObjectItemCaseSensitive(row, "shift_worker");
    const char *uid_value = cJSON_IsString(uid) ? uid->valuestring : "";
    const char *user_id_value =
        cJSON_IsString(user_id) ? user_id->valuestring : "";
    const char *display_name_value =
        cJSON_IsString(display_name) ? display_name->valuestring : "";
    const char *cnic_value = cJSON_IsString(cnic) ? cnic->valuestring : "";
    if ((!uid_value[0] && !user_id_value[0]) ||
        strlen(uid_value) >= sizeof(alias->uid) ||
        strlen(user_id_value) >= sizeof(alias->user_id) ||
        strlen(display_name_value) >= sizeof(alias->display_name) ||
        strlen(cnic_value) >= sizeof(alias->cnic)) {
        return false;
    }
    if (cnic_value[0]) {
        if (strlen(cnic_value) != 13) return false;
        for (size_t i = 0; i < 13; i++) {
            if (!isdigit((unsigned char)cnic_value[i])) return false;
        }
    }
    memset(alias, 0, sizeof(*alias));
    strlcpy(alias->uid, uid_value, sizeof(alias->uid));
    strlcpy(alias->user_id, user_id_value, sizeof(alias->user_id));
    strlcpy(
        alias->display_name,
        display_name_value,
        sizeof(alias->display_name));
    strlcpy(alias->cnic, cnic_value, sizeof(alias->cnic));
    alias->shift_worker = cJSON_IsTrue(shift_worker);
    return true;
}

static bool identity_catalog_stage_begin(cJSON *root)
{
    cJSON *catalog_id = root
        ? cJSON_GetObjectItemCaseSensitive(root, "catalog_id")
        : NULL;
    cJSON *rows_count = root
        ? cJSON_GetObjectItemCaseSensitive(root, "rows_count")
        : NULL;
    int expected = cJSON_IsNumber(rows_count) ? rows_count->valueint : -1;
    if (!cJSON_IsString(catalog_id) || !catalog_id->valuestring[0] ||
        strlen(catalog_id->valuestring) >= sizeof(s_identity_catalog_stage_id) ||
        expected < 0 || expected > ADD_IDENTITY_CATALOG_MAX_ROWS) {
        return false;
    }
    reset_identity_catalog_stage(true);
    bool memory_ok = expected == 0;
    if (expected > 0) {
        s_identity_catalog_stage_aliases = heap_caps_calloc(
            (size_t)expected,
            sizeof(add_identity_alias_t),
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s_identity_catalog_stage_aliases) {
            s_identity_catalog_stage_aliases = calloc(
                (size_t)expected,
                sizeof(add_identity_alias_t));
        }
        memory_ok = s_identity_catalog_stage_aliases != NULL;
        if (memory_ok) {
            s_identity_catalog_stage_alias_capacity = (size_t)expected;
        }
    }
    FILE *file = create_catalog_stage(ADD_IDENTITY_CATALOG_STAGE_PATH);
    cJSON *metadata = cJSON_CreateObject();
    bool ok = file && metadata;
    if (ok) {
        ok = cJSON_AddStringToObject(metadata, "schema_version", "3") &&
            cJSON_AddStringToObject(metadata, "type", "identity_catalog") &&
            cJSON_AddNumberToObject(metadata, "rows_count", expected) &&
            write_encrypted_json_line(file, metadata) &&
            fflush(file) == 0 && fsync(fileno(file)) == 0;
    }
    cJSON_Delete(metadata);
    if (file && fclose(file) != 0) ok = false;
    s_identity_catalog_stage_file_ok = ok;
    if (!ok) {
        (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
    }
    if (!ok && !memory_ok) {
        reset_identity_catalog_stage(true);
        return false;
    }
    strlcpy(
        s_identity_catalog_stage_id,
        catalog_id->valuestring,
        sizeof(s_identity_catalog_stage_id));
    s_identity_catalog_stage_expected = (size_t)expected;
    s_identity_catalog_stage_rows = 0;
    return true;
}

static bool identity_catalog_stage_chunk(cJSON *root)
{
    cJSON *catalog_id = root
        ? cJSON_GetObjectItemCaseSensitive(root, "catalog_id")
        : NULL;
    cJSON *offset = root ? cJSON_GetObjectItemCaseSensitive(root, "offset") : NULL;
    cJSON *rows = root ? cJSON_GetObjectItemCaseSensitive(root, "rows") : NULL;
    int chunk_offset = cJSON_IsNumber(offset) ? offset->valueint : -1;
    int chunk_rows = cJSON_IsArray(rows) ? cJSON_GetArraySize(rows) : -1;
    if (!cJSON_IsString(catalog_id) ||
        strcmp(catalog_id->valuestring, s_identity_catalog_stage_id) != 0 ||
        chunk_offset < 0 ||
        (size_t)chunk_offset != s_identity_catalog_stage_rows ||
        chunk_rows < 0 ||
        s_identity_catalog_stage_rows + (size_t)chunk_rows >
            s_identity_catalog_stage_expected) {
        return false;
    }
    bool memory_ok = chunk_rows == 0 ||
        (s_identity_catalog_stage_aliases != NULL &&
         s_identity_catalog_stage_rows + (size_t)chunk_rows <=
             s_identity_catalog_stage_alias_capacity);
    bool rows_ok = memory_ok || s_identity_catalog_stage_file_ok;
    cJSON *row = NULL;
    size_t row_index = s_identity_catalog_stage_rows;
    cJSON_ArrayForEach(row, rows) {
        if (!cJSON_IsObject(row)) {
            rows_ok = false;
            break;
        }
        if (memory_ok && !identity_alias_from_json(
                row,
                &s_identity_catalog_stage_aliases[row_index])) {
            rows_ok = false;
            break;
        }
        row_index++;
    }
    if (!rows_ok) return false;

    if (s_identity_catalog_stage_file_ok) {
        FILE *file = rel_open_append(ADD_IDENTITY_CATALOG_STAGE_PATH);
        bool file_ok = file != NULL;
        cJSON_ArrayForEach(row, rows) {
            if (file_ok && !write_encrypted_json_line(file, row)) {
                file_ok = false;
            }
        }
        if (file && (fflush(file) != 0 || fsync(fileno(file)) != 0)) {
            file_ok = false;
        }
        if (file && fclose(file) != 0) file_ok = false;
        if (!file_ok) {
            s_identity_catalog_stage_file_ok = false;
            (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
        }
    }
    if (memory_ok || s_identity_catalog_stage_file_ok) {
        s_identity_catalog_stage_rows += (size_t)chunk_rows;
        return true;
    }
    return false;
}

static bool identity_catalog_stage_commit_locked(
    cJSON *root,
    size_t *row_count_out,
    bool *volatile_fallback_out)
{
    if (volatile_fallback_out) *volatile_fallback_out = false;
    cJSON *catalog_id = root
        ? cJSON_GetObjectItemCaseSensitive(root, "catalog_id")
        : NULL;
    cJSON *rows_count = root
        ? cJSON_GetObjectItemCaseSensitive(root, "rows_count")
        : NULL;
    int expected = cJSON_IsNumber(rows_count) ? rows_count->valueint : -1;
    bool ok = cJSON_IsString(catalog_id) &&
        strcmp(catalog_id->valuestring, s_identity_catalog_stage_id) == 0 &&
        expected >= 0 &&
        (size_t)expected == s_identity_catalog_stage_expected &&
        s_identity_catalog_stage_rows == s_identity_catalog_stage_expected;
    bool persisted = ok && s_identity_catalog_stage_file_ok &&
        activate_identity_catalog(ADD_IDENTITY_CATALOG_STAGE_PATH);
    bool memory_ready = ok &&
        (s_identity_catalog_stage_expected == 0 ||
         (s_identity_catalog_stage_aliases != NULL &&
          s_identity_catalog_stage_alias_capacity ==
              s_identity_catalog_stage_expected));
    add_identity_alias_t *old_active = NULL;
    if (memory_ready && s_lock &&
        xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        old_active = s_identity_catalog_active_aliases;
        s_identity_catalog_active_aliases =
            s_identity_catalog_stage_aliases;
        s_identity_catalog_active_alias_rows =
            s_identity_catalog_stage_expected;
        s_identity_catalog_active_memory_valid = true;
        s_identity_catalog_stage_aliases = NULL;
        s_identity_catalog_stage_alias_capacity = 0;
        xSemaphoreGive(s_lock);
    } else if (memory_ready && !persisted) {
        ok = false;
    }
    free(old_active);
    ok = ok && (persisted || memory_ready);
    if (ok && row_count_out) {
        *row_count_out = s_identity_catalog_stage_rows;
    }
    if (ok && volatile_fallback_out) {
        *volatile_fallback_out = !persisted && memory_ready;
    }
    if (!persisted) {
        (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
    }
    reset_identity_catalog_stage(false);
    return ok;
}

static bool identity_catalog_stage_commit(cJSON *root, size_t *row_count_out, bool *volatile_fallback_out)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = identity_catalog_stage_commit_locked(root, row_count_out, volatile_fallback_out);
    xSemaphoreGive(s_catalog_lock);
    return ok;
}

static cJSON *load_catalog_for_tombstone(void)
{
    errno = 0;
    FILE *file = fopen(ADD_IDENTITY_CATALOG_PATH, "r");
    if (!file) {
        if (errno != ENOENT) return NULL;
        cJSON *empty = cJSON_CreateObject();
        if (!empty || !cJSON_AddArrayToObject(empty, "rows")) {
            cJSON_Delete(empty); return NULL;
        }
        return empty;
    }
    struct stat st;
    bool ok = fstat(fileno(file), &st) == 0 && st.st_size > 0 &&
        st.st_size <= ADD_IDENTITY_CATALOG_MAX_BYTES;
    char *line = ok ? malloc(ADD_COMMAND_LINE_BYTES) : NULL;
    cJSON *root = NULL;
    if (line && fgets(line, ADD_COMMAND_LINE_BYTES, file) && strchr(line, '\n')) {
        char *plain = decrypt_storage_line(line);
        root = plain ? cJSON_Parse(plain) : NULL;
        free(plain);
    }
    ok = ok && cJSON_IsObject(root);
    cJSON *rows = root ? cJSON_GetObjectItemCaseSensitive(root, "rows") : NULL;
    if (ok && !cJSON_IsArray(rows)) {
        cJSON *count = cJSON_GetObjectItemCaseSensitive(root, "rows_count");
        int expected = cJSON_IsNumber(count) ? count->valueint : -1;
        ok = expected >= 0 && expected <= ADD_IDENTITY_CATALOG_MAX_ROWS;
        rows = ok ? cJSON_AddArrayToObject(root, "rows") : NULL;
        ok = ok && rows;
        int seen = 0;
        while (ok && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
            if (!strchr(line, '\n') || seen >= expected) { ok = false; break; }
            char *plain = decrypt_storage_line(line);
            cJSON *row = plain ? cJSON_Parse(plain) : NULL;
            free(plain);
            if (!cJSON_IsObject(row) || !cJSON_AddItemToArray(rows, row)) {
                cJSON_Delete(row); ok = false; break;
            }
            seen++;
        }
        ok = ok && seen == expected;
    } else if (ok) {
        // Legacy single-object catalogs cannot hide extra or truncated rows.
        ok = fgetc(file) == EOF && cJSON_GetArraySize(rows) <= ADD_IDENTITY_CATALOG_MAX_ROWS;
    }
    if (ferror(file)) ok = false;
    if (fclose(file) != 0) ok = false;
    free(line);
    if (!ok) { cJSON_Delete(root); return NULL; }
    return root;
}

static bool add_connector_persist_command_tombstone_locked(const add_command_t *command)
{
    if (!command || !command->has_tombstone || !command->user_id[0]) return false;
    if (!recover_catalog_transaction_locked()) return false;
    cJSON *root = load_catalog_for_tombstone();
    if (!root) return false; // OOM or a failed read must never become an empty catalog.
    cJSON *rows = cJSON_GetObjectItemCaseSensitive(root, "rows");
    cJSON *target = NULL;
    cJSON *row = NULL;
    cJSON_ArrayForEach(row, rows) {
        cJSON *user_id = cJSON_GetObjectItemCaseSensitive(row, "user_id");
        cJSON *uid = cJSON_GetObjectItemCaseSensitive(row, "uid");
        if (cJSON_IsString(user_id) && cJSON_IsString(uid) &&
            !strcmp(user_id->valuestring, command->user_id) && !strcmp(uid->valuestring, command->uid)) {
            target = row; break;
        }
    }
    if (!target) {
        target = cJSON_CreateObject();
        if (!target || !cJSON_AddItemToArray(rows, target)) {
            cJSON_Delete(target); cJSON_Delete(root); return false;
        }
    }
    cJSON_DeleteItemFromObjectCaseSensitive(target, "uid");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "user_id");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "display_name");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "cnic");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "shift_worker");
    bool ok = cJSON_AddStringToObject(target, "uid", command->uid) &&
        cJSON_AddStringToObject(target, "user_id", command->user_id) &&
        cJSON_AddStringToObject(target, "display_name", command->tombstone_display_name) &&
        cJSON_AddStringToObject(target, "cnic", command->tombstone_cnic) &&
        cJSON_AddBoolToObject(target, "shift_worker", command->tombstone_shift_worker) &&
        persist_identity_catalog_locked(root, NULL);
    cJSON_Delete(root);
    return ok;
}

bool add_connector_persist_command_tombstone(const add_command_t *command)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = add_connector_persist_command_tombstone_locked(command);
    xSemaphoreGive(s_catalog_lock);
    return ok;
}

static bool append_cancelled_command(const char *command_id)
{
    FILE *file = rel_open_append(ADD_CANCELLED_COMMANDS_PATH);
    bool ok = file && fprintf(file, "%s\n", command_id) > 0 && fflush(file) == 0 &&
              fsync(fileno(file)) == 0;
    if (file && fclose(file) != 0) ok = false;
    return ok;
}

static bool parse_command_object(cJSON *root, add_command_t *command)
{
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    cJSON *command_id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
    cJSON *command_type = cJSON_GetObjectItemCaseSensitive(root, "command_type");
    cJSON *payload = cJSON_GetObjectItemCaseSensitive(root, "payload");
    cJSON *expected = cJSON_GetObjectItemCaseSensitive(root, "expected_state");
    if (!command || !cJSON_IsString(type) || strcmp(type->valuestring, "command") != 0 ||
        !cJSON_IsString(command_id) || !cJSON_IsString(command_type) || !cJSON_IsObject(payload)) {
        return false;
    }
    memset(command, 0, sizeof(*command));
#ifdef ZONE_LITE_HIKVISION
    /* Reject oversized identifiers before strlcpy can turn one employee into
     * another. ZKT's legacy numeric/binary command format remains separate. */
    const char *fields[] = {"uid", "user_id", "name"};
    const size_t limits[] = {32, 32, 128};
    for (unsigned i = 0; i < 3; i++) {
        cJSON *field = cJSON_GetObjectItemCaseSensitive(payload, fields[i]);
        if (field && (!cJSON_IsString(field) || strlen(field->valuestring) > limits[i])) return false;
        if (field && i < 2) {
            const char *p = field->valuestring;
            if (!*p) return false;
            for (; *p; p++) if (*p < '0' || *p > '9') return false;
        }
    }
    cJSON *serial = cJSON_GetObjectItemCaseSensitive(expected, "serial");
    if (serial && (!cJSON_IsString(serial) || strlen(serial->valuestring) >= sizeof(command->expected_serial))) return false;
    if (strlen(command_id->valuestring) >= sizeof(command->command_id) ||
        strlen(command_type->valuestring) >= sizeof(command->command_type)) return false;
#endif
    strlcpy(command->command_id, command_id->valuestring, sizeof(command->command_id));
    strlcpy(command->command_type, command_type->valuestring, sizeof(command->command_type));
    cJSON *expires = cJSON_GetObjectItemCaseSensitive(root, "expires_epoch");
    if (cJSON_IsNumber(expires)) command->expires_epoch = (int64_t)expires->valuedouble;
    cJSON *value = cJSON_GetObjectItemCaseSensitive(payload, "uid");
    if (cJSON_IsString(value)) strlcpy(command->uid, value->valuestring, sizeof(command->uid));
    value = cJSON_GetObjectItemCaseSensitive(payload, "user_id");
    if (cJSON_IsString(value)) strlcpy(command->user_id, value->valuestring, sizeof(command->user_id));
    value = cJSON_GetObjectItemCaseSensitive(payload, "user_key");
    if (cJSON_IsString(value)) strlcpy(command->user_key, value->valuestring, sizeof(command->user_key));
    value = cJSON_GetObjectItemCaseSensitive(payload, "name");
    if (cJSON_IsString(value)) {
        strlcpy(command->name, value->valuestring, sizeof(command->name));
        command->has_name = true;
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "lease_id");
    if (cJSON_IsString(value)) strlcpy(command->lease_id, value->valuestring, sizeof(command->lease_id));
    value = cJSON_GetObjectItemCaseSensitive(payload, "privilege");
    if (cJSON_IsNumber(value)) {
        command->privilege = value->valueint;
        command->has_privilege = true;
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "duration_seconds");
    if (cJSON_IsNumber(value)) command->duration_seconds = value->valueint;
    value = cJSON_GetObjectItemCaseSensitive(payload, "lease_expires_epoch");
    if (cJSON_IsNumber(value)) command->lease_expires_epoch = (int64_t)value->valuedouble;
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(expected, "attendance_count")
                : NULL;
    if (cJSON_IsNumber(value)) {
        command->expected_attendance_count = value->valueint;
        command->has_expected_attendance_count = true;
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "config_field");
    if (cJSON_IsString(value)) {
        strlcpy(command->config_field, value->valuestring, sizeof(command->config_field));
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "operation_id");
    if (cJSON_IsString(value)) {
        strlcpy(
            command->config_operation_id,
            value->valuestring,
            sizeof(command->config_operation_id));
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "mode");
    if (cJSON_IsString(value)) {
        strlcpy(command->config_mode, value->valuestring, sizeof(command->config_mode));
    }
    value = cJSON_GetObjectItemCaseSensitive(payload, "revision");
    if (cJSON_IsNumber(value) && value->valuedouble >= 1 && value->valuedouble <= UINT32_MAX) {
        command->config_revision = (uint32_t)value->valuedouble;
    }
    cJSON *sealed = cJSON_GetObjectItemCaseSensitive(payload, "sealed_value");
    if (cJSON_IsObject(sealed)) {
        value = cJSON_GetObjectItemCaseSensitive(sealed, "version");
        if (cJSON_IsNumber(value) && value->valueint >= 0 && value->valueint <= UINT8_MAX) {
            command->sealed_version = (uint8_t)value->valueint;
        }
        value = cJSON_GetObjectItemCaseSensitive(sealed, "nonce");
        if (cJSON_IsString(value)) {
            strlcpy(command->sealed_nonce, value->valuestring, sizeof(command->sealed_nonce));
        }
        value = cJSON_GetObjectItemCaseSensitive(sealed, "ciphertext");
        if (cJSON_IsString(value)) {
            strlcpy(
                command->sealed_ciphertext,
                value->valuestring,
                sizeof(command->sealed_ciphertext));
        }
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(expected, "serial")
                : NULL;
    if (cJSON_IsString(value)) {
        strlcpy(
            command->expected_serial,
            value->valuestring,
            sizeof(command->expected_serial));
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(expected, "name")
                : NULL;
    if (cJSON_IsString(value)) {
        strlcpy(command->expected_name, value->valuestring, sizeof(command->expected_name));
        command->has_expected_name = true;
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(
                      expected,
                      "terminal_identity_fingerprint")
                : NULL;
    if (cJSON_IsString(value) && strlen(value->valuestring) == 64) {
        strlcpy(
            command->expected_terminal_identity_fingerprint,
            value->valuestring,
            sizeof(command->expected_terminal_identity_fingerprint));
        command->has_expected_terminal_identity_fingerprint = true;
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(
                      expected,
                      "terminal_state_fingerprint")
                : NULL;
    if (cJSON_IsString(value) && strlen(value->valuestring) == 64) {
        strlcpy(
            command->expected_terminal_state_fingerprint,
            value->valuestring,
            sizeof(command->expected_terminal_state_fingerprint));
        command->has_expected_terminal_state_fingerprint = true;
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(expected, "privilege")
                : NULL;
    if (cJSON_IsNumber(value)) {
        command->expected_privilege = value->valueint;
        command->has_expected_privilege = true;
    }
    value = cJSON_IsObject(expected)
                ? cJSON_GetObjectItemCaseSensitive(expected, "row_version")
                : NULL;
    if (cJSON_IsNumber(value)) {
        command->expected_version = value->valueint;
        command->has_expected_version = true;
    }
    cJSON *tombstone = cJSON_GetObjectItemCaseSensitive(payload, "tombstone");
    if (cJSON_IsObject(tombstone)) {
        cJSON *display_name = cJSON_GetObjectItemCaseSensitive(tombstone, "display_name");
        cJSON *cnic = cJSON_GetObjectItemCaseSensitive(tombstone, "cnic");
        cJSON *shift_worker = cJSON_GetObjectItemCaseSensitive(tombstone, "shift_worker");
        if (cJSON_IsString(display_name) && (cJSON_IsString(cnic) || cJSON_IsNull(cnic))) {
            strlcpy(
                command->tombstone_display_name,
                display_name->valuestring,
                sizeof(command->tombstone_display_name));
            if (cJSON_IsString(cnic)) {
                strlcpy(
                    command->tombstone_cnic,
                    cnic->valuestring,
                    sizeof(command->tombstone_cnic));
            }
            command->tombstone_shift_worker = cJSON_IsTrue(shift_worker);
            command->has_tombstone = true;
        }
    }
    return true;
}

static bool parse_reconcile_assignment(
    cJSON *root,
    add_reconcile_assignment_t *assignment)
{
    if (!root || !assignment) return false;
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    cJSON *job_id = cJSON_GetObjectItemCaseSensitive(root, "job_id");
    cJSON *generation = cJSON_GetObjectItemCaseSensitive(root, "generation");
    cJSON *expected_serial = cJSON_GetObjectItemCaseSensitive(
        root,
        "expected_terminal_serial");
    cJSON *committed = cJSON_GetObjectItemCaseSensitive(
        root,
        "committed_next_ordinal");
    cJSON *chunk_records = cJSON_GetObjectItemCaseSensitive(root, "chunk_records");
    bool source_probe = cJSON_IsString(type) &&
        strcmp(type->valuestring, "source_probe_assignment") == 0;
    if (!cJSON_IsString(type) ||
        (!source_probe && strcmp(type->valuestring, "reconcile_assignment") != 0) ||
        !cJSON_IsString(job_id) || strlen(job_id->valuestring) != 36 ||
        !cJSON_IsNumber(generation) || generation->valuedouble < 1 ||
        !cJSON_IsString(expected_serial) || expected_serial->valuestring[0] == '\0' ||
        (!source_probe && (!cJSON_IsNumber(committed) || committed->valuedouble < 0)) ||
        (!source_probe && (!cJSON_IsNumber(chunk_records) || chunk_records->valuedouble < 1))) {
        return false;
    }
    memset(assignment, 0, sizeof(*assignment));
    strlcpy(assignment->job_id, job_id->valuestring, sizeof(assignment->job_id));
    strlcpy(
        assignment->expected_terminal_serial,
        expected_serial->valuestring,
        sizeof(assignment->expected_terminal_serial));
    assignment->generation = (uint32_t)generation->valuedouble;
    assignment->source_probe = source_probe;
    if (source_probe) {
        cJSON *ordinal = cJSON_GetObjectItemCaseSensitive(root, "ordinal");
        if (!cJSON_IsNumber(ordinal) || ordinal->valuedouble < 0) return false;
        assignment->probe_ordinal = (uint32_t)ordinal->valuedouble;
        assignment->chunk_records = 1;
    } else {
        assignment->committed_next_ordinal = (uint32_t)committed->valuedouble;
        assignment->chunk_records = (uint16_t)(chunk_records->valueint > 100
            ? 100
            : chunk_records->valueint);
    }
    cJSON *protocol = cJSON_GetObjectItemCaseSensitive(root, "protocol");
    assignment->stream_v2 = cJSON_IsString(protocol) &&
        strcmp(protocol->valuestring, "history_stream_v2") == 0;
    cJSON *assignment_id = cJSON_GetObjectItemCaseSensitive(root, "assignment_id");
    if (cJSON_IsString(assignment_id) && strlen(assignment_id->valuestring) == 36) {
        strlcpy(
            assignment->assignment_id,
            assignment_id->valuestring,
            sizeof(assignment->assignment_id));
    }
    cJSON *credit_end = cJSON_GetObjectItemCaseSensitive(root, "credit_end_ordinal");
    if (cJSON_IsNumber(credit_end) && credit_end->valuedouble >= committed->valuedouble) {
        assignment->credit_end_ordinal = (uint32_t)credit_end->valuedouble;
    }
    cJSON *max_chunks = cJSON_GetObjectItemCaseSensitive(root, "max_chunks");
    if (cJSON_IsNumber(max_chunks) && max_chunks->valueint > 0) {
        assignment->max_chunks = (uint16_t)(max_chunks->valueint > 20
            ? 20
            : max_chunks->valueint);
    } else {
        assignment->max_chunks = 1;
    }
    cJSON *lease_epoch = cJSON_GetObjectItemCaseSensitive(root, "lease_expires_epoch");
    if (cJSON_IsNumber(lease_epoch)) {
        assignment->lease_expires_epoch = (int64_t)lease_epoch->valuedouble;
    }
    if (!assignment->source_probe && assignment->stream_v2 &&
        (assignment->assignment_id[0] == '\0' ||
         !cJSON_IsNumber(credit_end) ||
         credit_end->valuedouble < committed->valuedouble)) {
        return false;
    }
    cJSON *cutoff = cJSON_GetObjectItemCaseSensitive(root, "cutoff_count");
    if (cJSON_IsNumber(cutoff) && cutoff->valuedouble >= 0) {
        assignment->has_cutoff = true;
        assignment->cutoff_count = (uint32_t)cutoff->valuedouble;
    }
    cJSON *anchor = cJSON_GetObjectItemCaseSensitive(root, "first_anchor_digest");
    if (cJSON_IsString(anchor) && strlen(anchor->valuestring) == 64) {
        strlcpy(
            assignment->first_anchor_digest,
            anchor->valuestring,
            sizeof(assignment->first_anchor_digest));
    }
    cJSON *chain = cJSON_GetObjectItemCaseSensitive(root, "preceding_chain_digest");
    if (cJSON_IsString(chain) && strlen(chain->valuestring) == 64) {
        strlcpy(
            assignment->preceding_chain_digest,
            chain->valuestring,
            sizeof(assignment->preceding_chain_digest));
    }
    cJSON *predecessor = cJSON_GetObjectItemCaseSensitive(
        root,
        "committed_predecessor_digest");
    if (cJSON_IsString(predecessor) && strlen(predecessor->valuestring) == 64) {
        strlcpy(
            assignment->committed_predecessor_digest,
            predecessor->valuestring,
            sizeof(assignment->committed_predecessor_digest));
    }
    return true;
}

static int command_transaction_load(void *context, ft_checkpoint_t *checkpoint)
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("file_tx", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t size = sizeof(*checkpoint);
    result = nvs_get_blob(handle, "commands", checkpoint, &size);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && size == sizeof(*checkpoint) ? 1 : -1;
}

static bool command_transaction_commit(void *context, const ft_checkpoint_t *checkpoint)
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("file_tx", NVS_READWRITE, &handle);
    if (result != ESP_OK) return false;
    result = nvs_set_blob(handle, "commands", checkpoint, sizeof(*checkpoint));
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}

static const ft_port_t command_transaction_port = {command_transaction_load, command_transaction_commit, NULL};
static bool command_journal_recover_locked(void)
{
    bool ok = ft_recover(ADD_COMMAND_INBOX_PATH, ADD_COMMAND_INBOX_TMP_PATH,
        ADD_COMMAND_INBOX_BACKUP_PATH, command_transaction_port);
    if (!ok) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    return ok;
}

/* -1 means unavailable; it is never permission to append a duplicate command. */
static int command_journal_contains_locked(const char *command_id)
{
    if (!command_journal_recover_locked()) return -1;
    FILE *file = fopen(ADD_COMMAND_INBOX_PATH, "r");
    if (!file) return errno == ENOENT ? 0 : -1;
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    int found = line ? 0 : -1;
    while (line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        size_t length = strlen(line);
        if (!length || line[length - 1] != '\n') { found = -1; break; }
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *id = root ? cJSON_GetObjectItemCaseSensitive(root, "command_id") : NULL;
        if (!cJSON_IsString(id)) found = -1;
        else if (strcmp(id->valuestring, command_id) == 0) found = 1;
        cJSON_Delete(root);
        free(plain);
        if (found) break;
    }
    free(line);
    if (ferror(file)) found = -1;
    if (fclose(file) != 0) found = -1;
    return found;
}

static bool command_journal_append(cJSON *root, const char *command_id)
{
    if (!s_command_lock || xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(2000)) != pdTRUE) {
        return false;
    }
    int existing = command_journal_contains_locked(command_id);
    if (existing != 0) {
        xSemaphoreGive(s_command_lock);
        return existing == 1;
    }
    char *plain = cJSON_PrintUnformatted(root);
    char *line = encrypt_storage_json(plain);
    struct stat st;
    int stat_result = stat(ADD_COMMAND_INBOX_PATH, &st);
    bool known = stat_result == 0 || errno == ENOENT;
    size_t bytes = line ? strlen(line) + 1 : 0;
    size_t existing_bytes = stat_result == 0 && st.st_size >= 0 ? (size_t)st.st_size : 0;
    bool admitted = line && known && existing_bytes <= ADD_COMMAND_INBOX_MAX_BYTES &&
        bytes <= ADD_COMMAND_INBOX_MAX_BYTES - existing_bytes && qs_local_begin(QS_ADMIT_RECOVERY, bytes);
    FILE *file = admitted ? rel_open_append(ADD_COMMAND_INBOX_PATH) : NULL;
    bool ok = file && fprintf(file, "%s\n", line) > 0 && fflush(file) == 0 &&
              fsync(fileno(file)) == 0;
    if (file && fclose(file) != 0) ok = false;
    if (admitted) qs_local_end(ok, ok ? 0 : errno);
    if (!ok) led_status_fault(LED_STATUS_LOCAL_FAILURE);
    free(line);
    free(plain);
    xSemaphoreGive(s_command_lock);
    return ok;
}

static bool command_is_scheduled_locked(const char *command_id)
{
    if (strcmp(s_running_command_id, command_id) == 0) return true;
    for (size_t index = 0; index < s_queued_command_count; index++) {
        if (strcmp(s_queued_command_ids[index], command_id) == 0) return true;
    }
    return false;
}

static void command_mark_queued_locked(const char *command_id)
{
    if (command_is_scheduled_locked(command_id) ||
        s_queued_command_count >= ADD_TRACKED_COMMAND_CAPACITY) {
        return;
    }
    strlcpy(
        s_queued_command_ids[s_queued_command_count++],
        command_id,
        sizeof(s_queued_command_ids[0]));
}

static void command_unmark_queued_locked(const char *command_id)
{
    for (size_t index = 0; index < s_queued_command_count; index++) {
        if (strcmp(s_queued_command_ids[index], command_id) != 0) continue;
        if (index + 1 < s_queued_command_count) {
            memmove(
                &s_queued_command_ids[index],
                &s_queued_command_ids[index + 1],
                (s_queued_command_count - index - 1) * sizeof(s_queued_command_ids[0]));
        }
        s_queued_command_count--;
        memset(s_queued_command_ids[s_queued_command_count], 0, sizeof(s_queued_command_ids[0]));
        return;
    }
}

static bool queue_command_if_idle(const add_command_t *command)
{
    if (!command || !s_command_lock ||
        xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return false;
    }
    if (command_is_scheduled_locked(command->command_id)) {
        xSemaphoreGive(s_command_lock);
        return true;
    }
    QueueHandle_t target = strcmp(command->command_type, "APPLY_CONFIG") == 0
        ? s_config_commands
        : s_commands;
    bool queued = target && xQueueSend(target, command, 0) == pdTRUE;
    if (queued) command_mark_queued_locked(command->command_id);
    xSemaphoreGive(s_command_lock);
    return queued;
}

static void restore_command_inbox(void)
{
    if (s_command_inbox_restored || !s_command_lock ||
        xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(2000)) != pdTRUE) {
        return;
    }
    if (!command_journal_recover_locked()) {
        xSemaphoreGive(s_command_lock);
        return;
    }
    FILE *file = fopen(ADD_COMMAND_INBOX_PATH, "r");
    bool complete = file != NULL || errno == ENOENT;
    char *line = file ? malloc(ADD_COMMAND_LINE_BYTES) : NULL;
    if (file && !line) complete = false;
    uint32_t restored = 0;
    while (complete && file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        size_t length = strlen(line);
        if (!length || line[length - 1] != '\n') { complete = false; break; }
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        add_command_t command = {0};
        bool parsed = root && parse_command_object(root, &command);
        QueueHandle_t target = parsed && strcmp(command.command_type, "APPLY_CONFIG") == 0
            ? s_config_commands
            : s_commands;
        if (!parsed || !command.command_id[0]) complete = false;
        else if (!command_is_scheduled_locked(command.command_id)) {
            if (target && xQueueSend(target, &command, 0) == pdTRUE) {
                command_mark_queued_locked(command.command_id);
                restored++;
            } else complete = false;
        }
        cJSON_Delete(root);
        free(plain);
    }
    if (file && ferror(file)) complete = false;
    if (file && fclose(file) != 0) complete = false;
    free(line);
    s_command_inbox_restored = complete;
    xSemaphoreGive(s_command_lock);
    if (restored > 0) {
        ESP_LOGW(TAG, "Restored %lu durable ADD command(s) after boot", (unsigned long)restored);
    }
}

static void parse_inbound(const char *data, size_t len)
{
    if (len == 0 || len > ADD_MAX_INBOUND_BYTES) {
        return;
    }
    cJSON *root = cJSON_Parse(data);
    if (!root) {
        return;
    }
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
#ifdef ZONE_LITE_HIKVISION
    if (cJSON_IsString(type) && !strcmp(type->valuestring, "hikvision_history_assignment")) {
        char *text = cJSON_PrintUnformatted(root);
        if (text && strlen(text) < 2048 && s_hikvision_assignments) {
            char assignment[2048] = {0};
            strlcpy(assignment, text, sizeof(assignment));
            xQueueOverwrite(s_hikvision_assignments, assignment);
        }
        free(text); cJSON_Delete(root); return;
    }
#endif
    if (cJSON_IsString(type) &&
        (strcmp(type->valuestring, "ack") == 0 ||
         strcmp(type->valuestring, "reconcile_anchor_ack") == 0 ||
         strcmp(type->valuestring, "reconcile_chunk_ack") == 0 ||
         strcmp(type->valuestring, "reconcile_manifest_ack") == 0 ||
         strcmp(type->valuestring, "source_probe_ack") == 0 ||
         strcmp(type->valuestring, "source_tail_ack") == 0 ||
         strcmp(type->valuestring, "queue_evidence_ack") == 0 ||
         strcmp(type->valuestring, "hikvision_observation_ack") == 0)) {
        cJSON *message_id = cJSON_GetObjectItemCaseSensitive(root, "message_id");
        if (cJSON_IsString(message_id) && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (strcmp(s_waiting_ack, message_id->valuestring) == 0) {
                memset(&s_reconcile_chunk_ack, 0, sizeof(s_reconcile_chunk_ack));
                memset(&s_source_tail_ack, 0, sizeof(s_source_tail_ack));
                memset(
                    &s_attendance_settlement_ack,
                    0,
                    sizeof(s_attendance_settlement_ack));
                if (strcmp(type->valuestring, "reconcile_chunk_ack") == 0) {
                    cJSON *assignment_id = cJSON_GetObjectItemCaseSensitive(root, "assignment_id");
                    cJSON *job_id = cJSON_GetObjectItemCaseSensitive(root, "job_id");
                    cJSON *generation = cJSON_GetObjectItemCaseSensitive(root, "generation");
                    cJSON *committed = cJSON_GetObjectItemCaseSensitive(root, "committed_next_ordinal");
                    cJSON *chain = cJSON_GetObjectItemCaseSensitive(root, "resulting_chain_digest");
                    cJSON *credit_end = cJSON_GetObjectItemCaseSensitive(root, "credit_end_ordinal");
                    cJSON *continue_allowed = cJSON_GetObjectItemCaseSensitive(root, "continue_allowed");
                    if (cJSON_IsString(assignment_id) && strlen(assignment_id->valuestring) == 36 &&
                        cJSON_IsString(job_id) && strlen(job_id->valuestring) == 36 &&
                        cJSON_IsNumber(generation) && cJSON_IsNumber(committed) &&
                        cJSON_IsString(chain) && strlen(chain->valuestring) == 64) {
                        strlcpy(s_reconcile_chunk_ack.assignment_id, assignment_id->valuestring,
                            sizeof(s_reconcile_chunk_ack.assignment_id));
                        strlcpy(s_reconcile_chunk_ack.job_id, job_id->valuestring,
                            sizeof(s_reconcile_chunk_ack.job_id));
                        strlcpy(s_reconcile_chunk_ack.resulting_chain_digest, chain->valuestring,
                            sizeof(s_reconcile_chunk_ack.resulting_chain_digest));
                        s_reconcile_chunk_ack.generation = (uint32_t)generation->valuedouble;
                        s_reconcile_chunk_ack.committed_next_ordinal = (uint32_t)committed->valuedouble;
                        s_reconcile_chunk_ack.credit_end_ordinal = cJSON_IsNumber(credit_end)
                            ? (uint32_t)credit_end->valuedouble
                            : 0;
                        s_reconcile_chunk_ack.continue_allowed = cJSON_IsTrue(continue_allowed);
                        s_reconcile_chunk_ack.valid = true;
                        strlcpy(s_reconcile_last_job_id, job_id->valuestring,
                            sizeof(s_reconcile_last_job_id));
                        s_reconcile_last_generation = s_reconcile_chunk_ack.generation;
                        s_reconcile_last_committed_ordinal =
                            s_reconcile_chunk_ack.committed_next_ordinal;
                    }
                } else if (strcmp(type->valuestring, "source_tail_ack") == 0) {
                    cJSON *terminal_serial = cJSON_GetObjectItemCaseSensitive(root, "terminal_serial");
                    cJSON *generation = cJSON_GetObjectItemCaseSensitive(root, "terminal_generation");
                    cJSON *committed = cJSON_GetObjectItemCaseSensitive(root, "committed_next_ordinal");
                    cJSON *chain = cJSON_GetObjectItemCaseSensitive(root, "resulting_chain_digest");
                    cJSON *exception_count = cJSON_GetObjectItemCaseSensitive(root, "exception_count");
                    if (cJSON_IsString(terminal_serial) && terminal_serial->valuestring[0] &&
                        cJSON_IsNumber(generation) && cJSON_IsNumber(committed) &&
                        cJSON_IsString(chain) && strlen(chain->valuestring) == 64) {
                        strlcpy(
                            s_source_tail_ack.terminal_serial,
                            terminal_serial->valuestring,
                            sizeof(s_source_tail_ack.terminal_serial));
                        strlcpy(
                            s_source_tail_ack.resulting_chain_digest,
                            chain->valuestring,
                            sizeof(s_source_tail_ack.resulting_chain_digest));
                        s_source_tail_ack.terminal_generation =
                            (uint32_t)generation->valuedouble;
                        s_source_tail_ack.committed_next_ordinal =
                            (uint32_t)committed->valuedouble;
                        s_source_tail_ack.exception_count = cJSON_IsNumber(exception_count)
                            ? (uint32_t)exception_count->valuedouble
                            : 0;
                        s_source_tail_ack.valid = true;
                    }
                } else if (strcmp(type->valuestring, "ack") == 0) {
                    cJSON *message_type = cJSON_GetObjectItemCaseSensitive(
                        root, "message_type");
                    if (cJSON_IsString(message_type) &&
                        strcmp(message_type->valuestring, "attendance_batch") == 0) {
                        cJSON *receipt_id = cJSON_GetObjectItemCaseSensitive(
                            root, "receipt_id");
                        cJSON *batch_id = cJSON_GetObjectItemCaseSensitive(
                            root, "batch_id");
                        cJSON *payload_digest = cJSON_GetObjectItemCaseSensitive(
                            root, "payload_digest");
                        cJSON *outcome = cJSON_GetObjectItemCaseSensitive(root, "outcome");
                        cJSON *accepted = cJSON_GetObjectItemCaseSensitive(root, "accepted");
                        cJSON *duplicates = cJSON_GetObjectItemCaseSensitive(root, "duplicates");
                        cJSON *quarantined = cJSON_GetObjectItemCaseSensitive(
                            root, "quarantined");
                        if (cJSON_IsString(receipt_id) &&
                            strlen(receipt_id->valuestring) == 36 &&
                            cJSON_IsString(batch_id) && batch_id->valuestring[0] &&
                            strlen(batch_id->valuestring) <= 120 &&
                            cJSON_IsString(payload_digest) &&
                            attendance_event_uid_is_valid(payload_digest->valuestring) &&
                            cJSON_IsString(outcome) &&
                            (strcmp(outcome->valuestring, "COMMITTED") == 0 ||
                             strcmp(outcome->valuestring, "COMMITTED_WITH_DUPLICATES") == 0 ||
                             strcmp(outcome->valuestring, "COMMITTED_WITH_QUARANTINE") == 0 ||
                             strcmp(outcome->valuestring, "QUARANTINED") == 0) &&
                            cJSON_IsNumber(accepted) && accepted->valuedouble >= 0 &&
                            accepted->valuedouble <= 100 &&
                            (double)(uint32_t)accepted->valuedouble == accepted->valuedouble &&
                            cJSON_IsNumber(duplicates) && duplicates->valuedouble >= 0 &&
                            duplicates->valuedouble <= 100 &&
                            (double)(uint32_t)duplicates->valuedouble == duplicates->valuedouble &&
                            cJSON_IsNumber(quarantined) && quarantined->valuedouble >= 0 &&
                            quarantined->valuedouble <= 100 &&
                            (double)(uint32_t)quarantined->valuedouble == quarantined->valuedouble) {
                            strlcpy(
                                s_attendance_settlement_ack.receipt_id,
                                receipt_id->valuestring,
                                sizeof(s_attendance_settlement_ack.receipt_id));
                            strlcpy(
                                s_attendance_settlement_ack.batch_id,
                                batch_id->valuestring,
                                sizeof(s_attendance_settlement_ack.batch_id));
                            strlcpy(
                                s_attendance_settlement_ack.payload_digest,
                                payload_digest->valuestring,
                                sizeof(s_attendance_settlement_ack.payload_digest));
                            strlcpy(
                                s_attendance_settlement_ack.outcome,
                                outcome->valuestring,
                                sizeof(s_attendance_settlement_ack.outcome));
                            s_attendance_settlement_ack.accepted =
                                (uint32_t)accepted->valuedouble;
                            s_attendance_settlement_ack.duplicates =
                                (uint32_t)duplicates->valuedouble;
                            s_attendance_settlement_ack.quarantined =
                                (uint32_t)quarantined->valuedouble;
                            s_attendance_settlement_ack.valid = true;
                        }
                    }
                }
                s_waiting_ack[0] = '\0';
                if (s_waiting_evidence) {
                    evidence_receipt_t receipt;
                    s_ack_matched = strcmp(type->valuestring, "queue_evidence_ack") == 0 &&
                        evidence_identity(root, &receipt, true) &&
                        evidence_receipt_matches(&s_evidence_expected, &receipt);
                } else if (s_waiting_hikvision) {
                    cJSON *hash = cJSON_GetObjectItemCaseSensitive(root, "observation_sha256");
                    cJSON *durable = cJSON_GetObjectItemCaseSensitive(root, "durable");
                    s_ack_matched = !strcmp(type->valuestring, "hikvision_observation_ack") &&
                        cJSON_IsTrue(durable) && cJSON_IsString(hash) &&
                        !strcmp(hash->valuestring, s_hikvision_expected);
                } else s_ack_matched = true;
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
    if (cJSON_IsString(type) && strcmp(type->valuestring, "source_coverage") == 0) {
        add_source_coverage_t coverage = {0};
        cJSON *terminal_serial = cJSON_GetObjectItemCaseSensitive(root, "terminal_serial");
        cJSON *generation = cJSON_GetObjectItemCaseSensitive(root, "terminal_generation");
        cJSON *committed = cJSON_GetObjectItemCaseSensitive(root, "source_committed_cursor");
        cJSON *chain = cJSON_GetObjectItemCaseSensitive(root, "source_committed_chain_digest");
        cJSON *active = cJSON_GetObjectItemCaseSensitive(root, "active");
        bool valid = cJSON_IsString(terminal_serial) && terminal_serial->valuestring[0] &&
            cJSON_IsNumber(generation) && cJSON_IsNumber(committed) &&
            cJSON_IsString(chain) && strlen(chain->valuestring) == 64 &&
            cJSON_IsBool(active);
        if (valid && s_source_coverage) {
            strlcpy(
                coverage.terminal_serial,
                terminal_serial->valuestring,
                sizeof(coverage.terminal_serial));
            strlcpy(
                coverage.committed_chain_digest,
                chain->valuestring,
                sizeof(coverage.committed_chain_digest));
            coverage.terminal_generation = (uint32_t)generation->valuedouble;
            coverage.committed_next_ordinal = (uint32_t)committed->valuedouble;
            coverage.active = cJSON_IsTrue(active);
            if (xQueueOverwrite(s_source_coverage, &coverage) != pdTRUE) {
                ESP_LOGW(TAG, "Could not queue authoritative ADD source coverage");
            }
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) &&
        strcmp(type->valuestring, "identity_catalog_begin") == 0) {
        if (!identity_catalog_stage_begin(root)) {
            add_connector_log(
                "ERROR",
                "identity",
                "IDENTITY_CATALOG_STAGE_FAILED",
                "Could not start bounded ADD identity catalog staging; active catalog remains unchanged");
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) &&
        strcmp(type->valuestring, "identity_catalog_chunk") == 0) {
        if (!identity_catalog_stage_chunk(root)) {
            reset_identity_catalog_stage(true);
            add_connector_log(
                "ERROR",
                "identity",
                "IDENTITY_CATALOG_CHUNK_REJECTED",
                "ADD identity catalog chunk was incomplete or out of order; active catalog remains unchanged");
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) &&
        strcmp(type->valuestring, "identity_catalog_commit") == 0) {
        size_t row_count = 0;
        bool volatile_fallback = false;
        if (!identity_catalog_stage_commit(
                root,
                &row_count,
                &volatile_fallback)) {
            add_connector_log(
                "ERROR",
                "identity",
                "IDENTITY_CATALOG_COMMIT_REJECTED",
                "ADD identity catalog commit did not match the complete staged row count; active catalog remains unchanged");
        } else if (s_lock &&
                   xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
            s_identity_catalog_rows = row_count;
            s_identity_catalog_generation++;
            if (s_identity_catalog_generation == 0) {
                s_identity_catalog_generation = 1;
            }
            xSemaphoreGive(s_lock);
            ESP_LOGI(
                TAG,
                "Committed bounded encrypted ADD identity catalog rows=%u",
                (unsigned)row_count);
            if (volatile_fallback) {
                add_connector_log(
                    "WARN",
                    "identity",
                    "IDENTITY_CATALOG_MEMORY_FALLBACK",
                    "Verified ADD identity catalog is active in bounded PSRAM because flash storage is full; encrypted persistence will retry on reconnect");
            }
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) && strcmp(type->valuestring, "identity_catalog") == 0) {
        size_t row_count = 0;
        if (!persist_identity_catalog(root, &row_count)) {
            ESP_LOGE(TAG, "Could not persist encrypted ADD identity tombstone catalog");
            add_connector_log(
                "ERROR",
                "identity",
                "IDENTITY_CATALOG_PERSIST_FAILED",
                "Fresh ADD identity catalog could not be committed atomically; boot health remains fail-closed");
        } else {
            if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
                s_identity_catalog_rows = row_count;
                s_identity_catalog_generation++;
                if (s_identity_catalog_generation == 0) {
                    s_identity_catalog_generation = 1;
                }
                xSemaphoreGive(s_lock);
            }
            ESP_LOGI(
                TAG,
                "Updated encrypted ADD identity tombstone catalog rows=%u",
                (unsigned)row_count);
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) && strcmp(type->valuestring, "command_cancel") == 0) {
        cJSON *command_id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
        if (cJSON_IsString(command_id)) {
            bool running = false;
            if (xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
                running = strcmp(s_running_command_id, command_id->valuestring) == 0;
                xSemaphoreGive(s_command_lock);
            }
            if (running) {
                (void)add_connector_command_update(
                    command_id->valuestring,
                    "RUNNING",
                    "COMMAND_ALREADY_RUNNING",
                    "Cancellation arrived after terminal execution had started.",
                    "{}");
            } else if (append_cancelled_command(command_id->valuestring)) {
                (void)add_connector_command_update(
                    command_id->valuestring,
                    "CANCELLED",
                    "COMMAND_CANCELLED",
                    "The command was cancelled before terminal execution.",
                    "{}");
                (void)add_connector_command_complete(command_id->valuestring);
            }
        }
        cJSON_Delete(root);
        return;
    }
    if (cJSON_IsString(type) &&
        (strcmp(type->valuestring, "reconcile_assignment") == 0 ||
         strcmp(type->valuestring, "source_probe_assignment") == 0)) {
        add_reconcile_assignment_t assignment;
        bool parsed = parse_reconcile_assignment(root, &assignment);
        bool stale = parsed &&
            !assignment.source_probe &&
            strcmp(assignment.job_id, s_reconcile_last_job_id) == 0 &&
            assignment.generation == s_reconcile_last_generation &&
            assignment.committed_next_ordinal < s_reconcile_last_committed_ordinal;
        if (!parsed || stale || !s_reconcile_assignments ||
            (!stale && xQueueOverwrite(s_reconcile_assignments, &assignment) != pdTRUE)) {
            ESP_LOGW(TAG, "Rejected malformed or unqueueable reconciliation assignment");
        }
        cJSON_Delete(root);
        return;
    }
    add_command_t command;
    if (!parse_command_object(root, &command)) {
        cJSON_Delete(root);
        return;
    }
    // ADD is the durable command authority and reoffers unfinished commands
    // after reconnect/reboot. The local journal is only a recovery cache: a
    // full SPIFFS partition must never prevent a safe command or lease from
    // reaching the serialized ZKT executor.
    bool journaled = command_journal_append(root, command.command_id);
    if (!journaled) {
        ESP_LOGW(
            TAG,
            "ADD command recovery cache unavailable; executing from durable control-plane offer command=%s",
            command.command_id);
    }
    if (!queue_command_if_idle(&command)) {
        ESP_LOGW(TAG, "Command queue full; rejecting %s", command.command_id);
        add_connector_command_update(
            command.command_id,
            "RETRYING",
            "COMMAND_EXECUTOR_BUSY",
            "ADD retains the command and will reoffer it to the serialized executor.",
            "{}");
    } else {
        add_connector_command_update(command.command_id, "ACKNOWLEDGED", NULL, NULL, "{}");
    }
    cJSON_Delete(root);
}

static void reset_inbound_payload(void)
{
    free(s_inbound_payload);
    s_inbound_payload = NULL;
    s_inbound_payload_expected = 0;
    s_inbound_payload_received = 0;
}

static void inbound_message_task(void *argument)
{
    (void)argument;
    add_inbound_message_t message = {0};
    while (true) {
        if (xQueueReceive(s_inbound_messages, &message, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        parse_inbound(message.data, message.length);
        free(message.data);
        message.data = NULL;
        message.length = 0;
    }
}

static void receive_inbound_fragment(const esp_websocket_event_data_t *event)
{
    if (!event || event->payload_len <= 0 || event->data_len < 0 ||
        event->payload_offset < 0 ||
        (size_t)event->payload_len > ADD_MAX_INBOUND_BYTES) {
        ESP_LOGE(TAG, "Rejected invalid or oversized ADD WebSocket message");
        reset_inbound_payload();
        return;
    }
    size_t expected = (size_t)event->payload_len;
    size_t offset = (size_t)event->payload_offset;
    size_t length = (size_t)event->data_len;
    if (offset == 0) {
        reset_inbound_payload();
        s_inbound_payload = heap_caps_malloc(
            expected + 1,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s_inbound_payload) {
            s_inbound_payload = malloc(expected + 1);
        }
        if (!s_inbound_payload) {
            ESP_LOGE(TAG, "Could not allocate fragmented ADD WebSocket message");
            return;
        }
        s_inbound_payload_expected = expected;
    }
    if (!s_inbound_payload || expected != s_inbound_payload_expected ||
        offset != s_inbound_payload_received ||
        length > expected - offset) {
        ESP_LOGE(TAG, "Rejected incomplete or out-of-order ADD WebSocket message");
        reset_inbound_payload();
        return;
    }
    memcpy(s_inbound_payload + offset, event->data_ptr, length);
    s_inbound_payload_received += length;
    if (s_inbound_payload_received == s_inbound_payload_expected) {
        s_inbound_payload[s_inbound_payload_expected] = '\0';
        add_inbound_message_t message = {
            .data = s_inbound_payload,
            .length = s_inbound_payload_expected,
        };
        s_inbound_payload = NULL;
        s_inbound_payload_expected = 0;
        s_inbound_payload_received = 0;
        if (xQueueSend(s_inbound_messages, &message, 0) != pdTRUE) {
            ESP_LOGE(TAG, "Inbound ADD message queue is full; dropping complete message");
            free(message.data);
        }
    }
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
        reset_inbound_payload();
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
        } else if (event && (event->op_code == 0x1 || event->op_code == 0x0)) {
            receive_inbound_fragment(event);
        }
        break;
    case WEBSOCKET_EVENT_ERROR:
        reset_inbound_payload();
        mark_transport_disconnected();
        ESP_LOGW(TAG, "ADD WebSocket transport error");
        break;
    default:
        break;
    }
}

void add_connector_report_ords_start(bool started, uint32_t attempts)
{
    s_ords_start_attempts = attempts;
    if (!started) s_ords_worker_started = false;
}

void add_connector_report_ords_worker(add_worker_operation_t operation)
{
    s_ords_worker_operation = operation;
    s_ords_worker_tick_ms = (uint32_t)monotonic_ms();
    s_ords_worker_started = true;
}

static bool append_worker_diagnostic(cJSON *workers, const char *name,
                                     bool started, uint32_t tick,
                                     add_worker_operation_t operation)
{
    int64_t now = monotonic_ms();
    uint32_t age = (uint32_t)now - tick;
    const char *state = !started ? "STOPPED" : age > 90000 ? "FAULT" :
        operation == ADD_WORKER_RESOURCE ? "WAITING_RESOURCE" :
        operation == ADD_WORKER_NETWORK ? "WAITING_NETWORK" : "RUNNING";
    const char *operation_name = operation == ADD_WORKER_READING ? "reading queue" :
        operation == ADD_WORKER_NETWORK ? "waiting for acknowledgement" :
        operation == ADD_WORKER_COMMITTING ? "committing receipt" :
        operation == ADD_WORKER_RESOURCE ? "allocating delivery buffer" : "idle";
    cJSON *worker = cJSON_CreateObject();
    if (!worker) return false;
    if (!cJSON_AddItemToArray(workers, worker)) { cJSON_Delete(worker); return false; }
    return cJSON_AddStringToObject(worker, "name", name) &&
        cJSON_AddStringToObject(worker, "state", state) &&
        cJSON_AddStringToObject(worker, "operation", operation_name) &&
        cJSON_AddNumberToObject(worker, "restart_attempts", !strcmp(name, "add_delivery")
            ? (s_outbox_retry.total ? s_outbox_retry.total - 1U : 0U)
            : (s_ords_start_attempts ? s_ords_start_attempts - 1U : 0U)) &&
        (!started || cJSON_AddNumberToObject(worker, "last_activity_uptime_ms", (double)(now - age)));
}

static void append_firmware_diagnostics(cJSON *payload, const add_zkt_telemetry_t *zkt, const char *activity)
{
    cJSON *diagnostics = cJSON_CreateObject();
    if (!diagnostics) return;
    cJSON *storage = cJSON_AddObjectToObject(diagnostics, "storage");
    cJSON *workers = cJSON_AddArrayToObject(diagnostics, "workers");
    cJSON *queues = cJSON_AddArrayToObject(diagnostics, "queues");
    if (!storage || !workers || !queues || !cJSON_AddNumberToObject(diagnostics, "schema_version", 1)) goto failed;
    size_t total = 0, used = 0;
    esp_err_t measured = esp_spiffs_info(NULL, &total, &used);
    if (measured == ESP_OK) {
        if (!cJSON_AddNumberToObject(storage, "total_bytes", (double)total) ||
            !cJSON_AddNumberToObject(storage, "used_bytes", (double)used)) goto failed;
    } else {
        if (!cJSON_AddStringToObject(storage, "error_operation", "filesystem_info") ||
            !cJSON_AddNumberToObject(storage, "error_code", measured)) goto failed;
    }
    qs_health_t measured_health = qs_health();
    if (measured_health.observed) {
        if (!cJSON_AddNumberToObject(storage, "write_failures", measured_health.write_failures) ||
            !cJSON_AddNumberToObject(storage, "read_failures", measured_health.read_failures) ||
            !cJSON_AddNumberToObject(storage, "admission_reserve_bytes", (double)measured_health.admission_reserve_bytes)) goto failed;
        if (measured == ESP_OK && measured_health.last_error &&
            (!cJSON_AddStringToObject(storage, "error_operation", measured_health.last_operation ? measured_health.last_operation : "storage_operation") ||
             !cJSON_AddNumberToObject(storage, "error_code", measured_health.last_error))) goto failed;
    }
    // A connected heartbeat does not prove persistence. Until a checked
    // recovery/write result is available, report UNKNOWN rather than healthy.
    const char *led = led_status_current_name();
    const char *durability = measured != ESP_OK || measured_health.last_error || !strcmp(led, "LOCAL_FAILURE") || !strcmp(led, "FATAL")
        ? "DEGRADED" : measured_health.recovery_complete && measured_health.persistence_verified
        ? "HEALTHY" : "UNKNOWN";
    if (!cJSON_AddStringToObject(storage, "durability", durability) ||
        !cJSON_AddBoolToObject(storage, "persistence_verified", measured_health.persistence_verified) ||
        !cJSON_AddBoolToObject(storage, "recovery_complete", measured_health.recovery_complete) ||
        !cJSON_AddStringToObject(storage, "upgrade_contract", storage_upgrade_contract()) ||
        !cJSON_AddStringToObject(storage, "upgrade_error", storage_upgrade_error()) ||
        !cJSON_AddBoolToObject(storage, "upgrade_ready", storage_upgrade_ready())) goto failed;
    if (!append_worker_diagnostic(workers, "add_delivery", s_outbox_task_handle != NULL,
            s_outbox_tick_ms, s_outbox_buffer_ready ? s_add_worker_operation : ADD_WORKER_RESOURCE)) goto failed;
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
    if (!append_worker_diagnostic(workers, "ords_delivery", s_ords_worker_started,
            s_ords_worker_tick_ms, s_ords_worker_operation)) goto failed;
#endif
    add_outbox_t *outboxes[] = {&s_live_outbox, &s_bulk_outbox};
    const char *names[] = {"live", "bulk"};
    for (size_t i = 0; i < 2; i++) {
        cJSON *queue = cJSON_CreateObject();
        if (!queue) goto failed;
        if (!cJSON_AddItemToArray(queues, queue)) { cJSON_Delete(queue); goto failed; }
        if (!cJSON_AddStringToObject(queue, "name", names[i])) goto failed;
        add_outbox_t *outbox = outboxes[i];
        if (outbox->lock && xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            bool fields_ok = cJSON_AddBoolToObject(queue, "count_known", outbox->depth_known) &&
                (!outbox->depth_known || cJSON_AddNumberToObject(queue, "records", outbox->depth));
            struct stat st;
            if (stat(outbox->path, &st) == 0) fields_ok = fields_ok && cJSON_AddNumberToObject(queue, "bytes", (double)st.st_size);
            else if (errno == ENOENT) fields_ok = fields_ok && cJSON_AddNumberToObject(queue, "bytes", 0);
            xSemaphoreGive(outbox->lock);
            if (!fields_ok) goto failed;
        }
    }
    const char *segmented_names[QS_COUNT] = {"segmented_live", "segmented_bulk", "segmented_ords",
        "segmented_blocked", "segmented_receipts", "segmented_evidence", "hikvision_source"};
    for (unsigned i = 0; i < QS_COUNT; i++) {
        cJSON *queue = cJSON_CreateObject();
        if (!queue) goto failed;
        if (!cJSON_AddItemToArray(queues, queue)) { cJSON_Delete(queue); goto failed; }
        uint32_t depth = 0;
        bool known = qs_snapshot((qs_lane_t)i, &depth);
        if (!cJSON_AddStringToObject(queue, "name", segmented_names[i]) ||
            !cJSON_AddBoolToObject(queue, "count_known", known) ||
            (known && !cJSON_AddNumberToObject(queue, "records", depth))) goto failed;
    }
    const char *mode = activity;
    if (!strcmp(activity, "LIVE_CAPTURE") || !strcmp(activity, "ONLINE"))
        mode = zkt->add_source_coverage_certified ? "APPEND_TAIL_ASSURANCE" : "IDLE";
    if (!cJSON_AddStringToObject(diagnostics, "reconciliation_mode", mode) ||
        (zkt->last_light_check_uptime_ms > 0 && !cJSON_AddNumberToObject(diagnostics,
            "last_light_check_uptime_ms", (double)zkt->last_light_check_uptime_ms)) ||
        (zkt->last_tail_audit_uptime_ms > 0 && !cJSON_AddNumberToObject(diagnostics,
            "last_tail_audit_uptime_ms", (double)zkt->last_tail_audit_uptime_ms)) ||
        (zkt->committed_source_known &&
            (!cJSON_AddNumberToObject(diagnostics, "source_generation", zkt->committed_source_generation) ||
             !cJSON_AddNumberToObject(diagnostics, "committed_source_cursor", zkt->committed_source_cursor)))) goto failed;
    if (cJSON_AddItemToObject(payload, "diagnostics", diagnostics)) return;
failed:
    cJSON_Delete(diagnostics);
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
            cJSON_AddStringToObject(payload, "firmware_version", firmware_version());
            cJSON_AddStringToObject(payload, "firmware_family", ZONE_LITE_FIRMWARE_FAMILY);
            cJSON_AddNumberToObject(payload, "config_version", 3);
            cJSON_AddBoolToObject(payload, "comm_key_management", true);
            cJSON_AddNumberToObject(
                payload,
                "comm_key_revision",
                zone_config_get()->zkt_comm_key_revision);
            cJSON_AddNumberToObject(payload, "uptime_seconds", (double)(esp_timer_get_time() / 1000000));
            cJSON_AddNumberToObject(payload, "rssi", rssi);
            cJSON_AddNumberToObject(payload, "free_heap", esp_get_free_heap_size());
            cJSON_AddNumberToObject(payload, "outbox_depth", add_connector_outbox_depth());
            append_firmware_diagnostics(payload, &zkt, activity);
            ota_manager_append_telemetry(payload);
            cJSON_AddStringToObject(payload, "current_activity", activity);
            cJSON_AddStringToObject(payload, "led_state", led_status_current_name());
            cJSON *zkt_json = cJSON_AddObjectToObject(payload, "zkt");
            cJSON_AddBoolToObject(zkt_json, "online", zkt.online);
            cJSON_AddStringToObject(zkt_json, "connection_state", zkt.connection_state[0] ? zkt.connection_state : "UNKNOWN");
            cJSON_AddStringToObject(zkt_json, "ip_address", zkt.ip_address);
            cJSON_AddStringToObject(zkt_json, "serial", zkt.serial);
            cJSON_AddStringToObject(zkt_json, "model", zkt.model);
            cJSON_AddStringToObject(zkt_json, "platform", zkt.platform);
            cJSON_AddBoolToObject(zkt_json, "comm_key_write_v1", false);
            if (zkt.device_time[0]) cJSON_AddStringToObject(zkt_json, "device_time", zkt.device_time);
            if (zkt.device_time_sampled_epoch > 0) {
                char sampled[32];
                iso_utc((time_t)zkt.device_time_sampled_epoch, sampled);
                cJSON_AddStringToObject(zkt_json, "device_time_sampled_at", sampled);
            }
            cJSON_AddStringToObject(zkt_json, "transition_reason", zkt.transition_reason);
            cJSON_AddNumberToObject(zkt_json, "user_count", zkt.user_count);
            cJSON_AddNumberToObject(zkt_json, "attendance_count", zkt.attendance_count);
            cJSON_AddNumberToObject(zkt_json, "consecutive_failures", zkt.consecutive_failures);
            cJSON_AddNumberToObject(zkt_json, "consecutive_successes", zkt.consecutive_successes);
            cJSON_AddNumberToObject(zkt_json, "flap_count_15m", zkt.flap_count_15m);
            cJSON_AddNumberToObject(zkt_json, "probe_latency_ms", zkt.probe_latency_ms);
            cJSON_AddNumberToObject(zkt_json, "user_record_size", zkt.user_record_size);
            cJSON *reconciliation = cJSON_AddObjectToObject(
                zkt_json,
                "reconciliation_capabilities");
            cJSON_AddBoolToObject(reconciliation, "history_stream_v1", true);
            cJSON_AddBoolToObject(reconciliation, "history_stream_v2", true);
            cJSON_AddBoolToObject(reconciliation, "partial_final_chunk_v1", true);
            cJSON_AddBoolToObject(reconciliation, "source_divergence_probe_v1", true);
            cJSON_AddBoolToObject(reconciliation, "source_tail_v1", true);
            cJSON_AddBoolToObject(
                reconciliation,
                "history_range_resume_verified",
                true);
            cJSON_AddNumberToObject(reconciliation, "max_chunk_records", 100);
            cJSON_AddNumberToObject(reconciliation, "max_credit_records", 400);
            cJSON_AddBoolToObject(
                reconciliation,
                "source_coverage_certified",
                zkt.add_source_coverage_certified);
            cJSON_AddNumberToObject(
                reconciliation,
                "source_coverage_cursor",
                zkt.add_source_coverage_cursor);
            json_add_epoch(zkt_json, "backoff_until", zkt.backoff_until_epoch);
            json_add_epoch(zkt_json, "stability_since", zkt.stability_since_epoch);
            json_add_epoch(zkt_json, "last_reconcile_at", zkt.last_reconcile_epoch);
            json_add_epoch(zkt_json, "next_restart_at", zkt.next_restart_epoch);
            cJSON *history = cJSON_AddObjectToObject(zkt_json, "history_backfill");
            cJSON_AddStringToObject(
                history,
                "state",
                zkt.history_backfill_state[0] ? zkt.history_backfill_state : "NOT_STARTED");
            if (zkt.history_cursor_year > 0 && zkt.history_cursor_month > 0) {
                char cursor[24];
                snprintf(
                    cursor,
                    sizeof(cursor),
                    "%04ld-%02ld",
                    (long)zkt.history_cursor_year,
                    (long)zkt.history_cursor_month);
                cJSON_AddStringToObject(history, "cursor_month", cursor);
            }
            if (zkt.history_oldest_year > 0 && zkt.history_oldest_month > 0) {
                char oldest[24];
                snprintf(
                    oldest,
                    sizeof(oldest),
                    "%04ld-%02ld",
                    (long)zkt.history_oldest_year,
                    (long)zkt.history_oldest_month);
                cJSON_AddStringToObject(history, "coverage_start_month", oldest);
            }
            json_add_epoch(history, "last_sweep_at", zkt.history_last_sweep_epoch);
            cJSON_AddNumberToObject(
                history,
                "failed_windows",
                zkt.history_failed_windows);
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
            hikvision_append_telemetry(payload);
#endif
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

static bool write_outbox_cursor(const add_outbox_t *outbox, off_t offset)
{
    FILE *file = fopen(outbox->cursor_tmp_path, "w");
    if (!file) return false;
    bool ok = fprintf(file, "%lld\n", (long long)offset) > 0 &&
              fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    if (!ok) {
        (void)remove(outbox->cursor_tmp_path);
        return false;
    }
    // If power is lost between remove and rename the cursor disappears and
    // acknowledged rows are replayed.  The backend event UID makes that safe;
    // skipping an unacknowledged row would not be safe.
    (void)remove(outbox->cursor_path);
    if (rename(outbox->cursor_tmp_path, outbox->cursor_path) != 0) {
        (void)remove(outbox->cursor_tmp_path);
        return false;
    }
    return true;
}

static void restore_outbox_if_needed(add_outbox_t *outbox)
{
    struct stat st;
    if (stat(outbox->path, &st) == 0) return;
    if (errno != ENOENT) { led_status_fault(LED_STATUS_LOCAL_FAILURE); return; }
    const char *generations[] = {outbox->backup_path, outbox->tmp_path};
    for (size_t i = 0; i < 2; i++) {
        if (stat(generations[i], &st) == 0) {
            if (rename(generations[i], outbox->path) != 0)
                led_status_fault(LED_STATUS_LOCAL_FAILURE);
            outbox->depth_known = false;
            return;
        }
        if (errno != ENOENT) { led_status_fault(LED_STATUS_LOCAL_FAILURE); return; }
    }
    // Each surviving generation is streamed independently. Existence of a
    // newer active file is never permission to discard an older backup/temp.
}

static int add_legacy_load(void *context, lq_checkpoint_t *checkpoint)
{
    add_outbox_t *outbox = context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("add_legacy", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t size = sizeof(*checkpoint);
    result = nvs_get_blob(handle, outbox->label, checkpoint, &size);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && size == sizeof(*checkpoint) ? 1 : -1;
}

static bool add_legacy_commit(void *context, const lq_checkpoint_t *checkpoint)
{
    add_outbox_t *outbox = context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("add_legacy", NVS_READWRITE, &handle);
    if (result != ESP_OK) return false;
    result = nvs_set_blob(handle, outbox->label, checkpoint, sizeof(*checkpoint));
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}

static off_t load_outbox_cursor(add_outbox_t *outbox)
{
    lq_port_t port = {add_legacy_load, add_legacy_commit, outbox};
    dq_result_t result = lq_open_step(&outbox->legacy, outbox->path, port);
    if (result != DQ_OK) {
        outbox->depth_known = false;
        if (result != DQ_PENDING) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        return 0;
    }
    // The old text cursor is not sufficient generation evidence. A first
    // upgrade conservatively replays the file; ADD settles existing UIDs once.
    return (off_t)outbox->legacy.checkpoint.offset;
}

static rel_scan_result_t count_outbox_rows(add_outbox_t *outbox)
{
    outbox->depth_known = false;
    if (!outbox->legacy.ready) return REL_SCAN_ERROR;
    errno = 0;
    FILE *file = fopen(outbox->path, "r");
    if (!file) {
        if (errno != ENOENT) return REL_SCAN_ERROR;
        outbox->depth = 0;
        outbox->depth_known = true;
        return REL_SCAN_EMPTY;
    }
    if (fseeko(file, outbox->offset, SEEK_SET) != 0) {
        fclose(file);
        return REL_SCAN_ERROR;
    }
    uint32_t count = 0;
    rel_scan_result_t result = rel_count_rows(file, &count);
    if (fclose(file) != 0) result = REL_SCAN_ERROR;
    if (result != REL_SCAN_ERROR) {
        outbox->depth = count;
        outbox->depth_known = true;
    }
    return result;
}

static bool compact_outbox_locked(add_outbox_t *outbox, bool force)
{
    (void)force;
    if (!outbox->legacy.ready) return false;
    struct stat st;
    if (stat(outbox->path, &st) != 0) return errno == ENOENT && !outbox->legacy.checkpoint.offset;
    if ((uint64_t)st.st_size != outbox->legacy.checkpoint.offset) return true;
    // No suffix rewrite or second copy. Clear the predecessor's text cursor
    // before retiring a fully settled file so rollback cannot skip a new file.
    if (!write_outbox_cursor(outbox, 0) || lq_reclaim(&outbox->legacy) != DQ_OK) return false;
    outbox->offset = 0;
    outbox->depth = 0;
    outbox->depth_known = true;
    outbox->ack_since_checkpoint = 0;
    restore_outbox_if_needed(outbox);
    if (stat(outbox->path, &st) == 0) outbox->depth_known = false;
    return true;
}

static bool advance_outbox_locked(add_outbox_t *outbox, off_t row_end, bool custody)
{
    if (row_end <= outbox->offset || row_end != (off_t)outbox->pending_token.end) return false;
    if ((custody ? lq_settle_evidence(&outbox->legacy, &outbox->pending_token) :
        lq_settle(&outbox->legacy, &outbox->pending_token)) != DQ_OK) {
        outbox->depth_known = false;
        return false;
    }
    outbox->offset = (off_t)outbox->legacy.checkpoint.offset;
    if (outbox->depth_known && outbox->depth) outbox->depth--;
    return compact_outbox_locked(outbox, false);
}

static bool read_outbox_row_locked(add_outbox_t *outbox, char *line, off_t *row_end)
{
    if (!outbox->legacy.ready) outbox->offset = load_outbox_cursor(outbox);
    if (!outbox->legacy.ready) return false;
    dq_result_t result = lq_peek(&outbox->legacy, line, ADD_OUTBOX_LINE_BYTES, &outbox->pending_token);
    if (result == DQ_EMPTY) {
        if (!compact_outbox_locked(outbox, true)) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        return false;
    }
    if (result != DQ_OK) {
        outbox->depth_known = false;
        led_status_fault(LED_STATUS_LOCAL_FAILURE);
        return false;
    }
    *row_end = (off_t)outbox->pending_token.end;
    return true;
}

static bool attendance_payload_is_live(const cJSON *payload)
{
    const cJSON *events = cJSON_GetObjectItemCaseSensitive(payload, "events");
    const cJSON *event = NULL;
    cJSON_ArrayForEach(event, events) {
        const cJSON *source = cJSON_GetObjectItemCaseSensitive(event, "source");
        if (cJSON_IsString(source) &&
            (strcmp(source->valuestring, "LIVE") == 0 ||
             strcmp(source->valuestring, "LIVE_POLL") == 0)) {
            return true;
        }
    }
    return false;
}

static bool attendance_event_uid_is_valid(const char *value)
{
    if (!value || strlen(value) != 64) return false;
    for (size_t i = 0; i < 64; i++) {
        char ch = value[i];
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

static bool attendance_timestamp_is_valid(const char *value)
{
    if (!value || strlen(value) != 20 || value[4] != '-' || value[7] != '-' ||
        value[10] != 'T' || value[13] != ':' || value[16] != ':' || value[19] != 'Z') {
        return false;
    }
    static const int digit_positions[] = {
        0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18,
    };
    for (size_t i = 0; i < sizeof(digit_positions) / sizeof(digit_positions[0]); i++) {
        char ch = value[digit_positions[i]];
        if (ch < '0' || ch > '9') return false;
    }
    return true;
}

static bool attendance_source_is_valid(const char *value)
{
    return value &&
        (strcmp(value, "LIVE") == 0 || strcmp(value, "LIVE_POLL") == 0 ||
         strcmp(value, "DUMP_STARTUP") == 0 || strcmp(value, "DUMP_RECONNECT") == 0 ||
         strcmp(value, "MANUAL_REPROCESS") == 0 ||
         strcmp(value, "RECONCILE_15M") == 0 ||
         strcmp(value, "FULL_HISTORY") == 0 ||
         strcmp(value, "CURRENT_RECONCILE") == 0);
}

static bool json_optional_string(const cJSON *value)
{
    return !value || cJSON_IsNull(value) || cJSON_IsString(value);
}

static bool json_optional_string_or_number(const cJSON *value)
{
    return !value || cJSON_IsNull(value) || cJSON_IsString(value) ||
        cJSON_IsNumber(value);
}

static bool attendance_payload_is_valid(const cJSON *payload)
{
    const cJSON *batch_id = cJSON_GetObjectItemCaseSensitive(payload, "batch_id");
    const cJSON *reported_digest = cJSON_GetObjectItemCaseSensitive(
        payload, "payload_digest");
    const cJSON *events = cJSON_GetObjectItemCaseSensitive(payload, "events");
    int count = cJSON_IsArray(events) ? cJSON_GetArraySize(events) : 0;
    if (!cJSON_IsString(batch_id) || !batch_id->valuestring[0] ||
        strlen(batch_id->valuestring) > 120 || count < 1 || count > 100 ||
        (reported_digest && !cJSON_IsNull(reported_digest) &&
         (!cJSON_IsString(reported_digest) ||
          !attendance_event_uid_is_valid(reported_digest->valuestring)))) {
        return false;
    }
    const cJSON *event = NULL;
    cJSON_ArrayForEach(event, events) {
        const cJSON *event_uid = cJSON_GetObjectItemCaseSensitive(event, "event_uid");
        const cJSON *uid = cJSON_GetObjectItemCaseSensitive(event, "uid");
        const cJSON *identity_fingerprint = cJSON_GetObjectItemCaseSensitive(
            event, "terminal_identity_fingerprint");
        const cJSON *user_id = cJSON_GetObjectItemCaseSensitive(event, "user_id");
        const cJSON *raw_name = cJSON_GetObjectItemCaseSensitive(event, "raw_name");
        const cJSON *device_time = cJSON_GetObjectItemCaseSensitive(event, "device_event_time");
        const cJSON *captured_at = cJSON_GetObjectItemCaseSensitive(event, "captured_at");
        const cJSON *source = cJSON_GetObjectItemCaseSensitive(event, "source");
        const cJSON *status = cJSON_GetObjectItemCaseSensitive(event, "status");
        const cJSON *punch = cJSON_GetObjectItemCaseSensitive(event, "punch");
        const cJSON *raw_punch = cJSON_GetObjectItemCaseSensitive(event, "raw_punch");
        const cJSON *clock_drift = cJSON_GetObjectItemCaseSensitive(
            event, "clock_drift_seconds");
        const cJSON *clock_quality = cJSON_GetObjectItemCaseSensitive(
            event, "clock_quality");
        const cJSON *boot_id = cJSON_GetObjectItemCaseSensitive(event, "boot_id");
        const cJSON *sequence = cJSON_GetObjectItemCaseSensitive(event, "sequence");
        const cJSON *raw_event = cJSON_GetObjectItemCaseSensitive(event, "raw_event");
        if (!cJSON_IsObject(event) || !cJSON_IsString(event_uid) ||
            !attendance_event_uid_is_valid(event_uid->valuestring) ||
            !json_optional_string(uid) ||
            (identity_fingerprint && !cJSON_IsNull(identity_fingerprint) &&
             (!cJSON_IsString(identity_fingerprint) ||
              !attendance_event_uid_is_valid(identity_fingerprint->valuestring))) ||
            !cJSON_IsString(user_id) || !user_id->valuestring[0] ||
            strlen(user_id->valuestring) > 100 ||
            !json_optional_string(raw_name) ||
            !cJSON_IsString(device_time) ||
            !attendance_timestamp_is_valid(device_time->valuestring) ||
            !cJSON_IsString(captured_at) ||
            !attendance_timestamp_is_valid(captured_at->valuestring) ||
            !cJSON_IsString(source) || !attendance_source_is_valid(source->valuestring) ||
            !json_optional_string_or_number(status) ||
            !json_optional_string_or_number(punch) ||
            (raw_punch && !cJSON_IsBool(raw_punch)) ||
            (clock_drift && !cJSON_IsNull(clock_drift) &&
             !cJSON_IsNumber(clock_drift)) ||
            (clock_quality && !cJSON_IsString(clock_quality)) ||
            !json_optional_string(boot_id) ||
            (sequence && !cJSON_IsNull(sequence) && !cJSON_IsNumber(sequence)) ||
            (raw_event && !cJSON_IsObject(raw_event))) {
            return false;
        }
    }
    return true;
}

static bool attendance_settlement_matches_payload(
    const char *payload_json,
    const add_attendance_settlement_ack_t *ack)
{
    if (!payload_json || !ack || !ack->valid) return false;
    cJSON *payload = cJSON_Parse(payload_json);
    if (!payload || !attendance_payload_is_valid(payload)) {
        cJSON_Delete(payload);
        return false;
    }
    const cJSON *batch_id = cJSON_GetObjectItemCaseSensitive(payload, "batch_id");
    const cJSON *reported_digest = cJSON_GetObjectItemCaseSensitive(
        payload, "payload_digest");
    const cJSON *events = cJSON_GetObjectItemCaseSensitive(payload, "events");
    uint32_t count = (uint32_t)cJSON_GetArraySize(events);
    uint32_t settled = ack->accepted + ack->duplicates + ack->quarantined;
    const char *expected_outcome = "COMMITTED";
    if (ack->quarantined > 0 && ack->accepted == 0 && ack->duplicates == 0) {
        expected_outcome = "QUARANTINED";
    } else if (ack->quarantined > 0) {
        expected_outcome = "COMMITTED_WITH_QUARANTINE";
    } else if (ack->duplicates > 0) {
        expected_outcome = "COMMITTED_WITH_DUPLICATES";
    }
    // A reported digest mismatch is itself a durable batch-level poison. ADD
    // returns its computed digest with an all-QUARANTINED settlement, so the
    // stale device-reported value must not keep that settled row at the head
    // of the outbox. Committed outcomes still require exact digest equality.
    bool digest_matches = !cJSON_IsString(reported_digest) ||
        strcmp(reported_digest->valuestring, ack->payload_digest) == 0 ||
        strcmp(ack->outcome, "QUARANTINED") == 0;
    bool matches = strcmp(batch_id->valuestring, ack->batch_id) == 0 &&
        settled == count && strcmp(ack->outcome, expected_outcome) == 0 &&
        digest_matches;
    cJSON_Delete(payload);
    return matches;
}

static bool oracle_confirmation_path_is_valid(const char *value)
{
    return value &&
        (strcmp(value, "FIRMWARE_LIVE") == 0 ||
         strcmp(value, "FIRMWARE_BULK") == 0 ||
         strcmp(value, "FIRMWARE_RECONCILE") == 0);
}

static bool oracle_receipt_payload_is_valid(const cJSON *payload)
{
    const cJSON *path = cJSON_GetObjectItemCaseSensitive(payload, "confirmation_path");
    const cJSON *observed_at = cJSON_GetObjectItemCaseSensitive(payload, "oracle_observed_at");
    const cJSON *event_uids = cJSON_GetObjectItemCaseSensitive(payload, "event_uids");
    bool path_valid = cJSON_IsString(path) &&
        oracle_confirmation_path_is_valid(path->valuestring);
    int count = cJSON_IsArray(event_uids) ? cJSON_GetArraySize(event_uids) : 0;
    if (!path_valid || !cJSON_IsString(observed_at) ||
        !attendance_timestamp_is_valid(observed_at->valuestring) ||
        count < 1 || count > 100) {
        return false;
    }
    for (int i = 0; i < count; i++) {
        const cJSON *uid = cJSON_GetArrayItem(event_uids, i);
        if (!cJSON_IsString(uid) || !attendance_event_uid_is_valid(uid->valuestring)) {
            return false;
        }
        for (int j = 0; j < i; j++) {
            const cJSON *previous = cJSON_GetArrayItem(event_uids, j);
            if (cJSON_IsString(previous) &&
                strcmp(previous->valuestring, uid->valuestring) == 0) {
                return false;
            }
        }
    }
    return true;
}

bool add_connector_transfer_queue_evidence(
    const char *queue, const char *generation, const char *record_id,
    const void *raw, size_t raw_length, const char *terminal_serial,
    const char *reason)
{
    if (!raw || !raw_length || raw_length > 8192 || !queue || !generation || !record_id || !reason)
        return false;
    unsigned char digest[32];
    if (mbedtls_sha256(raw, raw_length, digest, 0) != 0) return false;
    char hex[65];
    for (size_t i = 0; i < sizeof(digest); ++i) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    size_t encoded_capacity = ((raw_length + 2) / 3) * 4 + 1;
    unsigned char *encoded = malloc(encoded_capacity);
    size_t encoded_length = 0;
    if (!encoded || mbedtls_base64_encode(encoded, encoded_capacity, &encoded_length, raw, raw_length) != 0) {
        free(encoded); return false;
    }
    encoded[encoded_length] = 0;
    cJSON *payload = cJSON_CreateObject();
    cJSON *provenance = cJSON_CreateObject();
    bool ok = payload && provenance &&
        cJSON_AddNumberToObject(payload, "schema_version", 1) &&
        cJSON_AddStringToObject(payload, "connector_id", zone_config_get()->connector_id) &&
        cJSON_AddStringToObject(payload, "queue", queue) &&
        cJSON_AddStringToObject(payload, "queue_generation", generation) &&
        cJSON_AddStringToObject(payload, "record_id", record_id) &&
        cJSON_AddStringToObject(payload, "payload_digest", hex) &&
        cJSON_AddStringToObject(payload, "raw_b64", (const char *)encoded) &&
        cJSON_AddStringToObject(provenance, "reason", reason) &&
        cJSON_AddStringToObject(provenance, "encoding", "LEGACY_ROW");
    if (ok && terminal_serial && terminal_serial[0])
        ok = cJSON_AddStringToObject(provenance, "terminal_serial", terminal_serial) != NULL;
    if (ok) {
        ok = cJSON_AddItemToObject(payload, "provenance", provenance);
        if (ok) provenance = NULL;
    }
    char *json = ok ? cJSON_PrintUnformatted(payload) : NULL;
    cJSON_Delete(provenance); cJSON_Delete(payload); free(encoded);
    ok = json && send_payload_and_wait_for_ack("queue_evidence", json,
        pdMS_TO_TICKS(ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS), pdMS_TO_TICKS(ADD_OUTBOX_ACK_TIMEOUT_MS), NULL, NULL);
    free(json);
    return ok;
}

static char *outbox_record_line(
    const char *type,
    const char *payload_json,
    bool *live_out)
{
    if (!type || !payload_json) return NULL;
    cJSON *payload = cJSON_Parse(payload_json);
    if (!payload || !cJSON_IsObject(payload)) {
        cJSON_Delete(payload);
        return NULL;
    }
    bool live = false;
    if (strcmp(type, "attendance_batch") == 0) {
        if (!attendance_payload_is_valid(payload)) {
            ESP_LOGE(TAG, "Refusing to enqueue an invalid ADD attendance payload");
            cJSON_Delete(payload);
            return NULL;
        }
        live = attendance_payload_is_live(payload);
    } else if (strcmp(type, "oracle_receipt_batch") == 0) {
        if (!oracle_receipt_payload_is_valid(payload)) {
            ESP_LOGE(TAG, "Refusing to enqueue an invalid Oracle receipt payload");
            cJSON_Delete(payload);
            return NULL;
        }
        const cJSON *path = cJSON_GetObjectItemCaseSensitive(
            payload,
            "confirmation_path");
        live = cJSON_IsString(path) &&
            strcmp(path->valuestring, "FIRMWARE_LIVE") == 0;
    } else {
        ESP_LOGE(TAG, "Refusing unsupported ADD outbox message type=%s", type);
        cJSON_Delete(payload);
        return NULL;
    }
    cJSON *record = cJSON_CreateObject();
    if (!record) {
        cJSON_Delete(payload);
        return NULL;
    }
    if (!cJSON_AddStringToObject(record, "type", type) ||
        !cJSON_AddItemToObject(record, "payload", payload)) {
        cJSON_Delete(payload);
        cJSON_Delete(record);
        return NULL;
    }
    char *line = cJSON_PrintUnformatted(record);
    cJSON_Delete(record);
    size_t line_len = line ? strlen(line) : 0;
    if (!line || line_len + 2 >= ADD_OUTBOX_LINE_BYTES) {
        ESP_LOGE(
            TAG,
            "Refusing oversized ADD outbox message bytes=%lu limit=%u",
            (unsigned long)line_len,
            (unsigned)ADD_OUTBOX_LINE_BYTES);
        free(line);
        return NULL;
    }
    if (live_out) *live_out = live;
    return line;
}

static char *attendance_outbox_record_line(const char *payload_json, bool *live_out)
{
    return outbox_record_line("attendance_batch", payload_json, live_out);
}

static bool deliver_attendance_payloads_acknowledged(
    const char *const *payloads,
    size_t count)
{
    if (!payloads || count == 0 || !add_connector_is_connected()) return false;

    // The ZKT remains the durable source of truth while this bounded direct
    // path is in flight.  It is used only when the preserved reconcile outbox
    // cannot accept more bytes, and every batch is acknowledged by ADD before
    // success.  Event UIDs make a later terminal retry or old-outbox drain
    // idempotent.
    s_priority_delivery_until_ms = (uint32_t)monotonic_ms() +
        ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS + ADD_PRIORITY_ACK_TIMEOUT_MS;
    bool ok = true;
    for (size_t i = 0; i < count; i++) {
        bool inferred_live = false;
        char *line = attendance_outbox_record_line(payloads[i], &inferred_live);
        if (!line) {
            ok = false;
            break;
        }
        free(line);
        add_attendance_settlement_ack_t attendance_ack = {0};
        if (!send_payload_and_wait_for_ack(
                "attendance_batch",
                payloads[i],
                pdMS_TO_TICKS(ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS),
                pdMS_TO_TICKS(ADD_PRIORITY_ACK_TIMEOUT_MS),
                NULL,
                &attendance_ack) ||
            !attendance_settlement_matches_payload(payloads[i], &attendance_ack)) {
            ok = false;
            break;
        }
        s_priority_delivery_until_ms = (uint32_t)monotonic_ms() +
            ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS + ADD_PRIORITY_ACK_TIMEOUT_MS;
    }
    s_priority_delivery_until_ms =
        (uint32_t)monotonic_ms() + ADD_PRIORITY_HOLD_MS;
    ESP_LOGW(
        TAG,
        "ADD reconcile outbox unavailable; acknowledged direct delivery %s batches=%lu",
        ok ? "succeeded" : "failed",
        (unsigned long)count);
    char message[160];
    snprintf(
        message,
        sizeof(message),
        "Preserved attendance outbox unavailable; acknowledged direct ADD delivery %s batches=%lu",
        ok ? "succeeded" : "failed",
        (unsigned long)count);
    (void)add_connector_log(
        ok ? "WARN" : "ERROR",
        "reconcile",
        ok ? "ADD_DIRECT_ACK_FALLBACK_SUCCEEDED" : "ADD_DIRECT_ACK_FALLBACK_FAILED",
        message);
    return ok;
}

bool add_connector_deliver_attendance_acknowledged(const char *payload_json)
{
    if (!payload_json) return false;
    const char *payloads[] = {payload_json};
    bool ok = deliver_attendance_payloads_acknowledged(payloads, 1);
    (void)add_connector_log(
        ok ? "WARN" : "ERROR",
        "live",
        ok ? "LIVE_DIRECT_ACK_FALLBACK_SUCCEEDED" : "LIVE_DIRECT_ACK_FALLBACK_FAILED",
        ok
            ? "Local live preservation was unavailable; ADD acknowledged the idempotent event while the ZKT retained source truth"
            : "Local live preservation was unavailable and ADD did not acknowledge the event; the next counter reconcile must recover it from ZKT truth");
    return ok;
}

static void refresh_capacity_deadline_on_progress(
    const add_outbox_t *outbox,
    uint32_t *last_depth,
    int64_t *deadline_ms)
{
    if (*last_depth != UINT32_MAX && outbox->depth < *last_depth) {
        *deadline_ms = monotonic_ms() + ADD_BULK_CAPACITY_WAIT_MS;
    }
    *last_depth = outbox->depth;
}

static bool add_connector_enqueue_validated_line_with_policy(const char *line, bool live, qs_admission_t policy)
{
    if (!line) return false;
    if (storage_upgrade_segmented_writes()) {
        qs_lane_t lane = policy == QS_ADMIT_RECOVERY ? QS_RECEIPTS : live ? QS_LIVE : QS_BULK;
        return qs_append_with_policy(lane, line, strlen(line), policy) == DQ_OK;
    }
    bool ok = false;
    add_outbox_t *outbox = live ? &s_live_outbox : &s_bulk_outbox;
    TickType_t lock_timeout = pdMS_TO_TICKS(live ? 2000 : 10000);
    int64_t deadline_ms = monotonic_ms() + (live ? 0 : ADD_BULK_CAPACITY_WAIT_MS);
    uint32_t last_depth = UINT32_MAX;
    bool waiting_logged = false;
    do {
        if (outbox->lock && xSemaphoreTake(outbox->lock, lock_timeout) == pdTRUE) {
            refresh_capacity_deadline_on_progress(
                outbox,
                &last_depth,
                &deadline_ms);
            struct stat st = {0};
            off_t current = stat(outbox->path, &st) == 0 ? st.st_size : 0;
            if (current + (off_t)strlen(line) + 1 > outbox->max_bytes &&
                outbox->offset > 0) {
                (void)compact_outbox_locked(outbox, true);
                current = stat(outbox->path, &st) == 0 ? st.st_size : 0;
            }
            if (current + (off_t)strlen(line) + 1 <= outbox->max_bytes) {
                if (qs_local_begin(policy, strlen(line) + 1)) {
                    errno = 0;
                    FILE *file = rel_open_append(outbox->path);
                    int error = file ? 0 : errno;
                    if (file) {
                        ok = fprintf(file, "%s\n", line) > 0 && fflush(file) == 0 && fsync(fileno(file)) == 0;
                        if (!ok) error = errno;
                        if (fclose(file) != 0) { ok = false; if (!error) error = errno; }
                    }
                    qs_local_end(ok, error);
                    if (ok) outbox->depth++;
                    else outbox->depth_known = false;
                }
            }
            xSemaphoreGive(outbox->lock);
        }
        if (ok || live || monotonic_ms() >= deadline_ms) {
            break;
        }
        if (!waiting_logged) {
            waiting_logged = true;
            ESP_LOGW(
                TAG,
                "ADD reconcile outbox reached its bounded capacity; applying backpressure while acknowledged rows drain");
        }
        vTaskDelay(pdMS_TO_TICKS(ADD_BULK_CAPACITY_POLL_MS));
    } while (true);
    if (!ok) {
        ESP_LOGE(
            TAG,
            "Could not durably append ADD %s outbox row within the bounded wait",
            outbox->label);
    }
    return ok;
}

static bool add_connector_enqueue_validated_line(const char *line, bool live)
{
    return add_connector_enqueue_validated_line_with_policy(line, live,
        live ? QS_ADMIT_LIVE : QS_ADMIT_HISTORICAL);
}

static bool add_connector_enqueue_record(const char *type, const char *payload_json)
{
    if (!zone_config_get()->add_enabled) return true;
    bool live = false;
    char *line = outbox_record_line(type, payload_json, &live);
    if (!line) return false;
    bool ok = add_connector_enqueue_validated_line(line, live);
    free(line);
    return ok;
}

bool add_connector_enqueue_attendance(const char *payload_json)
{
    return add_connector_enqueue_record("attendance_batch", payload_json);
}

bool add_connector_enqueue_attendance_priority(const char *payload_json)
{
    if (!zone_config_get()->add_enabled) return true;
    bool inferred_live = false;
    char *line = attendance_outbox_record_line(payload_json, &inferred_live);
    if (!line) return false;
    // A current-day reconnect dump is not a historical bulk sweep: operators
    // need those punches even when an older reconcile backlog has filled the
    // bounded bulk outbox.  Route the already-validated batch through the
    // separately bounded live queue.  The ADD event UID keeps partial retries
    // idempotent, while ordinary historical windows remain on the bulk path.
    bool ok = add_connector_enqueue_validated_line(line, true);
    free(line);
    if (!ok && add_connector_is_connected()) {
        // A terminal with a fully occupied preservation partition may have no
        // filesystem block available even though the separately bounded live
        // outbox has not reached its own byte limit. Current-day truth still
        // exists durably on the ZKT, so use a bounded, acknowledged WebSocket
        // delivery as the recovery path. A timeout returns false and the
        // periodic terminal truth cycle retries without deleting any record.
        const char *payloads[] = {payload_json};
        ok = deliver_attendance_payloads_acknowledged(payloads, 1);
    }
    return ok;
}

bool add_connector_enqueue_oracle_receipts(
    const char *const *event_uids,
    size_t count,
    const char *confirmation_path)
{
    if (!zone_config_get()->add_enabled) return true;
    if (!event_uids || count < 1 || count > 100 ||
        !oracle_confirmation_path_is_valid(confirmation_path)) {
        return false;
    }

    // A full terminal dump remains resident in PSRAM while receipts are
    // staged. Building and then reparsing a 100-node cJSON tree here used
    // constrained internal heap and could fail late in a large truth cycle.
    // Construct the already-validated record directly in PSRAM instead. The
    // outbox worker independently parses and validates it again before send.
    char *line = heap_caps_malloc(
        ADD_OUTBOX_LINE_BYTES,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!line) {
        line = malloc(ADD_OUTBOX_LINE_BYTES);
    }
    if (!line) {
        ESP_LOGE(TAG, "Could not allocate Oracle receipt outbox record buffer");
        return false;
    }

    time_t now;
    time(&now);
    char observed_at[32];
    iso_utc(now, observed_at);
    if (!attendance_timestamp_is_valid(observed_at)) {
        ESP_LOGE(TAG, "Refusing Oracle receipt with an invalid observed timestamp");
        free(line);
        return false;
    }

    int prefix_bytes = snprintf(
        line,
        ADD_OUTBOX_LINE_BYTES,
        "{\"type\":\"oracle_receipt_batch\",\"payload\":{"
        "\"confirmation_path\":\"%s\","
        "\"oracle_observed_at\":\"%s\","
        "\"event_uids\":[",
        confirmation_path,
        observed_at);
    if (prefix_bytes < 0 || prefix_bytes >= ADD_OUTBOX_LINE_BYTES) {
        ESP_LOGE(TAG, "Could not serialize Oracle receipt outbox prefix");
        free(line);
        return false;
    }

    size_t used = (size_t)prefix_bytes;
    size_t unique_count = 0;
    size_t duplicate_count = 0;
    for (size_t i = 0; i < count; i++) {
        if (!attendance_event_uid_is_valid(event_uids[i])) {
            ESP_LOGE(TAG, "Refusing Oracle receipt with an invalid event UID");
            free(line);
            return false;
        }
        bool duplicate = false;
        for (size_t previous = 0; previous < i; previous++) {
            if (strcmp(event_uids[previous], event_uids[i]) == 0) {
                duplicate = true;
                break;
            }
        }
        if (duplicate) {
            duplicate_count++;
            continue;
        }

        int uid_bytes = snprintf(
            line + used,
            ADD_OUTBOX_LINE_BYTES - used,
            "%s\"%s\"",
            unique_count > 0 ? "," : "",
            event_uids[i]);
        if (uid_bytes < 0 ||
            (size_t)uid_bytes >= ADD_OUTBOX_LINE_BYTES - used) {
            ESP_LOGE(TAG, "Oracle receipt outbox record exceeded its bounded line size");
            free(line);
            return false;
        }
        used += (size_t)uid_bytes;
        unique_count++;
    }
    if (unique_count == 0) {
        free(line);
        return false;
    }

    int suffix_bytes = snprintf(
        line + used,
        ADD_OUTBOX_LINE_BYTES - used,
        "]}}");
    if (suffix_bytes < 0 ||
        (size_t)suffix_bytes >= ADD_OUTBOX_LINE_BYTES - used ||
        used + (size_t)suffix_bytes + 2 >= ADD_OUTBOX_LINE_BYTES) {
        ESP_LOGE(TAG, "Could not terminate Oracle receipt outbox record");
        free(line);
        return false;
    }

    if (duplicate_count > 0) {
        ESP_LOGI(
            TAG,
            "Collapsed %lu duplicate Oracle receipt event UID(s) before durable enqueue",
            (unsigned long)duplicate_count);
    }

    bool ok = add_connector_enqueue_validated_line_with_policy(line, true, QS_ADMIT_RECOVERY);
    // Receipt preservation has a bounded local attempt even for historical
    // delivery. A full legacy filesystem can still drain if ADD durably takes
    // responsibility for the Oracle proof. This caller owns no storage lock.
    if (!ok && add_connector_is_connected()) {
        cJSON *record = cJSON_Parse(line);
        cJSON *payload = record ? cJSON_GetObjectItemCaseSensitive(record, "payload") : NULL;
        char *json = cJSON_IsObject(payload) ? cJSON_PrintUnformatted(payload) : NULL;
        if (json) ok = send_payload_and_wait_for_ack("oracle_receipt_batch", json,
            pdMS_TO_TICKS(ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS), pdMS_TO_TICKS(ADD_OUTBOX_ACK_TIMEOUT_MS), NULL, NULL);
        free(json);
        cJSON_Delete(record);
    }
    free(line);
    return ok;
}

bool add_connector_enqueue_attendance_bulk(const char *const *payloads, size_t count)
{
    if (!zone_config_get()->add_enabled || count == 0) return true;
    if (!payloads || !s_bulk_outbox.lock) {
        ESP_LOGE(TAG, "ADD reconcile bulk append is not initialized");
        return false;
    }

    if (storage_upgrade_segmented_writes()) {
        for (size_t i = 0; i < count; ++i) {
            bool live = false;
            char *line = attendance_outbox_record_line(payloads[i], &live);
            bool ok = line && !live && qs_append_with_policy(QS_BULK, line, strlen(line), QS_ADMIT_HISTORICAL) == DQ_OK;
            free(line);
            if (!ok) return false; // Partial durable batches may replay idempotently.
        }
        return true;
    }
    size_t required_bytes = 0;
    for (size_t i = 0; i < count; i++) {
        if (!payloads[i]) return false;
        size_t payload_bytes = strlen(payloads[i]);
        if (payload_bytes + ADD_OUTBOX_RECORD_OVERHEAD_BYTES >= ADD_OUTBOX_LINE_BYTES ||
            required_bytes > SIZE_MAX - payload_bytes - ADD_OUTBOX_RECORD_OVERHEAD_BYTES) {
            ESP_LOGE(TAG, "ADD reconcile bulk append contains an oversized payload");
            return false;
        }
        required_bytes += payload_bytes + ADD_OUTBOX_RECORD_OVERHEAD_BYTES;
    }

    int64_t deadline_ms = monotonic_ms() + ADD_BULK_CAPACITY_WAIT_MS;
    uint32_t last_depth = UINT32_MAX;
    bool waiting_logged = false;
    bool direct_attempted = false;
    while (true) {
        if (xSemaphoreTake(s_bulk_outbox.lock, pdMS_TO_TICKS(10000)) != pdTRUE) {
            if (monotonic_ms() >= deadline_ms) {
                ESP_LOGE(TAG, "Timed out waiting to append ADD reconcile batches");
                return false;
            }
            vTaskDelay(pdMS_TO_TICKS(ADD_BULK_CAPACITY_POLL_MS));
            continue;
        }

        refresh_capacity_deadline_on_progress(
            &s_bulk_outbox,
            &last_depth,
            &deadline_ms);
        struct stat st = {0};
        off_t current = stat(s_bulk_outbox.path, &st) == 0 ? st.st_size : 0;
        if (current + (off_t)required_bytes > s_bulk_outbox.max_bytes &&
            s_bulk_outbox.offset > 0) {
            (void)compact_outbox_locked(&s_bulk_outbox, true);
            current = stat(s_bulk_outbox.path, &st) == 0 ? st.st_size : 0;
        }
        if (current + (off_t)required_bytes > s_bulk_outbox.max_bytes) {
            xSemaphoreGive(s_bulk_outbox.lock);
            if (!direct_attempted && add_connector_is_connected()) {
                direct_attempted = true;
                if (deliver_attendance_payloads_acknowledged(payloads, count)) {
                    return true;
                }
            }
            if (monotonic_ms() >= deadline_ms) {
                ESP_LOGE(
                    TAG,
                    "ADD reconcile attendance outbox remained full after bounded backpressure");
                return false;
            }
            if (!waiting_logged) {
                waiting_logged = true;
                ESP_LOGW(
                    TAG,
                    "ADD reconcile outbox reached its bounded capacity; waiting for acknowledged rows before appending more truth");
            }
            vTaskDelay(pdMS_TO_TICKS(ADD_BULK_CAPACITY_POLL_MS));
            continue;
        }

        bool admitted = qs_local_begin(QS_ADMIT_HISTORICAL, required_bytes);
        bool ok = admitted;
        errno = 0;
        FILE *file = admitted ? rel_open_append(s_bulk_outbox.path) : NULL;
        if (!file) ok = false;
        uint32_t written = 0;
        for (size_t i = 0; ok && i < count; i++) {
            bool live = false;
            char *line = attendance_outbox_record_line(payloads[i], &live);
            if (!line || live) {
                ESP_LOGE(TAG, "Rejected an invalid payload from the ADD reconcile bulk append");
                free(line);
                ok = false;
                break;
            }
            if (fprintf(file, "%s\n", line) <= 0) {
                free(line);
                ok = false;
                break;
            }
            written++;
            free(line);
        }
        if (file) {
            if (fflush(file) != 0 || fsync(fileno(file)) != 0) ok = false;
            if (fclose(file) != 0) ok = false;
        }
        int captured_error = errno;
        if (admitted) qs_local_end(ok, captured_error);
        if (ok) s_bulk_outbox.depth += written;
        else {
            s_bulk_outbox.depth_known = false;
            led_status_fault(LED_STATUS_LOCAL_FAILURE);
        }
        xSemaphoreGive(s_bulk_outbox.lock);
        if (ok && written > 0) {
            ESP_LOGI(
                TAG,
                "Durably appended %lu ADD reconcile batch(es) with one flash sync",
                (unsigned long)written);
        }
        if (ok && written == count) return true;

        // SPIFFS can run out of physical blocks before this logical outbox's
        // byte ceiling when another preserved queue is large.  Some rows may
        // already be durable locally; direct delivery of the complete chunk
        // is safe because ADD deduplicates every event UID.
        if (add_connector_is_connected() &&
            deliver_attendance_payloads_acknowledged(payloads, count)) {
            return true;
        }
        return false;
    }
}

static void outbox_task(void *arg)
{
    (void)arg;
    delivery_scheduler_t scheduler = {0};
    char *line = NULL;
    int64_t priority_started = 0;
    const qs_lane_t lanes[] = {QS_LIVE, QS_LIVE, QS_BULK, QS_BULK, QS_RECEIPTS, QS_EVIDENCE};
    for (;;) {
        int64_t now = monotonic_ms();
        s_outbox_tick_ms = (uint32_t)now;
        s_add_worker_operation = ADD_WORKER_READING;
        if (!line) line = allocate_outbox_line_buffer();
        s_outbox_buffer_ready = line != NULL;
        if (!line) {
            led_status_fault(LED_STATUS_LOCAL_FAILURE);
            vTaskDelay(pdMS_TO_TICKS(ADD_OUTBOX_RETRY_MS));
            continue;
        }
        if (!add_connector_is_connected()) {
            s_add_worker_operation = ADD_WORKER_IDLE;
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        bool background_due = false;
        if (priority_delivery_hold_active()) {
            if (!priority_started) priority_started = now;
            if (now - priority_started < ADD_PRIORITY_HOLD_MS) {
                vTaskDelay(pdMS_TO_TICKS(250));
                continue;
            }
            background_due = true;
        }
        priority_started = 0;
        unsigned ready = 0;
        for (unsigned i = 0; i < 6; i++) {
            uint32_t depth = 0;
            if (i == 0 || i == 2) {
                add_outbox_t *legacy = i == 0 ? &s_live_outbox : &s_bulk_outbox;
                if (legacy->lock && xSemaphoreTake(legacy->lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                    if (!legacy->depth_known || legacy->depth) ready |= 1U << i;
                    xSemaphoreGive(legacy->lock);
                }
            } else if (!qs_snapshot(lanes[i], &depth) || depth) ready |= 1U << i;
        }
        int selected = ds_pick(&scheduler, ready, (uint64_t)now, background_due);
        if (selected < 0) {
            s_add_worker_operation = ADD_WORKER_IDLE;
            vTaskDelay(pdMS_TO_TICKS(250));
            continue;
        }
        bool segmented = selected != 0 && selected != 2;
        add_outbox_t *outbox = selected == 0 ? &s_live_outbox : &s_bulk_outbox;
        off_t row_end = 0;
        dq_token_t token = {0};
        lq_token_t legacy_token = {0};
        size_t raw_length = 0;
        bool have_row = false;
        if (segmented) {
            size_t length = 0;
            dq_result_t read = qs_peek(lanes[selected], line, ADD_OUTBOX_LINE_BYTES - 1, &length, &token);
            have_row = read == DQ_OK;
            if (have_row) { line[length] = 0; raw_length = length; }
            else if (read != DQ_EMPTY) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        } else if (xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
            have_row = read_outbox_row_locked(outbox, line, &row_end);
            if (have_row) {
                legacy_token = outbox->pending_token;
                raw_length = legacy_token.end - legacy_token.offset;
            }
            xSemaphoreGive(outbox->lock);
        }
        if (!have_row) {
            scheduler.retry_at[selected] = (uint64_t)monotonic_ms() + 1000;
            continue;
        }
        ds_attempted(&scheduler, (unsigned)selected);
        bool syntax_valid = (segmented || !legacy_token.evidence_required) && !memchr(line, 0, raw_length) && rel_json_syntax_valid(line, raw_length);
        cJSON *record = syntax_valid ? cJSON_Parse(line) : NULL;
        if (syntax_valid && !record) {
            ds_complete(&scheduler, (unsigned)selected, monotonic_ms(), false, esp_random());
            continue;
        }
        cJSON *type = record ? cJSON_GetObjectItemCaseSensitive(record, "type") : NULL;
        cJSON *payload = record ? cJSON_GetObjectItemCaseSensitive(record, "payload") : NULL;
        evidence_receipt_t evidence_identity_fields;
        bool valid = cJSON_IsString(type) && cJSON_IsObject(payload) &&
            ((strcmp(type->valuestring, "attendance_batch") == 0 && attendance_payload_is_valid(payload)) ||
             (strcmp(type->valuestring, "oracle_receipt_batch") == 0 && oracle_receipt_payload_is_valid(payload)) ||
             (strcmp(type->valuestring, "queue_evidence") == 0 && evidence_identity(payload, &evidence_identity_fields, false)));
        char *payload_json = valid ? cJSON_PrintUnformatted(payload) : NULL;
        if (valid && !payload_json) {
            cJSON_Delete(record);
            ds_complete(&scheduler, (unsigned)selected, monotonic_ms(), false, esp_random());
            continue;
        }
        if (!valid) {
            cJSON_Delete(record);
            free(payload_json);
            char instance[33], generation[80], record_id[80];
            bool have_identity = qs_generation(instance);
            if (segmented) {
                snprintf(generation, sizeof(generation), "%s-segmented-v2", have_identity ? instance : "");
                snprintf(record_id, sizeof(record_id), "%lu:%lu:%lu", (unsigned long)token.segment,
                    (unsigned long)token.offset, (unsigned long)token.sequence);
            } else {
                snprintf(generation, sizeof(generation), "%s-legacy-%lu", have_identity ? instance : "",
                    (unsigned long)legacy_token.generation);
                snprintf(record_id, sizeof(record_id), "%lu:%lu", (unsigned long)legacy_token.offset,
                    (unsigned long)legacy_token.crc);
            }
            const char *queue_names[] = {"add_live_legacy", "add_live", "add_bulk_legacy", "add_bulk", "receipts", "evidence"};
            s_add_worker_operation = ADD_WORKER_NETWORK;
            bool preserved = have_identity && add_connector_transfer_queue_evidence(
                queue_names[selected], generation, record_id, line, raw_length, NULL, "MALFORMED");
            s_add_worker_operation = ADD_WORKER_COMMITTING;
            if (preserved) {
                if (segmented) preserved = qs_settle(lanes[selected], &token) == DQ_OK;
                else if (xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
                    preserved = advance_outbox_locked(outbox, row_end, true);
                    xSemaphoreGive(outbox->lock);
                } else preserved = false;
            }
            // Failed custody transfer leaves the exact original row in place.
            if (!preserved) led_status_fault(LED_STATUS_LOCAL_FAILURE);
            ds_complete(&scheduler, (unsigned)selected, monotonic_ms(), preserved, esp_random());
            continue;
        }
        add_attendance_settlement_ack_t attendance_ack = {0};
        bool is_attendance = strcmp(type->valuestring, "attendance_batch") == 0;
        s_add_worker_operation = ADD_WORKER_NETWORK;
        bool acknowledged = send_payload_and_wait_for_ack(type->valuestring, payload_json,
            pdMS_TO_TICKS(ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS), pdMS_TO_TICKS(ADD_OUTBOX_ACK_TIMEOUT_MS),
            NULL, is_attendance ? &attendance_ack : NULL);
        if (acknowledged && is_attendance && !attendance_settlement_matches_payload(payload_json, &attendance_ack))
            acknowledged = false;
        cJSON_Delete(record);
        free(payload_json);
        if (acknowledged && attendance_ack.valid && attendance_ack.quarantined > 0) {
            ESP_LOGW(TAG, "ADD durably quarantined %lu attendance row(s) without blocking",
                (unsigned long)attendance_ack.quarantined);
        }
        s_add_worker_operation = ADD_WORKER_COMMITTING;
        bool committed = false;
        if (acknowledged) {
            if (segmented) committed = qs_settle(lanes[selected], &token) == DQ_OK;
            else if (xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
                committed = advance_outbox_locked(outbox, row_end, false);
                xSemaphoreGive(outbox->lock);
            }
            if (!committed) led_status_fault(LED_STATUS_LOCAL_FAILURE);
        }
        if (committed) s_outbox_progress_ms = (uint32_t)monotonic_ms();
        ds_complete(&scheduler, (unsigned)selected, monotonic_ms(), committed, esp_random());
    }
}

static void delivery_supervisor_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t now = (uint32_t)monotonic_ms();
        if (s_client && s_outboxes_initialized) {
            if (!s_heartbeat_task_handle && worker_retry_allow(&s_heartbeat_retry, now) &&
                xTaskCreate(heartbeat_task, "add_heartbeat", 8192, NULL, 4, &s_heartbeat_task_handle) != pdPASS)
                s_heartbeat_task_handle = NULL;
            if (!s_outbox_task_handle && worker_retry_allow(&s_outbox_retry, now) &&
                xTaskCreate(outbox_task, "add_outbox", 8192, NULL, 4, &s_outbox_task_handle) != pdPASS)
                s_outbox_task_handle = NULL;
            s_worker_start_failed = !s_outbox_task_handle || !s_heartbeat_task_handle;
        }
        if (s_worker_start_failed || (s_outbox_tick_ms &&
            (uint32_t)((uint32_t)monotonic_ms() - s_outbox_tick_ms) > 90000U))
            led_status_fault(LED_STATUS_LOCAL_FAILURE);
        restore_command_inbox();
        // Do not asynchronously delete a task which might own a mutex.
        // Buffer failures self-retry; stalled operations are independently visible.
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void add_connector_init(void)
{
    if (!zone_config_get()->add_enabled || s_started) {
        return;
    }
    s_lock = xSemaphoreCreateMutex();
    s_send_lock = xSemaphoreCreateMutex();
    s_live_outbox.lock = xSemaphoreCreateMutex();
    s_bulk_outbox.lock = xSemaphoreCreateMutex();
    s_ack_sem = xSemaphoreCreateBinary();
    s_ack_wait_lock = xSemaphoreCreateMutex();
    s_command_lock = xSemaphoreCreateMutex();
    s_catalog_lock = xSemaphoreCreateMutex();
    s_commands = xQueueCreate(ADD_COMMAND_QUEUE_DEPTH, sizeof(add_command_t));
    s_config_commands = xQueueCreate(
        ADD_CONFIG_COMMAND_QUEUE_DEPTH,
        sizeof(add_command_t));
    s_reconcile_assignments = xQueueCreate(1, sizeof(add_reconcile_assignment_t));
#ifdef ZONE_LITE_HIKVISION
    s_hikvision_assignments = xQueueCreate(1, 2048);
#endif
    s_source_coverage = xQueueCreate(1, sizeof(add_source_coverage_t));
    s_inbound_messages = xQueueCreate(ADD_INBOUND_QUEUE_DEPTH, sizeof(add_inbound_message_t));
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(
        s_boot_id,
        sizeof(s_boot_id),
        "%02x%02x%02x%02x%02x%02x-%08lx",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], (unsigned long)esp_random());
    strlcpy(s_zkt.connection_state, "BOOTING", sizeof(s_zkt.connection_state));
    s_zkt.user_count = -1;
    s_zkt.attendance_count = -1;
    s_started = s_lock && s_send_lock && s_live_outbox.lock && s_bulk_outbox.lock &&
                s_ack_sem && s_ack_wait_lock && s_command_lock && s_catalog_lock && s_commands &&
                s_config_commands &&
                s_reconcile_assignments &&
                s_source_coverage &&
                s_inbound_messages;
    if (s_started && xTaskCreate(delivery_supervisor_task, "add_supervisor", 4096, NULL, 4, NULL) != pdPASS) {
        s_started = false;
        s_worker_start_failed = true;
        led_status_fault(LED_STATUS_LOCAL_FAILURE);
    }
    if (s_started &&
        xTaskCreate(inbound_message_task, "add_inbound", 12288, NULL, 4, NULL) != pdPASS) {
        s_started = false;
        ESP_LOGE(TAG, "Could not start bounded ADD inbound message worker");
    }
}

typedef struct {
    char body[1536];
    size_t used;
} onboarding_response_t;

static esp_err_t onboarding_http_event(esp_http_client_event_t *event)
{
    onboarding_response_t *response = event ? event->user_data : NULL;
    if (!response || event->event_id != HTTP_EVENT_ON_DATA || !event->data || event->data_len <= 0) {
        return ESP_OK;
    }
    size_t available = sizeof(response->body) - response->used - 1;
    size_t copy = (size_t)event->data_len < available ? (size_t)event->data_len : available;
    if (copy > 0) {
        memcpy(response->body + response->used, event->data, copy);
        response->used += copy;
        response->body[response->used] = '\0';
    }
    return ESP_OK;
}

static void hex_bytes(const unsigned char *value, size_t length, char *output)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t index = 0; index < length; index++) {
        output[index * 2] = digits[value[index] >> 4];
        output[index * 2 + 1] = digits[value[index] & 0x0f];
    }
    output[length * 2] = '\0';
}

static bool perform_onboarding(void)
{
    const zone_config_t *runtime = zone_config_get();
    if (!zone_config_needs_onboarding()) return true;
    time_t now;
    time(&now);
    if (now < 1767225600) {
        ESP_LOGW(TAG, "Trusted time unavailable; delaying ADD onboarding");
        return false;
    }
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char mac_text[18];
    snprintf(
        mac_text,
        sizeof(mac_text),
        "%02x:%02x:%02x:%02x:%02x:%02x",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    char timestamp[32];
    iso_utc(now, timestamp);
    unsigned char nonce_bytes[16];
    for (size_t index = 0; index < sizeof(nonce_bytes); index += 4) {
        uint32_t value = esp_random();
        memcpy(nonce_bytes + index, &value, 4);
    }
    char nonce[33];
    hex_bytes(nonce_bytes, sizeof(nonce_bytes), nonce);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "hardware_id", mac_text);
    cJSON_AddStringToObject(root, "zone_id", runtime->zone_id);
    cJSON_AddStringToObject(root, "zone_name", runtime->zone_name);
    cJSON_AddStringToObject(root, "device_id", runtime->zone_device_id);
    cJSON_AddStringToObject(root, "firmware_version", firmware_version());
    cJSON_AddStringToObject(root, "firmware_family", ZONE_LITE_FIRMWARE_FAMILY);
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    cJSON_AddStringToObject(root, "expected_serial", runtime->hik_expected_serial);
#else
    if (runtime->zkt_expected_serial[0]) {
        cJSON_AddStringToObject(root, "expected_serial", runtime->zkt_expected_serial);
    }
#endif
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return false;

    unsigned char body_digest[32];
    mbedtls_sha256((const unsigned char *)body, strlen(body), body_digest, 0);
    char body_hash[65];
    hex_bytes(body_digest, sizeof(body_digest), body_hash);
    char material[256];
    snprintf(
        material,
        sizeof(material),
        "POST\n/device/v2/onboard\n%s\n%s\n%s",
        timestamp,
        nonce,
        body_hash);
    unsigned char signature_bytes[32];
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    int hmac_result = mbedtls_md_hmac(
        md,
        (const unsigned char *)runtime->bootstrap_secret,
        strlen(runtime->bootstrap_secret),
        (const unsigned char *)material,
        strlen(material),
        signature_bytes);
    if (hmac_result != 0) {
        free(body);
        return false;
    }
    char signature[65];
    hex_bytes(signature_bytes, sizeof(signature_bytes), signature);

    onboarding_response_t response = {0};
    esp_http_client_config_t config = {
        .url = runtime->add_onboard_url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
        .event_handler = onboarding_http_event,
        .user_data = &response,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(body);
        return false;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "X-Zone-MAC", mac_text);
    esp_http_client_set_header(client, "X-ADD-Timestamp", timestamp);
    esp_http_client_set_header(client, "X-ADD-Nonce", nonce);
    esp_http_client_set_header(client, "X-ADD-Body-SHA256", body_hash);
    esp_http_client_set_header(client, "X-ADD-Signature", signature);
    esp_http_client_set_post_field(client, body, (int)strlen(body));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(body);
    if (err != ESP_OK || status < 200 || status >= 300) {
        ESP_LOGW(TAG, "ADD onboarding failed transport=%s status=%d", esp_err_to_name(err), status);
        return false;
    }
    cJSON *reply = cJSON_Parse(response.body);
    cJSON *connector_id = reply ? cJSON_GetObjectItemCaseSensitive(reply, "connector_id") : NULL;
    cJSON *device_token = reply ? cJSON_GetObjectItemCaseSensitive(reply, "device_token") : NULL;
    cJSON *ws_url = reply ? cJSON_GetObjectItemCaseSensitive(reply, "ws_url") : NULL;
    bool valid = cJSON_IsString(connector_id) && cJSON_IsString(device_token) &&
                 cJSON_IsString(ws_url);
    if (valid) {
        err = zone_config_save_connector(
            connector_id->valuestring,
            device_token->valuestring,
            ws_url->valuestring);
        valid = err == ESP_OK;
    }
    cJSON_Delete(reply);
    if (!valid) {
        ESP_LOGE(TAG, "ADD onboarding response could not be persisted");
        return false;
    }
    ESP_LOGI(TAG, "ADD automatic onboarding completed for connector %.8s…", zone_config_get()->connector_id);
    return true;
}

static void start_websocket(void)
{
    const zone_config_t *runtime = zone_config_get();
    if (!s_started || s_client || runtime->add_ws_url[0] == '\0' ||
        runtime->connector_id[0] == '\0' || runtime->device_token[0] == '\0') {
        return;
    }
    char headers[512];
    snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-ADD-Connector-Id: %s\r\n", runtime->device_token, runtime->connector_id);
    esp_websocket_client_config_t config = {
        .uri = runtime->add_ws_url,
        .headers = headers,
        .subprotocol = "add-device-v2",
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
    if (xSemaphoreTake(s_live_outbox.lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
        restore_outbox_if_needed(&s_live_outbox);
        s_live_outbox.offset = load_outbox_cursor(&s_live_outbox);
        (void)count_outbox_rows(&s_live_outbox);
        if (s_live_outbox.depth_known && !s_live_outbox.depth) (void)compact_outbox_locked(&s_live_outbox, true);
        xSemaphoreGive(s_live_outbox.lock);
    }
    if (xSemaphoreTake(s_bulk_outbox.lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
        restore_outbox_if_needed(&s_bulk_outbox);
        s_bulk_outbox.offset = load_outbox_cursor(&s_bulk_outbox);
        (void)count_outbox_rows(&s_bulk_outbox);
        if (s_bulk_outbox.depth_known && !s_bulk_outbox.depth) (void)compact_outbox_locked(&s_bulk_outbox, true);
        xSemaphoreGive(s_bulk_outbox.lock);
    }
    ESP_LOGI(
        TAG,
        "ADD outboxes restored live=%lu reconcile=%lu",
        (unsigned long)s_live_outbox.depth,
        (unsigned long)s_bulk_outbox.depth);
    s_outboxes_initialized = true;
    // The independent supervisor checks creation results and retries boundedly.
    // Start transport and heartbeat visibility before scanning the encrypted
    // catalog.  The scan is bounded and fail-closed, but SPIFFS latency must
    // never delay OTA boot supervision or make an otherwise running connector
    // disappear from the control plane.  Do not touch transaction files here:
    // a fresh catalog may already be arriving on the inbound worker.
    (void)restore_valid_identity_catalog();
}

static void onboarding_task(void *arg)
{
    (void)arg;
    uint32_t delay_ms = 5000;
    while (zone_config_needs_onboarding()) {
        if (perform_onboarding()) break;
        vTaskDelay(pdMS_TO_TICKS(delay_ms));
        if (delay_ms < 300000) delay_ms *= 2;
        if (delay_ms > 300000) delay_ms = 300000;
    }
    start_websocket();
    s_onboarding_task_started = false;
    vTaskDelete(NULL);
}

void add_connector_start(void)
{
    if (!s_started || s_client || s_onboarding_task_started) return;
    restore_command_inbox();
    if (zone_config_needs_onboarding()) {
        s_onboarding_task_started = true;
        if (xTaskCreate(onboarding_task, "add_onboard", 10240, NULL, 4, NULL) != pdPASS) {
            s_onboarding_task_started = false;
            ESP_LOGE(TAG, "Could not start ADD onboarding task");
        }
        return;
    }
    recover_identity_catalog_backup_if_active_missing();
    start_websocket();
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

bool add_connector_boot_health_ready(void)
{
#ifdef ZONE_LITE_HIKVISION
    return storage_upgrade_ready() && add_connector_is_connected() &&
        s_heartbeat_task_handle && add_connector_delivery_healthy() &&
        hikvision_boot_health_ready();
#else
    bool ready = false;
    if (!storage_upgrade_ready()) return false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        time_t now = time(NULL);
        bool authenticated_stability_elapsed =
            s_zkt.stability_since_epoch > 1700000000 &&
            now >= s_zkt.stability_since_epoch &&
            ((uint64_t)(now - s_zkt.stability_since_epoch) * 1000ULL) >=
                ZONE_LITE_RECOVERY_STABILITY_MS;
        bool stable_terminal_session =
            strcmp(s_zkt.connection_state, "ONLINE") == 0 ||
            (strcmp(s_zkt.connection_state, "RECOVERING") == 0 &&
             authenticated_stability_elapsed);
        // A complete authenticated terminal snapshot is sufficient to prove
        // the running image when a saturated preservation partition cannot
        // stage the optional ADD alias catalog. Oracle truth remains
        // independently fail-closed for every unresolved identity.
        bool identity_ready = s_identity_catalog_generation > 0 ||
            (s_zkt.user_count > 0 && s_zkt.user_record_size > 0);
        ready = s_connected
            && s_outbox_task_handle && s_heartbeat_task_handle && s_outbox_buffer_ready
            && !s_worker_start_failed && s_ords_worker_started
            && s_ords_worker_operation != ADD_WORKER_RESOURCE
            && (uint32_t)((uint32_t)monotonic_ms() - s_ords_worker_tick_ms) < 90000U
            && (uint32_t)((uint32_t)monotonic_ms() - s_outbox_tick_ms) < 90000U
            && identity_ready
            && s_zkt.online
            && stable_terminal_session
            && s_zkt.user_count >= 0
            && s_zkt.attendance_count >= 0;
        xSemaphoreGive(s_lock);
    }
    return ready;
#endif
}

bool add_connector_ota_reconcile_ready(void)
{
    if (!add_connector_boot_health_ready()) return false;
#ifdef ZONE_LITE_HIKVISION
    /* Boot recovery has validated source custody and its persisted checkpoint.
     * Empty means all retained local observations have an ADD durable receipt;
     * it does not certify terminal history or Oracle coverage. */
    uint32_t pending;
    return qs_snapshot(QS_HIK_SOURCE, &pending) && pending == 0;
#else
    bool ready = false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        ready = s_zkt.add_source_coverage_certified && s_zkt.attendance_count >= 0 &&
            s_zkt.add_source_coverage_cursor == (uint32_t)s_zkt.attendance_count;
        xSemaphoreGive(s_lock);
    }
    return ready;
#endif
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
    if (s_live_outbox.lock && xSemaphoreTake(s_live_outbox.lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        depth += s_live_outbox.depth;
        xSemaphoreGive(s_live_outbox.lock);
    }
    if (s_bulk_outbox.lock && xSemaphoreTake(s_bulk_outbox.lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        depth += s_bulk_outbox.depth;
        xSemaphoreGive(s_bulk_outbox.lock);
    }
    return depth;
}

bool add_connector_get_bulk_outbox_depth(uint32_t *depth_out)
{
    if (!depth_out || !s_bulk_outbox.lock ||
        xSemaphoreTake(s_bulk_outbox.lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return false;
    }
    *depth_out = s_bulk_outbox.depth;
    bool known = s_bulk_outbox.depth_known;
    xSemaphoreGive(s_bulk_outbox.lock);
    uint32_t segmented = 0;
    if (!known || !qs_snapshot(QS_BULK, &segmented) || segmented > UINT32_MAX - *depth_out) return false;
    *depth_out += segmented;
    return true;
}

void add_connector_set_activity(const char *activity)
{
    if (!s_lock || !activity) return;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (!s_ota_restart_claimed || strcmp(activity, "OTA_RESTART") == 0) {
            strlcpy(s_activity, activity, sizeof(s_activity));
        }
        xSemaphoreGive(s_lock);
    }
}

bool add_connector_begin_exclusive_activity(const char *activity)
{
    if (!s_lock || !activity) return false;
    bool started = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (!s_ota_restart_claimed) {
            strlcpy(s_activity, activity, sizeof(s_activity));
            started = true;
        }
        xSemaphoreGive(s_lock);
    }
    return started;
}

bool add_connector_claim_ota_restart(void)
{
    if (!s_lock) return false;
    bool claimed = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
#ifdef ZONE_LITE_HIKVISION
        bool restart_ready = !s_ota_restart_claimed && hikvision_claim_ota_restart();
#else
        bool activity_is_safe =
            strcmp(s_activity, "LIVE_CAPTURE") == 0 ||
            strcmp(s_activity, "ONLINE") == 0;
        bool terminal_is_stable =
            s_zkt.online && strcmp(s_zkt.connection_state, "ONLINE") == 0;
        bool restart_ready = !s_ota_restart_claimed && activity_is_safe && terminal_is_stable;
#endif
        if (restart_ready) {
            s_ota_restart_claimed = true;
            strlcpy(s_activity, "OTA_RESTART", sizeof(s_activity));
            claimed = true;
        }
        xSemaphoreGive(s_lock);
    }
    return claimed;
}

bool add_connector_begin_pending_command_activity(void)
{
    if (!s_lock || !s_commands) return false;
    bool started = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (!s_ota_restart_claimed && uxQueueMessagesWaiting(s_commands) > 0) {
            strlcpy(s_activity, "PROCESSING_COMMAND", sizeof(s_activity));
            started = true;
        }
        xSemaphoreGive(s_lock);
    }
    return started;
}

bool add_connector_begin_pending_config_activity(void)
{
    if (!s_lock || !s_config_commands) return false;
    bool started = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (!s_ota_restart_claimed && uxQueueMessagesWaiting(s_config_commands) > 0) {
            strlcpy(s_activity, "APPLYING_CONFIG", sizeof(s_activity));
            started = true;
        }
        xSemaphoreGive(s_lock);
    }
    return started;
}

bool add_connector_has_pending_config_command(void)
{
    return s_config_commands && uxQueueMessagesWaiting(s_config_commands) > 0;
}

bool add_connector_take_config_command(add_command_t *out)
{
    if (!s_config_commands || !out ||
        xQueueReceive(s_config_commands, out, 0) != pdTRUE) return false;
    if (s_command_lock && xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        command_unmark_queued_locked(out->command_id);
        strlcpy(s_running_command_id, out->command_id, sizeof(s_running_command_id));
        xSemaphoreGive(s_command_lock);
    }
    return true;
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
    if (!s_commands || !out || xQueueReceive(s_commands, out, 0) != pdTRUE) return false;
    if (s_command_lock && xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        command_unmark_queued_locked(out->command_id);
        strlcpy(s_running_command_id, out->command_id, sizeof(s_running_command_id));
        xSemaphoreGive(s_command_lock);
    }
    return true;
}

bool add_connector_take_reconcile_assignment(add_reconcile_assignment_t *out)
{
    return s_reconcile_assignments && out &&
        xQueueReceive(s_reconcile_assignments, out, 0) == pdTRUE;
}

bool add_connector_has_reconcile_assignment(void)
{
    return s_reconcile_assignments &&
        uxQueueMessagesWaiting(s_reconcile_assignments) > 0;
}

bool add_connector_take_source_coverage(add_source_coverage_t *out)
{
    return s_source_coverage && out &&
        xQueueReceive(s_source_coverage, out, 0) == pdTRUE;
}

void add_connector_command_retry(const char *command_id)
{
    if (!command_id || !s_command_lock ||
        xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return;
    }
    if (strcmp(s_running_command_id, command_id) == 0) s_running_command_id[0] = '\0';
    xSemaphoreGive(s_command_lock);
}

bool add_connector_command_complete(const char *command_id)
{
    if (!command_id || !s_command_lock ||
        xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(3000)) != pdTRUE) return false;
    if (!command_journal_recover_locked()) { xSemaphoreGive(s_command_lock); return false; }
    FILE *input = fopen(ADD_COMMAND_INBOX_PATH, "r");
    if (!input) {
        bool absent = errno == ENOENT;
        if (absent && strcmp(s_running_command_id, command_id) == 0) s_running_command_id[0] = '\0';
        xSemaphoreGive(s_command_lock);
        return absent;
    }
    struct stat st;
    bool bounded = fstat(fileno(input), &st) == 0 && st.st_size >= 0 && st.st_size <= ADD_COMMAND_INBOX_MAX_BYTES;
    bool admitted = bounded && qs_local_begin(QS_ADMIT_RECOVERY, (size_t)st.st_size);
    FILE *output = admitted ? fopen(ADD_COMMAND_INBOX_TMP_PATH, "w") : NULL;
    char *line = output ? malloc(ADD_COMMAND_LINE_BYTES) : NULL;
    bool ok = output && line;
    while (ok && fgets(line, ADD_COMMAND_LINE_BYTES, input)) {
        size_t length = strlen(line);
        if (!length || line[length - 1] != '\n') { ok = false; break; }
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *id = root ? cJSON_GetObjectItemCaseSensitive(root, "command_id") : NULL;
        if (!cJSON_IsString(id)) ok = false; // allocation/decryption errors retain the old generation
        bool remove_row = cJSON_IsString(id) && strcmp(id->valuestring, command_id) == 0;
        cJSON_Delete(root);
        free(plain);
        if (ok && !remove_row && fputs(line, output) == EOF) ok = false;
    }
    if (ferror(input)) ok = false;
    if (output && (fflush(output) != 0 || fsync(fileno(output)) != 0)) ok = false;
    if (fclose(input) != 0) ok = false;
    if (output && fclose(output) != 0) ok = false;
    free(line);
    if (ok) ok = ft_replace(ADD_COMMAND_INBOX_PATH, ADD_COMMAND_INBOX_TMP_PATH,
        ADD_COMMAND_INBOX_BACKUP_PATH, ADD_COMMAND_INBOX_MAX_BYTES, command_transaction_port);
    // Never remove a staged generation after an uncertain NVS commit.
    if (admitted) qs_local_end(ok, ok ? 0 : errno);
    if (ok) {
        if (strcmp(s_running_command_id, command_id) == 0) s_running_command_id[0] = '\0';
        command_unmark_queued_locked(command_id);
    } else led_status_fault(LED_STATUS_LOCAL_FAILURE);
    xSemaphoreGive(s_command_lock);
    return ok;
}

static bool add_connector_lookup_identity_locked(
    const char *user_id,
    const char *uid,
    char *display_name,
    size_t display_name_size,
    char *cnic,
    size_t cnic_size,
    bool *shift_worker)
{
    if ((!user_id || !user_id[0]) && (!uid || !uid[0])) return false;
    bool found = false;
    bool memory_catalog_valid = false;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        memory_catalog_valid = s_identity_catalog_active_memory_valid;
        for (size_t i = 0; i < s_identity_catalog_active_alias_rows; i++) {
            const add_identity_alias_t *alias =
                &s_identity_catalog_active_aliases[i];
            bool user_matches = !user_id || !user_id[0] ||
                strcmp(alias->user_id, user_id) == 0;
            bool uid_matches = !uid || !uid[0] ||
                strcmp(alias->uid, uid) == 0;
            if (!user_matches || !uid_matches) continue;
            if (display_name && display_name_size) {
                strlcpy(
                    display_name,
                    alias->display_name,
                    display_name_size);
            }
            if (cnic && cnic_size) {
                strlcpy(cnic, alias->cnic, cnic_size);
            }
            if (shift_worker) *shift_worker = alias->shift_worker;
            found = true;
            break;
        }
        xSemaphoreGive(s_lock);
    }
    // A complete volatile catalog is authoritative even when it contains no
    // matching alias. Falling through to an older flash catalog could revive
    // an alias that the latest verified catalog deliberately removed.
    if (memory_catalog_valid) return found;

    if (!recover_catalog_transaction_locked()) return false;
    FILE *file = fopen(ADD_IDENTITY_CATALOG_PATH, "r");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    if (file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *rows = root ? cJSON_GetObjectItemCaseSensitive(root, "rows") : NULL;
        bool legacy_catalog = cJSON_IsArray(rows);
        cJSON *row = NULL;
        cJSON_ArrayForEach(row, rows) {
            cJSON *candidate = cJSON_GetObjectItemCaseSensitive(row, "user_id");
            cJSON *candidate_uid = cJSON_GetObjectItemCaseSensitive(row, "uid");
            bool user_matches = !user_id || !user_id[0] ||
                (cJSON_IsString(candidate) && strcmp(candidate->valuestring, user_id) == 0);
            bool uid_matches = !uid || !uid[0] ||
                (cJSON_IsString(candidate_uid) && strcmp(candidate_uid->valuestring, uid) == 0);
            if (!user_matches || !uid_matches) continue;
            cJSON *name = cJSON_GetObjectItemCaseSensitive(row, "display_name");
            cJSON *identity = cJSON_GetObjectItemCaseSensitive(row, "cnic");
            cJSON *shift = cJSON_GetObjectItemCaseSensitive(row, "shift_worker");
            if (display_name && display_name_size && cJSON_IsString(name)) {
                strlcpy(display_name, name->valuestring, display_name_size);
            }
            if (cnic && cnic_size && cJSON_IsString(identity)) {
                strlcpy(cnic, identity->valuestring, cnic_size);
            }
            if (shift_worker) *shift_worker = cJSON_IsTrue(shift);
            found = true;
            break;
        }
        cJSON_Delete(root);
        free(plain);
        // Schema v3 stores one encrypted JSON row per line.  This keeps
        // lookups and catalog replacement bounded independently of fleet size
        // while retaining support for the legacy single-object catalog.
        if (!found && !legacy_catalog) {
            while (fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
                char *row_plain = decrypt_storage_line(line);
                row = row_plain ? cJSON_Parse(row_plain) : NULL;
                free(row_plain);
                cJSON *candidate = row
                    ? cJSON_GetObjectItemCaseSensitive(row, "user_id")
                    : NULL;
                cJSON *candidate_uid = row
                    ? cJSON_GetObjectItemCaseSensitive(row, "uid")
                    : NULL;
                bool user_matches = !user_id || !user_id[0] ||
                    (cJSON_IsString(candidate) &&
                     strcmp(candidate->valuestring, user_id) == 0);
                bool uid_matches = !uid || !uid[0] ||
                    (cJSON_IsString(candidate_uid) &&
                     strcmp(candidate_uid->valuestring, uid) == 0);
                if (user_matches && uid_matches) {
                    cJSON *name =
                        cJSON_GetObjectItemCaseSensitive(row, "display_name");
                    cJSON *identity =
                        cJSON_GetObjectItemCaseSensitive(row, "cnic");
                    cJSON *shift =
                        cJSON_GetObjectItemCaseSensitive(row, "shift_worker");
                    if (display_name && display_name_size &&
                        cJSON_IsString(name)) {
                        strlcpy(
                            display_name,
                            name->valuestring,
                            display_name_size);
                    }
                    if (cnic && cnic_size && cJSON_IsString(identity)) {
                        strlcpy(cnic, identity->valuestring, cnic_size);
                    }
                    if (shift_worker) *shift_worker = cJSON_IsTrue(shift);
                    found = true;
                }
                cJSON_Delete(row);
                if (found) break;
            }
        }
    }
    if (file) fclose(file);
    free(line);
    return found;
}

bool add_connector_lookup_identity(const char *user_id, const char *uid,
    char *display_name, size_t display_name_size, char *cnic, size_t cnic_size, bool *shift_worker)
{
    if (!s_catalog_lock || xSemaphoreTake(s_catalog_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return false;
    bool ok = add_connector_lookup_identity_locked(user_id, uid, display_name, display_name_size,
        cnic, cnic_size, shift_worker);
    xSemaphoreGive(s_catalog_lock);
    return ok;
}

uint32_t add_connector_identity_catalog_generation(size_t *row_count)
{
    uint32_t generation = 0;
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        generation = s_identity_catalog_generation;
        if (row_count) {
            *row_count = s_identity_catalog_rows;
        }
        xSemaphoreGive(s_lock);
    } else if (row_count) {
        *row_count = 0;
    }
    return generation;
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

bool add_connector_delivery_healthy(void)
{
    return s_outbox_task_handle && s_outbox_buffer_ready && !s_worker_start_failed &&
        (uint32_t)((uint32_t)monotonic_ms() - s_outbox_tick_ms) <= 90000U;
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

bool add_connector_take_hikvision_assignment(char out[2048])
{
#ifdef ZONE_LITE_HIKVISION
    return out && s_hikvision_assignments && xQueueReceive(s_hikvision_assignments, out, 0) == pdTRUE;
#else
    (void)out; return false;
#endif
}
