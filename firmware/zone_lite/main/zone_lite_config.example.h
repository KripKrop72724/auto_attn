#pragma once

// Copy this file to zone_lite_config.h for local flashing.
// zone_lite_config.h is intentionally ignored by git because it contains secrets.

#define ZONE_LITE_WIFI_SSID "your-wifi-name"
#define ZONE_LITE_WIFI_PASSWORD "your-wifi-password"

#define ZONE_LITE_ZKT_PORT 4370
#define ZONE_LITE_ZKT_COMM_KEY 0
#define ZONE_LITE_ZKT_PREFERRED_IP "0.0.0.0"
#define ZONE_LITE_ZONE_DEVICE_ID "1"
#define ZONE_LITE_ZONE_ID "SLIC-TOWER-11-FLOOR"
#define ZONE_LITE_ZONE_NAME "SLIC-TOWER-11-FLOOR"

#define ZONE_LITE_ORDS_BASE_URL "https://example.invalid/ords/slic_hrm/raw_attn_capture_event"
#define ZONE_LITE_ORDS_USERNAME "slic_zone_agent"
#define ZONE_LITE_ORDS_PASSWORD "replace-me"
#define ZONE_LITE_ORDS_BULK_CHUNK_SIZE 100
#define ZONE_LITE_ORDS_TIMEOUT_MS 15000
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS 60000
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS (10 * 60 * 1000)
#define ZONE_LITE_ORDS_RECONCILE_ENABLED 1
#define ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS 5000

// Discovery scans the DHCP subnet and accepts the first host that passes
// ZKT CMD_CONNECT + Comm Key authentication.
#define ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS 450
#define ZONE_LITE_DISCOVERY_RETRY_DELAY_MS 15000
#define ZONE_LITE_RECONCILE_INTERVAL_MS 60000
#define ZONE_LITE_ZKT_USER_REFRESH_RETRIES 3
#define ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS 2000
#define ZONE_LITE_SNTP_SERVER "pool.ntp.org"
#define ZONE_LITE_SNTP_SYNC_TIMEOUT_MS 15000
#define ZONE_LITE_MIN_VALID_UNIX_TIME 1767225600

// ESP32-S3-DevKitC-1 onboard addressable RGB LED. If a board revision or clone
// uses a different RGB pin, override only ZONE_LITE_LED_GPIO in local config.
#define ZONE_LITE_LED_ENABLED 1
#define ZONE_LITE_LED_GPIO 48
#define ZONE_LITE_LED_BRIGHTNESS 96
#define ZONE_LITE_LED_FAULT_LATCH_MS 180000
#define ZONE_LITE_LED_ACTIVITY_FLASH_MS 250

// Optional ZKT OS recovery. Keep disabled unless the attendance device has a
// confirmed telnet account dedicated to controlled recovery.
#define ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED 0
#define ZONE_LITE_ZKT_RECOVERY_FAILURES 2
#define ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS (30 * 60 * 1000)
#define ZONE_LITE_ZKT_REBOOT_WAIT_MS 90000
#define ZONE_LITE_ZKT_TELNET_PORT 23
#define ZONE_LITE_ZKT_TELNET_USERNAME "root"
#define ZONE_LITE_ZKT_TELNET_PASSWORD "replace-me"
#define ZONE_LITE_ZKT_TELNET_EXPECT_BANNER "Linux"
#define ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND "reboot"
