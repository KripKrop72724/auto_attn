#include "ota_manager.h"
#include "firmware_family.h"
#include "ota_checkpoint.h"
#include "setup_portal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_random.h"
#include "esp_secure_boot.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#include "add_connector.h"
#include "zone_config.h"

#define OTA_NAMESPACE "zone_ota"
#define OTA_POLL_MS 60000
#define OTA_BOOT_CONFIRM_SECONDS 900
#define OTA_BOOT_HEALTH_REPORT_SECONDS 30
#define OTA_SAFEPOINT_REPORT_SECONDS 30
#define OTA_RESUME_CHECKPOINT_BYTES (64 * 1024)
#define OTA_HTTP_RESPONSE_BYTES 8192
#define OTA_HTTP_TRANSPORT_BUFFER_BYTES 4096

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
} ota_response_t;

static const char *TAG = "ota_manager";
static ota_journal_t s_journal;
static ota_journal_t s_committed_journal;
static uint32_t s_journal_generation;
static bool s_journal_ready;
static bool s_started;
static bool s_busy;
static char s_last_error[64];
// The running partition cannot change before reboot. Hash it before delivery
// workers consume the internal heap, then reuse the verified digest for every
// authenticated progress report and heartbeat on this boot.
static char s_running_image_digest[65];

static void wait_for_capture_safepoint(void);
static bool acknowledge_pending_success(void);

static void hex_bytes(const unsigned char *input, size_t length, char *output)
{
    static const char alphabet[] = "0123456789abcdef";
    for (size_t index = 0; index < length; ++index) {
        output[index * 2] = alphabet[input[index] >> 4];
        output[index * 2 + 1] = alphabet[input[index] & 0x0f];
    }
    output[length * 2] = '\0';
}

static void api_base(char *output, size_t size)
{
    strlcpy(output, zone_config_get()->add_onboard_url, size);
    char *suffix = strstr(output, "/device/v2/onboard");
    if (suffix) *suffix = '\0';
}

static esp_err_t response_event(esp_http_client_event_t *event)
{
    ota_response_t *response = event->user_data;
    if (event->event_id != HTTP_EVENT_ON_DATA || !response || event->data_len <= 0) return ESP_OK;
    size_t wanted = response->length + (size_t)event->data_len + 1;
    if (wanted > response->capacity) return ESP_ERR_NO_MEM;
    memcpy(response->data + response->length, event->data, (size_t)event->data_len);
    response->length += (size_t)event->data_len;
    response->data[response->length] = '\0';
    return ESP_OK;
}

static bool journal_failure(const char *error)
{
    s_journal = s_committed_journal;
    s_journal_ready = false;
    strlcpy(s_last_error, error, sizeof(s_last_error));
    return false;
}

static bool save_journal(void)
{
    if (!s_journal_ready || !ota_journal_valid(&s_journal) || s_journal_generation == UINT32_MAX)
        return journal_failure("OTA_JOURNAL_INVALID");
    ota_checkpoint_t checkpoint = {0};
    checkpoint.version = OTA_CHECKPOINT_VERSION;
    checkpoint.generation = s_journal_generation + 1;
    checkpoint.journal = s_journal;
    checkpoint.crc = dq_crc32(&checkpoint, offsetof(ota_checkpoint_t, crc));
    nvs_handle_t handle;
    if (nvs_open(OTA_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK)
        return journal_failure("OTA_JOURNAL_OPEN_FAILED");
    esp_err_t err = nvs_set_blob(handle, "journal_v1", &checkpoint, sizeof(checkpoint));
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err != ESP_OK) return journal_failure("OTA_JOURNAL_COMMIT_FAILED");
    s_committed_journal = s_journal;
    s_journal_generation = checkpoint.generation;
    return true;
}

static bool clear_journal(void)
{
    memset(&s_journal, 0, sizeof(s_journal));
    strlcpy(s_journal.state, "IDLE", sizeof(s_journal.state));
    return save_journal();
}

