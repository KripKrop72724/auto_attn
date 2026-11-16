/*
 * State Life board hardware test program 1.0.0.
 *
 * The installer writes this program to a new (blank) board, reads its
 * "SLTEST ..." lines on the USB port, then erases it again. The program:
 *   prints BEGIN, runs the psram, iram, flash, cpu and wifi stages (one
 *   RESULT line each, ALIVE heartbeats, ERR lines), prints END with a CRC,
 *   erases the flash blocks it used and prints CLEAN, then restarts into the
 *   ROM download mode so the installer can erase the board without a replug.
 *
 * It never writes eFuses, never enables Secure Boot or flash encryption, and
 * stores nothing that outlives the installer's erase. See README.md.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "board_test.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_psram.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/soc.h"

/* ---------------------------------------------------- two-core workers */

typedef struct {
    sl_job_fn fn;
    void *arg;
    SemaphoreHandle_t go;
    SemaphoreHandle_t done;
} worker_t;

static worker_t s_workers[2];

static void worker_task(void *p)
{
    worker_t *w = p;
    for (;;) {
        xSemaphoreTake(w->go, portMAX_DELAY);
        if (w->fn) {
            w->fn(w->arg);
        }
        xSemaphoreGive(w->done);
    }
}

static void start_workers(void)
{
    for (int core = 0; core < 2; core++) {
        worker_t *w = &s_workers[core];
        w->go = xSemaphoreCreateBinary();
        w->done = xSemaphoreCreateBinary();
        configASSERT(w->go && w->done);
        BaseType_t ok = xTaskCreatePinnedToCore(worker_task, core ? "sl_core1" : "sl_core0",
                                                6144, w, 3, NULL, core);
        configASSERT(ok == pdPASS);
    }
}

void sl_run_on_both(sl_job_fn job0, void *arg0, sl_job_fn job1, void *arg1)
{
    s_workers[0].fn = job0;
    s_workers[0].arg = arg0;
    s_workers[1].fn = job1;
    s_workers[1].arg = arg1;
    xSemaphoreGive(s_workers[0].go);
    xSemaphoreGive(s_workers[1].go);
    /* No timeout: a core that hangs stops the heartbeat, which the installer
     * reports as a hang in this stage. */
    xSemaphoreTake(s_workers[0].done, portMAX_DELAY);
    xSemaphoreTake(s_workers[1].done, portMAX_DELAY);
}

/* --------------------------------------------------------------- main */

static void print_begin(uint32_t *flash_id_out)
{
    uint8_t mac[6] = {0};
    (void)esp_read_mac(mac, ESP_MAC_WIFI_STA);
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    uint32_t id = 0;
    (void)esp_flash_read_id(NULL, &id);
    uint32_t size = 0;
    if (esp_flash_get_physical_size(NULL, &size) != ESP_OK) {
        (void)esp_flash_get_size(NULL, &size);
    }
    size_t psram = esp_psram_is_initialized() ? esp_psram_get_size() : 0;
    *flash_id_out = id;
    sl_line("BEGIN proto=%d fw=%s mac=%02x:%02x:%02x:%02x:%02x:%02x chip_rev=v%d.%d cores=%d "
            "idf=%s flash_id=%06lx flash_size=%lu psram_size=%u",
            SL_PROTO, SL_FW_VERSION, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
            chip.revision / 100, chip.revision % 100, chip.cores, esp_get_idf_version(),
            (unsigned long)id, (unsigned long)size, (unsigned)psram);
}

static void add_failed(char *list, size_t len, const char *stage)
{
    if (strcmp(list, "none") == 0) {
        list[0] = '\0';
    } else {
        strlcat(list, ",", len);
    }
    strlcat(list, stage, len);
}

void app_main(void)
{
    /* Keep IDF's own INFO chatter (Wi-Fi) off the console during the test;
     * start-up lines before app_main are printed at INFO as usual. */
    esp_log_level_set("*", ESP_LOG_WARN);
    sl_out_init();
    start_workers();
    /* Give the installer time to reopen the USB port after the reset. */
    vTaskDelay(pdMS_TO_TICKS(1500));

    uint32_t flash_id = 0;
    print_begin(&flash_id);
    sl_heartbeat_start();

    char failed[64] = "none";
    if (!sl_test_psram()) {
        add_failed(failed, sizeof(failed), "psram");
    }
    if (!sl_test_iram()) {
        add_failed(failed, sizeof(failed), "iram");
    }
    if (!sl_test_flash(flash_id)) {
        add_failed(failed, sizeof(failed), "flash");
    }
    if (!sl_test_cpu()) {
        add_failed(failed, sizeof(failed), "cpu");
    }
    if (!sl_test_wifi()) {
        add_failed(failed, sizeof(failed), "wifi");
    }

    sl_heartbeat_stop();
    sl_end(strcmp(failed, "none") == 0, failed);

    bool clean = sl_flash_clean();
    sl_line("CLEAN result=%s", clean ? "ok" : "fail");
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(300)); /* let the USB port send the last lines */

    /* Restart into the ROM download mode on the same USB port. */
    REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    esp_restart();
}
