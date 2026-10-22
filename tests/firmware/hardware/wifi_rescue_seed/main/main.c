/* Public synthetic settings only; no fleet credentials or attendance code. */
#include <stdio.h>
#include <string.h>
#include "sdkconfig.h"
#include "esp_efuse.h"
#include "esp_efuse_table.h"
#include "esp_secure_boot.h"
#include "esp_mac.h"
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

void app_main(void)
{
    const uint8_t spare[] = {0x14,0xc1,0x9f,0x46,0xae,0x1c};
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    if (memcmp(mac, spare, 6)) { printf("SEED_REFUSED wrong_mac\n"); return; }
#if CONFIG_NVS_ENCRYPTION
    /* Enrollment is a separate, explicitly approved operation. Never let NVS
     * generate a new key on an unprepared spare as a side effect of boot. */
    if (!esp_secure_boot_enabled() ||
        esp_efuse_get_key_purpose(EFUSE_BLK_KEY0) != ESP_EFUSE_KEY_PURPOSE_HMAC_UP) {
        printf("SEED_REFUSED security_not_enrolled\n"); return;
    }
#endif
    ESP_ERROR_CHECK(nvs_flash_init());
    const esp_partition_t *storage = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "storage");
    if (!storage || storage->address != 0x7a0000 || storage->size != 0x800000) {
        printf("SEED_REFUSED storage_layout\n"); return;
    }
    uint8_t storage_hash[32];
    ESP_ERROR_CHECK(esp_partition_get_sha256(storage, storage_hash));
    printf("SLIC_RESCUE_SEED_STORAGE_SHA256=");
    for (size_t i = 0; i < sizeof(storage_hash); ++i) printf("%02x", storage_hash[i]);
    printf("\n");
    nvs_handle_t h;
    ESP_ERROR_CHECK(nvs_open("zone_cfg", NVS_READWRITE, &h));
    uint8_t seeded = 0;
    (void)nvs_get_u8(h, "provisioned", &seeded);
    if (!seeded) {
        ESP_ERROR_CHECK(nvs_set_u8(h, "provisioned", 1));
        ESP_ERROR_CHECK(nvs_set_str(h, "zone_id", "ZONE-LAHORE-01"));
        ESP_ERROR_CHECK(nvs_set_str(h, "zone_dev", "1"));
        ESP_ERROR_CHECK(nvs_set_str(h, "wifi_ssid", ""));
        ESP_ERROR_CHECK(nvs_set_str(h, "wifi_pass", "old-password"));
        ESP_ERROR_CHECK(nvs_set_str(h, "connector_id", "PUBLIC-TEST-CONNECTOR"));
        ESP_ERROR_CHECK(nvs_set_u32(h, "zkt_key", 1979));
        ESP_ERROR_CHECK(nvs_commit(h));
    }
    nvs_close(h);
    ESP_ERROR_CHECK(nvs_open("preserve", NVS_READWRITE, &h));
    seeded = 0;
    (void)nvs_get_u8(h, "seeded", &seeded);
    uint8_t blob[1024];
    for (size_t i = 0; i < sizeof(blob); ++i) blob[i] = i & 255;
    if (!seeded) {
        ESP_ERROR_CHECK(nvs_set_u8(h, "seeded", 1));
        ESP_ERROR_CHECK(nvs_set_i8(h, "i8", -100));
        ESP_ERROR_CHECK(nvs_set_u16(h, "u16", 54321));
        ESP_ERROR_CHECK(nvs_set_i16(h, "i16", -12345));
        ESP_ERROR_CHECK(nvs_set_u32(h, "u32", 1234567890));
        ESP_ERROR_CHECK(nvs_set_i32(h, "i32", -1234567890));
        ESP_ERROR_CHECK(nvs_set_u64(h, "u64", UINT64_C(123456789012345)));
        ESP_ERROR_CHECK(nvs_set_i64(h, "i64", -INT64_C(123456789012345)));
        ESP_ERROR_CHECK(nvs_set_str(h, "text", "PUBLIC IMMUTABLE TEST FIXTURE"));
        ESP_ERROR_CHECK(nvs_set_blob(h, "blob", blob, sizeof(blob)));
        ESP_ERROR_CHECK(nvs_commit(h));
    }
    nvs_close(h);
    for (int iteration = 0; iteration < 30; ++iteration) {
        ESP_ERROR_CHECK(nvs_open("zone_cfg", NVS_READONLY, &h));
        char ssid[33] = {0}, pass[64] = {0}, connector[64] = {0};
        size_t len = sizeof(ssid); ESP_ERROR_CHECK(nvs_get_str(h, "wifi_ssid", ssid, &len));
        len = sizeof(pass); ESP_ERROR_CHECK(nvs_get_str(h, "wifi_pass", pass, &len));
        len = sizeof(connector); ESP_ERROR_CHECK(nvs_get_str(h, "connector_id", connector, &len));
        uint32_t key = 0; ESP_ERROR_CHECK(nvs_get_u32(h, "zkt_key", &key));
        nvs_close(h);
        ESP_ERROR_CHECK(nvs_open("preserve", NVS_READONLY, &h));
        uint8_t actual[1024]; len = sizeof(actual);
        ESP_ERROR_CHECK(nvs_get_blob(h, "blob", actual, &len));
        nvs_close(h);
        bool intact = len == sizeof(blob) && !memcmp(blob, actual, sizeof(blob)) &&
            key == 1979 && !strcmp(connector, "PUBLIC-TEST-CONNECTOR");
        printf("SLIC_RESCUE_SEED_BOOT ssid_state=%s password_matches_target=%d other_fixture_intact=%d\n",
            !ssid[0] ? "EMPTY" : !strcmp(ssid,"RE7") ? "RE7" : "OTHER",
            !strcmp(pass,"HIL-Recovery-Only"), intact);
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
