#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* Order-independent logical snapshot of all keys except the Wi-Fi pair.
 * Values stay in bounded RAM and are never printed or exported. */
esp_err_t rescue_nvs_snapshot(uint8_t digest[32], size_t *key_count);
