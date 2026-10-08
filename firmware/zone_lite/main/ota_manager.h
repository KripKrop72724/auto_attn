#pragma once

#include <stdbool.h>

#include "cJSON.h"

#define ZONE_LITE_OTA_PARTITION_LAYOUT "zone-lite-ota-v1"

void ota_manager_init(void);
void ota_manager_start(void);
void ota_manager_append_telemetry(cJSON *heartbeat);
bool ota_manager_busy(void);
/* Nonblocking exclusive controller claim for one expiring experimental test.
 * The gateway releases it on every abort; successful restart clears RAM. */
bool ota_manager_hil_reboot_capable(void);
bool ota_manager_hil_reboot_reserve(void);
void ota_manager_hil_reboot_release(void);
bool ota_manager_running_image(char output[65]);
