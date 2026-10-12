#pragma once

#include <stdbool.h>

#include "cJSON.h"

/* How far the previous boot got before it ended. Stages only advance. */
typedef enum {
    BOOT_STAGE_NONE = 0,
    BOOT_STAGE_STARTED,
    BOOT_STAGE_NVS_READY,
    BOOT_STAGE_STORAGE_INIT_RETURNED,
    BOOT_STAGE_NETWORK_READY,
    BOOT_STAGE_ADD_CONNECTED,
    BOOT_STAGE_COUNT
} boot_stage_t;

/* Call first in app_main. Reads why the ESP reset and what the previous boot
 * left in RTC memory, then starts this boot's record. RTC memory survives
 * software, panic, watchdog and most brownout resets, not a power cut.
 *
 * The record keeps this boot's uptime current every 10 s, at esp_restart()
 * and at a panic, so the next boot can say how long this one ran. A panic or
 * a failed ESP_ERROR_CHECK also records the exception, faulting address,
 * running task and abort message (the main component links with
 * -Wl,--wrap for esp_panic_handler and _esp_error_check_failed). */
void boot_report_init(void);
void boot_report_stage(boot_stage_t stage);
/* Add diagnostics.boot_report to a heartbeat. */
bool boot_report_append(cJSON *diagnostics);
