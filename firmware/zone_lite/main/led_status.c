#include "led_status.h"

#include <stdint.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "led_strip.h"

#include "zone_lite_config.example.h"

#ifndef ZONE_LITE_LED_ENABLED
#define ZONE_LITE_LED_ENABLED 1
#endif
#ifndef ZONE_LITE_LED_GPIO
#define ZONE_LITE_LED_GPIO 48
#endif
#ifndef ZONE_LITE_LED_BRIGHTNESS
#define ZONE_LITE_LED_BRIGHTNESS 96
#endif
#ifndef ZONE_LITE_LED_FAULT_LATCH_MS
#define ZONE_LITE_LED_FAULT_LATCH_MS (2 * 60 * 1000)
#endif
#ifndef ZONE_LITE_LED_ACTIVITY_FLASH_MS
#define ZONE_LITE_LED_ACTIVITY_FLASH_MS 250
#endif

#define LED_TICK_MS 100
#define LED_STACK_BYTES 3072

static const char *TAG = "led_status";

typedef struct {
    led_status_t base;
    led_status_t latched_fault;
    int64_t latched_until_ms;
    bool has_latched_fault;
    bool has_backlog;
    bool live_flash;
    int64_t live_flash_until_ms;
} led_state_t;

static led_strip_handle_t s_led_strip;
static SemaphoreHandle_t s_led_lock;
static led_state_t s_state = {
    .base = LED_STATUS_BOOTING,
};
static bool s_started;
static bool s_available;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static uint8_t clamp_brightness(void)
{
    if (ZONE_LITE_LED_BRIGHTNESS <= 0) {
        return 1;
    }
    if (ZONE_LITE_LED_BRIGHTNESS > 255) {
        return 255;
    }
    return (uint8_t)ZONE_LITE_LED_BRIGHTNESS;
}

static uint8_t scale(uint8_t value, uint8_t brightness)
{
    return (uint8_t)(((uint16_t)value * brightness) / 255);
}

static int priority_for_status(led_status_t status)
{
    switch (status) {
    case LED_STATUS_FATAL:
        return 100;
    case LED_STATUS_RECOVERY_REBOOT:
        return 90;
    case LED_STATUS_LOCAL_FAILURE:
        return 85;
    case LED_STATUS_ORDS_FAILURE:
        return 80;
    case LED_STATUS_ZKT_FAILURE:
        return 75;
    case LED_STATUS_ZKT_FLAPPING:
        return 74;
    case LED_STATUS_TRUTH_REPAIR:
        return 72;
    case LED_STATUS_BLOCKED_IDENTITY:
        return 70;
    case LED_STATUS_SYNCING:
        return 60;
    case LED_STATUS_BACKLOG:
        return 55;
    case LED_STATUS_ZKT_AUTHENTICATED:
        return 50;
    case LED_STATUS_ZKT_DISCOVERING:
        return 40;
    case LED_STATUS_WIFI_CONNECTING:
        return 30;
    case LED_STATUS_BOOTING:
        return 20;
    case LED_STATUS_HEALTHY:
    default:
        return 10;
    }
}

static bool is_fault_status(led_status_t status)
{
    return status == LED_STATUS_ORDS_FAILURE || status == LED_STATUS_ZKT_FAILURE ||
           status == LED_STATUS_TRUTH_REPAIR || status == LED_STATUS_BLOCKED_IDENTITY ||
           status == LED_STATUS_LOCAL_FAILURE || status == LED_STATUS_FATAL;
}

static void expire_recoverable_fault(led_state_t *state, int64_t tick_ms)
{
    if (state->has_latched_fault && tick_ms >= state->latched_until_ms &&
        state->latched_fault != LED_STATUS_FATAL) {
        state->has_latched_fault = false;
    }
}

static led_status_t select_status(const led_state_t *state, int64_t tick_ms, bool *live_flash)
{
    *live_flash = false;
    led_status_t selected = state->base;
    if (selected == LED_STATUS_HEALTHY && state->has_backlog) {
        selected = LED_STATUS_BACKLOG;
    }
    if (state->has_latched_fault && tick_ms < state->latched_until_ms &&
        priority_for_status(state->latched_fault) > priority_for_status(selected)) {
        selected = state->latched_fault;
    }
    if (state->live_flash && tick_ms < state->live_flash_until_ms &&
        priority_for_status(selected) <= priority_for_status(LED_STATUS_BACKLOG)) {
        *live_flash = true;
    }
    return selected;
}

