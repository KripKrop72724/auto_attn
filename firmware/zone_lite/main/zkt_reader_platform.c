#include "zkt_reader_platform.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_secure_boot.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "sdkconfig.h"
#include <stdlib.h>
#include <string.h>

#define LAYOUT_PARTITIONS_MAX 32U
static int read_proof(void *context, uint8_t bytes[ZJ_READER_PROOF_BYTES])
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("zkt_journal", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t length = ZJ_READER_PROOF_BYTES;
    result = nvs_get_blob(handle, "reader_v1", bytes, &length);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && length == ZJ_READER_PROOF_BYTES ? 1 : -1;
}
static bool write_proof(void *context, const uint8_t bytes[ZJ_READER_PROOF_BYTES])
{
    (void)context;
    nvs_handle_t handle;
    if (nvs_open("zkt_journal", NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t result = nvs_set_blob(handle, "reader_v1", bytes, ZJ_READER_PROOF_BYTES);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}
static void put32(uint8_t *bytes, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) bytes[i] = (uint8_t)(value >> (8 * i));
}
static int partition_order(const void *left, const void *right)
{
    /* A total canonical order independent of iterator ordering, with no
     * native-struct padding or mutable partition contents in the digest. */
    return memcmp(left, right, 32);
}
static bool layout_digest(uint8_t out[32])
{
    uint8_t bytes[32 + 32 * LAYOUT_PARTITIONS_MAX] = {0};
    memcpy(bytes, "ZKT-JOURNAL-LAYOUT-V1", 21);
    esp_partition_iterator_t it = esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL);
    unsigned count = 0;
    bool valid = it != NULL;
    while (it) {
        const esp_partition_t *p = esp_partition_get(it);
        if (!p || count == LAYOUT_PARTITIONS_MAX || strnlen(p->label, sizeof(p->label)) > 16) { valid = false; break; }
        uint8_t *entry = bytes + 32 + count++ * 32;
        entry[0] = (uint8_t)p->type;
        entry[1] = (uint8_t)p->subtype;
        entry[2] = p->encrypted ? 1 : 0;
        put32(entry + 4, p->address);
        put32(entry + 8, p->size);
        memcpy(entry + 12, p->label, strlen(p->label));
        it = esp_partition_next(it);
    }
    esp_partition_iterator_release(it);
    if (!valid || !count) return false;
    put32(bytes + 24, count);
    qsort(bytes + 32, count, 32, partition_order);
    return mbedtls_sha256(bytes, 32 + count * 32, out, 0) == 0;
}
static bool identity(const esp_partition_t *partition, const zj_reader_identity_t *binding,
                      zj_reader_identity_t *out)
{
    if (!partition) return false;
    *out = *binding;
    out->slot_address = partition->address;
    out->slot_size = partition->size;
    return esp_partition_get_sha256(partition, out->image_digest) == ESP_OK;
}
static bool ota(const esp_partition_t *partition)
{
    return partition && partition->type == ESP_PARTITION_TYPE_APP &&
        (partition->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 ||
         partition->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1);
}
static bool validated(const esp_partition_t *partition)
{
    esp_ota_img_states_t state;
    return ota(partition) && esp_ota_get_state_partition(partition, &state) == ESP_OK && state == ESP_OTA_IMG_VALID;
}
static bool descriptor_valid(const esp_app_desc_t *app)
{
    return app && memchr(app->project_name, 0, sizeof(app->project_name)) &&
        memchr(app->version, 0, sizeof(app->version));
}
static zj_compat_result_t current_reader(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, zj_reader_environment_t *env,
    zj_reader_identity_t *current_id)
{
    if (!terminal_serial || !capture_epoch || !strlen(terminal_serial) || strlen(terminal_serial) > 80)
        return ZJ_COMPAT_INVALID;
    const esp_app_desc_t *app = esp_app_get_description();
    if (!descriptor_valid(app) || strcmp(app->project_name, "zone_lite") ||
        (strcmp(app->version, ZJ_BRIDGE_VERSION) && strcmp(app->version, ZJ_WRITER_VERSION))) return ZJ_COMPAT_VERSION;
    *env = (zj_reader_environment_t){.application = app->project_name, .version = app->version,
        .secure_boot = esp_secure_boot_enabled(), .reader_ready = reader_ready,
        .delivery_ready = delivery_ready, .persistence_verified = persistence_verified,
        .recovery_pending = recovery_pending};
#if defined(CONFIG_NVS_ENCRYPTION) && CONFIG_NVS_ENCRYPTION
    env->encrypted_nvs = true;
#endif
    if (!env->secure_boot || !env->encrypted_nvs) return ZJ_COMPAT_SECURITY;
#if !defined(ZONE_LITE_JOURNAL_WRITES) || !ZONE_LITE_JOURNAL_WRITES
    /* A bridge that cannot capture after rollback is not a compatible reader
     * for this release, even while it is still using the legacy path. */
    return ZJ_COMPAT_CAPTURE_DISABLED;
#endif
    if (!reader_ready || !delivery_ready || !persistence_verified || recovery_pending) return ZJ_COMPAT_NOT_READY;
    const esp_partition_t *current = esp_ota_get_running_partition();
    env->ota_slot = ota(current);
    env->image_validated = validated(current);
    if (!env->ota_slot) return ZJ_COMPAT_SECURITY;
    zj_reader_identity_t binding = {0};
    uint8_t terminal_bytes[112] = {0};
    memcpy(terminal_bytes, "ZKT-JOURNAL-TERMINAL-V1", 23);
    memcpy(terminal_bytes + 31, terminal_serial, strlen(terminal_serial));
    memcpy(binding.capture_epoch, capture_epoch, 16);
    if (mbedtls_sha256(terminal_bytes, sizeof(terminal_bytes), binding.terminal_digest, 0) ||
        !layout_digest(binding.layout_digest) || !identity(current, &binding, current_id)) return ZJ_COMPAT_IO;
    return ZJ_COMPAT_OK;
}
zj_compat_result_t zj_reader_platform_writer_identity(const char *terminal_serial,
    const uint8_t capture_epoch[16], zj_reader_identity_t *out)
{
    if (!out) return ZJ_COMPAT_INVALID;
    memset(out, 0, sizeof(*out));
    zj_reader_environment_t environment;
    zj_reader_identity_t identity;
    zj_compat_result_t result = current_reader(terminal_serial, capture_epoch, true, true, true, false,
        &environment, &identity);
    if (result != ZJ_COMPAT_OK) return result;
    if (strcmp(environment.version, ZJ_WRITER_VERSION)) return ZJ_COMPAT_VERSION;
    *out = identity;
    return ZJ_COMPAT_OK;
}
zj_compat_result_t zj_reader_platform_check(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, bool *writer_allowed)
{
    if (!writer_allowed) return ZJ_COMPAT_INVALID;
    *writer_allowed = false;
    zj_reader_environment_t env;
    zj_reader_identity_t current_id, previous_id;
    zj_compat_result_t result = current_reader(terminal_serial, capture_epoch, reader_ready,
        delivery_ready, persistence_verified, recovery_pending, &env, &current_id);
    if (result != ZJ_COMPAT_OK) return result;
    zj_reader_proof_port_t port = {read_proof, write_proof, NULL};
    if (!strcmp(env.version, ZJ_BRIDGE_VERSION)) return zj_reader_attest(port, &env, &current_id);
    const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
    esp_app_desc_t previous_app;
    if (!previous) return ZJ_COMPAT_IO;
    if (!ota(previous)) return ZJ_COMPAT_SECURITY;
    if (esp_ota_get_partition_description(previous, &previous_app) != ESP_OK || !descriptor_valid(&previous_app) ||
        !identity(previous, &current_id, &previous_id)) return ZJ_COMPAT_IO;
    zj_reader_environment_t rollback = {.application = previous_app.project_name, .version = previous_app.version,
        .secure_boot = env.secure_boot, .encrypted_nvs = env.encrypted_nvs,
        .ota_slot = ota(previous), .image_validated = validated(previous)};
    result = zj_reader_check_writer(port, &env, &current_id, &rollback, &previous_id);
    *writer_allowed = result == ZJ_COMPAT_OK;
    return result;
}
zj_compat_result_t zj_reader_platform_update(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, uint32_t target_address,
    uint32_t target_size, const char *target_version)
{
    zj_reader_environment_t env;
    zj_reader_identity_t current_id;
    zj_compat_result_t result = current_reader(terminal_serial, capture_epoch, reader_ready,
        delivery_ready, persistence_verified, recovery_pending, &env, &current_id);
    if (result != ZJ_COMPAT_OK) return result;
    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (!target) return ZJ_COMPAT_IO;
    if (!ota(target) || target->address != target_address || target->size != target_size)
        return ZJ_COMPAT_PROTECTED_SLOT;
    zj_reader_proof_port_t port = {read_proof, NULL, NULL};
    return zj_reader_check_update(port, &env, &current_id, target_address, target_size, target_version);
}

