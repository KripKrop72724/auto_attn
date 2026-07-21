#pragma once

#include <stdbool.h>

#include "cJSON.h"

#define ZONE_LITE_OTA_PARTITION_LAYOUT "zone-lite-ota-v1"

void ota_manager_init(void);
void ota_manager_start(void);
void ota_manager_append_telemetry(cJSON *heartbeat);
bool ota_manager_busy(void);