static bool load_journal(void)
{
    nvs_handle_t handle;
    ota_checkpoint_t checkpoint = {0};
    ota_journal_t restored = {0};
    uint32_t generation = 0;
    esp_err_t err = nvs_open(OTA_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) return journal_failure("OTA_JOURNAL_OPEN_FAILED");
    if (err == ESP_OK) {
        size_t size = sizeof(checkpoint);
        err = nvs_get_blob(handle, "journal_v1", &checkpoint, &size);
        if (err == ESP_OK) {
            if (size != sizeof(checkpoint) || !ota_checkpoint_valid(&checkpoint)) {
                nvs_close(handle); return journal_failure("OTA_JOURNAL_CORRUPT");
            }
            restored = checkpoint.journal;
            generation = checkpoint.generation;
        } else if (err == ESP_ERR_NVS_NOT_FOUND) {
            // Legacy v0 has the same ESP32 field layout. Only absence of v1
            // permits fallback; a corrupt v1 must never resurrect stale v0.
            size = sizeof(restored);
            err = nvs_get_blob(handle, "journal", &restored, &size);
            if (err == ESP_OK && (size != sizeof(restored) || !ota_journal_valid(&restored))) {
                nvs_close(handle); return journal_failure("OTA_LEGACY_JOURNAL_CORRUPT");
            }
        }
        nvs_close(handle);
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        memset(&restored, 0, sizeof(restored));
        strlcpy(restored.state, "IDLE", sizeof(restored.state));
    } else if (err != ESP_OK) return journal_failure("OTA_JOURNAL_READ_FAILED");
    s_journal = s_committed_journal = restored;
    s_journal_generation = generation;
    s_journal_ready = true;
    return true;
}

static bool signed_request(
    const char *method,
    const char *path,
    const char *body,
    ota_response_t *response,
    int *status)
{
    const zone_config_t *config = zone_config_get();
    if (!config->device_token[0] || !config->connector_id[0]) return false;
    time_t now = time(NULL);
    if (now < 1700000000) return false;
    struct tm utc;
    gmtime_r(&now, &utc);
    char timestamp[32];
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
    unsigned char nonce_raw[16];
    for (size_t index = 0; index < sizeof(nonce_raw); index += 4) {
        uint32_t value = esp_random();
        memcpy(nonce_raw + index, &value, 4);
    }
    char nonce[33];
    hex_bytes(nonce_raw, sizeof(nonce_raw), nonce);
    const char *payload = body ? body : "";
    unsigned char body_digest[32];
    mbedtls_sha256((const unsigned char *)payload, strlen(payload), body_digest, 0);
    char body_hash[65];
    hex_bytes(body_digest, sizeof(body_digest), body_hash);
    char material[768];
    snprintf(material, sizeof(material), "%s\n%s\n%s\n%s\n%s", method, path, timestamp, nonce, body_hash);
    unsigned char signature_raw[32];
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (mbedtls_md_hmac(
            md,
            (const unsigned char *)config->device_token,
            strlen(config->device_token),
            (const unsigned char *)material,
            strlen(material),
            signature_raw) != 0) {
        return false;
    }
    char signature[65];
    hex_bytes(signature_raw, sizeof(signature_raw), signature);
    char base[256];
    api_base(base, sizeof(base));
    char url[768];
    snprintf(url, sizeof(url), "%s%s", base, path);
    esp_http_client_config_t http = {
        .url = url,
        .method = strcmp(method, "POST") == 0 ? HTTP_METHOD_POST : HTTP_METHOD_GET,
        .event_handler = response_event,
        .user_data = response,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 15000,
        .buffer_size = OTA_HTTP_TRANSPORT_BUFFER_BYTES,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http);
    if (!client) return false;
    char authorization[160];
    snprintf(authorization, sizeof(authorization), "Bearer %s", config->device_token);
    esp_http_client_set_header(client, "Authorization", authorization);
    esp_http_client_set_header(client, "X-ADD-Connector-Id", config->connector_id);
    esp_http_client_set_header(client, "X-ADD-Timestamp", timestamp);
    esp_http_client_set_header(client, "X-ADD-Nonce", nonce);
    esp_http_client_set_header(client, "X-ADD-Body-SHA256", body_hash);
    esp_http_client_set_header(client, "X-ADD-Signature", signature);
    if (body) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, body, (int)strlen(body));
    }
    esp_err_t err = esp_http_client_perform(client);
    *status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    return err == ESP_OK;
}

