#include "ota_manager.h"
#include "firmware_family.h"
#include "ota_checkpoint.h"
#include "ota_progress_receipt.h"
#include "setup_portal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_image_format.h"
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
#include "boot_confirm.h"
#include "storage_recovery.h"
#include "zone_config.h"
#if defined(ZONE_LITE_STORAGE_REPAIR) && ZONE_LITE_STORAGE_REPAIR
/* ZKT 2.7.7: the confirm-first storage exception (storage_exception.h). */
#include "esp_heap_caps.h"
#include "esp_spiffs.h"
#include "led_status.h"
#include "queue_store.h"
#include "storage_exception.h"
#include "storage_repair.h"
#include "upgrade_guard.h"
#define OTA_STORAGE_EXCEPTION 1
#else
#define OTA_STORAGE_EXCEPTION 0
#endif
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
#include "zkt_ota_guard.h"
#include "zkt_storage_owner.h"
#include "zkt_rollback.h"
#include "zkt_journal_runtime.h"
#include "zkt_factory_platform.h"
#endif

#define OTA_NAMESPACE "zone_ota"

#include <stdatomic.h>
static atomic_flag s_control_owner = ATOMIC_FLAG_INIT;
static atomic_bool s_hil_reboot_reserved;
#define OTA_POLL_MS 60000
#define OTA_BOOT_CONFIRM_SECONDS 900
#define OTA_BOOT_HEALTH_REPORT_SECONDS 30
#define OTA_SAFEPOINT_REPORT_SECONDS 30
/* The boot partition is already switched while the restart waits for a
 * capture safepoint; an offline or busy terminal may hold it only this long. */
#define OTA_SAFEPOINT_MAX_SECONDS (30 * 60)
#define OTA_RESUME_CHECKPOINT_BYTES (64 * 1024)
#define OTA_HTTP_RESPONSE_BYTES 8192
#define OTA_HTTP_TRANSPORT_BUFFER_BYTES 4096
/* The request line and headers of a signed ADD request (bearer token, five
 * X-ADD headers, 64-hex signature) exceed esp_http_client's default 512-byte
 * transmit buffer, which logged "HTTP_HEADER: Buffer length is small to fit
 * all the headers" on every 60 s poll. */
#define OTA_HTTP_REQUEST_HEADER_BYTES 1024
#define OTA_TASK_PRIORITY 5

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
static bool s_failed_boot_pending;
static char s_last_error[64];
static volatile uint32_t s_boot_health_checks;
static volatile bool s_boot_health_last_ready;
/* The running new-image confirmation, for heartbeats; zero when none ran. */
static boot_confirm_t s_boot_confirm;
static volatile uint32_t s_progress_attempts;
static volatile uint32_t s_progress_successes;
static volatile int s_progress_last_http_status;
// The running partition cannot change before reboot. Hash it before delivery
// workers consume the internal heap, then reuse the verified digest for every
// authenticated progress report and heartbeat on this boot.
static char s_running_image_digest[65];
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
static uint64_t s_reader_ticket;
static uint64_t s_failed_boot_ticket;
#endif

static void wait_for_capture_safepoint(void);
static bool acknowledge_pending_success(void);
/* The capability ADD accepted on this boot (also a term of the 2.7.7 gate). */
static bool s_capability_reported;
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
static bool report_factory_trial(void);
#endif

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
        .buffer_size_tx = OTA_HTTP_REQUEST_HEADER_BYTES,
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

static bool uses_local_boot_confirmation(void);

static bool post_json(const char *path, cJSON *root, int *http_status, const char *progress_state)
{
    if (http_status) *http_status = 0;
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
    if (ok && progress_state && uses_local_boot_confirmation()) {
        cJSON *receipt = cJSON_Parse(response.data);
        ok = ota_progress_receipt_matches(receipt, s_journal.deployment_id,
            progress_state, s_journal.target_version, s_running_image_digest);
        cJSON_Delete(receipt);
    }
    if (http_status) *http_status = status;
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

#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
static bool factory_reported;
static const char *json_string(cJSON *root,const char *name)
{
    cJSON *item=cJSON_GetObjectItemCaseSensitive(root,name);
    return cJSON_IsString(item) && item->valuestring ? item->valuestring:NULL;
}
static bool same_json(cJSON *left,cJSON *right,const char *name)
{
    const char *a=json_string(left,name),*b=json_string(right,name);
    return a && b && !strcmp(a,b);
}
static bool report_factory_trial(void)
{
    if (factory_reported) return true;
    zf_proof_t proof;
    if (!zf_platform_proof(&proof) || proof.state!=ZF_REVOKED ||
        strcmp(s_journal.target_version,"2.6.22") || strcmp(proof.deployment_id,s_journal.deployment_id)) return false;
    char path[160];snprintf(path,sizeof(path),"/device/v2/firmware/deployments/%s/factory-trial",proof.deployment_id);
    char *data=calloc(1,OTA_HTTP_RESPONSE_BYTES);
    if(!data)return false;
    ota_response_t response={.data=data,.capacity=OTA_HTTP_RESPONSE_BYTES};int status=0;
    bool received=signed_request("GET",path,NULL,&response,&status) && status==200;
    cJSON *context=received ? cJSON_Parse(data):NULL,*body=NULL,*receipt=NULL;
    const char *trial=json_string(context,"trial_id"),*challenge=json_string(context,"challenge"),
        *deployment=json_string(context,"deployment_id");
    cJSON *generation=cJSON_GetObjectItemCaseSensitive(context,"onboarding_generation"),
        *expiry=cJSON_GetObjectItemCaseSensitive(context,"expires_epoch");
    const zf_target_t *target=zf_target(proof.target);
    time_t now=time(NULL);
    bool valid=trial && strlen(trial)==36 && strspn(trial,"0123456789abcdef-")==36 &&
        challenge && strlen(challenge)==64 && strspn(challenge,"0123456789abcdef")==64 &&
        deployment && !strcmp(deployment,proof.deployment_id) && target &&
        cJSON_IsNumber(generation) && generation->valuedouble==target->onboarding_generation &&
        cJSON_IsNumber(expiry) && now>=1700000000 && expiry->valuedouble>(double)now &&
        expiry->valuedouble<=(double)now+3600;
    if(valid){
        body=cJSON_CreateObject();
        valid=body && zf_platform_add_proof(body) &&
            cJSON_AddStringToObject(body,"trial_id",trial) && cJSON_AddStringToObject(body,"challenge",challenge) &&
            cJSON_AddStringToObject(body,"boot_id",add_connector_boot_id()) &&
            cJSON_AddNumberToObject(body,"onboarding_generation",target->onboarding_generation);
    }
    char *encoded=valid ? cJSON_PrintUnformatted(body):NULL;
    if(encoded){
        response.length=0;data[0]=0;
        received=signed_request("POST",path,encoded,&response,&status) && status==200;
        receipt=received ? cJSON_Parse(data):NULL;
        factory_reported=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(receipt,"accepted")) &&
            same_json(receipt,body,"trial_id") && same_json(receipt,body,"challenge") &&
            same_json(receipt,body,"deployment_id") && same_json(receipt,body,"boot_id") &&
            same_json(receipt,body,"checkpoint_sha256") && same_json(receipt,body,"proof_state");
    }
    free(encoded);cJSON_Delete(receipt);cJSON_Delete(body);cJSON_Delete(context);free(data);
    return factory_reported;
}
#endif

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
    s_progress_attempts++;
    if (!s_journal.deployment_id[0]) return false;
    char path[160];
    snprintf(path, sizeof(path), "/device/v2/firmware/deployments/%s/progress", s_journal.deployment_id);
    cJSON *root = cJSON_CreateObject();
    bool valid = root && cJSON_AddStringToObject(root, "state", state) &&
        cJSON_AddNumberToObject(root, "bytes_written", (double)s_journal.bytes_written) &&
        add_running_image_evidence(root);
    if (valid && error && error[0]) valid = cJSON_AddStringToObject(root, "error_code", error) != NULL;
    int http_status = 0;
    bool ok = valid && post_json(path, root, &http_status, state);
    s_progress_last_http_status = http_status;
    if (ok) s_progress_successes++;
    cJSON_Delete(root);
    return ok;
}

/* ZKT 2.7.7: a progress report with an operator-visible message whose
 * receipt the caller may check (the exception's BOOTED_PENDING, RECONCILING
 * and terminal FAILED reports, and a written image that did not verify). */
