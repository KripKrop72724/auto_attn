#include "zkt_journal_runtime.h"
#include "zkt_capture_runtime.h"
#include "zkt_journal_diagnostics.h"
#include "queue_store.h"
#include "zone_config.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "esp_secure_boot.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"
#include <string.h>

static zj_boot_t state, snapshot;
static SemaphoreHandle_t health_lock;
static bool published;
static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static zj_boot_mode_t mode(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    if (!app || strcmp(app->project_name, "zone_lite")) return ZJ_BOOT_DISABLED;
    if (!strcmp(app->version, ZJ_BRIDGE_VERSION)) return ZJ_BOOT_BRIDGE;
    if (!strcmp(app->version, ZJ_WRITER_VERSION)) return ZJ_BOOT_WRITER;
    return ZJ_BOOT_DISABLED;
}
static bool start_owner(void *context, const char *serial)
{
    (void)context;
    /* This epoch only validates the metadata template. Before any file can
     * be opened, the owner replaces it with the encrypted NVS root's epoch. */
    zj_metadata_t metadata = {.segment_id = 1, .capture_epoch = {1},
        .decoder_profile = "zkt-unqualified", .decoder_version = "zkt-raw-1"};
    strcpy(metadata.terminal_serial, serial);
    return zj_owner_start(ZJ_DEVICE_PREFIX, &metadata);
}
static bool owner_health(void *context, zj_owner_health_t *out)
{ (void)context; return zj_owner_health(out); }
static bool start_transport(void *context) { (void)context; return zj_transport_start(); }
static bool transport_health(void *context, zj_transport_health_t *out)
{ (void)context; return zj_transport_health(out); }
static bool start_capture(void *context) { (void)context; return zj_capture_runtime_start(); }
static bool submit(void *context, const zj_request_t *request, uint64_t *ticket)
{ (void)context; return zj_owner_submit(request, ticket); }
static bool poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{ (void)context; return zj_owner_poll(ticket, reply, complete); }
static bool abandon(void *context, uint64_t ticket)
{ (void)context; return zj_owner_abandon(ticket); }