static bool post_json(const char *path, cJSON *root)
{
    char *body = cJSON_PrintUnformatted(root);
    if (!body) return false;
    char *response_data = calloc(1, OTA_HTTP_RESPONSE_BYTES);
    if (!response_data) {
        free(body);
        return false;
    }
    ota_response_t response = {.data = response_data, .capacity = OTA_HTTP_RESPONSE_BYTES};
    int status = 0;
    bool ok = signed_request("POST", path, body, &response, &status) && status >= 200 && status < 300;
    free(response_data);
    free(body);
    return ok;
}

static bool cache_running_image_digest(void)
{
    if (s_running_image_digest[0]) return true;
    const esp_partition_t *running = esp_ota_get_running_partition();
    unsigned char digest[32];
    if (!running || esp_partition_get_sha256(running, digest) != ESP_OK) return false;
    hex_bytes(digest, sizeof(digest), s_running_image_digest);
    return true;
}

static bool add_running_image_evidence(cJSON *root)
{
    const esp_app_desc_t *description = esp_app_get_description();
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (!root || !description || !running || !cache_running_image_digest()) return false;
    return cJSON_AddStringToObject(root, "running_version", description->version) &&
        cJSON_AddStringToObject(root, "running_partition", running->label) &&
        cJSON_AddStringToObject(root, "image_sha256", s_running_image_digest);
}

static bool report_state(const char *state, const char *error)
{
    if (!s_journal.deployment_id[0]) return false;
    char path[160];
    snprintf(path, sizeof(path), "/device/v2/firmware/deployments/%s/progress", s_journal.deployment_id);
    cJSON *root = cJSON_CreateObject();
    bool valid = root && cJSON_AddStringToObject(root, "state", state) &&
        cJSON_AddNumberToObject(root, "bytes_written", (double)s_journal.bytes_written) &&
        add_running_image_evidence(root);
    if (valid && error && error[0]) valid = cJSON_AddStringToObject(root, "error_code", error) != NULL;
    bool ok = valid && post_json(path, root);
    cJSON_Delete(root);
    return ok;
}

static bool report_capability(void)
{
    cJSON *root = cJSON_CreateObject();
    bool valid = root && cJSON_AddBoolToObject(root, "capable", true) &&
        cJSON_AddBoolToObject(root, "secure_boot", esp_secure_boot_enabled()) &&
        cJSON_AddBoolToObject(root, "rollback_enabled", true) &&
        cJSON_AddStringToObject(root, "partition_layout", ZONE_LITE_OTA_PARTITION_LAYOUT) &&
        add_running_image_evidence(root);
    bool ok = valid && post_json("/device/v2/firmware/capability", root);
    cJSON_Delete(root);
    return ok;
}

