/* One-shot Wi-Fi-key repair. No networking, terminal, queue or filesystem code.
 * This app never burns eFuses or erases NVS. The host installer is separate and
 * remains blocked until signed/Secure Download hardware qualification passes. */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "sdkconfig.h"
#include "esp_efuse.h"
#include "esp_efuse_table.h"
#include "esp_flash_encrypt.h"
#include "esp_flash_partitions.h"
#include "esp_image_format.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_rom_crc.h"
#include "esp_secure_boot.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "nvs_snapshot.h"
#include "rescue_config.h"
#include "wifi_keys.h"

#ifndef RESCUE_TESTING_UNENCRYPTED
#if !CONFIG_SECURE_BOOT || !CONFIG_SECURE_SIGNED_ON_UPDATE || !CONFIG_NVS_ENCRYPTION
#error "Production rescue requires Secure Boot signature checks and encrypted NVS"
#endif
#if !CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC || CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID != 0
#error "Production rescue requires the existing HMAC key in block 0"
#endif
#endif

static const uint8_t spare_mac[6] = {0x14,0xc1,0x9f,0x46,0xae,0x1c};
#ifndef RESCUE_TESTING_UNENCRYPTED
static const uint8_t lahore_mac[6] = {0xac,0x27,0x6e,0xa3,0x16,0xd4};
static const char predecessor_sha[] =
    "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6";
#endif

static const esp_partition_t *fixed_partition(esp_partition_type_t type,
        esp_partition_subtype_t subtype, const char *name, uint32_t address, uint32_t size)
{
    const esp_partition_t *p = esp_partition_find_first(type, subtype, name);
    return p && p->address == address && p->size == size && !p->encrypted ? p : NULL;
}

static bool matches_hex(const uint8_t hash[32], const char *hex)
{
    static const char digits[] = "0123456789abcdef";
    unsigned diff = 0;
    if (strlen(hex) != 64) return false;
    for (size_t i = 0; i < 32; ++i) {
        diff |= digits[hash[i] >> 4] ^ hex[i*2];
        diff |= digits[hash[i] & 15] ^ hex[i*2+1];
    }
    return diff == 0;
}