void zj_runtime_step(void)
{
    if (!health_lock) health_lock = xSemaphoreCreateMutex();
    if (!health_lock) return;
    zj_boot_input_t input = {.now_ms = now_ms(), .mode = mode()};
    if (input.mode != ZJ_BOOT_DISABLED) {
        const zone_config_t *config = zone_config_get();
        input.terminal_serial = config->zkt_expected_serial;
#if defined(CONFIG_NVS_ENCRYPTION) && CONFIG_NVS_ENCRYPTION
        const esp_partition_t *running = esp_ota_get_running_partition();
        esp_ota_img_states_t running_state;
        input.bridge_validation_pending = input.mode == ZJ_BOOT_BRIDGE && running &&
            esp_ota_get_state_partition(running, &running_state) == ESP_OK &&
            running_state == ESP_OTA_IMG_PENDING_VERIFY;
        input.secure = config->provisioned && !strcmp(config->firmware_family, "zkt") &&
            esp_secure_boot_enabled() && running && running->type == ESP_PARTITION_TYPE_APP &&
            (running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 || running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1);
#endif
        qs_health_t storage = qs_health();
        input.storage_available = storage.observed && storage.available;
        input.storage_ready = storage.observed && storage.available && storage.recovery_complete &&
            storage.persistence_verified && !storage.last_error && !storage.persistence_probe_error;
#if defined(ZONE_LITE_JOURNAL_WRITES) && ZONE_LITE_JOURNAL_WRITES
        input.writer_build = true;
#endif
    }
    zj_boot_port_t port = {start_owner, owner_health, start_transport, transport_health,
        start_capture, submit, poll, abandon, NULL};
    zj_boot_step(&state, port, &input);
    if (xSemaphoreTake(health_lock, pdMS_TO_TICKS(20)) != pdTRUE) return;
    snapshot = state;
    published = true;
    xSemaphoreGive(health_lock);
}
bool zj_runtime_health(zj_boot_t *out)
{
    if (!out || !health_lock || xSemaphoreTake(health_lock, pdMS_TO_TICKS(20)) != pdTRUE) return false;
    bool ready = published;
    if (ready) *out = snapshot;
    xSemaphoreGive(health_lock);
    return ready;
}
bool zj_runtime_boot_ready(void)
{
    zj_boot_mode_t required = mode();
    if (required == ZJ_BOOT_DISABLED) return true;
    zj_boot_t current;
    return zj_runtime_health(&current) && current.mode == required && zj_boot_local_ready(&current, now_ms());
}
bool zj_runtime_writer_ready(void)
{
    zj_boot_t current;
    zj_boot_mode_t required = mode();
    return required != ZJ_BOOT_DISABLED && zj_runtime_health(&current) && current.mode == required &&
        current.delivery_authority == ZJ_AUTHORITY_ADD && current.writer_ready &&
        zj_boot_local_ready(&current, now_ms());
}
bool zj_runtime_legacy_capture_allowed(void)
{
    zj_boot_mode_t required = mode();
    if (required == ZJ_BOOT_DISABLED) return true;
    zj_boot_t current;
    return required == ZJ_BOOT_BRIDGE && zj_runtime_health(&current) && current.mode == required &&
        current.delivery_authority == ZJ_AUTHORITY_LEGACY && zj_boot_local_ready(&current, now_ms());
}
bool zj_runtime_raw_source_required(void)
{
    return !zj_runtime_legacy_capture_allowed();
}
bool zj_runtime_append_diagnostics(cJSON *diagnostics)
{
    zj_boot_t current = {0};
    bool observed = zj_runtime_health(&current);
    zj_boot_mode_t required = mode();
    bool recent = observed && current.mode == required && (uint32_t)(now_ms() - current.sampled_ms) < 45000U;
    bool legacy = required == ZJ_BOOT_DISABLED || (recent && required == ZJ_BOOT_BRIDGE &&
        current.delivery_authority == ZJ_AUTHORITY_LEGACY);
    cJSON *runtime = cJSON_CreateObject();
    if (!runtime) return false;
    bool ok = cJSON_AddStringToObject(diagnostics, "runtime_profile", legacy ? "ZKT_LEGACY" : "ZKT_JOURNAL_V1") &&
        cJSON_AddStringToObject(diagnostics, "delivery_authority", legacy ? "LEGACY_DUAL" :
            recent && current.delivery_authority == ZJ_AUTHORITY_ADD ? "ADD" : "UNKNOWN") &&
        (required == ZJ_BOOT_DISABLED || cJSON_AddNumberToObject(diagnostics, "journal_format", 1)) &&
        cJSON_AddBoolToObject(runtime, "observed", observed) &&
        cJSON_AddStringToObject(runtime, "phase", observed ? zj_boot_phase_name(current.phase) : "NOT_STARTED") &&
        cJSON_AddBoolToObject(runtime, "reader_ready", recent && current.reader_ready) &&
        cJSON_AddStringToObject(runtime, "delivery_authority", !recent ? "UNKNOWN" :
            current.delivery_authority == ZJ_AUTHORITY_ADD ? "ADD" :
            current.delivery_authority == ZJ_AUTHORITY_LEGACY ? "LEGACY" : "UNKNOWN") &&
        cJSON_AddBoolToObject(runtime, "writer_ready", recent && current.writer_ready && zj_boot_local_ready(&current, now_ms())) &&
        cJSON_AddNumberToObject(runtime, "start_attempts", current.start_attempts) &&
        cJSON_AddNumberToObject(runtime, "storage_starts", current.owner_starts) &&
        cJSON_AddNumberToObject(runtime, "delivery_starts", current.transport_starts) &&
        cJSON_AddNumberToObject(runtime, "capture_starts", current.capture_starts) &&
        cJSON_AddNumberToObject(runtime, "proof_attempts", current.proof_attempts) &&
        cJSON_AddNumberToObject(runtime, "failures", current.failures) &&
        (!observed || (cJSON_AddNumberToObject(runtime, "sampled_uptime_ms", current.sampled_ms) &&
            cJSON_AddNumberToObject(runtime, "last_progress_uptime_ms", current.progress_ms) &&
            cJSON_AddStringToObject(runtime, "compatibility", zj_compat_error(current.compatibility))));
    if (ok && cJSON_AddItemToObject(diagnostics, "journal_runtime", runtime)) {
        if (required == ZJ_BOOT_DISABLED) return true;
        zj_diagnostics_snapshot_t workers = {0};
        workers.owner_observed = zj_owner_health(&workers.owner);
        workers.transport_observed = zj_transport_health(&workers.transport);
        workers.capture_observed = zj_capture_runtime_health(&workers.capture);
        return zj_diagnostics_append(diagnostics, &current, recent, legacy, &workers,
            (uint64_t)(esp_timer_get_time() / 1000));
    }
    cJSON_Delete(runtime);
    return false;
}