static bool fetch_assignment(void)
{
    char *response_data = calloc(1, OTA_HTTP_RESPONSE_BYTES);
    if (!response_data) return false;
    ota_response_t response = {.data = response_data, .capacity = OTA_HTTP_RESPONSE_BYTES};
    int status = 0;
    if (!signed_request("GET", "/device/v2/firmware/assignment", NULL, &response, &status)) {
        free(response_data);
        return false;
    }
    if (status == 204) {
        free(response_data);
        return false;
    }
    if (status != 200) {
        free(response_data);
        return false;
    }
    cJSON *root = cJSON_Parse(response.data);
    cJSON *deployment = root ? cJSON_GetObjectItemCaseSensitive(root, "deployment_id") : NULL;
    cJSON *release = root ? cJSON_GetObjectItemCaseSensitive(root, "release_id") : NULL;
    cJSON *version = root ? cJSON_GetObjectItemCaseSensitive(root, "version") : NULL;
    cJSON *sha = root ? cJSON_GetObjectItemCaseSensitive(root, "image_sha256") : NULL;
    cJSON *url = root ? cJSON_GetObjectItemCaseSensitive(root, "download_url") : NULL;
    cJSON *size = root ? cJSON_GetObjectItemCaseSensitive(root, "image_size") : NULL;
    cJSON *family = root ? cJSON_GetObjectItemCaseSensitive(root, "firmware_family") : NULL;
    const char *offered_family = cJSON_IsString(family) ? family->valuestring : "zkt";
    bool valid = !strcmp(offered_family, ZONE_LITE_FIRMWARE_FAMILY) &&
                 (!family || cJSON_IsString(family)) &&
                 cJSON_IsString(deployment) && cJSON_IsString(release) && cJSON_IsString(version) &&
                 cJSON_IsString(sha) && strlen(sha->valuestring) == 64 && cJSON_IsString(url) &&
                 cJSON_IsNumber(size) && size->valuedouble > 0 &&
                 size->valuedouble <= OTA_APPLICATION_MAX_BYTES &&
                 (double)(uint32_t)size->valuedouble == size->valuedouble &&
                 strlen(deployment->valuestring) < sizeof(s_journal.deployment_id) &&
                 strlen(release->valuestring) < sizeof(s_journal.release_id) &&
                 strlen(version->valuestring) < sizeof(s_journal.target_version) &&
                 strlen(url->valuestring) < sizeof(s_journal.download_url);
    if (valid) {
        const esp_app_desc_t *running = esp_app_get_description();
        if (running && strcmp(running->version, version->valuestring) == 0) {
            // A same-version heartbeat is not proof of this deployment.
            // Existing durable RECONCILING journals are retried by the task.
            strlcpy(s_last_error, "OTA_ASSIGNMENT_ALREADY_RUNNING", sizeof(s_last_error));
            cJSON_Delete(root);
            free(response_data);
            return false;
        }
        bool same = strcmp(s_journal.state, "DOWNLOADING") == 0 &&
                    strcmp(s_journal.deployment_id, deployment->valuestring) == 0 &&
                    strcmp(s_journal.image_sha256, sha->valuestring) == 0;
        size_t resume = same ? s_journal.bytes_written : 0;
        memset(&s_journal, 0, sizeof(s_journal));
        strlcpy(s_journal.deployment_id, deployment->valuestring, sizeof(s_journal.deployment_id));
        strlcpy(s_journal.release_id, release->valuestring, sizeof(s_journal.release_id));
        strlcpy(s_journal.target_version, version->valuestring, sizeof(s_journal.target_version));
        strlcpy(s_journal.image_sha256, sha->valuestring, sizeof(s_journal.image_sha256));
        strlcpy(s_journal.download_url, url->valuestring, sizeof(s_journal.download_url));
        s_journal.image_size = (size_t)size->valuedouble;
        s_journal.bytes_written = resume;
        strlcpy(s_journal.state, "DOWNLOADING", sizeof(s_journal.state));
        valid = save_journal();
    }
    cJSON_Delete(root);
    free(response_data);
    return valid;
}