static bool selection_deadline(uint64_t deadline_us)
{
    int64_t now = esp_timer_get_time();
    return now >= 0 && deadline_us > (uint64_t)now &&
        deadline_us - (uint64_t)now <= 5000000U;
}

zj_compat_result_t zj_reader_platform_select(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending,
    const uint8_t expected_digest[32], uint64_t deadline_us)
{
    if (!expected_digest) return ZJ_COMPAT_INVALID;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
#if defined(CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK) && CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK
    /* IDF 5.5.3's setter can erase an image rejected by anti-rollback. This
     * release uses secure boot with anti-rollback disabled; do not enter the
     * destructive code path in an unqualified build configuration. */
    return ZJ_COMPAT_ANTI_ROLLBACK;
#endif
    zj_reader_environment_t env;
    zj_reader_identity_t current_id, previous_id;
    zj_compat_result_t result = current_reader(terminal_serial, capture_epoch, reader_ready,
        delivery_ready, persistence_verified, recovery_pending, &env, &current_id);
    if (result != ZJ_COMPAT_OK) return result;
    /* A bridge cannot use this operation to select an arbitrary previous
     * writer. It is exactly the writer -> attested reader edge. */
    if (strcmp(env.version, ZJ_WRITER_VERSION)) return ZJ_COMPAT_VERSION;
    const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
    esp_app_desc_t previous_app;
    if (!ota(previous)) return ZJ_COMPAT_SECURITY;
    if (esp_ota_get_partition_description(previous, &previous_app) != ESP_OK || !descriptor_valid(&previous_app) ||
        !identity(previous, &current_id, &previous_id)) return ZJ_COMPAT_IO;
    if (memcmp(expected_digest, previous_id.image_digest, 32)) return ZJ_COMPAT_ROLLBACK;
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (!boot) return ZJ_COMPAT_SELECTION_UNCERTAIN;
    bool already_selected = boot->address == previous->address && boot->size == previous->size;
    esp_ota_img_states_t previous_state;
    if (esp_ota_get_state_partition(previous, &previous_state) != ESP_OK) return ZJ_COMPAT_IO;
    if (previous_state != ESP_OTA_IMG_VALID && !(already_selected && previous_state == ESP_OTA_IMG_NEW))
        return ZJ_COMPAT_SECURITY;
    zj_reader_environment_t rollback = {.application = previous_app.project_name, .version = previous_app.version,
        .secure_boot = env.secure_boot, .encrypted_nvs = env.encrypted_nvs,
        .ota_slot = true, .image_validated = previous_state == ESP_OTA_IMG_VALID};
    zj_reader_proof_port_t port = {read_proof, NULL, NULL};
    result = already_selected && previous_state == ESP_OTA_IMG_NEW
        ? zj_reader_check_selected(port, &env, &current_id, &rollback, &previous_id)
        : zj_reader_check_writer(port, &env, &current_id, &rollback, &previous_id);
    if (result != ZJ_COMPAT_OK) return result;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
    /* Retry after lost success must still verify all proof above, but need
     * not rewrite otadata. No other task may select/erase a slot concurrently. */
    if (already_selected) return ZJ_COMPAT_OK;
    if (boot->address != current_id.slot_address || boot->size != current_id.slot_size)
        return ZJ_COMPAT_ROLLBACK;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
    /* IDF validates the signed image before selecting it. An error or failed
     * readback may follow a partial otadata write: preserve the uncertainty. */
    if (esp_ota_set_boot_partition(previous) != ESP_OK) return ZJ_COMPAT_SELECTION_UNCERTAIN;
    boot = esp_ota_get_boot_partition();
    return boot && boot->address == previous->address && boot->size == previous->size
        ? ZJ_COMPAT_OK : ZJ_COMPAT_SELECTION_UNCERTAIN;
}

