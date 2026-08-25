#include "zone_config.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"

#include "zone_lite_config.example.h"

#if !defined(CONFIG_NVS_ENCRYPTION)
#error "Zone Lite requires CONFIG_NVS_ENCRYPTION"
#endif
#if !defined(CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC)
#error "Zone Lite requires HMAC-backed NVS key protection"
#endif
#if !defined(CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID) || CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID != 0
#error "Zone Lite provisioning expects the HMAC key in eFuse key block 0"
#endif

#ifndef ZONE_LITE_ZKT_EXPECTED_SERIAL
#define ZONE_LITE_ZKT_EXPECTED_SERIAL ""
#endif
#ifndef ZONE_LITE_ADD_ENABLED
#define ZONE_LITE_ADD_ENABLED 0
#endif
#ifndef ZONE_LITE_ADD_ONBOARD_URL
#define ZONE_LITE_ADD_ONBOARD_URL "https://autoattn.slichealth.com/device/v2/onboard"
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
#ifndef ZONE_LITE_ADD_BOOTSTRAP_SECRET
#define ZONE_LITE_ADD_BOOTSTRAP_SECRET ""
#endif
#ifndef ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED
#define ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED 0
#endif
#ifndef ZONE_LITE_ZKT_RECOVERY_FAILURES
#define ZONE_LITE_ZKT_RECOVERY_FAILURES 2
#endif
#ifndef ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS
#define ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS (30 * 60 * 1000)
#endif
#ifndef ZONE_LITE_ZKT_REBOOT_WAIT_MS
#define ZONE_LITE_ZKT_REBOOT_WAIT_MS 90000
#endif
#ifndef ZONE_LITE_ZKT_TELNET_PORT
#define ZONE_LITE_ZKT_TELNET_PORT 23
#endif
#ifndef ZONE_LITE_ZKT_TELNET_USERNAME
#define ZONE_LITE_ZKT_TELNET_USERNAME "root"
#endif
#ifndef ZONE_LITE_ZKT_TELNET_PASSWORD
#define ZONE_LITE_ZKT_TELNET_PASSWORD ""
#endif
#ifndef ZONE_LITE_ZKT_TELNET_EXPECT_BANNER
#define ZONE_LITE_ZKT_TELNET_EXPECT_BANNER "Linux"
#endif
#ifndef ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND
#define ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND "reboot"
#endif

static const char *TAG = "zone_config";
static zone_config_t s_config;

static esp_err_t recover_pending_comm_key(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) return ESP_OK;
    if (err != ESP_OK) return err;
    uint8_t phase = 0;
    (void)nvs_get_u8(handle, "zkt_key_phase", &phase);
    nvs_close(handle);
    if (phase != 1) {
        return ESP_OK;
    }
    err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    uint32_t pending_key = 0;
    uint32_t pending_revision = 0;
    uint32_t active_key = 0;
    uint32_t active_revision = 0;
    char pending_operation_id[40] = {0};
    size_t operation_id_size = sizeof(pending_operation_id);
    if (nvs_get_u32(handle, "zkt_key_pending", &pending_key) != ESP_OK ||
        nvs_get_u32(handle, "zkt_rev_pending", &pending_revision) != ESP_OK ||
        nvs_get_str(handle, "zkt_op_pending", pending_operation_id, &operation_id_size) != ESP_OK ||
        pending_operation_id[0] == '\0' ||
        pending_revision == 0) {
        nvs_close(handle);
        return ESP_ERR_INVALID_STATE;
    }
    (void)nvs_get_u32(handle, "zkt_key", &active_key);
    (void)nvs_get_u32(handle, "zkt_key_rev", &active_revision);
    err = nvs_set_u32(handle, "zkt_key_prev", active_key);
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_rev_prev", active_revision);
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_key", pending_key);
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_key_rev", pending_revision);
    if (err == ESP_OK) err = nvs_set_str(handle, "zkt_op_applied", pending_operation_id);
    if (err == ESP_OK) err = nvs_erase_key(handle, "zkt_key_pending");
    if (err == ESP_OK) err = nvs_erase_key(handle, "zkt_rev_pending");
    if (err == ESP_OK) (void)nvs_erase_key(handle, "zkt_op_pending");
    if (err == ESP_OK) err = nvs_set_u8(handle, "zkt_key_phase", 0);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "Completed an authenticated COMM Key commit interrupted by reset");
    }
    return err;
}

