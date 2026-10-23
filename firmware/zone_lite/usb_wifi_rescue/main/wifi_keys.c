#include "wifi_keys.h"
#include <string.h>

static void wipe(void *buffer, size_t size)
{
    volatile unsigned char *p = buffer;
    while (size--) *p++ = 0;
}

static esp_err_t save_wifi_keys(nvs_handle_t handle, const char *ssid,
                               const char *password, bool *changed, bool replace)
{
    if (!ssid || !password || !changed) return ESP_ERR_INVALID_ARG;
    *changed = false;
    size_t ssid_len = strnlen(ssid, 33), pass_len = strnlen(password, 64);
    if (!ssid_len || ssid_len > 32 || pass_len < 8 || pass_len > 63) {
        return ESP_ERR_INVALID_ARG;
    }
    char previous[33] = {0};
    size_t size = sizeof(previous);
    esp_err_t err = nvs_get_str(handle, "wifi_ssid", previous, &size);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) return err;
    if (previous[0] && !replace) return ESP_OK;
    if (replace && !strcmp(previous, ssid)) {
        char previous_pass[64] = {0};
        size = sizeof(previous_pass);
        err = nvs_get_str(handle, "wifi_pass", previous_pass, &size);
        bool equal = err == ESP_OK && !strcmp(previous_pass, password);
        wipe(previous_pass, sizeof(previous_pass));
        if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) return err;
        if (equal) return ESP_OK;
    }
    err = nvs_set_str(handle, "wifi_pass", password);
    if (err == ESP_OK) err = nvs_set_str(handle, "wifi_ssid", ssid);
    if (err == ESP_OK) err = nvs_commit(handle);
    char readback_pass[64] = {0}, readback_ssid[33] = {0};
    size = sizeof(readback_pass);
    if (err == ESP_OK) err = nvs_get_str(handle, "wifi_pass", readback_pass, &size);
    size = sizeof(readback_ssid);
    if (err == ESP_OK) err = nvs_get_str(handle, "wifi_ssid", readback_ssid, &size);
    if (err == ESP_OK && (strcmp(readback_ssid, ssid) || strcmp(readback_pass, password))) {
        err = ESP_FAIL;
    }
    wipe(readback_pass, sizeof(readback_pass));
    if (err == ESP_OK) *changed = true;
    return err;
}

esp_err_t rescue_wifi_keys(nvs_handle_t handle, const char *ssid,
                          const char *password, bool *changed)
{
    return save_wifi_keys(handle, ssid, password, changed, false);
}

esp_err_t rescue_wifi_keys_exact(nvs_handle_t handle, const char *ssid,
                                const char *password, bool *changed)
{
    return save_wifi_keys(handle, ssid, password, changed, true);
}
