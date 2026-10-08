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
#define OTA_RESUME_CHECKPOINT_BYTES (64 * 1024)
#define OTA_HTTP_RESPONSE_BYTES 8192
#define OTA_HTTP_TRANSPORT_BUFFER_BYTES 4096
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
    bool offered = writer && !strcmp(s_journal.target_version, ZJ_BRIDGE_VERSION) &&
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
    if (esp_partition_get_sha256(target, digest) != ESP_OK) {
        esp_https_ota_abort(handle);
        strlcpy(s_last_error, "PARTITION_HASH_FAILED", sizeof(s_last_error));
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
        bool boot_health_ready = add_acknowledged && add_connector_boot_health_ready();
        s_boot_health_checks++;
        s_boot_health_last_ready = boot_health_ready;
        if (boot_health_ready) {
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
            boot_checked = confirm_or_report_rollback();
            if (!boot_checked) {
                atomic_flag_clear_explicit(&s_control_owner, memory_order_release);
                vTaskDelay(pdMS_TO_TICKS(OTA_POLL_MS)); continue;
            }
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
    if (valid && cJSON_AddItemToObject(heartbeat, "ota", ota)) return;
    cJSON_Delete(ota);
}
