#pragma once

// Non-secret compile-time behavior defaults for the generic production image.
// Per-device identity, network, ZKT, ORDS, recovery credentials, and ADD
// onboarding material must come only from the encrypted NVS provisioner.

#define ZONE_LITE_WIFI_SSID ""
#define ZONE_LITE_WIFI_PASSWORD ""

#define ZONE_LITE_ZKT_PORT 4370
#define ZONE_LITE_ZKT_COMM_KEY 0
#define ZONE_LITE_ZKT_PREFERRED_IP "0.0.0.0"
#define ZONE_LITE_ZKT_EXPECTED_SERIAL ""
#define ZONE_LITE_ZONE_DEVICE_ID ""
#define ZONE_LITE_ZONE_ID ""
#define ZONE_LITE_ZONE_NAME ""

#define ZONE_LITE_ORDS_BASE_URL ""
#define ZONE_LITE_ORDS_USERNAME ""
#define ZONE_LITE_ORDS_PASSWORD ""
#define ZONE_LITE_ORDS_BULK_CHUNK_SIZE 100
#define ZONE_LITE_ORDS_TIMEOUT_MS 15000
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS 60000
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS (10 * 60 * 1000)
#define ZONE_LITE_ORDS_RECONCILE_ENABLED 1
#define ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS 5000
// Optional PEM CA chain bundle dedicated to the ORDS endpoint. Keep this as
// root/intermediate CA material, not a short-lived leaf/server certificate.
// Include the issuing intermediate if the endpoint or ESP TLS stack needs it.
// When NULL, firmware uses the ESP-IDF built-in certificate bundle.
#define ZONE_LITE_ORDS_CA_CERT_PEM NULL

// ADD uses an outbound-only TLS WebSocket. Encrypted NVS carries the per-MAC
// bootstrap secret and receives a rotated connector token during onboarding.
#define ZONE_LITE_ADD_ENABLED 0
#define ZONE_LITE_ADD_ONBOARD_URL "https://autoattn.slichealth.com/device/v2/onboard"
#define ZONE_LITE_ADD_WS_URL ""
#define ZONE_LITE_ADD_CONNECTOR_ID ""
#define ZONE_LITE_ADD_DEVICE_TOKEN ""
#define ZONE_LITE_ADD_BOOTSTRAP_SECRET ""
#define ZONE_LITE_ADD_HEARTBEAT_SECONDS 15
#define ZONE_LITE_ADD_RECONNECT_MS 30000

// Discovery scans the DHCP subnet and accepts the first host that passes
// ZKT CMD_CONNECT + Comm Key authentication.
#define ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS 450
#define ZONE_LITE_DISCOVERY_RETRY_DELAY_MS 15000
#define ZONE_LITE_DISCOVERY_FULL_SCAN_INTERVAL_MS (15 * 60 * 1000)
#define ZONE_LITE_RECONCILE_INTERVAL_MS (15 * 60 * 1000)
#define ZONE_LITE_FULL_TRUTH_RECONCILE_MS (6 * 60 * 60 * 1000LL)
#define ZONE_LITE_HISTORY_RETRY_SECONDS (24 * 60 * 60)
#define ZONE_LITE_HISTORY_SWEEP_SECONDS (7 * 24 * 60 * 60)
#define ZONE_LITE_RECOVERY_STABILITY_MS (2 * 60 * 1000)
#define ZONE_LITE_FLAP_WINDOW_MS (15 * 60 * 1000)
#define ZONE_LITE_FLAP_THRESHOLD 3
#define ZONE_LITE_FLAP_QUIET_MS (5 * 60 * 1000)
#define ZONE_LITE_ZKT_BACKOFF_MAX_MS (10 * 60 * 1000)
#define ZONE_LITE_ZKT_USER_REFRESH_RETRIES 3
#define ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS 2000
#define ZONE_LITE_SNTP_SERVER "pool.ntp.org"
#define ZONE_LITE_SNTP_SYNC_TIMEOUT_MS 15000
#define ZONE_LITE_MIN_VALID_UNIX_TIME 1767225600

// Preventive maintenance restarts. When enabled, the ESP32 attempts an
// authenticated protocol restart in each configured local-time window, with a
// bounded recovery-channel fallback only when explicitly configured.
#define ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED 1
#define ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES 300
#define ZONE_LITE_DAILY_ZKT_REBOOT_WINDOW_MINUTES 30
#define ZONE_LITE_DAILY_ZKT_REBOOT_RETRY_DELAY_MS (5 * 60 * 1000)
#define ZONE_LITE_RESTART_SLOT_1_HOUR 2
#define ZONE_LITE_RESTART_SLOT_2_HOUR 12
#define ZONE_LITE_RESTART_SLOT_3_HOUR 22

// ESP32-S3-DevKitC-1 onboard addressable RGB LED. If a board revision or clone
// uses a different RGB pin, override only ZONE_LITE_LED_GPIO in local config.
#define ZONE_LITE_LED_ENABLED 1
#define ZONE_LITE_LED_GPIO 48
#define ZONE_LITE_LED_BRIGHTNESS 96
#define ZONE_LITE_LED_FAULT_LATCH_MS (2 * 60 * 1000)
#define ZONE_LITE_LED_ACTIVITY_FLASH_MS 250

// Optional ZKT OS recovery. Keep disabled unless the attendance device has a
// confirmed telnet account dedicated to controlled recovery.
#define ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED 0
#define ZONE_LITE_ZKT_RECOVERY_FAILURES 2
#define ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS (30 * 60 * 1000)
#define ZONE_LITE_ZKT_REBOOT_WAIT_MS 90000
#define ZONE_LITE_ZKT_TELNET_PORT 23
#define ZONE_LITE_ZKT_TELNET_USERNAME ""
#define ZONE_LITE_ZKT_TELNET_PASSWORD ""
#define ZONE_LITE_ZKT_TELNET_EXPECT_BANNER "Linux"
#define ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND "reboot"