static bool report_progress(const char *state, const char *error, const char *message, cJSON **receipt_out)
{
    if (receipt_out) *receipt_out = NULL;
    s_progress_attempts++;
    if (!s_journal.deployment_id[0]) return false;
    char path[160];
    snprintf(path, sizeof(path), "/device/v2/firmware/deployments/%s/progress", s_journal.deployment_id);
    cJSON *root = cJSON_CreateObject();
    bool valid = root && cJSON_AddStringToObject(root, "state", state) &&
        cJSON_AddNumberToObject(root, "bytes_written", (double)s_journal.bytes_written) &&
        add_running_image_evidence(root) && (!error || !error[0] || cJSON_AddStringToObject(root, "error_code", error)) &&
        (!message || !message[0] || cJSON_AddStringToObject(root, "error_message", message));
    char *body = valid ? cJSON_PrintUnformatted(root) : NULL;
    cJSON_Delete(root);
    char *data = body ? calloc(1, OTA_HTTP_RESPONSE_BYTES) : NULL;
    ota_response_t response = {.data = data, .capacity = OTA_HTTP_RESPONSE_BYTES};
    int status = 0;
    bool ok = data && signed_request("POST", path, body, &response, &status) && status >= 200 && status < 300;
    s_progress_last_http_status = status;
    if (ok) {
        s_progress_successes++;
        if (receipt_out) *receipt_out = cJSON_Parse(data);
    }
    free(data);
    free(body);
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
    bool ok = valid && post_json("/device/v2/firmware/capability", root, NULL, NULL);
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

/* ZKT 2.7.7: the written OTA slot did not verify. On 26 October 2026
 * ZONE-SWAT-01 and ZONE-QUETTA-01 (2.7.3) downloaded all 1,511,424 bytes of
 * 2.7.6 and esp_partition_get_sha256() failed: the cause (RAM corruption
 * before the write, or flash programming) was not provable, because that
 * call folds every failure into ESP_ERR_IMAGE_INVALID and the device kept
 * resuming the same deployment over the same bytes. Its steps are re-run
 * here to name the one that fails: the image header and segments
 * (esp_image_get_metadata), a plain read of the hashed range, and the
 * appended digest against that read and against the release's. Then the
 * written range is erased and the checkpoint goes back to byte 0, so a retry
 * downloads the whole image again; the finding goes to ADD as a log and with
 * the FAILED report. Never the running slot (target is the update slot) and
 * never before esp_https_ota_abort released it. */
typedef struct {
    esp_image_metadata_t meta;
    mbedtls_sha256_context sha;
    unsigned char block[4096];
    unsigned char digest[32];
    char computed[65], appended[65];
    char finding[240];
} ota_verify_scratch_t;

/* Out of line, its texts on the heap: perform_update's frame stays as it was
 * under the FAILED report's TLS handshake (ota_manager's 12 KiB stack). */
static __attribute__((noinline)) void written_image_unverified(const esp_partition_t *target, esp_err_t hash_error)
{
    strlcpy(s_last_error, "PARTITION_HASH_FAILED", sizeof(s_last_error));
    ota_verify_scratch_t *m = calloc(1, sizeof(*m));
    const char *step = "SCRATCH", *readback = "UNREAD", *appended = "UNREAD";
    esp_err_t step_error = ESP_ERR_NO_MEM;
    if (m) {
        const esp_partition_pos_t position = {.offset = target->address, .size = target->size};
        step_error = esp_image_get_metadata(&position, &m->meta);
        if (step_error != ESP_OK) step = "HEADER";
        else if (!m->meta.image.hash_appended || m->meta.image_len <= sizeof(m->digest) ||
                 m->meta.image_len > target->size) {
            step = "HEADER";
            step_error = ESP_ERR_IMAGE_INVALID;
        } else {
            /* A plain read, no mmap: the bytes the slot holds. */
            uint32_t hashed = m->meta.image_len - sizeof(m->digest);
            mbedtls_sha256_init(&m->sha);
            int sha = mbedtls_sha256_starts(&m->sha, 0);
            for (uint32_t at = 0; !sha && step_error == ESP_OK && at < hashed; at += sizeof(m->block)) {
                uint32_t chunk = hashed - at < sizeof(m->block) ? hashed - at : sizeof(m->block);
                step_error = esp_partition_read(target, at, m->block, chunk);
                if (step_error == ESP_OK) sha = mbedtls_sha256_update(&m->sha, m->block, chunk);
                if (!(at % (64U * 1024U))) vTaskDelay(1);
            }
            if (!sha && step_error == ESP_OK) sha = mbedtls_sha256_finish(&m->sha, m->digest);
            mbedtls_sha256_free(&m->sha);
            if (step_error != ESP_OK) step = "READ";
            else if (sha) { step = "READ"; step_error = ESP_FAIL; }
            else {
                hex_bytes(m->digest, sizeof(m->digest), m->computed);
                hex_bytes(m->meta.image_digest, sizeof(m->meta.image_digest), m->appended);
                /* Flash content that does not match its own appended digest:
                 * corrupt bytes reached the slot. Content that matches: the
                 * IDF check failed elsewhere (mapping) and did not reproduce. */
                step = strcmp(m->computed, m->appended) ? "DIGEST" : "UNREPRODUCED";
                readback = strcmp(m->computed, s_journal.image_sha256) ? "MISMATCH" : "MATCH";
                appended = strcmp(m->appended, s_journal.image_sha256) ? "MISMATCH" : "MATCH";
            }
        }
    }
    uint32_t written = s_journal.image_size + target->erase_size - 1;
    written -= written % target->erase_size;
    if (written > target->size) written = target->size;
    esp_err_t erased = esp_partition_erase_range(target, 0, written);
    s_journal.bytes_written = 0;
    bool restarted = save_journal();
    const char *finding = "The downloaded image did not verify on the device (no memory to name the step).";
    if (m) {
        snprintf(m->finding, sizeof(m->finding),
                 "The downloaded image did not verify on the device: step %s (%s), hash %s; read-back %s and "
                 "appended digest %s the release; slot erase %s; next download from byte %s.",
                 step, esp_err_to_name(step_error), esp_err_to_name(hash_error), readback, appended,
                 erased == ESP_OK ? "done" : esp_err_to_name(erased), restarted ? "0" : "unknown");
        finding = m->finding;
    }
    ESP_LOGE(TAG, "%s (slot erase %s)", finding, esp_err_to_name(erased));
    (void)add_connector_log("ERROR", "ota", "OTA_IMAGE_UNVERIFIED", finding);
    cJSON *receipt = NULL;
    (void)report_progress("FAILED", "PARTITION_HASH_FAILED", finding, &receipt);
    cJSON_Delete(receipt);
    free(m);
    /* save_journal failing restores the committed journal: keep the error. */
    strlcpy(s_last_error, "PARTITION_HASH_FAILED", sizeof(s_last_error));
}

/* The existing attested bridge is selected locally. A network download must
 * never overwrite that rollback slot. This task is the only coordinator;
 * each turn is bounded and leaves accepted owner work intact on timeout. */
static bool advance_failed_boot_rollback(void)
{
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    return true;
#else
    const esp_app_desc_t *app = esp_app_get_description();
    bool zkt = app && !strcmp(app->project_name, "zone_lite");
    bool bridge = zkt && !strcmp(app->version, ZJ_BRIDGE_VERSION);
    bool intent = !strcmp(s_journal.state, "FAILED_BOOT_INTENT");
    bool observed_return = bridge && s_journal.deployment_id[0] &&
        !strcmp(s_journal.target_version, ZJ_WRITER_VERSION) &&
        (!strcmp(s_journal.state, "READY_TO_BOOT") || !strcmp(s_journal.state, "LOCAL_VALIDATED") ||
         !strcmp(s_journal.state, "BOOT_REPORTED") || !strcmp(s_journal.state, "RECONCILING"));
    if (!intent && !observed_return && !s_failed_boot_pending && !s_failed_boot_ticket) return true;
    s_busy = true;
    if ((intent || observed_return) && bridge) {
        /* IDF returns to the previously VALID bridge. Local journal recovery
         * must finish before reporting rollback; terminal/remote HIL health
         * is not inferred and the original deployment identity is retained. */
        if (s_failed_boot_ticket || !zj_runtime_boot_ready()) {
            strlcpy(s_last_error, "BOOT_ROLLBACK_READER_RECOVERY", sizeof(s_last_error));
            return false;
        }
        /* A return before the failure intent commits has no proved reset
         * cause. Preserve that distinction in the original deployment. */
        if (!report_state("ROLLED_BACK", intent ? "BOOT_HEALTH_TIMEOUT" : "PREVIOUS_FIRMWARE_OBSERVED") ||
            !clear_journal()) return false;
        s_failed_boot_pending = s_busy = false;
        return true;
    }
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
    if (zkt && !strcmp(app->version,"2.6.22") && s_failed_boot_pending) {
        if (!zf_platform_pending_fallback()) {
            strlcpy(s_last_error,zf_platform_error(),sizeof(s_last_error));
            return false;
        }
        if (!add_connector_claim_failed_boot_restart()) {
            strlcpy(s_last_error,"FACTORY_TRIAL_SESSION_CLEANUP",sizeof(s_last_error));
            return false;
        }
        if (!zj_owner_quiesce_factory()) {
            strlcpy(s_last_error,"FACTORY_TRIAL_STORAGE_DRAIN",sizeof(s_last_error));
            return false;
        }
        /* Final local control only after every accepted storage/session
         * operation finishes. A failed/uncertain selection stays quiescent. */
        if (zf_platform_select_factory()) esp_restart();
        strlcpy(s_last_error,zf_platform_error(),sizeof(s_last_error));
        return false;
    }
#endif
    if (!zkt || strcmp(app->version, ZJ_WRITER_VERSION)) {
        /* No reader certificate exists for bridge -> legacy/factory or an
         * arbitrary image. Keep that failed boot explicit, never invalidate
         * it based only on version ordering. */
        strlcpy(s_last_error, "BOOT_ROLLBACK_PREDECESSOR_UNQUALIFIED", sizeof(s_last_error));
        return false;
    }
    ota_checkpoint_t expected = {.version = OTA_CHECKPOINT_VERSION,
        .generation = s_journal_generation, .journal = s_committed_journal};
    expected.crc = dq_crc32(&expected, offsetof(ota_checkpoint_t, crc));
    if (!zj_rollback_failed_boot(&expected) || !cache_running_image_digest() ||
        strcmp(s_running_image_digest, expected.journal.image_sha256)) {
        strlcpy(s_last_error, "BOOT_ROLLBACK_WRITER_IDENTITY", sizeof(s_last_error));
        return false;
    }
    if (s_failed_boot_ticket) {
        zj_reply_t reply;
        bool complete = false;
        if (!zj_owner_poll(s_failed_boot_ticket, &reply, &complete) || !complete) {
            strlcpy(s_last_error, "BOOT_ROLLBACK_SELECTION_PENDING", sizeof(s_last_error));
            return false;
        }
        s_failed_boot_ticket = 0;
        bool committed = zj_rollback_same_target(&expected, &reply.rollback_intent) &&
            !strcmp(reply.rollback_intent.journal.state, "FAILED_BOOT_INTENT") &&
            reply.rollback_intent.generation >= expected.generation;
        if (committed) {
            s_committed_journal = s_journal = reply.rollback_intent.journal;
            s_journal_generation = reply.rollback_intent.generation;
        }
        if (committed && reply.result == ZJ_OK && reply.compatibility == ZJ_COMPAT_OK) {
            esp_restart();
            return false;
        }
        strlcpy(s_last_error, "BOOT_ROLLBACK_SELECTION_HELD", sizeof(s_last_error));
        return false;
    }
    if (!add_connector_claim_failed_boot_restart()) {
        strlcpy(s_last_error, "BOOT_ROLLBACK_SESSION_CLEANUP", sizeof(s_last_error));
        return false;
    }
    if (!zj_owner_quiesce()) {
        strlcpy(s_last_error, "BOOT_ROLLBACK_STORAGE_DRAIN", sizeof(s_last_error));
        return false;
    }
    if (!zj_owner_select_quiesced_reader(&expected, &s_failed_boot_ticket) || !s_failed_boot_ticket) {
        strlcpy(s_last_error, "BOOT_ROLLBACK_SELECTION_UNAVAILABLE", sizeof(s_last_error));
        return false;
    }
    strlcpy(s_last_error, "BOOT_ROLLBACK_SELECTION_PENDING", sizeof(s_last_error));
    return false;
#endif
}

static bool advance_reader_rollback(void)
{
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    return true;
#else
    const esp_app_desc_t *app = esp_app_get_description();
    bool intent = !strcmp(s_journal.state, "READER_INTENT");
    bool writer = app && !strcmp(app->project_name, "zone_lite") && !strcmp(app->version, ZJ_WRITER_VERSION);
    bool offered = writer && zj_reader_policy_target(s_journal.target_version, s_journal.image_sha256) &&
        !strcmp(s_journal.state, "DOWNLOADING");
    if (!intent && !offered && !s_reader_ticket) return true;
    s_busy = true;
    if (intent && app && !strcmp(app->project_name, "zone_lite") && !strcmp(app->version, ZJ_BRIDGE_VERSION)) {
        /* A version alone cannot complete the intent. Re-establish exact
         * running-image evidence before entering the normal local boot proof. */
        if (s_reader_ticket || !cache_running_image_digest() ||
            strcmp(s_running_image_digest, s_journal.image_sha256)) {
            strlcpy(s_last_error, "JOURNAL_READER_BOOT_IMAGE_MISMATCH", sizeof(s_last_error));
            return false;
        }
        s_journal.bytes_written = s_journal.image_size;
        strlcpy(s_journal.state, "READY_TO_BOOT", sizeof(s_journal.state));
        if (!save_journal()) return false;
        s_busy = false;
        return true;
    }
    if (!writer || (!intent && !offered)) {
        strlcpy(s_last_error, "JOURNAL_READER_INTENT_IMAGE_MISMATCH", sizeof(s_last_error));
        return false;
    }
    ota_checkpoint_t expected = {.version = OTA_CHECKPOINT_VERSION,
        .generation = s_journal_generation, .journal = s_committed_journal};
    expected.crc = dq_crc32(&expected, offsetof(ota_checkpoint_t, crc));
    if (!zj_rollback_request_valid(&expected)) {
        strlcpy(s_last_error, "JOURNAL_READER_INTENT_INVALID", sizeof(s_last_error));
        return false;
    }
    if (s_reader_ticket) {
        zj_reply_t reply;
        bool complete = false;
        if (!zj_owner_poll(s_reader_ticket, &reply, &complete) || !complete) {
            strlcpy(s_last_error, "JOURNAL_READER_SELECTION_PENDING", sizeof(s_last_error));
            return false;
        }
        s_reader_ticket = 0;
        bool committed = zj_rollback_same_target(&expected, &reply.rollback_intent) &&
            !strcmp(reply.rollback_intent.journal.state, "READER_INTENT") &&
            reply.rollback_intent.generation >= expected.generation;
        if (committed) {
            s_committed_journal = s_journal = reply.rollback_intent.journal;
            s_journal_generation = reply.rollback_intent.generation;
        }
        if (reply.result == ZJ_OK && reply.compatibility == ZJ_COMPAT_OK && committed) {
            /* No network or additional persistence is required between the
             * confirmed selection and restart. Post-boot identity/health and
             * ADD's committed progress remain separate checks. */
            esp_restart();
            return false;
        }
        const char *error = reply.result == ZJ_OK ? "JOURNAL_READER_INTENT_REPLY_INVALID" :
            reply.result == ZJ_UNCERTAIN ? "JOURNAL_READER_INTENT_UNCERTAIN" :
            reply.compatibility != ZJ_COMPAT_NOT_READY ? zj_compat_error(reply.compatibility) :
            "JOURNAL_READER_SELECTION_HELD";
        strlcpy(s_last_error, error, sizeof(s_last_error));
        return false;
    }
    if (!add_connector_claim_ota_restart()) {
        strlcpy(s_last_error, "WAITING_FOR_ZKT_SAFEPOINT", sizeof(s_last_error));
        return false;
    }
    if (!zj_owner_quiesce()) {
        strlcpy(s_last_error, "WAITING_FOR_JOURNAL_QUIESCE", sizeof(s_last_error));
        return false;
    }
    if (!zj_owner_select_quiesced_reader(&expected, &s_reader_ticket) || !s_reader_ticket) {
        strlcpy(s_last_error, "JOURNAL_READER_SELECTION_UNAVAILABLE", sizeof(s_last_error));
        return false;
    }
    strlcpy(s_last_error, "JOURNAL_READER_SELECTION_PENDING", sizeof(s_last_error));
    return false;
#endif
}

static bool perform_update(void)
{
    // Secure-boot artifacts are padded; aligned complete writes ensure IDF
    // has no encrypted flash tail waiting for esp_ota_end to flush.
    if (!s_journal.image_size || s_journal.image_size % 16U) {
        strlcpy(s_last_error, "IMAGE_SIZE_ALIGNMENT", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (!target || target->size <= (128 * 1024) || s_journal.image_size > target->size - (128 * 1024)) {
        strlcpy(s_last_error, "IMAGE_TOO_LARGE", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    if (target->erase_size < 16U || target->erase_size % 16U) {
        strlcpy(s_last_error, "PARTITION_ERASE_ALIGNMENT", sizeof(s_last_error));
        return false;
    }
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
    const char *journal_error = zj_ota_before_download(target->address, target->size, s_journal.target_version);
    if (journal_error) {
        strlcpy(s_last_error, journal_error, sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
#endif
    // A receive counter may include IDF's buffered encrypted-flash tail.
    // Rewind old unaligned checkpoints to a complete erase sector so restart
    // re-downloads and erases the uncertain tail instead of skipping bytes.
    if (s_journal.bytes_written > s_journal.image_size) {
        strlcpy(s_last_error, "OTA_RESUME_OFFSET_INVALID", sizeof(s_last_error));
        return false;
    }
    // The last network step can complete all bytes before finalize/selection.
    // Re-read the final sector after such a reset; Range starting at EOF is
    // invalid and would otherwise leave the download permanently retrying.
    if (s_journal.bytes_written == s_journal.image_size) --s_journal.bytes_written;
    s_journal.bytes_written -= s_journal.bytes_written % target->erase_size;
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
        if (written > 0) {
            if ((size_t)written > s_journal.image_size) {
                esp_https_ota_abort(handle);
                strlcpy(s_last_error, "IMAGE_LENGTH_MISMATCH", sizeof(s_last_error));
                return false;
            }
            s_journal.bytes_written = (size_t)written - (size_t)written % target->erase_size;
        }
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
    int complete_bytes = esp_https_ota_get_image_len_read(handle);
    if (complete_bytes <= 0 || (size_t)complete_bytes != s_journal.image_size) {
        esp_https_ota_abort(handle);
        strlcpy(s_last_error, "IMAGE_LENGTH_MISMATCH", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
        return false;
    }
    // esp_https_ota_finish verifies the signature AND changes boot selection.
    // Check the expected application identity first. Selecting then restoring
    // a different signed image leaves an unsafe reset window between them.
    unsigned char digest[32];
    char digest_hex[65];
    esp_err_t hash_error = esp_partition_get_sha256(target, digest);
    if (hash_error != ESP_OK) {
        esp_https_ota_abort(handle);
        written_image_unverified(target, hash_error);
        return false;
    }
    hex_bytes(digest, sizeof(digest), digest_hex);
    if (strcmp(digest_hex, s_journal.image_sha256) != 0) {
        esp_https_ota_abort(handle);
        strlcpy(s_last_error, "IMAGE_HASH_MISMATCH", sizeof(s_last_error));
        (void)report_state("FAILED", s_last_error);
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
    (void)report_state("READY_TO_BOOT", NULL);
    wait_for_capture_safepoint();
    esp_restart();
    return true;
}

static void wait_for_capture_safepoint(void)
{
    int64_t last_report = 0;
    int64_t started = esp_timer_get_time() / 1000000;
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000000;
        /* After the bounded wait an offline or busy terminal no longer keeps
         * the old image running: new sessions stop and the current one
         * finishes its normal cleanup. */
        if (now - started < OTA_SAFEPOINT_MAX_SECONDS ? add_connector_claim_ota_restart()
                                                      : add_connector_claim_ota_restart_after_wait()) break;
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
#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
    if (uses_local_boot_confirmation()) {
        /* A capture timeout releases the caller, not its accepted write.
         * Stop admission only after the terminal owner finishes, then wait
         * for all accepted journal work. Never kill a task owning storage. */
        strlcpy(s_last_error, "WAITING_FOR_JOURNAL_QUIESCE", sizeof(s_last_error));
        while (!zj_owner_quiesce()) vTaskDelay(pdMS_TO_TICKS(20));
        s_last_error[0] = '\0';
    }
#endif
}

static bool uses_local_boot_confirmation(void)
{
#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
    return false;
#else
    const esp_app_desc_t *app = esp_app_get_description();
    return app && !strcmp(app->project_name, "zone_lite") &&
        (!strcmp(app->version, ZJ_BRIDGE_VERSION) || !strcmp(app->version, ZJ_WRITER_VERSION));
#endif
}

/* Never report a later transition before its preceding local checkpoint is
 * durable. A lost HTTP response or NVS commit repeats only an idempotent state. */
static bool report_local_boot_confirmation(void)
{
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
    if (!report_factory_trial()) return false;
#endif
    if (!strcmp(s_journal.state, "LOCAL_VALIDATED")) {
        if (!report_state("BOOTED_PENDING", "LOCAL_RUNTIME_HEALTHY")) return false;
        strlcpy(s_journal.state, "BOOT_REPORTED", sizeof(s_journal.state));
        if (!save_journal()) return false;
    }
    if (!strcmp(s_journal.state, "BOOT_REPORTED")) {
        if (!report_state("RECONCILING", NULL)) return false;
        strlcpy(s_journal.state, "RECONCILING", sizeof(s_journal.state));
        if (!save_journal()) return false;
    }
    return !strcmp(s_journal.state, "RECONCILING");
}

static bool confirm_local_boot(void)
{
    int64_t deadline = (esp_timer_get_time() / 1000000) + OTA_BOOT_CONFIRM_SECONDS;
    while ((esp_timer_get_time() / 1000000) < deadline) {
        s_boot_health_checks++;
        const char *local_error = add_connector_local_boot_health_error();
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
        if (!zf_platform_startup_allowed()) local_error=zf_platform_error();
#endif
        s_boot_health_last_ready = local_error == NULL;
        strlcpy(s_last_error, local_error ? local_error : "", sizeof(s_last_error));
        if (s_boot_health_last_ready) {
            if (esp_ota_mark_app_valid_cancel_rollback() != ESP_OK) {
                strlcpy(s_last_error, "BOOT_LOCAL_MARK_VALID_FAILED", sizeof(s_last_error));
                return false;
            }
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
            if (!zf_platform_revoke()) {
                strlcpy(s_last_error,zf_platform_error(),sizeof(s_last_error));
                return false;
            }
#endif
            strlcpy(s_journal.state, "LOCAL_VALIDATED", sizeof(s_journal.state));
            if (!save_journal()) return false;
            /* Local validity is durable before the first network request.
             * Remote progress, source certification and HIL stay separate. */
            return report_local_boot_confirmation();
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    (void)report_state("FAILED", "BOOT_HEALTH_TIMEOUT");
    s_failed_boot_pending = true;
    /* The next bounded coordinator turn drains accepted work and verifies
     * the retained reader. A health timeout is not rollback authority. */
    return false;
}

static bool running_state_is(esp_ota_img_states_t wanted)
{
    esp_ota_img_states_t state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    return running && esp_ota_get_state_partition(running, &state) == ESP_OK && state == wanted;
}

#if OTA_STORAGE_EXCEPTION
/* ---- ZKT 2.7.7: the confirm-first storage exception ------------------------
 * Owner decision D3 (a), 21 October 2026; docs/zone-lite-2.7.7-confirm-first.md.
 * storage_exception.c decides; this glue collects its inputs, runs the
 * confirmation sequence and what follows it. None of its reasons ever reaches
 * s_last_error (M2): ADD turns an active deployment's last_error into FAILED. */
#define SX_EVIDENCE_ACK_MS 10000U
#define SX_LOG_SECONDS 60U
/* A RECONCILING report of an exception deployment: when its status changes,
 * else at most this often (each is an ADD event row). */
#define SX_RECONCILING_REPORT_SECONDS (15U * 60U)
_Static_assert((unsigned)LED_QUEUE_COUNT == SX_QUEUE_COUNT, "the LED's reclaim queues are the gate's");
#if defined(ZONE_LITE_DIRECT_LEGACY_UPGRADE) && ZONE_LITE_DIRECT_LEGACY_UPGRADE && !defined(ZONE_LITE_QUETTA_DIAGNOSTIC)
#define SX_PRODUCTION_ROLE true
#else
#define SX_PRODUCTION_ROLE false
#endif

/* PSRAM, once: the gate's inputs and the texts the reports carry, off the
 * OTA task's stack (its deepest path is a TLS handshake). */
typedef struct {
    sx_repair_t repair;
    sx_inventory_t inventory;
    sx_runtime_t runtime;
    char message[240], context[480], status[48];
} sx_inputs_t;

static struct {
    sx_inputs_t *inputs;          /* PSRAM, once */
    sx_state_t state;             /* this unconfirmed boot's gate */
    sx_reason_t reason;
    uint32_t since_s, blockers, unhealthy_since_s, reconciling_reported_s, attempt_s;
    int8_t passive;               /* -1 unknown */
    bool evaluated, confirming_written, evidence_logged;
    sx_window_t window;
    uint32_t logged_s;
    sx_reason_t logged_reason;
    char reconciling_status[48];
    /* ZKT 2.7.8: every term failing at the last evaluation (bit 1 << reason)
     * and the inventory's condition behind INVENTORY_UNEXPLAINED; diagnostics
     * only, the decision is sx_evaluate's. */
    uint64_t terms;
    sx_why_t why;
} s_sx = {.passive = -1};

/* " DETAIL" when the reason is INVENTORY_UNEXPLAINED and its condition is
 * known, else "". Static text (the OTA task's stack is the TLS handshake's). */
static const char *sx_detail_suffix(sx_reason_t reason)
{
    static char text[24];
    text[0] = '\0';
    if (reason == SX_INVENTORY_UNEXPLAINED && s_sx.why.detail != SX_DETAIL_NONE)
        snprintf(text, sizeof(text), " %s", sx_detail_name(s_sx.why.detail));
    return text;
}

static uint32_t uptime_s(void) { return (uint32_t)(esp_timer_get_time() / 1000000); }

/* The passive slot exists, is not the running one and holds a predecessor
 * whose exact signed digest this release knows (critique 6). Hashed once. */
static bool sx_passive_slot_ok(void)
{
    if (s_sx.passive >= 0) return s_sx.passive;
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    esp_app_desc_t description;
    uint8_t digest[32];
    bool ok = running && other && running->address != other->address &&
        (other->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || other->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1) &&
        esp_ota_get_partition_description(other, &description) == ESP_OK &&
        !strcmp(description.project_name, "zone_lite") && esp_partition_get_sha256(other, digest) == ESP_OK &&
        ug_direct_predecessor_matches(description.version, digest);
    s_sx.passive = ok ? 1 : 0;
    return ok;
}

static sx_inputs_t *sx_buffers(void)
{
    if (!s_sx.inputs) s_sx.inputs = heap_caps_calloc(1, sizeof(*s_sx.inputs), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return s_sx.inputs;
}

/* Every input of the gate now, and the store's fingerprint. */
static __attribute__((noinline)) bool sx_collect(sx_input_t *in, sx_store_t *store)
{
    sx_inputs_t *b = sx_buffers();
    if (!b) return false;
    (void)storage_repair_exception_repair(&b->repair);
    (void)storage_repair_exception_inventory(&b->inventory);
    sx_runtime_t *rt = &b->runtime;
    memset(rt, 0, sizeof(*rt));
    qs_health_t health;
    rt->qs_known = qs_health_known(&health);
    rt->qs_observed = health.observed;
    rt->qs_available = health.available;
    rt->last_error = health.last_error;
    rt->last_operation = health.last_operation;
    rt->probe_error = health.persistence_probe_error;
    rt->probe_operation = health.persistence_probe_operation;
    led_local_causes_t causes;
    led_status_local_causes(&causes);
    rt->led_known = causes.known;
    rt->led_fatal = causes.fatal;
    rt->local_failures = causes.incidents;
    rt->room_failures = causes.room;
    rt->held_read_failures = causes.held_reads;
    rt->held_read_queues = causes.held_queues;
    memcpy(rt->held_read_last, causes.held_last, sizeof(rt->held_read_last));
    size_t total = 0, used = 0;
    rt->spiffs_mounted = esp_spiffs_mounted(NULL) && esp_spiffs_info(NULL, &total, &used) == ESP_OK;
    rt->spiffs_used = (uint32_t)used;
    rt->spiffs_total = (uint32_t)total;
    uint32_t blockers = add_connector_boot_health_blockers();
    rt->health_known = !(blockers & SX_H_LOCK);
    rt->health_blockers = blockers & ~(uint32_t)SX_H_LOCK;
    s_sx.blockers = rt->health_blockers;
    rt->add_connected = add_connector_is_connected();
    /* D2: ADD acknowledged a heartbeat within SX_HEARTBEAT_ACK_SECONDS. */
    int64_t acked_ms = add_connector_heartbeat_acked_ms();
    rt->heartbeat_acked = acked_ms > 0 &&
        esp_timer_get_time() / 1000 - acked_ms <= (int64_t)SX_HEARTBEAT_ACK_SECONDS * 1000;
    rt->capability_accepted = s_capability_reported;
    rt->ota_precheck_ok = zj_ota_precheck_cached();
    rt->passive_slot_ok = sx_passive_slot_ok();
    rt->internal_largest = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    const esp_app_desc_t *app = esp_app_get_description();
    sx_record_t record;
    memset(in, 0, sizeof(*in));
    in->production_role = SX_PRODUCTION_ROLE;
    in->running_pending = running_state_is(ESP_OTA_IMG_PENDING_VERIFY);
    in->ota_deployment = app && s_journal.deployment_id[0] && !strcmp(s_journal.state, "READY_TO_BOOT") &&
        !strcmp(app->version, s_journal.target_version) && cache_running_image_digest() &&
        !strcmp(s_running_image_digest, s_journal.image_sha256);
    in->record_terminal = storage_repair_exception_record(&record) &&
        sx_record_ended(&record, sx_deployment_crc(s_journal.deployment_id));
    in->repair = &b->repair;
    in->inventory = &b->inventory;
    in->runtime = rt;
    store->used = (uint32_t)used;
    store->total = (uint32_t)total;
    store->writes = storage_repair_store_writes();
    return true;
}

/* The gate's state for the heartbeat, and a log line when its reason
 * changes (at most once a minute) or it first holds. */
static void sx_note(sx_reason_t reason)
{
    uint32_t now = uptime_s();
    sx_state_t state = reason == SX_OK ? SX_STATE_ELIGIBLE :
        reason == SX_STABILIZING || reason == SX_STORE_CHANGED ? SX_STATE_ELIGIBLE :
        sx_reason_unknown(reason) ? SX_STATE_EVALUATING : SX_STATE_REFUSED;
    if (!s_sx.evaluated || state != s_sx.state || reason != s_sx.reason) s_sx.since_s = now;
    s_sx.evaluated = true;
    s_sx.state = state;
    s_sx.reason = reason;
    if (reason == s_sx.logged_reason || (s_sx.logged_s && now - s_sx.logged_s < SX_LOG_SECONDS)) return;
    char message[160];
    const char *detail = sx_detail_suffix(reason);
    snprintf(message, sizeof(message), "%s: %s%s%s%s; terms failing 0x%06lx%08lx; boot-health terms failing 0x%08lx",
             sx_state_name(state), sx_reason_name(reason), detail,
             detail[0] && s_sx.why.file[0] ? " " : "", detail[0] ? s_sx.why.file : "",
             (unsigned long)(s_sx.terms >> 32), (unsigned long)(s_sx.terms & 0xffffffffU),
             (unsigned long)s_sx.blockers);
    (void)add_connector_log(state == SX_STATE_REFUSED ? "WARN" : "INFO", "storage",
                            state == SX_STATE_REFUSED ? "STORAGE_REPAIR_EXCEPTION_REFUSED" :
                                                        "STORAGE_REPAIR_EXCEPTION_ELIGIBLE", message);
    s_sx.logged_s = now;
    s_sx.logged_reason = reason;
}

/* The evidence ADD stores before the image confirms (R14). */
static void sx_evidence(const sx_input_t *in, char *message, size_t message_bytes, char *context, size_t context_bytes)
{
    const sx_repair_t *r = in->repair;
    const sx_repair_file_t *held = NULL;
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES && !held; ++i)
        if (r->file[i].blocked_spans) held = &r->file[i];
    snprintf(message, message_bytes,
             "%s %s; %lu pages short; custody %lu blocks, %lu droppable pages; %lu spans held%s%s; probe %s %d",
             r->result, r->error_stage, (unsigned long)r->want, (unsigned long)r->custody_blocks,
             (unsigned long)r->custody_gain, (unsigned long)(held ? held->blocked_spans : 0), held ? " in " : "",
             held ? held->name : "", in->runtime->probe_operation ? in->runtime->probe_operation : "none",
             in->runtime->probe_error);
    snprintf(context, context_bytes,
             "{\"deployment_id\":\"%s\",\"want\":%lu,\"custody_gain\":%lu,\"custody_blocks\":%lu,"
             "\"erased_at_mount\":%lu,\"held_spans\":%lu,\"held_offset\":%lu,\"held_last\":%lu,\"held_digest\":%lu,"
             "\"local_failures\":%lu,\"room_failures\":%lu,\"held_reads\":%lu,\"health_blockers\":%lu,"
             "\"store_used\":%lu,\"store_writes\":%lu,\"internal_largest\":%lu}",
             s_journal.deployment_id, (unsigned long)r->want, (unsigned long)r->custody_gain,
             (unsigned long)r->custody_blocks, (unsigned long)r->erased_at_mount,
             (unsigned long)(held ? held->blocked_spans : 0), (unsigned long)(held ? held->blocked_offset : 0),
             (unsigned long)(held ? held->blocked_last : 0), (unsigned long)(held ? held->blocked_digest : 0),
             (unsigned long)in->runtime->local_failures, (unsigned long)in->runtime->room_failures,
             (unsigned long)in->runtime->held_read_failures, (unsigned long)in->runtime->health_blockers,
             (unsigned long)in->runtime->spiffs_used, (unsigned long)s_sx.window.store.writes,
             (unsigned long)in->runtime->internal_largest);
}

static bool sx_store_record(sx_state_t state)
{
    sx_record_t record = {0};
    (void)storage_repair_exception_record(&record);
    uint32_t crc = sx_deployment_crc(s_journal.deployment_id), tag = storage_repair_image_tag();
    if (record.deployment_crc != crc || record.image_tag != tag) memset(&record, 0, sizeof(record));
    record.state = (uint8_t)state;
    record.deployment_crc = crc;
    record.image_tag = tag;
    if (s_sx.inputs) {
        record.want = s_sx.inputs->repair.want;
        record.custody_gain = s_sx.inputs->repair.custody_gain;
    }
    return storage_repair_exception_store(&record);
}

typedef enum { SX_STEP_WAIT = 0, SX_STEP_CONFIRMED, SX_STEP_FAILED } sx_step_t;

/* One pass of the gate inside the confirmation loop, and the confirmation
 * when it has held SX_STABLE_SECONDS (section 4.1): the record CONFIRMING
 * (once), the evidence ADD stores, the receipted BOOTED_PENDING ADD allows,
 * the gate again with the store unchanged and the budget left, then valid,
 * the record CONFIRMED, the journal RECONCILING. */
/* The gate's verdict now (sx_evaluate decides), and every failing term with
 * the inventory's condition for the reports (2.7.8). */
static __attribute__((noinline)) sx_reason_t sx_judge(const sx_input_t *in)
{
    sx_reason_t verdict = sx_evaluate(in);
    (void)sx_evaluate_terms(in, &s_sx.terms, &s_sx.why);
    return verdict;
}

static __attribute__((noinline)) sx_step_t sx_confirm_step(const boot_confirm_t *progress, uint32_t stepped_s)
{
    sx_input_t in;
    sx_store_t store;
    if (!sx_collect(&in, &store)) return SX_STEP_WAIT;
    sx_reason_t verdict = sx_judge(&in);
    if (verdict == SX_CAPABILITY && in.runtime->add_connected) {
        s_capability_reported = report_capability();
        s_sx.inputs->runtime.capability_accepted = s_capability_reported;
        verdict = sx_judge(&in);
    }
    sx_reason_t step = sx_window_step(&s_sx.window, verdict, uptime_s(), &store);
    sx_note(step);
    if (step != SX_OK) return SX_STEP_WAIT;
    /* A sequence that did not finish is tried again at most every 30 s. */
    uint32_t up = uptime_s();
    if (s_sx.attempt_s && up - s_sx.attempt_s < 30U) return SX_STEP_WAIT;
    s_sx.attempt_s = up ? up : 1;
    if (progress->active_seconds + SX_BUDGET_MARGIN_SECONDS > BOOT_CONFIRM_ACTIVE_SECONDS ||
        progress->elapsed_seconds + SX_BUDGET_MARGIN_SECONDS > BOOT_CONFIRM_MAX_SECONDS) {
        sx_note(SX_BUDGET);
        return SX_STEP_WAIT;
    }
    s_sx.state = SX_STATE_CONFIRMING;
    if (!s_sx.confirming_written) {
        if (!sx_store_record(SX_STATE_CONFIRMING)) {
            sx_note(SX_NVS_RECORD);
            return SX_STEP_WAIT;
        }
        s_sx.confirming_written = true;
    }
    char *message = s_sx.inputs->message, *context = s_sx.inputs->context;
    sx_evidence(&in, message, sizeof(s_sx.inputs->message), context, sizeof(s_sx.inputs->context));
    if (!s_sx.evidence_logged) {
        s_sx.evidence_logged = add_connector_log_acknowledged("WARN", "storage", "STORAGE_REPAIR_EXCEPTION_EVIDENCE",
                                                              message, context, SX_EVIDENCE_ACK_MS);
        if (!s_sx.evidence_logged) {
            sx_note(SX_EVIDENCE_UNACKED);
            return SX_STEP_WAIT;
        }
    }
    cJSON *receipt = NULL;
    bool accepted = report_progress("BOOTED_PENDING", "STORAGE_REPAIR_EXCEPTION", message, &receipt) &&
        ota_progress_receipt_exception(receipt, s_journal.deployment_id, s_journal.target_version,
                                       s_running_image_digest);
    cJSON_Delete(receipt);
    if (!accepted) {
        sx_note(SX_RECEIPT);
        return SX_STEP_WAIT;
    }
    /* The last look: the gate holds now, nothing wrote the store since the
     * window opened, and the budget is not spent (M6). */
    uint32_t spent = uptime_s() - stepped_s;
    sx_reason_t last = !sx_collect(&in, &store) ? SX_STORAGE_UNKNOWN : sx_judge(&in);
    if (last == SX_OK && !sx_window_same(&s_sx.window, &store)) last = SX_STORE_CHANGED;
    if (last == SX_OK && (progress->active_seconds + spent >= BOOT_CONFIRM_ACTIVE_SECONDS ||
                          progress->elapsed_seconds + spent >= BOOT_CONFIRM_MAX_SECONDS))
        last = SX_BUDGET;
    if (last != SX_OK) {
        s_sx.window.open = false;
        sx_note(last);
        return SX_STEP_WAIT;
    }
    if (esp_ota_mark_app_valid_cancel_rollback() != ESP_OK) {
        (void)sx_store_record(SX_STATE_ABANDONED);
        sx_note(SX_MARK_VALID);
        return SX_STEP_FAILED;
    }
    s_sx.state = SX_STATE_CONFIRMED;
    storage_repair_exception_mark_confirmed();
    /* A cut from here on boots a valid image: the record CONFIRMING already
     * means confirmed there, and READY_TO_BOOT on a valid image reconciles. */
    if (!sx_store_record(SX_STATE_CONFIRMED))
        (void)add_connector_log("ERROR", "storage", "STORAGE_REPAIR_EXCEPTION_RECORD", "the CONFIRMED record failed");
    strlcpy(s_journal.state, "RECONCILING", sizeof(s_journal.state));
    (void)save_journal();
    receipt = NULL;
    (void)report_progress("RECONCILING", "STORAGE_REPAIR_EXCEPTION", "confirmed under the storage exception; one "
                          "restart at a capture safepoint, then the reformat round", &receipt);
    cJSON_Delete(receipt);
    (void)add_connector_log("WARN", "storage", "STORAGE_REPAIR_EXCEPTION_CONFIRMED", message);
    return SX_STEP_CONFIRMED;
}

/* Before the ordinary path marks the image valid: no record of this image
 * (one this boot wrote, or another deployment's that never reached
 * mark_app_valid; review of 26 October, D1) may make the next boot an
 * exception boot. */
static bool sx_ordinary_confirmation(void)
{
    sx_record_t record;
    bool stale = storage_repair_exception_record(&record) &&
        sx_record_clear_on_ordinary(&record, storage_repair_image_tag());
    if (!s_sx.confirming_written && !stale) return true;
    if (!sx_store_record(SX_STATE_NONE)) return false;
    s_sx.confirming_written = false;
    return true;
}

/* An unconfirmed boot, before its gate: another deployment's record of this
 * image is ABANDONED (D1). The device refuses an assignment of the version
 * it runs, so the image this boot falls back to is never that record's. */
static void sx_retire_other_record(void)
{
    sx_record_t record;
    if (!storage_repair_exception_record(&record) ||
        !sx_record_retire_other(&record, storage_repair_image_tag(), sx_deployment_crc(s_journal.deployment_id)))
        return;
    if (!storage_repair_exception_store(&record))
        (void)add_connector_log("ERROR", "storage", "STORAGE_REPAIR_EXCEPTION_RECORD",
                                "another deployment's record of this image could not be retired");
}

/* The budget ran out: a CONFIRMING record is abandoned before the rollback
 * (critique 14), and the deployment says why. */
static void sx_rollback_note(char *message, size_t bytes)
{
    if (s_sx.confirming_written) (void)sx_store_record(SX_STATE_ABANDONED);
    if (s_sx.evaluated) snprintf(message, bytes, "storage exception %s: %s%s", sx_state_name(s_sx.state),
                                 sx_reason_name(s_sx.reason), sx_detail_suffix(s_sx.reason));
}

/* The controlled restart (M3): counted in the record before the safepoint
 * is claimed, no progress report while it waits, and once claimed the ESP
 * restarts whatever happens. */
static void sx_restart(sx_record_t *record)
{
    time_t now = time(NULL);
    record->restarts++;
    record->restart_epoch = now >= 1700000000 ? (uint32_t)now : 0;
    if (!storage_repair_exception_store(record)) return;
    char message[96];
    snprintf(message, sizeof(message), "controlled restart %u of %u for the reformat round",
             (unsigned)record->restarts, (unsigned)SX_MAX_RESTARTS);
    (void)add_connector_log("WARN", "storage", "STORAGE_REPAIR_EXCEPTION_RESTART", message);
    int64_t started = esp_timer_get_time() / 1000000;
    for (;;) {
        int64_t waited = esp_timer_get_time() / 1000000 - started;
        if (waited < OTA_SAFEPOINT_MAX_SECONDS ? add_connector_claim_ota_restart()
                                               : add_connector_claim_ota_restart_after_wait())
            break;
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

/* The exception ended (UNREPAIRED, UNHEALTHY): the record first, then FAILED
 * with a checked receipt, then the journal cleared, then the record marked
 * reported. A boot that finds the record ended and the journal RECONCILING
 * resumes here (critique 14). */
static bool sx_report_ended(sx_record_t *record)
{
    const char *code = record->state == SX_STATE_UNHEALTHY ? "STORAGE_REPAIR_EXCEPTION_UNHEALTHY" :
                                                             "STORAGE_REPAIR_EXCEPTION_UNREPAIRED";
    if (!strcmp(s_journal.state, "RECONCILING") && s_journal.deployment_id[0]) {
        sx_inputs_t *b = sx_buffers();
        if (!b) return false;
        char *message = b->message;
        snprintf(message, sizeof(b->message), "%s after %u round boot(s), %u restart(s), %u crash(es); last outcome %s",
                 sx_state_name((sx_state_t)record->state), (unsigned)record->round_boots, (unsigned)record->restarts,
                 (unsigned)record->crashes, sx_outcome_name((sx_outcome_t)record->reason));
        cJSON *receipt = NULL;
        bool reported = report_progress("FAILED", code, message, &receipt) &&
            (ota_progress_receipt_matches(receipt, s_journal.deployment_id, "FAILED", s_journal.target_version,
                                          s_running_image_digest) ||
             ota_progress_receipt_terminal(receipt, s_journal.deployment_id, s_journal.target_version));
        cJSON_Delete(receipt);
        if (!reported || !clear_journal()) return false;
        (void)add_connector_log("ERROR", "storage", code, message);
    }
    record->reported = 1;
    return storage_repair_exception_store(record);
}

/* Every OTA turn after the boot check, on an image confirmed under the
 * exception: the controlled restart, the retries, UNREPAIRED, UNHEALTHY. */
static __attribute__((noinline)) void sx_after_confirm_step(void)
{
    if (!storage_repair_exception_active()) return;
    sx_record_t record;
    if (!storage_repair_exception_record(&record)) return;
    if (sx_record_ended(&record, record.deployment_crc) && !record.reported) {
        (void)sx_report_ended(&record);
        return;
    }
    uint32_t wait = 0;
    time_t now = time(NULL);
    sx_next_t next = sx_next_step(&record, now >= 1700000000, now >= 1700000000 ? (uint32_t)now : 0, uptime_s(), &wait);
    if (next == SX_NEXT_UNREPAIRED &&
        (record.state == SX_STATE_CONFIRMED || record.state == SX_STATE_CONFIRMING)) {
        /* The round boot could not record its outcome (its NVS write failed):
         * before ending the deployment, re-derive the outcome from this
         * boot's own repair report and store that instead (design 14.1 item 5:
         * the round may in fact have REPAIRED the store). */
        sx_inputs_t *b = sx_buffers();
        if (b && storage_repair_exception_repair(&b->repair) &&
            sx_record_after_boot(&record, storage_repair_image_tag(), storage_repair_running_pending(), false,
                                 sx_round_outcome(&b->repair))) {
            (void)storage_repair_exception_store(&record);
            next = sx_next_step(&record, now >= 1700000000, now >= 1700000000 ? (uint32_t)now : 0, uptime_s(), &wait);
        }
    }
    if (next == SX_NEXT_RESTART) {
        sx_restart(&record);
        return;
    }
    if (next == SX_NEXT_UNREPAIRED) {
        record.state = SX_STATE_UNREPAIRED;
        if (storage_repair_exception_store(&record)) (void)sx_report_ended(&record);
        return;
    }
    /* Round done, the store still failing its storage terms with ADD
     * reachable: after SX_UNHEALTHY_SECONDS the deployment ends FAILED (H4). */
    if (record.state != SX_STATE_ROUND_DONE || strcmp(s_journal.state, "RECONCILING") || !add_connector_is_connected()) {
        s_sx.unhealthy_since_s = 0;
        return;
    }
    uint32_t blockers = add_connector_boot_health_blockers();
    bool failing = !(blockers & SX_H_LOCK) &&
        (blockers & (SX_HEALTH_EXCEPTED | SX_H_SPIFFS_INFO | SX_H_QS_OBSERVED | SX_H_STORAGE_UPGRADE));
    uint32_t up = uptime_s();
    if (!failing) s_sx.unhealthy_since_s = 0;
    else if (!s_sx.unhealthy_since_s) s_sx.unhealthy_since_s = up ? up : 1;
    else if (up - s_sx.unhealthy_since_s >= SX_UNHEALTHY_SECONDS) {
        record.state = SX_STATE_UNHEALTHY;
        if (storage_repair_exception_store(&record)) (void)sx_report_ended(&record);
    }
}

/* An exception deployment waiting in RECONCILING (source coverage not yet
 * certified) reports its status with a checked receipt; when ADD has ended
 * the deployment the journal is cleared so OTA is never blocked for good
 * (critique 5). True when the journal no longer waits. */
static __attribute__((noinline)) bool sx_reconciling_report(void)
{
    sx_record_t record;
    sx_inputs_t *b = sx_buffers();
    if (!b || !storage_repair_exception_active() || !storage_repair_exception_record(&record)) return false;
    char *status = b->status;
    snprintf(status, sizeof(b->status), "%s %s", sx_state_name((sx_state_t)record.state),
             sx_outcome_name((sx_outcome_t)record.reason));
    uint32_t up = uptime_s();
    if (!strcmp(status, s_sx.reconciling_status) && s_sx.reconciling_reported_s &&
        up - s_sx.reconciling_reported_s < SX_RECONCILING_REPORT_SECONDS)
        return false;
    char *message = b->message;
    snprintf(message, sizeof(b->message), "confirmed under the storage exception: %s; %u round boot(s); waiting for "
             "storage health and certified source coverage", status, (unsigned)record.round_boots);
    cJSON *receipt = NULL;
    bool reported = report_progress("RECONCILING", "STORAGE_REPAIR_EXCEPTION", message, &receipt);
    bool ended = reported && ota_progress_receipt_terminal(receipt, s_journal.deployment_id, s_journal.target_version);
    cJSON_Delete(receipt);
    if (reported) {
        strlcpy(s_sx.reconciling_status, status, sizeof(s_sx.reconciling_status));
        s_sx.reconciling_reported_s = up ? up : 1;
    }
    if (!ended || !clear_journal()) return false;
    (void)add_connector_log("WARN", "storage", "STORAGE_REPAIR_EXCEPTION_DEPLOYMENT_ENDED",
                            "ADD ended the exception deployment; the OTA journal is clear");
    return true;
}

static void sx_append_telemetry(cJSON *ota)
{
    sx_record_t record;
    bool have = storage_repair_exception_record(&record) && record.image_tag == storage_repair_image_tag();
    bool active = storage_repair_exception_active();
    if (!s_sx.evaluated && !active && !have) return;
    cJSON *sx = cJSON_AddObjectToObject(ota, "storage_exception");
    if (!sx) return;
    sx_state_t state = s_sx.evaluated && !active ? s_sx.state : have ? (sx_state_t)record.state : SX_STATE_NONE;
    const char *reason = s_sx.evaluated && !active ? sx_reason_name(s_sx.reason) :
        have && record.state == SX_STATE_ROUND_SHORT ? sx_outcome_name((sx_outcome_t)record.reason) : NULL;
    bool ok = cJSON_AddStringToObject(sx, "state", sx_state_name(state)) &&
        (!reason || cJSON_AddStringToObject(sx, "reason", reason)) &&
        (!s_sx.evaluated || active || cJSON_AddNumberToObject(sx, "since_seconds", uptime_s() - s_sx.since_s)) &&
        (!have || (cJSON_AddNumberToObject(sx, "restarts", record.restarts) &&
                   cJSON_AddNumberToObject(sx, "round_boots", record.round_boots) &&
                   cJSON_AddNumberToObject(sx, "crashes", record.crashes)));
    /* 2.7.8: every term failing at the last evaluation (bit 1 << reason, in
     * sx_reason_name order) and the inventory's condition behind
     * INVENTORY_UNEXPLAINED with its file. */
    if (ok && s_sx.evaluated && !active && s_sx.terms)
        ok = cJSON_AddNumberToObject(sx, "terms", (double)s_sx.terms) != NULL;
    if (ok && s_sx.evaluated && !active && s_sx.why.detail != SX_DETAIL_NONE &&
        (s_sx.terms & (1ULL << SX_INVENTORY_UNEXPLAINED))) {
        /* The OTA task may be rewriting it: a copy, printable and terminated. */
        char file[sizeof(s_sx.why.file)];
        memcpy(file, s_sx.why.file, sizeof(file));
        file[sizeof(file) - 1] = '\0';
        for (char *c = file; *c; ++c)
            if ((unsigned char)*c < 0x20 || (unsigned char)*c > 0x7e) *c = '?';
        ok = cJSON_AddStringToObject(sx, "detail", sx_detail_name(s_sx.why.detail)) &&
            (!file[0] || cJSON_AddStringToObject(sx, "detail_file", file));
    }
    if (ok && active) ok = cJSON_AddStringToObject(ota, "confirm_reason", "STORAGE_REPAIR_EXCEPTION") != NULL;
    if (ok && s_sx.evaluated && !active && s_sx.blockers) {
        cJSON *list = cJSON_AddArrayToObject(ota, "boot_health_blockers");
        for (uint32_t bit = 1; list && bit && bit <= SX_H_TERMINAL_COUNTS; bit <<= 1) {
            if (!(s_sx.blockers & bit)) continue;
            cJSON *name = cJSON_CreateString(sx_health_name(bit));
            if (!name || !cJSON_AddItemToArray(list, name)) {
                cJSON_Delete(name);
                break;
            }
        }
    }
}
#endif

static bool confirm_or_report_rollback(void)
{
    const esp_app_desc_t *running = esp_app_get_description();
    if (!running) return false;
    bool local_pending = !strcmp(s_journal.state, "LOCAL_VALIDATED") ||
        !strcmp(s_journal.state, "BOOT_REPORTED");
    if (!s_journal.deployment_id[0] || (!local_pending && strcmp(s_journal.state, "READY_TO_BOOT"))) return true;
    if (strcmp(running->version, s_journal.target_version) != 0) {
        if (!report_state("ROLLED_BACK", "BOOTLOADER_ROLLBACK")) return false;
        return clear_journal();
    }
    if (local_pending) return report_local_boot_confirmation();
    if (uses_local_boot_confirmation()) return confirm_local_boot();
    /* ZKT 2.7.7 (critiques 13, M4): READY_TO_BOOT on an image that is
     * already valid (a reset after it marked itself valid, before the
     * journal said RECONCILING) goes straight to RECONCILING: the loop below
     * could time out and invalidate it, handing a store that may have had
     * custody windows to an image that cannot read the redo area. */
    if (running_state_is(ESP_OTA_IMG_VALID)) {
        strlcpy(s_journal.state, "RECONCILING", sizeof(s_journal.state));
        if (!save_journal()) return false;
        (void)report_state("RECONCILING", NULL);
        (void)acknowledge_pending_success();
        return true;
    }
    /* OTA_BOOT_CONFIRM_SECONDS of accountable time, paused while a healthy-
     * looking image only waits on the site, within a hard cap. */
    _Static_assert(BOOT_CONFIRM_ACTIVE_SECONDS == OTA_BOOT_CONFIRM_SECONDS, "one confirmation budget");
    boot_confirm_t progress = {0};
    boot_confirm_verdict_t verdict = BOOT_CONFIRM_WAIT;
    int64_t last_step = esp_timer_get_time() / 1000000;
    int64_t last_health_report = 0;
    bool add_acknowledged = false;
#if OTA_STORAGE_EXCEPTION
    sx_retire_other_record();
#endif
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000000;
        if (!add_acknowledged) {
            add_acknowledged = report_state(
                "BOOTED_PENDING",
                "WAITING_FOR_RUNTIME_HEALTH");
            last_health_report = now;
        }
        bool boot_health_ready = add_acknowledged && add_connector_boot_health_ready();
#if OTA_STORAGE_EXCEPTION
        /* ZKT 2.7.7: full boot health still wins; otherwise the narrow gate.
         * Its sequence never changes the budget below. */
        if (add_acknowledged && !boot_health_ready) {
            sx_step_t exception = sx_confirm_step(&progress, (uint32_t)now);
            if (exception == SX_STEP_CONFIRMED) {
                s_boot_health_checks++;
                return true;
            }
            if (exception == SX_STEP_FAILED) {
                verdict = BOOT_CONFIRM_EXPIRED;
                break;
            }
        }
#endif
        s_boot_health_checks++;
        s_boot_health_last_ready = boot_health_ready;
        boot_confirm_input_t input = {
            .healthy = boot_health_ready,
            .local_failure = add_connector_boot_local_failure(),
            .environment_ready = add_connector_boot_environment_ready(),
        };
        verdict = boot_confirm_step(&progress, now > last_step ? (uint32_t)(now - last_step) : 0U, &input);
        last_step = now;
        s_boot_confirm = progress;
        if (verdict == BOOT_CONFIRM_EXPIRED || verdict == BOOT_CONFIRM_CAPPED) break;
        if (verdict == BOOT_CONFIRM_ACCEPT) {
#if OTA_STORAGE_EXCEPTION
            if (!sx_ordinary_confirmation()) {
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
#endif
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
#if OTA_STORAGE_EXCEPTION
            /* ZKT 2.7.7: the operator sees the gate's state on the deployment. */
            char status[96] = "";
            if (s_sx.evaluated)
                snprintf(status, sizeof(status), "storage exception %s: %s%s for %lu s", sx_state_name(s_sx.state),
                         sx_reason_name(s_sx.reason), sx_detail_suffix(s_sx.reason),
                         (unsigned long)(uptime_s() - s_sx.since_s));
            cJSON *receipt = NULL;
            (void)report_progress("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH", status, &receipt);
            cJSON_Delete(receipt);
#else
            (void)report_state("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH");
#endif
            last_health_report = now;
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    ESP_LOGE(TAG, "New image not confirmed (%s): %lu s accountable, %lu s waiting on the site",
             boot_confirm_verdict_name(verdict), (unsigned long)progress.active_seconds,
             (unsigned long)progress.paused_seconds);
#if OTA_STORAGE_EXCEPTION
    char why[96] = "";
    sx_rollback_note(why, sizeof(why));
    cJSON *receipt = NULL;
    (void)report_progress("FAILED", verdict == BOOT_CONFIRM_CAPPED ? "BOOT_CONFIRM_CAP" : "BOOT_HEALTH_TIMEOUT", why,
                          &receipt);
    cJSON_Delete(receipt);
#else
    (void)report_state("FAILED", verdict == BOOT_CONFIRM_CAPPED ? "BOOT_CONFIRM_CAP" : "BOOT_HEALTH_TIMEOUT");
#endif
    /* Never a valid image (critique 13, M4): only an unconfirmed one rolls
     * back. */
    if (!running_state_is(ESP_OTA_IMG_VALID)) (void)esp_ota_mark_app_invalid_rollback_and_reboot();
    return false;
}

#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
#define OTA_RECOVERY_START_SECONDS (30 * 60)
#define OTA_RECOVERY_MAX_SECONDS (9 * 60 * 60)
#define OTA_RECOVERY_STOP_WAIT_SECONDS (30 * 60)
#define OTA_RECOVERY_REPORT_SECONDS 60
/* The returning 2.5.2 cannot be relied on to settle the deployment, so the
 * terminal outcome is retried for about two minutes before the rollback. */
#define OTA_RECOVERY_FINAL_REPORT_ATTEMPTS 12
static bool s_recovery_authorized;

bool ota_manager_storage_recovery_authorized(void) { return s_recovery_authorized; }

static bool recovery_journal_matches(const ota_journal_t *journal)
{
    const esp_app_desc_t *running = esp_app_get_description();
    return journal && running && journal->deployment_id[0] &&
        !strcmp(journal->state, "READY_TO_BOOT") &&
        !strcmp(running->version, journal->target_version) &&
        !strcmp(running->version, STORAGE_RECOVERY_VERSION);
}

/* The recovery image never calls esp_ota_mark_app_valid_cancel_rollback().
 * It leaves either through the bootloader rollback below or through any reset,
 * which IDF also turns into a rollback because the image remains pending. No
 * OTA journal is written: 2.5.2 reads its own legacy journal after returning,
 * and a stale journal_v1 must never be left for a later 2.6.x image. */
static bool storage_recovery_boot(void)
{
    (void)confirm_or_report_rollback; /* Ordinary boot proof is never used here. */
    int64_t started = esp_timer_get_time() / 1000000;
    int64_t last_report = started;
    int64_t stop_at = 0;
    /* Exact running-image evidence first, even if the run ends immediately. */
    if (s_recovery_authorized) (void)report_state("BOOTED_PENDING", "STORAGE_RECOVERY_RUNNING");
    while (s_recovery_authorized && storage_recovery_state() != STORAGE_RECOVERY_DONE) {
        int64_t now = esp_timer_get_time() / 1000000;
        bool never_started = storage_recovery_state() == STORAGE_RECOVERY_IDLE &&
            now - started >= OTA_RECOVERY_START_SECONDS;
        if (!stop_at && (never_started || now - started >= OTA_RECOVERY_MAX_SECONDS)) {
            storage_recovery_request_stop();
            stop_at = now;
        }
        // Transfers stop between rows; retirement finishes. Both are bounded.
        if (stop_at && (never_started || now - stop_at >= OTA_RECOVERY_STOP_WAIT_SECONDS)) break;
        if (now - last_report >= OTA_RECOVERY_REPORT_SECONDS) {
            (void)report_state("BOOTED_PENDING", "STORAGE_RECOVERY_RUNNING");
            last_report = now;
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
    const char *code = !s_recovery_authorized ? "STORAGE_RECOVERY_NOT_AUTHORIZED" :
        storage_recovery_state() == STORAGE_RECOVERY_DONE ? storage_recovery_code() : "STORAGE_RECOVERY_TIMEOUT";
    // FAILED is the transition that already returned these targets to 2.5.2.
    // Only an authorized deployment is reported; s_last_error stays empty until
    // then so a heartbeat cannot turn the outcome into a separate OTA alert.
    for (int attempt = 0; attempt < OTA_RECOVERY_FINAL_REPORT_ATTEMPTS && s_recovery_authorized; ++attempt) {
        if (report_state("FAILED", code)) break;
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
    strlcpy(s_last_error, code, sizeof(s_last_error));
    vTaskDelay(pdMS_TO_TICKS(3000));
    esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    // Only reached when IDF has no bootable predecessor. Stay reachable so ADD
    // can reinstall the signed 2.5.2 release through the ordinary OTA path.
    ESP_LOGE(TAG, "Storage recovery rollback failed: %s", esp_err_to_name(err));
    strlcpy(s_last_error, "STORAGE_RECOVERY_ROLLBACK_FAILED", sizeof(s_last_error));
    return true;
}
#endif

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
#if OTA_STORAGE_EXCEPTION
    if (!add_connector_ota_reconcile_ready()) return sx_reconciling_report();
#else
    if (!add_connector_ota_reconcile_ready()) return false;
#endif
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
    int64_t add_seen_us = esp_timer_get_time();
    while (true) {
        if (atomic_flag_test_and_set_explicit(&s_control_owner, memory_order_acquire)) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (!s_journal_ready && !load_journal()) {
            atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
            vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
            continue;
        }
        if (!advance_failed_boot_rollback() || !advance_reader_rollback()) {
            atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }
        if (!boot_checked) {
#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
            boot_checked = storage_recovery_boot();
#else
            boot_checked = confirm_or_report_rollback();
#endif
            if (!boot_checked) {
                atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
                vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS)); continue;
            }
        }
        bool reachable = add_connector_is_connected();
        if (reachable) add_seen_us = esp_timer_get_time();
#if !OTA_STORAGE_EXCEPTION
        (void)add_seen_us;
#endif
#if OTA_STORAGE_EXCEPTION
        /* ZKT 2.7.7: the guard's bridge-era inventory once per boot (cached;
         * a busy store is asked again a minute later), for the heartbeat. */
        (void)zj_ota_precheck_cached();
        sx_after_confirm_step();
        /* An exception image out of ADD's WebSocket this long still polls OTA
         * (signed HTTPS needs no WebSocket): a failed reconnect must not cut
         * a device with no rollback off from its fix (critique 6). */
        if (!reachable && storage_repair_exception_active() &&
            esp_timer_get_time() - add_seen_us >= (int64_t)SX_OTA_WITHOUT_ADD_SECONDS * 1000000LL)
            reachable = true;
#endif
        if (reachable) {
            if (!s_capability_reported) {
                s_capability_reported = report_capability();
                if (s_capability_reported) {
                    ESP_LOGI(TAG, "ADD accepted OTA capability; connector is OTA ready");
                } else {
                    ESP_LOGW(TAG, "ADD OTA capability report failed; retrying in %u ms", OTA_POLL_MS);
                }
            }
            if (s_capability_reported && !s_busy) {
                if (!acknowledge_pending_success()) {
                    atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
                    vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
                    continue;
                }
                /* A local network change and an OTA write never run concurrently. */
                if (!setup_portal_active() && fetch_assignment()) {
                    if (advance_reader_rollback()) {
                        s_busy = true;
                        (void)perform_update();
                        s_busy = false;
                    }
                }
            }
        }
        atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
        vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS));
    }
}

void ota_manager_init(void)
{
    if (!cache_running_image_digest())
        ESP_LOGW(TAG, "Running image digest unavailable at boot; OTA evidence will retry");
    load_journal();
#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
    {
        /* Authorize storage changes only for the exact deployment that just
         * selected this image. Decided once, before any concurrent reader. */
        s_recovery_authorized = s_journal_ready && recovery_journal_matches(&s_journal);
        if (!s_recovery_authorized) {
            /* A stale journal_v1 from an older 2.6.x attempt must not hide the
             * legacy journal 2.5.2 wrote for this deployment. It is used in
             * memory only; this image never writes either journal. */
            ota_journal_t legacy = {0};
            size_t size = sizeof(legacy);
            nvs_handle_t handle;
            if (nvs_open(OTA_NAMESPACE, NVS_READONLY, &handle) == ESP_OK) {
                if (nvs_get_blob(handle, "journal", &legacy, &size) == ESP_OK && size == sizeof(legacy) &&
                    ota_journal_valid(&legacy) && recovery_journal_matches(&legacy)) {
                    s_journal = s_committed_journal = legacy;
                    s_journal_ready = s_recovery_authorized = true;
                }
                nvs_close(handle);
            }
        }
    }
#endif
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
    zf_platform_prepare(s_journal_ready ? s_journal.deployment_id : "",s_running_image_digest);
#endif
}

void ota_manager_start(void)
{
    if (s_started) return;
    // Boot confirmation and rollback must keep running while capture and
    // delivery workers are busy at priorities 5, 4 and 3 respectively.
    s_started = xTaskCreate(ota_task, "ota_manager", 12288, NULL, OTA_TASK_PRIORITY, NULL) == pdPASS;
}

bool ota_manager_owns_boot_rollback(void)
{
#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
    return false;
#else
    return !uses_local_boot_confirmation();
#endif
}

bool ota_manager_busy(void)
{
    return s_busy || atomic_load_explicit(&s_hil_reboot_reserved, memory_order_acquire);
}

bool ota_manager_hil_reboot_capable(void)
{
#if defined(ZONE_LITE_JOURNAL_WRITER_IMAGE) && !defined(ZONE_LITE_HIKVISION) && CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    const esp_app_desc_t *app = esp_app_get_description();
    return app && !strcmp(app->project_name, "zone_lite") && !strcmp(app->version, "2.7.0") &&
        esp_secure_boot_enabled() && s_running_image_digest[0];
#else
    return false;
#endif
}
bool ota_manager_running_image(char output[65])
{
    if (!output || !s_running_image_digest[0]) return false;
    memcpy(output, s_running_image_digest, 65);
    return true;
}
bool ota_manager_hil_reboot_reserve(void)
{
    if (!ota_manager_hil_reboot_capable() ||
        atomic_flag_test_and_set_explicit(&s_control_owner, memory_order_acquire)) return false;
    atomic_store_explicit(&s_hil_reboot_reserved, true, memory_order_release);
    if (s_busy || setup_portal_active()) {
        atomic_store_explicit(&s_hil_reboot_reserved, false, memory_order_release);
        atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
        return false;
    }
    return true;
}
void ota_manager_hil_reboot_release(void)
{
    atomic_store_explicit(&s_hil_reboot_reserved, false, memory_order_release);
    atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
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
        cJSON_AddNumberToObject(ota, "image_size", (double)s_journal.image_size) &&
        cJSON_AddNumberToObject(ota, "boot_health_checks", s_boot_health_checks) &&
        cJSON_AddBoolToObject(ota, "boot_health_last_ready", s_boot_health_last_ready) &&
        cJSON_AddNumberToObject(ota, "progress_attempts", s_progress_attempts) &&
        cJSON_AddNumberToObject(ota, "progress_successes", s_progress_successes) &&
        cJSON_AddNumberToObject(ota, "progress_last_http_status", s_progress_last_http_status);
    if (valid && s_last_error[0]) valid = cJSON_AddStringToObject(ota, "last_error", s_last_error) != NULL;
    if (valid && s_boot_confirm.elapsed_seconds) {
        valid = cJSON_AddNumberToObject(ota, "boot_confirm_active_seconds", s_boot_confirm.active_seconds) &&
            cJSON_AddNumberToObject(ota, "boot_confirm_paused_seconds", s_boot_confirm.paused_seconds) &&
            cJSON_AddNumberToObject(ota, "boot_confirm_elapsed_seconds", s_boot_confirm.elapsed_seconds);
    }
#if OTA_STORAGE_EXCEPTION
    if (valid) sx_append_telemetry(ota);
#endif
    if (valid && cJSON_AddItemToObject(heartbeat, "ota", ota)) return;
    cJSON_Delete(ota);
}
