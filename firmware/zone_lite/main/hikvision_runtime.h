#pragma once
#include "cJSON.h"
#include <stdbool.h>
void hikvision_gateway_task(void *argument);
void hikvision_append_telemetry(cJSON *payload);
bool hikvision_boot_health_ready(void);
bool hikvision_claim_ota_restart(void);
