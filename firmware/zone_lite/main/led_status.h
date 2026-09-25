#pragma once

#include <stdbool.h>
#include <stddef.h>

typedef enum {
    LED_STATUS_BOOTING = 0,
    LED_STATUS_WIFI_CONNECTING,
    LED_STATUS_ZKT_DISCOVERING,
    LED_STATUS_ZKT_AUTHENTICATED,
    LED_STATUS_SYNCING,
    LED_STATUS_HEALTHY,
    LED_STATUS_BACKLOG,
    LED_STATUS_ORDS_FAILURE,
    LED_STATUS_ZKT_FAILURE,
    LED_STATUS_ZKT_FLAPPING,
    LED_STATUS_TRUTH_REPAIR,
    LED_STATUS_RECOVERY_REBOOT,
    LED_STATUS_LOCAL_FAILURE,
    LED_STATUS_FATAL,
    LED_STATUS_BLOCKED_IDENTITY,
} led_status_t;

typedef enum {
    LED_EVENT_LIVE_PUNCH = 0,
} led_status_event_t;

void led_status_init(void);
void led_status_set(led_status_t status);
void led_status_fault_at(led_status_t status, const char *file, int line);
#define led_status_fault(status) led_status_fault_at((status), __FILE__, __LINE__)
void led_status_clear_fault(led_status_t status);
void led_status_local_failure_source(char *destination, size_t capacity);
void led_status_event(led_status_event_t event);
void led_status_set_backlog(bool has_backlog);
const char *led_status_current_name(void);
