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

// Discovery scans the DHCP subnet and accepts the first host that passes
// ZKT CMD_CONNECT + Comm Key authentication.
#define ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS 450
#define ZONE_LITE_DISCOVERY_RETRY_DELAY_MS 15000
#define ZONE_LITE_RECONCILE_INTERVAL_MS 60000
