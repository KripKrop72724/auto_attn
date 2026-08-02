#pragma once

#include <stdbool.h>

#include "esp_err.h"

typedef void (*setup_portal_station_visibility_cb_t)(bool connected);

/* Prepare the normally-dormant AP netif before esp_wifi_init(). */
esp_err_t setup_portal_prepare(setup_portal_station_visibility_cb_t visibility_cb);

/* Start the low-priority recovery/BOOT-button controller after esp_wifi_start(). */
esp_err_t setup_portal_start_controller(void);

/* Return true when the portal owns the station transition. */
bool setup_portal_handle_sta_disconnected(void);
bool setup_portal_handle_sta_got_ip(void);

bool setup_portal_active(void);
