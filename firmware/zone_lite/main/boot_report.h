#pragma once

#include <stdbool.h>

#include "cJSON.h"
#include "esp_system.h"

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
 * -Wl,--wrap for esp_panic_handler and _esp_error_check_failed). A panic
 * also keeps what ESP-IDF prints on the console: the panic reason, the cache
 * error and fault address, and each core's task, PC and backtrace. */
void boot_report_init(void);
void boot_report_stage(boot_stage_t stage);
/* ZKT 2.7.5 boot storage repair: its phase and check pass, whether its
 * deadline is restarting the ESP, and whether it returned, kept for the next
 * boot (RTC memory survives every reset except a power cut). */
void boot_report_repair(unsigned phase, unsigned pass, bool deadline);
void boot_report_repair_returned(void);
bool boot_report_previous_repair(unsigned *phase, unsigned *pass, bool *deadline, bool *returned);
esp_reset_reason_t boot_report_reset_reason(void);
/* Add diagnostics.boot_report to a heartbeat. */
bool boot_report_append(cJSON *diagnostics);