static void copy_default(char *target, size_t size, const char *value)
{
    strlcpy(target, value ? value : "", size);
}

static void read_string(nvs_handle_t handle, const char *key, char *target, size_t size)
{
    size_t required = size;
    if (nvs_get_str(handle, key, target, &required) != ESP_OK) {
        return;
    }
    target[size - 1] = '\0';
}

static void load_defaults(void)
{
    memset(&s_config, 0, sizeof(s_config));
    copy_default(s_config.wifi_ssid, sizeof(s_config.wifi_ssid), ZONE_LITE_WIFI_SSID);
    copy_default(s_config.wifi_password, sizeof(s_config.wifi_password), ZONE_LITE_WIFI_PASSWORD);
    s_config.zkt_port = ZONE_LITE_ZKT_PORT;
    s_config.zkt_comm_key = ZONE_LITE_ZKT_COMM_KEY;
    copy_default(s_config.zkt_preferred_ip, sizeof(s_config.zkt_preferred_ip), ZONE_LITE_ZKT_PREFERRED_IP);
    copy_default(s_config.zkt_expected_serial, sizeof(s_config.zkt_expected_serial), ZONE_LITE_ZKT_EXPECTED_SERIAL);
    copy_default(s_config.zone_device_id, sizeof(s_config.zone_device_id), ZONE_LITE_ZONE_DEVICE_ID);
    copy_default(s_config.zone_id, sizeof(s_config.zone_id), ZONE_LITE_ZONE_ID);
    copy_default(s_config.zone_name, sizeof(s_config.zone_name), ZONE_LITE_ZONE_NAME);
    copy_default(s_config.ords_base_url, sizeof(s_config.ords_base_url), ZONE_LITE_ORDS_BASE_URL);
    copy_default(s_config.ords_username, sizeof(s_config.ords_username), ZONE_LITE_ORDS_USERNAME);
    copy_default(s_config.ords_password, sizeof(s_config.ords_password), ZONE_LITE_ORDS_PASSWORD);
    s_config.zkt_recovery_enabled = ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED != 0;
    s_config.zkt_recovery_failures = ZONE_LITE_ZKT_RECOVERY_FAILURES;
    s_config.zkt_recovery_cooldown_ms = ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS;
    s_config.zkt_reboot_wait_ms = ZONE_LITE_ZKT_REBOOT_WAIT_MS;
    s_config.zkt_telnet_port = ZONE_LITE_ZKT_TELNET_PORT;
    copy_default(s_config.zkt_telnet_username, sizeof(s_config.zkt_telnet_username), ZONE_LITE_ZKT_TELNET_USERNAME);
    copy_default(s_config.zkt_telnet_password, sizeof(s_config.zkt_telnet_password), ZONE_LITE_ZKT_TELNET_PASSWORD);
    copy_default(s_config.zkt_telnet_banner, sizeof(s_config.zkt_telnet_banner), ZONE_LITE_ZKT_TELNET_EXPECT_BANNER);
    copy_default(s_config.zkt_telnet_command, sizeof(s_config.zkt_telnet_command), ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND);
    s_config.add_enabled = ZONE_LITE_ADD_ENABLED != 0;
    copy_default(s_config.add_onboard_url, sizeof(s_config.add_onboard_url), ZONE_LITE_ADD_ONBOARD_URL);
    copy_default(s_config.add_ws_url, sizeof(s_config.add_ws_url), ZONE_LITE_ADD_WS_URL);
    copy_default(s_config.bootstrap_secret, sizeof(s_config.bootstrap_secret), ZONE_LITE_ADD_BOOTSTRAP_SECRET);
    copy_default(s_config.connector_id, sizeof(s_config.connector_id), ZONE_LITE_ADD_CONNECTOR_ID);
    copy_default(s_config.device_token, sizeof(s_config.device_token), ZONE_LITE_ADD_DEVICE_TOKEN);
}

