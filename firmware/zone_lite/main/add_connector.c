#include "add_connector.h"
#include "ota_manager.h"

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
#define ADD_INBOUND_QUEUE_DEPTH 96
#define ADD_SEND_TIMEOUT_MS 5000
#define ADD_MAX_INBOUND_BYTES (512 * 1024)
#define ADD_IDENTITY_CATALOG_MAX_ROWS 4096
#define ADD_ACK_TIMEOUT_MS 60000
#define ADD_OUTBOX_RETRY_MS 5000
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
#define ADD_COMMAND_INBOX_PATH "/storage/add_commands.jsonl"
#define ADD_COMMAND_INBOX_TMP_PATH "/storage/add_commands.tmp"
#define ADD_COMMAND_LINE_BYTES 12288
#define ADD_IDENTITY_CATALOG_PATH "/storage/add_identities.enc"
#define ADD_IDENTITY_CATALOG_TMP_PATH "/storage/add_identities.tmp"
#define ADD_IDENTITY_CATALOG_STAGE_PATH "/storage/add_identities.stage"
#define ADD_IDENTITY_CATALOG_BACKUP_PATH "/storage/add_identities.backup"
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
    uint32_t ack_since_checkpoint;
    const char *label;
    SemaphoreHandle_t lock;
} add_outbox_t;

typedef struct {
    char *data;
    size_t length;
} add_inbound_message_t;

static const char *TAG = "add_connector";
static esp_websocket_client_handle_t s_client;
static QueueHandle_t s_commands;
static QueueHandle_t s_inbound_messages;
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_send_lock;
static SemaphoreHandle_t s_ack_sem;
static SemaphoreHandle_t s_command_lock;
static add_zkt_telemetry_t s_zkt;
static char s_activity[64] = "BOOTING";
static char s_boot_id[48];
static uint64_t s_sequence;
static bool s_started;
static bool s_connected;
static bool s_connected_edge;
static bool s_ack_matched;
static bool s_onboarding_task_started;
static bool s_command_inbox_restored;
static char s_waiting_ack[80];
static char s_running_command_id[48];
static char s_queued_command_ids[ADD_COMMAND_QUEUE_DEPTH][48];
static size_t s_queued_command_count;
static char *s_inbound_payload;
static size_t s_inbound_payload_expected;
static size_t s_inbound_payload_received;
static uint32_t s_identity_catalog_generation;
static size_t s_identity_catalog_rows;
static char s_identity_catalog_stage_id[40];
static size_t s_identity_catalog_stage_expected;
static size_t s_identity_catalog_stage_rows;
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