static void set_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_available) {
        return;
    }
    uint8_t brightness = clamp_brightness();
    esp_err_t err = led_strip_set_pixel(s_led_strip, 0, scale(r, brightness), scale(g, brightness), scale(b, brightness));
    if (err == ESP_OK) {
        err = led_strip_refresh(s_led_strip);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not refresh status LED: %s", esp_err_to_name(err));
    }
}

static void render_status(led_status_t status, bool live_flash, int64_t tick_ms)
{
    if (live_flash) {
        set_rgb(0, 255, 0);
        return;
    }

    int phase = (int)(tick_ms % 2000);
    bool slow_on = (phase % 1000) < 500;
    bool fast_on = (tick_ms % 300) < 150;
    bool heartbeat_on = phase < 120 || (phase >= 260 && phase < 380);
    uint8_t pulse = (uint8_t)((phase < 1000 ? phase : 2000 - phase) * 180 / 1000 + 32);

    switch (status) {
    case LED_STATUS_BOOTING:
        set_rgb(pulse, pulse, pulse);
        break;
    case LED_STATUS_WIFI_CONNECTING:
        set_rgb(0, 0, slow_on ? 255 : 0);
        break;
    case LED_STATUS_ZKT_DISCOVERING:
        set_rgb(0, pulse, pulse);
        break;
    case LED_STATUS_ZKT_AUTHENTICATED:
        set_rgb(0, 255, 255);
        break;
    case LED_STATUS_SYNCING:
        set_rgb(pulse, 0, pulse);
        break;
    case LED_STATUS_HEALTHY:
        set_rgb(0, 255, 0);
        break;
    case LED_STATUS_BACKLOG:
        set_rgb(heartbeat_on ? 255 : 20, heartbeat_on ? 160 : 12, 0);
        break;
    case LED_STATUS_ORDS_FAILURE:
        set_rgb(slow_on ? 255 : 0, slow_on ? 80 : 0, 0);
        break;
    case LED_STATUS_ZKT_FAILURE:
        set_rgb(slow_on ? 255 : 0, slow_on ? 220 : 0, 0);
        break;
    case LED_STATUS_ZKT_FLAPPING:
        set_rgb(fast_on ? 255 : 0, fast_on ? 120 : 40, fast_on ? 0 : 90);
        break;
    case LED_STATUS_TRUTH_REPAIR:
        set_rgb(slow_on ? 255 : 0, slow_on ? 170 : 0, 0);
        break;
    case LED_STATUS_RECOVERY_REBOOT:
        set_rgb(fast_on ? 255 : 0, 0, 0);
        break;
    case LED_STATUS_LOCAL_FAILURE:
        set_rgb(slow_on ? 255 : 0, slow_on ? 24 : 0, 0);
        break;
    case LED_STATUS_FATAL:
        set_rgb(255, 0, 0);
        break;
    case LED_STATUS_BLOCKED_IDENTITY:
        set_rgb(slow_on ? 255 : 0, 0, slow_on ? 255 : 0);
        break;
    default:
        set_rgb(0, 0, 0);
        break;
    }
}

static void led_status_task(void *arg)
{
    (void)arg;
    while (true) {
        led_state_t snapshot;
        int64_t tick_ms = now_ms();
        bool live_flash = false;
        if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            expire_recoverable_fault(&s_state, tick_ms);
            snapshot = s_state;
            xSemaphoreGive(s_led_lock);
        } else {
            memset(&snapshot, 0, sizeof(snapshot));
            snapshot.base = LED_STATUS_FATAL;
        }
        led_status_t status = select_status(&snapshot, tick_ms, &live_flash);
        render_status(status, live_flash, tick_ms);
        vTaskDelay(pdMS_TO_TICKS(LED_TICK_MS));
    }
}