static bool perform_update(void)
{
    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (!target || target->size <= (128 * 1024) || s_journal.image_size > target->size - (128 * 1024)) {
        strlcpy(s_last_error, "IMAGE_TOO_LARGE", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    esp_http_client_config_t http = {
        .url = s_journal.download_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 20000,
        .keep_alive_enable = true,
    };
    esp_https_ota_config_t config = {
        .http_config = &http,
        .partial_http_download = true,
        .max_http_request_size = 16384,
        .ota_resumption = s_journal.bytes_written > 0,
        .ota_image_bytes_written = s_journal.bytes_written,
    };
    esp_https_ota_handle_t handle = NULL;
    if (esp_https_ota_begin(&config, &handle) != ESP_OK) {
        strlcpy(s_last_error, "DOWNLOAD_BEGIN_FAILED", sizeof(s_last_error));
        return false;
    }
    esp_app_desc_t descriptor;
    if (esp_https_ota_get_img_desc(handle, &descriptor) != ESP_OK ||
        strcmp(descriptor.project_name, ZONE_LITE_PROJECT_NAME) != 0 ||
        strcmp(descriptor.version, s_journal.target_version) != 0) {
        esp_https_ota_abort(handle);
        strlcpy(s_last_error, "IMAGE_DESCRIPTOR_MISMATCH", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    size_t checkpoint = s_journal.bytes_written;
    esp_err_t result;
    while ((result = esp_https_ota_perform(handle)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        int written = esp_https_ota_get_image_len_read(handle);
        if (written > 0) s_journal.bytes_written = (size_t)written;
        if (s_journal.bytes_written >= checkpoint + OTA_RESUME_CHECKPOINT_BYTES) {
            checkpoint = s_journal.bytes_written;
            if (!save_journal()) { esp_https_ota_abort(handle); return false; }
            (void)report_state("DOWNLOADING", NULL);
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (result != ESP_OK || !esp_https_ota_is_complete_data_received(handle)) {
        esp_https_ota_abort(handle);
        strlcpy(s_last_error, "DOWNLOAD_INCOMPLETE", sizeof(s_last_error));
        return false;
    }
    // Persist the boot-recovery journal before finish can select the OTA slot.
    // A reset before finish conservatively reports rollback from the old image;
    // a reset after finish finds the new image's READY_TO_BOOT checkpoint.
    s_journal.bytes_written = s_journal.image_size;
    strlcpy(s_journal.state, "READY_TO_BOOT", sizeof(s_journal.state));
    if (!save_journal()) { esp_https_ota_abort(handle); return false; }
    if (esp_https_ota_finish(handle) != ESP_OK) {
        strlcpy(s_last_error, "DOWNLOAD_OR_SIGNATURE_FAILED", sizeof(s_last_error));
        return false;
    }
    unsigned char digest[32];
    char digest_hex[65];
    if (esp_partition_get_sha256(target, digest) != ESP_OK) {
        strlcpy(s_last_error, "PARTITION_HASH_FAILED", sizeof(s_last_error));
        const esp_partition_t *running = esp_ota_get_running_partition();
        if (!running || esp_ota_set_boot_partition(running) != ESP_OK)
            strlcpy(s_last_error, "BOOT_SELECTION_RECOVERY_FAILED", sizeof(s_last_error));
        return false;
    }
    hex_bytes(digest, sizeof(digest), digest_hex);
    if (strcmp(digest_hex, s_journal.image_sha256) != 0) {
        strlcpy(s_last_error, "IMAGE_HASH_MISMATCH", sizeof(s_last_error));
        const esp_partition_t *running = esp_ota_get_running_partition();
        if (!running || esp_ota_set_boot_partition(running) != ESP_OK)
            strlcpy(s_last_error, "BOOT_SELECTION_RECOVERY_FAILED", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    (void)report_state("READY_TO_BOOT", NULL);
    wait_for_capture_safepoint();
    esp_restart();
    return true;
}

static void wait_for_capture_safepoint(void)
{
    int64_t last_report = 0;
    while (!add_connector_claim_ota_restart()) {
        int64_t now = esp_timer_get_time() / 1000000;
        if (last_report == 0 || now - last_report >= OTA_SAFEPOINT_REPORT_SECONDS) {
#ifdef ZONE_LITE_HIKVISION
            (void)report_state("READY_TO_BOOT", "WAITING_FOR_HIK_SAFEPOINT");
#else
            (void)report_state("READY_TO_BOOT", "WAITING_FOR_ZKT_SAFEPOINT");
#endif
            last_report = now;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static bool confirm_or_report_rollback(void)
{
    const esp_app_desc_t *running = esp_app_get_description();
    if (!s_journal.deployment_id[0] || strcmp(s_journal.state, "READY_TO_BOOT") != 0) return true;
    if (strcmp(running->version, s_journal.target_version) != 0) {
        if (!report_state("ROLLED_BACK", "BOOTLOADER_ROLLBACK")) return false;
        return clear_journal();
    }
    int64_t deadline = (esp_timer_get_time() / 1000000) + OTA_BOOT_CONFIRM_SECONDS;
    int64_t last_health_report = 0;
    bool add_acknowledged = false;
    while ((esp_timer_get_time() / 1000000) < deadline) {
        int64_t now = esp_timer_get_time() / 1000000;
        if (!add_acknowledged) {
            add_acknowledged = report_state(
                "BOOTED_PENDING",
                "WAITING_FOR_RUNTIME_HEALTH");
            last_health_report = now;
        }
        if (add_acknowledged && add_connector_boot_health_ready()) {
            if (!report_state("BOOTED_PENDING", "RUNTIME_HEALTHY")) {
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
            if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
                strlcpy(s_journal.state, "RECONCILING", sizeof(s_journal.state));
                if (!save_journal()) return false;
                (void)report_state("RECONCILING", NULL);
                // Boot confirmation and source reconciliation are separate.
                // The durable journal remains until certified source coverage
                // and the checked server progress transition both succeed.
                (void)acknowledge_pending_success();
                return true;
            }
            break;
        }
        if (add_acknowledged &&
            now - last_health_report >= OTA_BOOT_HEALTH_REPORT_SECONDS) {
            (void)report_state("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH");
            last_health_report = now;
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    (void)report_state("FAILED", "BOOT_HEALTH_TIMEOUT");
    (void)esp_ota_mark_app_invalid_rollback_and_reboot();
    return false;
}

static bool acknowledge_pending_success(void)
{
    if (!s_journal.deployment_id[0] || strcmp(s_journal.state, "RECONCILING") != 0) {
        return true;
    }
    const esp_app_desc_t *running = esp_app_get_description();
    if (!running || strcmp(running->version, s_journal.target_version) != 0) {
        if (!report_state("ROLLED_BACK", "RUNNING_VERSION_MISMATCH")) return false;
        return clear_journal();
    }
    if (!add_connector_ota_reconcile_ready()) return false;
    // Retry the preceding transition if its acknowledgement was lost.
    if (!report_state("RECONCILING", NULL)) return false;
    if (!report_state("SUCCEEDED", NULL)) {
        ESP_LOGW(TAG, "ADD did not acknowledge OTA success; retaining journal for retry");
        return false;
    }
    if (!clear_journal()) return false;
    ESP_LOGI(TAG, "ADD acknowledged durable OTA success");
    return true;
}

static void ota_task(void *argument)
{
    (void)argument;
    bool boot_checked = false;
    bool capability_reported = false;
    while (true) {
        if (!s_journal_ready && !load_journal()) {
            vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
            continue;
        }
        if (!boot_checked) {
            boot_checked = confirm_or_report_rollback();
            if (!boot_checked) { vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS)); continue; }
        }
        if (add_connector_is_connected()) {
            if (!capability_reported) {
                capability_reported = report_capability();
                if (capability_reported) {
                    ESP_LOGI(TAG, "ADD accepted OTA capability; connector is OTA ready");
                } else {
                    ESP_LOGW(TAG, "ADD OTA capability report failed; retrying in %u ms", OTA_POLL_MS);
                }
            }
            if (capability_reported && !s_busy) {
                if (!acknowledge_pending_success()) {
                    vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
                    continue;
                }
                /* A local network change and an OTA write never run concurrently. */
                if (!setup_portal_active() && fetch_assignment()) {
                    s_busy = true;
                    (void)perform_update();
                    s_busy = false;
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
    }
}

void ota_manager_init(void)
{
    if (!cache_running_image_digest())
        ESP_LOGW(TAG, "Running image digest unavailable at boot; OTA evidence will retry");
    load_journal();
}

void ota_manager_start(void)
{
    if (s_started) return;
    s_started = xTaskCreate(ota_task, "ota_manager", 12288, NULL, 2, NULL) == pdPASS;
}

bool ota_manager_busy(void)
{
    return s_busy;
}

void ota_manager_append_telemetry(cJSON *heartbeat)
{
    if (!heartbeat) return;
    cJSON *ota = cJSON_CreateObject();
    bool valid = ota && add_running_image_evidence(ota) &&
        cJSON_AddBoolToObject(ota, "capable", true) &&
        cJSON_AddBoolToObject(ota, "secure_boot", esp_secure_boot_enabled()) &&
        cJSON_AddBoolToObject(ota, "rollback_enabled", true) &&
        cJSON_AddStringToObject(ota, "partition_layout", ZONE_LITE_OTA_PARTITION_LAYOUT) &&
        cJSON_AddStringToObject(ota, "state", s_busy ? "UPDATING" : s_journal.state) &&
        cJSON_AddStringToObject(ota, "target_version", s_journal.target_version) &&
        cJSON_AddNumberToObject(ota, "bytes_written", (double)s_journal.bytes_written) &&
        cJSON_AddNumberToObject(ota, "image_size", (double)s_journal.image_size);
    if (valid && s_last_error[0]) valid = cJSON_AddStringToObject(ota, "last_error", s_last_error) != NULL;
    if (valid && cJSON_AddItemToObject(heartbeat, "ota", ota)) return;
    cJSON_Delete(ota);
}