static int64_t monotonic_ms(void)
{
    return esp_timer_get_time() / 1000;
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
    cJSON_AddStringToObject(root, "schema_version", "2");
    cJSON_AddStringToObject(root, "message_id", message_id);
    cJSON_AddStringToObject(root, "connector_id", zone_config_get()->connector_id);
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
    mbedtls_sha256((const unsigned char *)material, strlen(material), key, 0);
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
    unsigned char key[32];
    mbedtls_sha256((const unsigned char *)material, strlen(material), key, 0);
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

static bool write_encrypted_json_line(FILE *file, cJSON *value)
{
    char *plain = value ? cJSON_PrintUnformatted(value) : NULL;
    char *encrypted = encrypt_storage_json(plain);
    bool ok = file && encrypted && fprintf(file, "%s\n", encrypted) > 0;
    free(plain);
    free(encrypted);
    return ok;
}

static void recover_identity_catalog_backup_if_active_missing(void)
{
    struct stat active = {0};
    errno = 0;
    if (stat(ADD_IDENTITY_CATALOG_PATH, &active) == 0) {
        return;
    }
    if (errno != ENOENT) {
        ESP_LOGW(TAG, "Could not inspect the active ADD identity catalog");
        return;
    }

    // Finish only the interrupted active-to-backup transaction before the
    // WebSocket can deliver a fresh catalog.  This rename is constant-time and
    // does not parse or decrypt catalog rows, so it cannot hide the first boot
    // heartbeat.  Once transport starts, restore_valid_identity_catalog()
    // validates the recovered bytes and exact row count before publishing a
    // non-zero generation to the OTA health gate.
    errno = 0;
    if (rename(
            ADD_IDENTITY_CATALOG_BACKUP_PATH,
            ADD_IDENTITY_CATALOG_PATH) == 0) {
        ESP_LOGW(TAG, "Recovered interrupted ADD identity catalog transaction");
    } else if (errno != ENOENT) {
        ESP_LOGW(TAG, "Could not recover the ADD identity catalog backup");
    }
}

static bool restore_valid_identity_catalog(void)
{
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
    if (file) fclose(file);
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

static bool activate_identity_catalog(const char *staged_path)
{
    if (!staged_path) return false;
    (void)remove(ADD_IDENTITY_CATALOG_BACKUP_PATH);
    // Some ESP-IDF VFS backends do not implement access() even though rename()
    // is supported.  Treat a successful rename as the authoritative existence
    // check; ENOENT is the only valid "no active catalog yet" result.
    errno = 0;
    int backup_result = rename(
        ADD_IDENTITY_CATALOG_PATH,
        ADD_IDENTITY_CATALOG_BACKUP_PATH);
    bool had_active = backup_result == 0;
    if (!had_active && errno != ENOENT) {
        return false;
    }
    if (rename(staged_path, ADD_IDENTITY_CATALOG_PATH) == 0) {
        (void)remove(ADD_IDENTITY_CATALOG_BACKUP_PATH);
        return true;
    }
    if (had_active) {
        (void)rename(
            ADD_IDENTITY_CATALOG_BACKUP_PATH,
            ADD_IDENTITY_CATALOG_PATH);
    }
    return false;
}

static bool persist_identity_catalog(cJSON *root, size_t *row_count_out)
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
    FILE *file = fopen(ADD_IDENTITY_CATALOG_TMP_PATH, "w");
    cJSON *metadata = cJSON_CreateObject();
    bool ok = file && metadata;
    if (ok) {
        cJSON_AddStringToObject(metadata, "schema_version", "3");
        cJSON_AddStringToObject(metadata, "type", "identity_catalog");
        cJSON_AddNumberToObject(metadata, "rows_count", row_count);
        ok = write_encrypted_json_line(file, metadata);
    }
    cJSON_Delete(metadata);
    cJSON *row = NULL;
    cJSON_ArrayForEach(row, rows) {
        if (ok && !write_encrypted_json_line(file, row)) {
            ok = false;
        }
    }
    if (file && (fflush(file) != 0 || fsync(fileno(file)) != 0)) ok = false;
    if (file) fclose(file);
    if (ok) {
        ok = activate_identity_catalog(ADD_IDENTITY_CATALOG_TMP_PATH);
    }
    if (!ok) (void)remove(ADD_IDENTITY_CATALOG_TMP_PATH);
    if (ok && row_count_out) {
        *row_count_out = (size_t)row_count;
    }
    return ok;
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
    FILE *file = fopen(ADD_IDENTITY_CATALOG_STAGE_PATH, "w");
    cJSON *metadata = cJSON_CreateObject();
    bool ok = file && metadata;
    if (ok) {
        cJSON_AddStringToObject(metadata, "schema_version", "3");
        cJSON_AddStringToObject(metadata, "type", "identity_catalog");
        cJSON_AddNumberToObject(metadata, "rows_count", expected);
        ok = write_encrypted_json_line(file, metadata) &&
            fflush(file) == 0 && fsync(fileno(file)) == 0;
    }
    cJSON_Delete(metadata);
    if (file) fclose(file);
    if (!ok) {
        (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
        s_identity_catalog_stage_id[0] = '\0';
        s_identity_catalog_stage_expected = 0;
        s_identity_catalog_stage_rows = 0;
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
    FILE *file = fopen(ADD_IDENTITY_CATALOG_STAGE_PATH, "a");
    bool ok = file != NULL;
    cJSON *row = NULL;
    cJSON_ArrayForEach(row, rows) {
        if (!cJSON_IsObject(row) ||
            (ok && !write_encrypted_json_line(file, row))) {
            ok = false;
        }
    }
    if (file && (fflush(file) != 0 || fsync(fileno(file)) != 0)) ok = false;
    if (file) fclose(file);
    if (ok) {
        s_identity_catalog_stage_rows += (size_t)chunk_rows;
    }
    return ok;
}

static bool identity_catalog_stage_commit(cJSON *root, size_t *row_count_out)
{
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
    if (ok) {
        ok = activate_identity_catalog(ADD_IDENTITY_CATALOG_STAGE_PATH);
    }
    if (ok && row_count_out) {
        *row_count_out = s_identity_catalog_stage_rows;
    }
    if (!ok) {
        (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
    }
    s_identity_catalog_stage_id[0] = '\0';
    s_identity_catalog_stage_expected = 0;
    s_identity_catalog_stage_rows = 0;
    return ok;
}

bool add_connector_persist_command_tombstone(const add_command_t *command)
{
    if (!command || !command->has_tombstone || !command->user_id[0]) return false;
    cJSON *root = NULL;
    FILE *file = fopen(ADD_IDENTITY_CATALOG_PATH, "r");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    if (file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        root = plain ? cJSON_Parse(plain) : NULL;
        free(plain);
        cJSON *legacy_rows = root
            ? cJSON_GetObjectItemCaseSensitive(root, "rows")
            : NULL;
        if (root && !cJSON_IsArray(legacy_rows)) {
            cJSON_Delete(root);
            root = cJSON_CreateObject();
            cJSON_AddStringToObject(root, "schema_version", "3");
            cJSON_AddStringToObject(root, "type", "identity_catalog");
            cJSON *stream_rows = cJSON_AddArrayToObject(root, "rows");
            while (stream_rows && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
                char *row_plain = decrypt_storage_line(line);
                cJSON *row = row_plain ? cJSON_Parse(row_plain) : NULL;
                free(row_plain);
                if (!cJSON_IsObject(row)) {
                    cJSON_Delete(row);
                    cJSON_Delete(root);
                    root = NULL;
                    break;
                }
                cJSON_AddItemToArray(stream_rows, row);
            }
        }
    }
    if (file) fclose(file);
    free(line);
    if (!root || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "schema_version", "2");
        cJSON_AddStringToObject(root, "type", "identity_catalog");
        cJSON_AddArrayToObject(root, "rows");
    }
    cJSON *rows = cJSON_GetObjectItemCaseSensitive(root, "rows");
    if (!cJSON_IsArray(rows)) {
        cJSON_DeleteItemFromObjectCaseSensitive(root, "rows");
        rows = cJSON_AddArrayToObject(root, "rows");
    }
    cJSON *target = NULL;
    cJSON *row = NULL;
    cJSON_ArrayForEach(row, rows) {
        cJSON *user_id = cJSON_GetObjectItemCaseSensitive(row, "user_id");
        if (cJSON_IsString(user_id) && strcmp(user_id->valuestring, command->user_id) == 0) {
            target = row;
            break;
        }
    }
    if (!target) {
        target = cJSON_CreateObject();
        cJSON_AddItemToArray(rows, target);
    }
    cJSON_DeleteItemFromObjectCaseSensitive(target, "uid");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "user_id");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "display_name");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "cnic");
    cJSON_DeleteItemFromObjectCaseSensitive(target, "shift_worker");
    cJSON_AddStringToObject(target, "uid", command->uid);
    cJSON_AddStringToObject(target, "user_id", command->user_id);
    cJSON_AddStringToObject(target, "display_name", command->tombstone_display_name);
    cJSON_AddStringToObject(target, "cnic", command->tombstone_cnic);
    cJSON_AddBoolToObject(target, "shift_worker", command->tombstone_shift_worker);
    bool ok = persist_identity_catalog(root, NULL);
    cJSON_Delete(root);
    return ok;
}

