#pragma once

// Copy this file to zone_lite_config.h for local flashing.
// zone_lite_config.h is intentionally ignored by git because it contains secrets.

#define ZONE_LITE_WIFI_SSID "your-wifi-name"
#define ZONE_LITE_WIFI_PASSWORD "your-wifi-password"

#define ZONE_LITE_ZKT_PORT 4370
#define ZONE_LITE_ZKT_COMM_KEY 0
#define ZONE_LITE_ZONE_DEVICE_ID "1"

// Discovery scans the DHCP subnet and accepts the first host that passes
// ZKT CMD_CONNECT + Comm Key authentication.
#define ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS 450
#define ZONE_LITE_DISCOVERY_RETRY_DELAY_MS 15000
