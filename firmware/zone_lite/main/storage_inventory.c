#include "storage_inventory.h"

#include <stdio.h>
#include <string.h>

#include "add_connector.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "zone_storage_paths.h"

/* Boot, terminal session and boot confirmation settle first; the scan then
 * yields to every storage user and stops at its budget. */
#define SI_START_DELAY_MS (60 * 1000)
#define SI_BUDGET_US (180LL * 1000LL * 1000LL)
/* A yield per 32 pages that read back, as in 2.7.4, where a read was a page;
 * a read is now a chunk of data spans, and only a failed chunk is read again
 * by span. */
#define SI_YIELD_EVERY_READS (32U / SI_CHUNK_SPANS)
#define SI_LOGGED_FILES 4U
_Static_assert(SI_YIELD_EVERY_READS * SI_CHUNK_SPANS == 32U, "one yield per 32 pages read back");

static si_report_t s_report;
static bool s_ready;
static int64_t s_deadline_us;
static uint32_t s_reads;

static bool keep_going(void *context)
{
    (void)context;
    if (++s_reads % SI_YIELD_EVERY_READS == 0) vTaskDelay(pdMS_TO_TICKS(2));
    return esp_timer_get_time() < s_deadline_us;
}

static void log_report(void)
{
    char message[192];
    for (size_t index = 0; index < s_report.count && index < SI_LOGGED_FILES; ++index) {
        const si_file_t *file = &s_report.file[index];
        if (!file->first_error) break;
        snprintf(message, sizeof(message),
                 "%s: %lu of %lu bytes unreadable, first at offset %lu (errno %d)", file->name,
                 (unsigned long)file->unreadable_bytes, (unsigned long)file->bytes,
                 (unsigned long)file->first_error_offset, file->first_error);
        (void)add_connector_log("WARN", "storage", "STORAGE_FILE_UNREADABLE", message);
    }
    snprintf(message, sizeof(message), "%lu files, %llu bytes, %lu unreadable files, %llu unreadable bytes%s",
             (unsigned long)s_report.files, (unsigned long long)s_report.bytes,
             (unsigned long)s_report.unreadable_files, (unsigned long long)s_report.unreadable_bytes,
             s_report.complete ? "" : ", scan incomplete");
    (void)add_connector_log(s_report.unreadable_files || !s_report.complete ? "WARN" : "INFO", "storage",
                            "STORAGE_INVENTORY", message);
}

static void inventory_task(void *argument)
{
    (void)argument;
    vTaskDelay(pdMS_TO_TICKS(SI_START_DELAY_MS));
    s_deadline_us = esp_timer_get_time() + SI_BUDGET_US;
    const si_ports_t ports = {.keep_going = keep_going};
    si_scan(ZONE_STORAGE_BASE, &ports, &s_report);
    __atomic_store_n(&s_ready, true, __ATOMIC_RELEASE);
    log_report();
    vTaskDelete(NULL);
}

void storage_inventory_start(void)
{
    static bool started;
    if (started) return;
    started = xTaskCreate(inventory_task, "storage_scan", 4096, NULL, tskIDLE_PRIORITY + 1, NULL) == pdPASS;
}

static void printable(char *text)
{
    for (; *text; ++text) {
        unsigned char value = (unsigned char)*text;
        if (value < 0x20 || value > 0x7e) *text = '?';
    }
}

static bool append_file(cJSON *rows, const si_file_t *source)
{
    si_file_t file = *source;
    printable(file.name);
    cJSON *row = cJSON_CreateObject();
    bool ok = row && cJSON_AddStringToObject(row, "name", file.name[0] ? file.name : "?") &&
              cJSON_AddNumberToObject(row, "bytes", file.bytes) &&
              (!file.first_error ||
               (cJSON_AddNumberToObject(row, "unreadable_bytes", file.unreadable_bytes) &&
                cJSON_AddNumberToObject(row, "first_error", file.first_error) &&
                cJSON_AddNumberToObject(row, "first_error_offset", file.first_error_offset) &&
                /* 2.7.7: the unreadable spans, as the exception compares them. */
                cJSON_AddNumberToObject(row, "unreadable_spans", file.unreadable_spans) &&
                cJSON_AddNumberToObject(row, "last_error_offset", file.last_error_offset) &&
                cJSON_AddNumberToObject(row, "span_digest", file.span_digest) &&
                cJSON_AddBoolToObject(row, "other_unreadable", file.other_unreadable)));
    if (ok && cJSON_AddItemToArray(rows, row)) return true;
    cJSON_Delete(row);
    return false;
}

bool storage_inventory_snapshot(si_report_t *out)
{
    if (!out || !__atomic_load_n(&s_ready, __ATOMIC_ACQUIRE)) return false;
    *out = s_report; /* written once, before s_ready */
    return true;
}

bool storage_inventory_append(cJSON *storage)
{
    if (!__atomic_load_n(&s_ready, __ATOMIC_ACQUIRE)) return true;
    cJSON *inventory = cJSON_AddObjectToObject(storage, "inventory");
    cJSON *rows = inventory && cJSON_AddBoolToObject(inventory, "complete", s_report.complete) &&
                  cJSON_AddNumberToObject(inventory, "files", s_report.files) &&
                  cJSON_AddNumberToObject(inventory, "bytes", (double)s_report.bytes) &&
                  cJSON_AddNumberToObject(inventory, "unreadable_files", s_report.unreadable_files) &&
                  cJSON_AddNumberToObject(inventory, "unreadable_bytes", (double)s_report.unreadable_bytes) &&
                  (!s_report.error || cJSON_AddNumberToObject(inventory, "error", s_report.error))
                  ? cJSON_AddArrayToObject(inventory, "entries") : NULL;
    if (!rows) return false;
    for (size_t index = 0; index < s_report.count; ++index) {
        if (!append_file(rows, &s_report.file[index])) return false;
    }
    return true;
}
