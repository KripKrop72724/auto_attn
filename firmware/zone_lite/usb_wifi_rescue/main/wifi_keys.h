#pragma once
#include <stdbool.h>
#include "nvs.h"

/* Caller has verified device/partition identity and the provisioned namespace.
 * Never replace a nonempty SSID: the operator may already have corrected it.
 * Password is written first; SSID last, making an interrupted repair retryable.
 * No erase API is used, and no key outside this pair is written. */
esp_err_t rescue_wifi_keys(nvs_handle_t handle, const char *ssid,
                          const char *password, bool *changed);
/* Explicit operator-authorized replacement of this same two-key pair. */
esp_err_t rescue_wifi_keys_exact(nvs_handle_t handle, const char *ssid,
                                const char *password, bool *changed);
