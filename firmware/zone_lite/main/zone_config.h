#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    bool provisioned;
    char wifi_ssid[33];
    char wifi_password[65];
    uint16_t zkt_port;
    uint32_t zkt_comm_key;
    char zkt_preferred_ip[16];
    char zkt_expected_serial[80];
    char zone_device_id[32];
    char zone_id[100];
    char zone_name[128];
    char ords_base_url[512];
    char ords_username[128];
    char ords_password[256];
    bool zkt_recovery_enabled;
    uint32_t zkt_recovery_failures;
    uint32_t zkt_recovery_cooldown_ms;
    uint32_t zkt_reboot_wait_ms;
    uint16_t zkt_telnet_port;
    char zkt_telnet_username[64];
    char zkt_telnet_password[128];
    char zkt_telnet_banner[64];
    char zkt_telnet_command[64];
    bool add_enabled;
    char add_onboard_url[256];
    char add_ws_url[320];
    char bootstrap_secret[96];
    char connector_id[48];
    char device_token[96];
} zone_config_t;

esp_err_t zone_config_init(void);
const zone_config_t *zone_config_get(void);
bool zone_config_needs_onboarding(void);
esp_err_t zone_config_save_connector(
    const char *connector_id,
    const char *device_token,
    const char *ws_url);
esp_err_t zone_config_save_wifi(const char *ssid, const char *password);