esp_err_t zone_config_init(void)
{
    load_defaults();
    esp_err_t recovery = recover_pending_comm_key();
    if (recovery != ESP_OK) {
        ESP_LOGE(TAG, "Could not recover pending COMM Key transaction: %s", esp_err_to_name(recovery));
        return recovery;
    }
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "No encrypted per-device provisioning namespace; firmware remains inert");
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Could not open encrypted provisioning namespace: %s", esp_err_to_name(err));
        return err;
    }
    uint8_t provisioned = 0;
    (void)nvs_get_u8(handle, "provisioned", &provisioned);
    s_config.provisioned = provisioned == 1;
    read_string(handle, "wifi_ssid", s_config.wifi_ssid, sizeof(s_config.wifi_ssid));
    read_string(handle, "wifi_pass", s_config.wifi_password, sizeof(s_config.wifi_password));
    (void)nvs_get_u16(handle, "zkt_port", &s_config.zkt_port);
    (void)nvs_get_u32(handle, "zkt_key", &s_config.zkt_comm_key);
    (void)nvs_get_u32(handle, "zkt_key_rev", &s_config.zkt_comm_key_revision);
    read_string(
        handle,
        "zkt_op_applied",
        s_config.zkt_comm_key_operation_id,
        sizeof(s_config.zkt_comm_key_operation_id));
    read_string(handle, "zkt_ip", s_config.zkt_preferred_ip, sizeof(s_config.zkt_preferred_ip));
    read_string(handle, "zkt_serial", s_config.zkt_expected_serial, sizeof(s_config.zkt_expected_serial));
    read_string(handle, "zone_dev", s_config.zone_device_id, sizeof(s_config.zone_device_id));
    read_string(handle, "zone_id", s_config.zone_id, sizeof(s_config.zone_id));
    read_string(handle, "zone_name", s_config.zone_name, sizeof(s_config.zone_name));
    read_string(handle, "ords_url", s_config.ords_base_url, sizeof(s_config.ords_base_url));
    read_string(handle, "ords_user", s_config.ords_username, sizeof(s_config.ords_username));
    read_string(handle, "ords_pass", s_config.ords_password, sizeof(s_config.ords_password));
    uint8_t enabled = s_config.zkt_recovery_enabled;
    (void)nvs_get_u8(handle, "rec_enable", &enabled);
    s_config.zkt_recovery_enabled = enabled == 1;
    (void)nvs_get_u32(handle, "rec_fails", &s_config.zkt_recovery_failures);
    (void)nvs_get_u32(handle, "rec_cool", &s_config.zkt_recovery_cooldown_ms);
    (void)nvs_get_u32(handle, "reboot_wait", &s_config.zkt_reboot_wait_ms);
    (void)nvs_get_u16(handle, "tel_port", &s_config.zkt_telnet_port);
    read_string(handle, "tel_user", s_config.zkt_telnet_username, sizeof(s_config.zkt_telnet_username));
    read_string(handle, "tel_pass", s_config.zkt_telnet_password, sizeof(s_config.zkt_telnet_password));
    read_string(handle, "tel_banner", s_config.zkt_telnet_banner, sizeof(s_config.zkt_telnet_banner));
    read_string(handle, "tel_cmd", s_config.zkt_telnet_command, sizeof(s_config.zkt_telnet_command));
    enabled = s_config.add_enabled;
    (void)nvs_get_u8(handle, "add_enabled", &enabled);
    s_config.add_enabled = enabled == 1;
    read_string(handle, "add_onboard", s_config.add_onboard_url, sizeof(s_config.add_onboard_url));
    read_string(handle, "add_ws", s_config.add_ws_url, sizeof(s_config.add_ws_url));
    read_string(handle, "boot_secret", s_config.bootstrap_secret, sizeof(s_config.bootstrap_secret));
    read_string(handle, "conn_id", s_config.connector_id, sizeof(s_config.connector_id));
    read_string(handle, "dev_token", s_config.device_token, sizeof(s_config.device_token));
    nvs_close(handle);
    ESP_LOGI(TAG, "Loaded per-device provisioning for zone=%s", s_config.zone_id);
    return ESP_OK;
}

const zone_config_t *zone_config_get(void)
{
    return &s_config;
}

bool zone_config_needs_onboarding(void)
{
    return s_config.add_enabled && s_config.bootstrap_secret[0] != '\0' &&
           (s_config.connector_id[0] == '\0' || s_config.device_token[0] == '\0' ||
            s_config.add_ws_url[0] == '\0');
}