void led_status_init(void)
{
    if (!ZONE_LITE_LED_ENABLED || s_started) {
        return;
    }
    s_led_lock = xSemaphoreCreateMutex();
    if (s_led_lock == NULL) {
        ESP_LOGW(TAG, "Status LED disabled: could not create lock");
        return;
    }

    led_strip_config_t strip_config = {
        .strip_gpio_num = ZONE_LITE_LED_GPIO,
        .max_leds = 1,
    };
    led_strip_rmt_config_t rmt_config = {
        .resolution_hz = 10 * 1000 * 1000,
        .flags = {
            .with_dma = false,
        },
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_config, &rmt_config, &s_led_strip);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Status LED disabled: RMT init failed: %s", esp_err_to_name(err));
        return;
    }
    s_available = true;
    (void)led_strip_clear(s_led_strip);
    BaseType_t task_ok = xTaskCreate(led_status_task, "led_status", LED_STACK_BYTES, NULL, 2, NULL);
    if (task_ok != pdPASS) {
        ESP_LOGW(TAG, "Status LED disabled: could not start task");
        s_available = false;
        (void)led_strip_clear(s_led_strip);
        return;
    }
    s_started = true;
}

void led_status_set(led_status_t status)
{
    if (!s_started || s_led_lock == NULL) {
        return;
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_state.base = status;
        if (status == LED_STATUS_FATAL) {
            s_state.latched_fault = status;
            s_state.has_latched_fault = true;
            s_state.latched_until_ms = now_ms() + ZONE_LITE_LED_FAULT_LATCH_MS;
        }
        xSemaphoreGive(s_led_lock);
    }
}

void led_status_fault(led_status_t status)
{
    if (!s_started || s_led_lock == NULL || !is_fault_status(status)) {
        return;
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_state.latched_fault = status;
        s_state.has_latched_fault = true;
        s_state.latched_until_ms = now_ms() + ZONE_LITE_LED_FAULT_LATCH_MS;
        if (status == LED_STATUS_FATAL) {
            s_state.base = status;
        }
        xSemaphoreGive(s_led_lock);
    }
}

void led_status_clear_fault(led_status_t status)
{
    if (!s_started || s_led_lock == NULL || status == LED_STATUS_FATAL) {
        return;
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        if (s_state.has_latched_fault && s_state.latched_fault == status) {
            s_state.has_latched_fault = false;
        }
        xSemaphoreGive(s_led_lock);
    }
}

void led_status_event(led_status_event_t event)
{
    if (!s_started || s_led_lock == NULL) {
        return;
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        if (event == LED_EVENT_LIVE_PUNCH) {
            s_state.live_flash = true;
            s_state.live_flash_until_ms = now_ms() + ZONE_LITE_LED_ACTIVITY_FLASH_MS;
        }
        xSemaphoreGive(s_led_lock);
    }
}

void led_status_set_backlog(bool has_backlog)
{
    if (!s_started || s_led_lock == NULL) {
        return;
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        s_state.has_backlog = has_backlog;
        xSemaphoreGive(s_led_lock);
    }
}

const char *led_status_current_name(void)
{
    if (!s_started || s_led_lock == NULL) {
        return "UNAVAILABLE";
    }
    if (xSemaphoreTake(s_led_lock, pdMS_TO_TICKS(50)) != pdTRUE) {
        return "STATE_LOCK_BUSY";
    }
    int64_t tick_ms = now_ms();
    bool live_flash = false;
    expire_recoverable_fault(&s_state, tick_ms);
    led_status_t status = select_status(&s_state, tick_ms, &live_flash);
    xSemaphoreGive(s_led_lock);

    switch (status) {
    case LED_STATUS_BOOTING: return "BOOTING";
    case LED_STATUS_WIFI_CONNECTING: return "WIFI_CONNECTING";
    case LED_STATUS_ZKT_DISCOVERING: return "ZKT_DISCOVERING";
    case LED_STATUS_ZKT_AUTHENTICATED: return "ZKT_AUTHENTICATED";
    case LED_STATUS_SYNCING: return "SYNCING";
    case LED_STATUS_HEALTHY: return "HEALTHY";
    case LED_STATUS_BACKLOG: return "BACKLOG";
    case LED_STATUS_ORDS_FAILURE: return "ORDS_FAILURE";
    case LED_STATUS_ZKT_FAILURE: return "ZKT_FAILURE";
    case LED_STATUS_ZKT_FLAPPING: return "ZKT_FLAPPING";
    case LED_STATUS_TRUTH_REPAIR: return "TRUTH_REPAIR";
    case LED_STATUS_RECOVERY_REBOOT: return "RECOVERY_REBOOT";
    case LED_STATUS_LOCAL_FAILURE: return "LOCAL_FAILURE";
    case LED_STATUS_FATAL: return "FATAL";
    case LED_STATUS_BLOCKED_IDENTITY: return "BLOCKED_IDENTITY";
    default: return "UNKNOWN";
    }
}
