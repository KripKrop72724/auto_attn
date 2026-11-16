/*
 * CPU test (both cores compute the same checksum) and Wi-Fi radio test
 * (station mode start + one scan). The Wi-Fi settings are kept in RAM only.
 */
#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "board_test.h"
#include "driver/temperature_sensor.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"

/* ------------------------------------------------------------------ cpu */

#define CPU_ROUNDS 16
#define CPU_LOOPS_PER_ROUND 200000u

typedef struct {
    uint32_t result[2]; /* two runs on the same core */
} cpu_job_t;

static uint32_t cpu_checksum(void)
{
    uint32_t a = 0x12345678u, b = 0x9E3779B9u, c = 0;
    float f = 1.0f;
    for (int round = 0; round < CPU_ROUNDS; round++) {
        for (uint32_t i = 0; i < CPU_LOOPS_PER_ROUND; i++) {
            a = a * 1664525u + 1013904223u;
            b ^= a >> 7;
            b = (b << 5) | (b >> 27);
            b += a * (i | 1u);
            c += (b / ((a & 0xFFFFu) | 1u)) ^ (a % 251u);
            f = f * 1.0000001f + (float)(a & 0xFFu) * 0.0001f;
            if (f > 1.0e6f) {
                f *= 0.5f;
            }
        }
        sl_tick(0);
        sl_yield();
    }
    uint32_t fbits;
    memcpy(&fbits, &f, sizeof(fbits));
    return a ^ b ^ c ^ fbits;
}

static void cpu_job(void *arg)
{
    cpu_job_t *j = arg;
    j->result[0] = cpu_checksum();
    j->result[1] = cpu_checksum();
}

static void read_temperature(char *out, size_t len)
{
    snprintf(out, len, "na");
    temperature_sensor_handle_t h = NULL;
    temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    if (temperature_sensor_install(&cfg, &h) != ESP_OK) {
        return;
    }
    float t = 0;
    if (temperature_sensor_enable(h) == ESP_OK) {
        if (temperature_sensor_get_celsius(h, &t) == ESP_OK) {
            snprintf(out, len, "%.1f", (double)t);
        }
        temperature_sensor_disable(h);
    }
    temperature_sensor_uninstall(h);
}

bool sl_test_cpu(void)
{
    int64_t t0 = sl_now_us();
    sl_stage("cpu", 2);
    sl_errors_reset();
    sl_step(1);
    cpu_job_t core0 = {{0}}, core1 = {{0}};
    sl_run_on_both(cpu_job, &core0, cpu_job, &core1);
    uint32_t want = core0.result[0];
    const uint32_t *all[4] = {&core0.result[0], &core0.result[1], &core1.result[0],
                              &core1.result[1]};
    for (int i = 1; i < 4; i++) {
        if (*all[i] != want) {
            sl_err(i < 2 ? "core0" : "core1", (uint32_t)i, want, *all[i]);
        }
    }
    sl_step(2);
    char temp[16];
    read_temperature(temp, sizeof(temp));
    long ms = (long)((sl_now_us() - t0) / 1000);
    if (sl_errors()) {
        sl_line("RESULT stage=cpu result=fail code=CPU_MISMATCH detail=%s errors=%lu ms=%ld "
                "temp_c=%s",
                sl_first_failed_test(), (unsigned long)sl_errors(), ms, temp);
        return false;
    }
    sl_line("RESULT stage=cpu result=ok code=OK detail=cores_agree errors=0 ms=%ld temp_c=%s "
            "sum=%08lx",
            ms, temp, (unsigned long)want);
    return true;
}

/* ----------------------------------------------------------------- wifi */

#define BIT_STARTED BIT0
#define BIT_SCAN_DONE BIT1
#define SCAN_WAIT_MS 6000
#define MAX_RECORDS 32

static EventGroupHandle_t s_wifi_events;

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)data;
    if (id == WIFI_EVENT_STA_START) {
        xEventGroupSetBits(s_wifi_events, BIT_STARTED);
    } else if (id == WIFI_EVENT_SCAN_DONE) {
        xEventGroupSetBits(s_wifi_events, BIT_SCAN_DONE);
    }
}