zj_compat_result_t zj_reader_platform_failed_boot(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending,
    const uint8_t expected_writer_digest[32], uint64_t deadline_us)
{
    if (!expected_writer_digest) return ZJ_COMPAT_INVALID;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
#if defined(CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK) && CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK
    return ZJ_COMPAT_ANTI_ROLLBACK;
#endif
    if (esp_ota_get_app_partition_count() != 2) return ZJ_COMPAT_SECURITY;
    zj_reader_environment_t env;
    zj_reader_identity_t current_id, previous_id;
    zj_compat_result_t result = current_reader(terminal_serial, capture_epoch, reader_ready,
        delivery_ready, persistence_verified, recovery_pending, &env, &current_id);
    if (result != ZJ_COMPAT_OK) return result;
    if (strcmp(env.version, ZJ_WRITER_VERSION)) return ZJ_COMPAT_VERSION;
    if (memcmp(expected_writer_digest, current_id.image_digest, 32)) return ZJ_COMPAT_ROLLBACK;
    const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
    esp_app_desc_t previous_app;
    if (!ota(previous) || !validated(previous)) return ZJ_COMPAT_SECURITY;
    if (esp_ota_get_partition_description(previous, &previous_app) != ESP_OK || !descriptor_valid(&previous_app) ||
        !identity(previous, &current_id, &previous_id)) return ZJ_COMPAT_IO;
    zj_reader_environment_t rollback = {.application = previous_app.project_name, .version = previous_app.version,
        .secure_boot = env.secure_boot, .encrypted_nvs = env.encrypted_nvs,
        .ota_slot = true, .image_validated = true};
    zj_reader_proof_port_t port = {read_proof, NULL, NULL};
    result = zj_reader_check_writer(port, &env, &current_id, &rollback, &previous_id);
    if (result != ZJ_COMPAT_OK) return result;
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (!boot) return ZJ_COMPAT_SELECTION_UNCERTAIN;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
    if (boot->address == previous_id.slot_address && boot->size == previous_id.slot_size)
        return ZJ_COMPAT_OK;
    if (boot->address != current_id.slot_address || boot->size != current_id.slot_size)
        return ZJ_COMPAT_ROLLBACK;
    if (!esp_ota_check_rollback_is_possible()) return ZJ_COMPAT_ROLLBACK;
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
    /* The application intent and accepted writes are durable before IDF can
     * mark this image invalid. Its existing VALID bridge remains VALID, so a
     * terminal outage does not turn the recovery image into another trial. */
    if (esp_ota_mark_app_invalid_rollback() != ESP_OK) return ZJ_COMPAT_SELECTION_UNCERTAIN;
    boot = esp_ota_get_boot_partition();
    if (!selection_deadline(deadline_us)) return ZJ_COMPAT_SELECTION_EXPIRED;
    return boot && boot->address == previous_id.slot_address && boot->size == previous_id.slot_size && validated(previous)
        ? ZJ_COMPAT_OK : ZJ_COMPAT_SELECTION_UNCERTAIN;
}