esp_err_t zone_config_save_connector(
    const char *connector_id,
    const char *device_token,
    const char *ws_url)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    if ((err = nvs_set_str(handle, "conn_id", connector_id)) == ESP_OK &&
        (err = nvs_set_str(handle, "dev_token", device_token)) == ESP_OK &&
        (err = nvs_set_str(handle, "add_ws", ws_url)) == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err == ESP_OK) {
        copy_default(s_config.connector_id, sizeof(s_config.connector_id), connector_id);
        copy_default(s_config.device_token, sizeof(s_config.device_token), device_token);
        copy_default(s_config.add_ws_url, sizeof(s_config.add_ws_url), ws_url);
    }
    return err;
}

esp_err_t zone_config_save_zkt_serial(const char *serial)
{
    if (!serial || serial[0] == '\0' || strlen(serial) >= sizeof(s_config.zkt_expected_serial)) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    if ((err = nvs_set_str(handle, "zkt_serial", serial)) == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err == ESP_OK) {
        copy_default(
            s_config.zkt_expected_serial,
            sizeof(s_config.zkt_expected_serial),
            serial);
        ESP_LOGI(TAG, "Pinned authenticated ZKT serial in encrypted configuration");
    }
    return err;
}

esp_err_t zone_config_save_wifi(const char *ssid, const char *password)
{
    if (!ssid || !password) return ESP_ERR_INVALID_ARG;
    size_t ssid_length = strlen(ssid);
    size_t password_length = strlen(password);
    if (ssid_length == 0 || ssid_length > 32 || password_length < 8 || password_length > 63) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    if ((err = nvs_set_str(handle, "wifi_ssid", ssid)) == ESP_OK &&
        (err = nvs_set_str(handle, "wifi_pass", password)) == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err == ESP_OK) {
        copy_default(s_config.wifi_ssid, sizeof(s_config.wifi_ssid), ssid);
        copy_default(s_config.wifi_password, sizeof(s_config.wifi_password), password);
        ESP_LOGI(TAG, "Updated encrypted Wi-Fi configuration for SSID=%s", ssid);
    }
    return err;
}

esp_err_t zone_config_save_zkt_comm_key(
    uint32_t comm_key,
    uint32_t revision,
    const char *operation_id)
{
    if (!operation_id || operation_id[0] == '\0' || strlen(operation_id) >= 40 ||
        revision == 0 || revision != s_config.zkt_comm_key_revision + 1) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t handle;
    esp_err_t err = nvs_open("zone_cfg", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    if ((err = nvs_set_u32(handle, "zkt_key_pending", comm_key)) == ESP_OK &&
        (err = nvs_set_u32(handle, "zkt_rev_pending", revision)) == ESP_OK &&
        (err = nvs_set_str(handle, "zkt_op_pending", operation_id)) == ESP_OK &&
        (err = nvs_set_u8(handle, "zkt_key_phase", 1)) == ESP_OK) {
        err = nvs_commit(handle);
    }
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_key_prev", s_config.zkt_comm_key);
    if (err == ESP_OK) {
        err = nvs_set_u32(handle, "zkt_rev_prev", s_config.zkt_comm_key_revision);
    }
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_key", comm_key);
    if (err == ESP_OK) err = nvs_set_u32(handle, "zkt_key_rev", revision);
    if (err == ESP_OK) err = nvs_set_str(handle, "zkt_op_applied", operation_id);
    if (err == ESP_OK) err = nvs_erase_key(handle, "zkt_key_pending");
    if (err == ESP_OK) err = nvs_erase_key(handle, "zkt_rev_pending");
    if (err == ESP_OK) (void)nvs_erase_key(handle, "zkt_op_pending");
    if (err == ESP_OK) err = nvs_set_u8(handle, "zkt_key_phase", 0);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err == ESP_OK) {
        s_config.zkt_comm_key = comm_key;
        s_config.zkt_comm_key_revision = revision;
        copy_default(
            s_config.zkt_comm_key_operation_id,
            sizeof(s_config.zkt_comm_key_operation_id),
            operation_id);
        ESP_LOGI(TAG, "Committed authenticated ZKT COMM Key revision %lu", (unsigned long)revision);
    }
    return err;
}
