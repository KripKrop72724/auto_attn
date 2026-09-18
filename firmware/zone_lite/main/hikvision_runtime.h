#pragma once
#include "cJSON.h"
void hikvision_gateway_task(void *argument);
void hikvision_append_telemetry(cJSON *payload);