static bool append_cancelled_command(const char *command_id)
{
    FILE *file = fopen(ADD_CANCELLED_COMMANDS_PATH, "a");
    bool ok = file && fprintf(file, "%s\n", command_id) > 0 && fflush(file) == 0 &&
              fsync(fileno(file)) == 0;
    if (file) fclose(file);
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

static bool command_journal_contains_locked(const char *command_id)
{
    FILE *file = fopen(ADD_COMMAND_INBOX_PATH, "r");
    if (!file) return false;
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    bool found = false;
    while (line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *id = root ? cJSON_GetObjectItemCaseSensitive(root, "command_id") : NULL;
        if (cJSON_IsString(id) && strcmp(id->valuestring, command_id) == 0) found = true;
        cJSON_Delete(root);
        free(plain);
        if (found) break;
    }
    free(line);
    fclose(file);
    return found;
}

static bool command_journal_append(cJSON *root, const char *command_id)
{
    if (!s_command_lock || xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(2000)) != pdTRUE) {
        return false;
    }
    if (command_journal_contains_locked(command_id)) {
        xSemaphoreGive(s_command_lock);
        return true;
    }
    char *plain = cJSON_PrintUnformatted(root);
    char *line = encrypt_storage_json(plain);
    FILE *file = line ? fopen(ADD_COMMAND_INBOX_PATH, "a") : NULL;
    bool ok = file && fprintf(file, "%s\n", line) > 0 && fflush(file) == 0 &&
              fsync(fileno(file)) == 0;
    if (file) fclose(file);
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
        s_queued_command_count >= ADD_COMMAND_QUEUE_DEPTH) {
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
    bool queued = xQueueSend(s_commands, command, 0) == pdTRUE;
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
    FILE *file = fopen(ADD_COMMAND_INBOX_PATH, "r");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    uint32_t restored = 0;
    while (file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        add_command_t command;
        if (root && parse_command_object(root, &command) &&
            xQueueSend(s_commands, &command, 0) == pdTRUE) {
            command_mark_queued_locked(command.command_id);
            restored++;
        }
        cJSON_Delete(root);
        free(plain);
    }
    if (file) fclose(file);
    free(line);
    s_command_inbox_restored = true;
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
            (void)remove(ADD_IDENTITY_CATALOG_STAGE_PATH);
            s_identity_catalog_stage_id[0] = '\0';
            s_identity_catalog_stage_expected = 0;
            s_identity_catalog_stage_rows = 0;
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
        if (!identity_catalog_stage_commit(root, &row_count)) {
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
    add_command_t command;
    if (!parse_command_object(root, &command)) {
        cJSON_Delete(root);
        return;
    }
    if (!command_journal_append(root, command.command_id)) {
        ESP_LOGE(TAG, "Could not durably journal ADD command %s", command.command_id);
        add_connector_command_update(
            command.command_id,
            "FAILED",
            "COMMAND_JOURNAL_FAILED",
            "Command was not acknowledged because durable storage failed.",
            "{}");
    } else if (!queue_command_if_idle(&command)) {
        ESP_LOGW(TAG, "Command queue full; rejecting %s", command.command_id);
        add_connector_command_update(
            command.command_id,
            "RETRYING",
            "COMMAND_EXECUTOR_BUSY",
            "Command is durable and waiting for the serialized executor.",
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
            cJSON_AddNumberToObject(payload, "config_version", 3);
            cJSON_AddNumberToObject(payload, "uptime_seconds", (double)(esp_timer_get_time() / 1000000));
            cJSON_AddNumberToObject(payload, "rssi", rssi);
            cJSON_AddNumberToObject(payload, "free_heap", esp_get_free_heap_size());
            cJSON_AddNumberToObject(payload, "outbox_depth", add_connector_outbox_depth());
            ota_manager_append_telemetry(payload);
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
    fclose(file);
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
    struct stat pending = {0};
    struct stat backup = {0};
    bool has_pending = stat(outbox->path, &pending) == 0;
    bool has_backup = stat(outbox->backup_path, &backup) == 0;
    if (!has_pending && has_backup) {
        if (rename(outbox->backup_path, outbox->path) == 0) {
            ESP_LOGW(TAG, "Recovered ADD %s outbox after interrupted compaction", outbox->label);
            has_pending = true;
        }
    }
    if (has_pending) (void)remove(outbox->backup_path);
    (void)remove(outbox->tmp_path);
    (void)remove(outbox->cursor_tmp_path);
}

static off_t load_outbox_cursor(add_outbox_t *outbox)
{
    struct stat st = {0};
    if (stat(outbox->path, &st) != 0 || st.st_size <= 0) {
        (void)remove(outbox->cursor_path);
        return 0;
    }
    FILE *cursor = fopen(outbox->cursor_path, "r");
    long long value = 0;
    bool parsed = cursor && fscanf(cursor, "%lld", &value) == 1;
    if (cursor) fclose(cursor);
    if (!parsed || value < 0 || value > (long long)st.st_size) {
        (void)remove(outbox->cursor_path);
        return 0;
    }
    if (value > 0) {
        FILE *file = fopen(outbox->path, "r");
        int preceding = EOF;
        if (file && fseek(file, (long)value - 1, SEEK_SET) == 0) preceding = fgetc(file);
        if (file) fclose(file);
        if (preceding != '\n') {
            ESP_LOGW(TAG, "Ignoring invalid ADD %s outbox cursor", outbox->label);
            (void)remove(outbox->cursor_path);
            return 0;
        }
    }
    return (off_t)value;
}

static uint32_t count_outbox_rows(add_outbox_t *outbox)
{
    FILE *file = fopen(outbox->path, "r");
    if (!file) return 0;
    if (outbox->offset > 0 && fseek(file, (long)outbox->offset, SEEK_SET) != 0) {
        fclose(file);
        outbox->offset = 0;
        (void)remove(outbox->cursor_path);
        file = fopen(outbox->path, "r");
        if (!file) return 0;
    }
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

static bool compact_outbox_locked(add_outbox_t *outbox, bool force)
{
    if (outbox->offset <= 0) return true;
    struct stat st = {0};
    if (stat(outbox->path, &st) != 0) {
        outbox->offset = 0;
        outbox->depth = 0;
        outbox->ack_since_checkpoint = 0;
        (void)remove(outbox->cursor_path);
        return true;
    }
    if (!force && (outbox->offset < ADD_OUTBOX_COMPACT_MIN_BYTES ||
        outbox->offset < st.st_size / 2)) {
        return true;
    }
    if (outbox->depth == 0 || outbox->offset >= st.st_size) {
        (void)remove(outbox->path);
        (void)remove(outbox->cursor_path);
        outbox->offset = 0;
        outbox->depth = 0;
        outbox->ack_since_checkpoint = 0;
        return true;
    }

    FILE *in = fopen(outbox->path, "r");
    FILE *out = fopen(outbox->tmp_path, "w");
    char *buffer = allocate_outbox_line_buffer();
    if (!in || !out || !buffer || fseek(in, (long)outbox->offset, SEEK_SET) != 0) {
        if (in) fclose(in);
        if (out) fclose(out);
        free(buffer);
        (void)remove(outbox->tmp_path);
        return false;
    }
    bool ok = true;
    size_t read = 0;
    while ((read = fread(buffer, 1, ADD_OUTBOX_LINE_BYTES, in)) > 0) {
        if (fwrite(buffer, 1, read, out) != read) {
            ok = false;
            break;
        }
    }
    if (ferror(in) || fflush(out) != 0 || fsync(fileno(out)) != 0) ok = false;
    fclose(in);
    fclose(out);
    free(buffer);
    if (!ok || !write_outbox_cursor(outbox, 0)) {
        (void)remove(outbox->tmp_path);
        return false;
    }
    (void)remove(outbox->backup_path);
    if (rename(outbox->path, outbox->backup_path) != 0) {
        (void)remove(outbox->tmp_path);
        return false;
    }
    if (rename(outbox->tmp_path, outbox->path) != 0) {
        (void)rename(outbox->backup_path, outbox->path);
        return false;
    }
    (void)remove(outbox->backup_path);
    outbox->offset = 0;
    outbox->ack_since_checkpoint = 0;
    ESP_LOGI(TAG, "Compacted acknowledged prefix from ADD %s outbox", outbox->label);
    return true;
}

static bool advance_outbox_locked(add_outbox_t *outbox, off_t row_end)
{
    if (row_end <= outbox->offset) return false;
    outbox->offset = row_end;
    if (outbox->depth > 0) outbox->depth--;
    outbox->ack_since_checkpoint++;
    if (outbox->depth == 0) {
        // Every row in this generation has been acknowledged, so deleting the
        // file is itself the durable checkpoint.
        (void)remove(outbox->path);
        (void)remove(outbox->cursor_path);
        outbox->offset = 0;
        outbox->ack_since_checkpoint = 0;
        return true;
    }
    // Checkpoint in small groups to limit flash wear.  A power loss before a
    // checkpoint only replays already-acknowledged event UIDs.
    if (outbox->ack_since_checkpoint >= 16 && write_outbox_cursor(outbox, row_end)) {
        outbox->ack_since_checkpoint = 0;
    }
    return compact_outbox_locked(outbox, false);
}

static bool read_outbox_row_locked(add_outbox_t *outbox, char *line, off_t *row_end)
{
    FILE *file = fopen(outbox->path, "r");
    if (!file) return false;
    bool ok = fseek(file, (long)outbox->offset, SEEK_SET) == 0 &&
              fgets(line, ADD_OUTBOX_LINE_BYTES, file) != NULL;
    long end = ok ? ftell(file) : -1;
    fclose(file);
    if (!ok || end <= (long)outbox->offset) return false;
    *row_end = (off_t)end;
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
         strcmp(value, "MANUAL_REPROCESS") == 0);
}

static bool attendance_payload_is_valid(const cJSON *payload)
{
    const cJSON *events = cJSON_GetObjectItemCaseSensitive(payload, "events");
    int count = cJSON_IsArray(events) ? cJSON_GetArraySize(events) : 0;
    if (count < 1 || count > 100) return false;
    const cJSON *event = NULL;
    cJSON_ArrayForEach(event, events) {
        const cJSON *event_uid = cJSON_GetObjectItemCaseSensitive(event, "event_uid");
        const cJSON *user_id = cJSON_GetObjectItemCaseSensitive(event, "user_id");
        const cJSON *device_time = cJSON_GetObjectItemCaseSensitive(event, "device_event_time");
        const cJSON *captured_at = cJSON_GetObjectItemCaseSensitive(event, "captured_at");
        const cJSON *source = cJSON_GetObjectItemCaseSensitive(event, "source");
        if (!cJSON_IsObject(event) || !cJSON_IsString(event_uid) ||
            !attendance_event_uid_is_valid(event_uid->valuestring) ||
            !cJSON_IsString(user_id) || !user_id->valuestring[0] ||
            strlen(user_id->valuestring) > 100 ||
            !cJSON_IsString(device_time) ||
            !attendance_timestamp_is_valid(device_time->valuestring) ||
            !cJSON_IsString(captured_at) ||
            !attendance_timestamp_is_valid(captured_at->valuestring) ||
            !cJSON_IsString(source) || !attendance_source_is_valid(source->valuestring)) {
            return false;
        }
    }
    return true;
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

static void preserve_corrupt_outbox_row(const char *line)
{
    FILE *file = fopen(ADD_CORRUPT_OUTBOX_PATH, "a");
    if (!file) return;
    (void)fprintf(file, "%s\n", line);
    (void)fflush(file);
    (void)fsync(fileno(file));
    fclose(file);
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
    cJSON_AddStringToObject(record, "type", type);
    cJSON_AddItemToObject(record, "payload", payload);
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

static bool add_connector_enqueue_validated_line(const char *line, bool live)
{
    if (!line) return false;
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
                FILE *file = fopen(outbox->path, "a");
                if (file) {
                    ok = fprintf(file, "%s\n", line) > 0 &&
                        fflush(file) == 0 &&
                        fsync(fileno(file)) == 0;
                    fclose(file);
                    if (ok) outbox->depth++;
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

    bool ok = add_connector_enqueue_validated_line(
        line,
        strcmp(confirmation_path, "FIRMWARE_LIVE") == 0);
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

        bool ok = true;
        FILE *file = fopen(s_bulk_outbox.path, "a");
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
            fclose(file);
        }
        s_bulk_outbox.depth += written;
        xSemaphoreGive(s_bulk_outbox.lock);
        if (written > 0) {
            ESP_LOGI(
                TAG,
                "Durably appended %lu ADD reconcile batch(es) with one flash sync",
                (unsigned long)written);
        }
        return ok && written == count;
    }
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
        add_outbox_t *outbox = NULL;
        off_t row_end = 0;
        uint32_t live_depth = 0;
        if (s_live_outbox.lock && xSemaphoreTake(s_live_outbox.lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            live_depth = s_live_outbox.depth;
            xSemaphoreGive(s_live_outbox.lock);
        }
        outbox = live_depth > 0 ? &s_live_outbox : &s_bulk_outbox;
        if (add_connector_is_connected() && outbox->lock &&
            xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
            have_row = outbox->depth > 0 && read_outbox_row_locked(outbox, line, &row_end);
            xSemaphoreGive(outbox->lock);
        }
        if (!have_row) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        line[strcspn(line, "\r\n")] = '\0';
        cJSON *record = cJSON_Parse(line);
        cJSON *type = record ? cJSON_GetObjectItemCaseSensitive(record, "type") : NULL;
        cJSON *payload = record ? cJSON_GetObjectItemCaseSensitive(record, "payload") : NULL;
        bool valid = cJSON_IsString(type) && cJSON_IsObject(payload) &&
            ((strcmp(type->valuestring, "attendance_batch") == 0 &&
              attendance_payload_is_valid(payload)) ||
             (strcmp(type->valuestring, "oracle_receipt_batch") == 0 &&
              oracle_receipt_payload_is_valid(payload)));
        char *payload_json = valid ? cJSON_PrintUnformatted(payload) : NULL;
        if (!valid || !payload_json) {
            cJSON_Delete(record);
            free(payload_json);
            ESP_LOGE(TAG, "Preserving and skipping a corrupt ADD %s outbox row", outbox->label);
            if (xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
                preserve_corrupt_outbox_row(line);
                (void)advance_outbox_locked(outbox, row_end);
                xSemaphoreGive(outbox->lock);
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
        if (acknowledged && xSemaphoreTake(outbox->lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
            if (!advance_outbox_locked(outbox, row_end)) {
                ESP_LOGE(TAG, "Could not advance acknowledged ADD %s attendance outbox", outbox->label);
            }
            xSemaphoreGive(outbox->lock);
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
    if (!zone_config_get()->add_enabled || s_started) {
        return;
    }
    s_lock = xSemaphoreCreateMutex();
    s_send_lock = xSemaphoreCreateMutex();
    s_live_outbox.lock = xSemaphoreCreateMutex();
    s_bulk_outbox.lock = xSemaphoreCreateMutex();
    s_ack_sem = xSemaphoreCreateBinary();
    s_command_lock = xSemaphoreCreateMutex();
    s_commands = xQueueCreate(ADD_COMMAND_QUEUE_DEPTH, sizeof(add_command_t));
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
                s_ack_sem && s_command_lock && s_commands && s_inbound_messages;
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
    if (runtime->zkt_expected_serial[0]) {
        cJSON_AddStringToObject(root, "expected_serial", runtime->zkt_expected_serial);
    }
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
        s_live_outbox.depth = count_outbox_rows(&s_live_outbox);
        xSemaphoreGive(s_live_outbox.lock);
    }
    if (xSemaphoreTake(s_bulk_outbox.lock, pdMS_TO_TICKS(2000)) == pdTRUE) {
        restore_outbox_if_needed(&s_bulk_outbox);
        s_bulk_outbox.offset = load_outbox_cursor(&s_bulk_outbox);
        s_bulk_outbox.depth = count_outbox_rows(&s_bulk_outbox);
        xSemaphoreGive(s_bulk_outbox.lock);
    }
    ESP_LOGI(
        TAG,
        "ADD outboxes restored live=%lu reconcile=%lu",
        (unsigned long)s_live_outbox.depth,
        (unsigned long)s_bulk_outbox.depth);
    xTaskCreate(heartbeat_task, "add_heartbeat", 8192, NULL, 4, NULL);
    xTaskCreate(outbox_task, "add_outbox", 8192, NULL, 4, NULL);
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
    bool ready = false;
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
        ready = s_connected
            && s_identity_catalog_generation > 0
            && s_zkt.online
            && stable_terminal_session
            && s_zkt.user_count >= 0
            && s_zkt.attendance_count >= 0;
        xSemaphoreGive(s_lock);
    }
    return ready;
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
    xSemaphoreGive(s_bulk_outbox.lock);
    return true;
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
    if (!s_commands || !out || xQueueReceive(s_commands, out, 0) != pdTRUE) return false;
    if (s_command_lock && xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(1000)) == pdTRUE) {
        command_unmark_queued_locked(out->command_id);
        strlcpy(s_running_command_id, out->command_id, sizeof(s_running_command_id));
        xSemaphoreGive(s_command_lock);
    }
    return true;
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
        xSemaphoreTake(s_command_lock, pdMS_TO_TICKS(3000)) != pdTRUE) {
        return false;
    }
    if (strcmp(s_running_command_id, command_id) == 0) s_running_command_id[0] = '\0';
    command_unmark_queued_locked(command_id);
    FILE *input = fopen(ADD_COMMAND_INBOX_PATH, "r");
    if (!input) {
        xSemaphoreGive(s_command_lock);
        return true;
    }
    FILE *output = fopen(ADD_COMMAND_INBOX_TMP_PATH, "w");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    bool ok = output && line;
    while (ok && fgets(line, ADD_COMMAND_LINE_BYTES, input)) {
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *id = root ? cJSON_GetObjectItemCaseSensitive(root, "command_id") : NULL;
        bool remove = cJSON_IsString(id) && strcmp(id->valuestring, command_id) == 0;
        cJSON_Delete(root);
        free(plain);
        if (!remove && fputs(line, output) == EOF) ok = false;
    }
    if (output && (fflush(output) != 0 || fsync(fileno(output)) != 0)) ok = false;
    fclose(input);
    if (output) fclose(output);
    free(line);
    if (ok) {
        (void)remove(ADD_COMMAND_INBOX_PATH);
        ok = rename(ADD_COMMAND_INBOX_TMP_PATH, ADD_COMMAND_INBOX_PATH) == 0;
    }
    if (!ok) (void)remove(ADD_COMMAND_INBOX_TMP_PATH);
    xSemaphoreGive(s_command_lock);
    return ok;
}

bool add_connector_lookup_identity(
    const char *user_id,
    char *display_name,
    size_t display_name_size,
    char *cnic,
    size_t cnic_size,
    bool *shift_worker)
{
    if (!user_id || !user_id[0]) return false;
    FILE *file = fopen(ADD_IDENTITY_CATALOG_PATH, "r");
    char *line = malloc(ADD_COMMAND_LINE_BYTES);
    bool found = false;
    if (file && line && fgets(line, ADD_COMMAND_LINE_BYTES, file)) {
        char *plain = decrypt_storage_line(line);
        cJSON *root = plain ? cJSON_Parse(plain) : NULL;
        cJSON *rows = root ? cJSON_GetObjectItemCaseSensitive(root, "rows") : NULL;
        bool legacy_catalog = cJSON_IsArray(rows);
        cJSON *row = NULL;
        cJSON_ArrayForEach(row, rows) {
            cJSON *candidate = cJSON_GetObjectItemCaseSensitive(row, "user_id");
            if (!cJSON_IsString(candidate) || strcmp(candidate->valuestring, user_id) != 0) continue;
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
                if (cJSON_IsString(candidate) &&
                    strcmp(candidate->valuestring, user_id) == 0) {
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