/* "init_esp_err_no_mem": the failing step and the error name, snake case. */
static void err_detail(char *out, size_t len, const char *step, esp_err_t err)
{
    int n = snprintf(out, len, "%s_%s", step, esp_err_to_name(err));
    for (int i = 0; i < n && i < (int)len - 1; i++) {
        char ch = out[i];
        out[i] = isalnum((unsigned char)ch) ? (char)tolower((unsigned char)ch) : '_';
    }
}

static bool radio_failed(int64_t t0, const char *step, esp_err_t err)
{
    char detail[64];
    err_detail(detail, sizeof(detail), step, err);
    sl_line("RESULT stage=wifi result=fail code=WIFI_RADIO_FAILED detail=%s errors=1 ms=%ld",
            detail, (long)((sl_now_us() - t0) / 1000));
    return false;
}

bool sl_test_wifi(void)
{
    int64_t t0 = sl_now_us();
    sl_stage("wifi", 3);
    sl_errors_reset();
    sl_step(1);
    s_wifi_events = xEventGroupCreate();
    if (s_wifi_events == NULL) {
        return radio_failed(t0, "events", ESP_ERR_NO_MEM);
    }
    /* NVS only for the radio's calibration data; the installer erases it
     * afterwards. If NVS cannot start, Wi-Fi runs without it. */
    sl_busy(3000);
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        if (nvs_flash_erase() == ESP_OK) {
            err = nvs_flash_init();
        }
    }
    bool nvs_ok = err == ESP_OK;

    if ((err = esp_netif_init()) != ESP_OK) {
        return radio_failed(t0, "netif", err);
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return radio_failed(t0, "event_loop", err);
    }
    if (esp_netif_create_default_wifi_sta() == NULL) {
        return radio_failed(t0, "netif_sta", ESP_FAIL);
    }
    err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL);
    if (err != ESP_OK) {
        return radio_failed(t0, "handler", err);
    }
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    if (!nvs_ok) {
        cfg.nvs_enable = 0;
    }
    sl_busy(5000); /* radio start-up includes the PHY calibration */
    if ((err = esp_wifi_init(&cfg)) != ESP_OK) {
        return radio_failed(t0, "init", err);
    }
    (void)esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if ((err = esp_wifi_set_mode(WIFI_MODE_STA)) != ESP_OK) {
        return radio_failed(t0, "mode", err);
    }
    if ((err = esp_wifi_start()) != ESP_OK) {
        return radio_failed(t0, "start", err);
    }
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, BIT_STARTED, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(3000));
    if (!(bits & BIT_STARTED)) {
        return radio_failed(t0, "start", ESP_ERR_TIMEOUT);
    }

    sl_step(2);
    wifi_scan_config_t scan = {
        .show_hidden = true,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time.active = {.min = 0, .max = 120},
    };
    sl_busy(SCAN_WAIT_MS + 1000);
    if ((err = esp_wifi_scan_start(&scan, false)) != ESP_OK) {
        return radio_failed(t0, "scan", err);
    }
    bits = xEventGroupWaitBits(s_wifi_events, BIT_SCAN_DONE, pdFALSE, pdTRUE,
                               pdMS_TO_TICKS(SCAN_WAIT_MS));
    if (!(bits & BIT_SCAN_DONE)) {
        (void)esp_wifi_scan_stop();
        return radio_failed(t0, "scan", ESP_ERR_TIMEOUT);
    }

    sl_step(3);
    uint16_t found = 0;
    (void)esp_wifi_scan_get_ap_num(&found);
    static wifi_ap_record_t records[MAX_RECORDS];
    uint16_t count = MAX_RECORDS;
    int best = -127;
    if (esp_wifi_scan_get_ap_records(&count, records) == ESP_OK) {
        for (int i = 0; i < count; i++) {
            if (records[i].rssi > best) {
                best = records[i].rssi;
            }
        }
    } else {
        count = 0;
    }
    (void)esp_wifi_clear_ap_list();
    (void)esp_wifi_stop();
    long ms = (long)((sl_now_us() - t0) / 1000);
    if (found == 0) {
        sl_line("RESULT stage=wifi result=warn code=WIFI_NO_NETWORKS detail=0 errors=0 ms=%ld "
                "networks=0",
                ms);
        return true;
    }
    sl_line("RESULT stage=wifi result=ok code=OK detail=scan_ok errors=0 ms=%ld networks=%u "
            "best_rssi=%d",
            ms, (unsigned)found, best);
    return true;
}