static esp_err_t select_original_valid(const esp_partition_t *otadata)
{
    /* ota_1 corresponds to (ota_seq - 1) % 2 == 1. Two equal VALID records
     * restore the already verified predecessor without first-boot rollback.
     * Each sector is separately erased/written/read back. A power cut before
     * completion leaves either a predecessor record or the installed rescue
     * fallback. This function may only write the fixed otadata partition. */
    if (!otadata || otadata->address != 0x17000 || otadata->size != 0x2000) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_ota_select_entry_t entry;
    memset(&entry, 0xff, sizeof(entry));
    entry.ota_seq = 2;
    entry.ota_state = ESP_OTA_IMG_VALID;
    entry.crc = esp_rom_crc32_le(UINT32_MAX, (const uint8_t *)&entry.ota_seq, 4);
    for (size_t offset = 0; offset < 0x2000; offset += 0x1000) {
        esp_err_t err = esp_partition_erase_range(otadata, offset, 0x1000);
        if (err != ESP_OK) return err;
        err = esp_partition_write(otadata, offset, &entry, sizeof(entry));
        if (err != ESP_OK) return err;
        esp_ota_select_entry_t readback;
        err = esp_partition_read(otadata, offset, &readback, sizeof(readback));
        if (err != ESP_OK) return err;
        if (memcmp(&entry, &readback, sizeof(entry))) return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t verify_configuration_identity(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READONLY, &handle);
    if (err != ESP_OK) return err;
    uint8_t provisioned = 0;
    char zone[100] = {0}, device[32] = {0};
    size_t size = sizeof(zone);
    err = nvs_get_u8(handle, "provisioned", &provisioned);
    if (err == ESP_OK) err = nvs_get_str(handle, "zone_id", zone, &size);
    size = sizeof(device);
    if (err == ESP_OK) err = nvs_get_str(handle, "zone_dev", device, &size);
    nvs_close(handle);
    if (err == ESP_OK && (provisioned != 1 || strcmp(zone, "ZONE-LAHORE-01") || strcmp(device, "1"))) {
        err = ESP_ERR_INVALID_STATE;
    }
    return err;
}

void app_main(void)
{
    printf("SLIC_WIFI_RESCUE_START version=0.1.0\n");
    uint8_t mac[6];
    esp_err_t err = esp_read_mac(mac, ESP_MAC_WIFI_STA);
    const char *expected_sha = NULL;
#ifdef RESCUE_TESTING_UNENCRYPTED
    /* An unsigned test must never become a recovery route for Lahore. */
    if (err == ESP_OK && !memcmp(mac, spare_mac, sizeof(mac))) {
        expected_sha = RESCUE_SPARE_PREDECESSOR_SHA256;
    }
    printf("SLIC_WIFI_RESCUE_UNSIGNED_TEST_ONLY\n");
#else
    if (!esp_secure_boot_enabled() || esp_flash_encryption_enabled() ||
        esp_efuse_get_key_purpose(EFUSE_BLK_KEY0) != ESP_EFUSE_KEY_PURPOSE_HMAC_UP) {
        printf("SLIC_WIFI_RESCUE_REFUSED security_profile\n");
        return;
    }
    if (err == ESP_OK && !memcmp(mac, lahore_mac, sizeof(mac))) expected_sha = predecessor_sha;
    if (err == ESP_OK && !memcmp(mac, spare_mac, sizeof(mac))) expected_sha = RESCUE_SPARE_PREDECESSOR_SHA256;
#endif
    if (!expected_sha) { printf("SLIC_WIFI_RESCUE_REFUSED device_identity\n"); return; }
    const esp_partition_t *original = fixed_partition(ESP_PARTITION_TYPE_APP,
        ESP_PARTITION_SUBTYPE_APP_OTA_1, "ota_1", 0x520000, 0x280000);
    const esp_partition_t *storage = fixed_partition(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "storage", 0x7a0000, 0x800000);
    const esp_partition_t *otadata = fixed_partition(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_OTA, "otadata", 0x17000, 0x2000);
    const esp_partition_t *nvs = fixed_partition(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_NVS, "nvs", 0x11000, 0x6000);
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *factory = fixed_partition(ESP_PARTITION_TYPE_APP,
        ESP_PARTITION_SUBTYPE_APP_FACTORY, "factory", 0x20000, 0x280000);
    const esp_partition_t *ota0 = fixed_partition(ESP_PARTITION_TYPE_APP,
        ESP_PARTITION_SUBTYPE_APP_OTA_0, "ota_0", 0x2a0000, 0x280000);
    if (!original || !storage || !otadata || !nvs || !factory || !ota0 || !running ||
        (running->address != 0x20000 && running->address != 0x2a0000)) {
        printf("SLIC_WIFI_RESCUE_REFUSED partition_layout\n"); return;
    }
    uint8_t hash[32], storage_before[32], storage_after[32];
    esp_partition_pos_t position = {.offset = original->address, .size = original->size};
    esp_image_metadata_t metadata;
    err = esp_image_verify(ESP_IMAGE_VERIFY, &position, &metadata);
    if (err == ESP_OK) err = esp_partition_get_sha256(original, hash);
    if (err != ESP_OK || !matches_hex(hash, expected_sha)) {
        printf("SLIC_WIFI_RESCUE_REFUSED predecessor_image\n"); return;
    }
    err = esp_partition_get_sha256(storage, storage_before);
    if (err != ESP_OK) { printf("SLIC_WIFI_RESCUE_REFUSED storage_read\n"); return; }
    err = select_original_valid(otadata);
    if (err != ESP_OK) { printf("SLIC_WIFI_RESCUE_REFUSED boot_selection\n"); return; }
    printf("SLIC_WIFI_RESCUE_ORIGINAL_SELECTED\n");
    /* nvs_flash_init derives XTS keys using the EXISTING HMAC eFuse. Never
     * retry with erase, create another HMAC key, or initialize another layout. */
    err = nvs_flash_init();
    if (err == ESP_OK) err = verify_configuration_identity();
    uint8_t nvs_before[32], nvs_after[32];
    size_t count_before = 0, count_after = 0;
    if (err == ESP_OK) err = rescue_nvs_snapshot(nvs_before, &count_before);
    bool changed = false;
    if (err == ESP_OK) {
        nvs_handle_t handle;
        err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
        if (err == ESP_OK) {
#if RESCUE_REPLACE_WIFI
            printf("SLIC_WIFI_RESCUE_PROFILE direct_credentials\n");
            err = rescue_wifi_keys_exact(handle, RESCUE_WIFI_SSID, RESCUE_WIFI_PASSWORD, &changed);
#else
            err = rescue_wifi_keys(handle, RESCUE_WIFI_SSID, RESCUE_WIFI_PASSWORD, &changed);
#endif
            nvs_close(handle);
        }
    }
    if (err == ESP_OK) err = rescue_nvs_snapshot(nvs_after, &count_after);
    bool other_unchanged = err == ESP_OK && count_before == count_after &&
        memcmp(nvs_before, nvs_after, sizeof(nvs_before)) == 0;
    esp_err_t storage_err = esp_partition_get_sha256(storage, storage_after);
    bool storage_unchanged = storage_err == ESP_OK &&
        memcmp(storage_before, storage_after, sizeof(storage_before)) == 0;
    printf("SLIC_WIFI_RESCUE_RESULT status=%s changed=%d other_keys_unchanged=%d storage_unchanged=%d error=%s\n",
        err == ESP_OK && other_unchanged && storage_unchanged ? "SUCCESS" : "FAILED",
        changed, other_unchanged, storage_unchanged, esp_err_to_name(err));
    printf("SLIC_WIFI_RESCUE_RETURNING_ORIGINAL\n");
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(2000));
    esp_restart();
}
