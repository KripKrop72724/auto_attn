#pragma once
#include <stddef.h>
#include "nvs.h"
esp_err_t esp_spiffs_info(const char *label, size_t *total, size_t *used);
