#include "platform_memory.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "zkt_memory_diagnostics.h"

/* The tasks closest to overflow are what ADD needs; a full list would double
 * the heartbeat for no extra signal. */
#define PLATFORM_STACK_REPORT_MAX 16U
#define PLATFORM_FAILURE_SAMPLES 4U

static void *json_malloc(size_t size)
{
    /* JSON is CPU-only data, so PSRAM is always safe; fall back to internal
     * RAM only when PSRAM itself is exhausted. */
    void *block = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return block ? block : heap_caps_malloc(size, MALLOC_CAP_8BIT);
}

static void json_free(void *block)
{
    heap_caps_free(block);
}

void platform_memory_init(void)
{
    cJSON_Hooks hooks = {.malloc_fn = json_malloc, .free_fn = json_free};
    cJSON_InitHooks(&hooks);
}

static bool add_pool(cJSON *memory, const char *prefix, uint32_t caps)
{
    /* Each value is sampled independently; together they are not an atomic
     * heap snapshot. */
    const struct {
        const char *suffix;
        size_t value;
    } rows[] = {
        {"free_bytes", heap_caps_get_free_size(caps)},
        {"largest_block_bytes", heap_caps_get_largest_free_block(caps)},
        {"min_free_bytes", heap_caps_get_minimum_free_size(caps)},
    };
    char key[48];
    for (size_t index = 0; index < sizeof(rows) / sizeof(rows[0]); ++index) {
        snprintf(key, sizeof(key), "%s_%s", prefix, rows[index].suffix);
        if (!cJSON_AddNumberToObject(memory, key, (double)rows[index].value)) return false;
    }
    return true;
}

static bool add_failures(cJSON *memory)
{
    zmd_counts_t counts;
    if (!zkt_memory_diag_counts(&counts)) return true; /* no failure hook on this family */
    if (!cJSON_AddNumberToObject(memory, "alloc_failures", (double)counts.failures) ||
        !cJSON_AddNumberToObject(memory, "alloc_failures_dropped", (double)counts.dropped)) return false;
    zmd_sample_t samples[PLATFORM_FAILURE_SAMPLES];
    unsigned taken = zkt_memory_diag_take(samples, PLATFORM_FAILURE_SAMPLES);
    cJSON *recent = cJSON_AddArrayToObject(memory, "recent_alloc_failures");
    if (!recent) return false;
    for (unsigned index = 0; index < taken; ++index) {
        cJSON *row = cJSON_CreateObject();
        if (!row || !cJSON_AddNumberToObject(row, "bytes", (double)samples[index].bytes) ||
            !cJSON_AddNumberToObject(row, "caps", (double)samples[index].caps) ||
            !cJSON_AddItemToArray(recent, row)) {
            cJSON_Delete(row);
            return false;
        }
    }
    return true;
}

#if configUSE_TRACE_FACILITY
static int least_headroom_first(const void *left, const void *right)
{
    const TaskStatus_t *a = left, *b = right;
    return (a->usStackHighWaterMark > b->usStackHighWaterMark) -
           (a->usStackHighWaterMark < b->usStackHighWaterMark);
}

static bool add_stacks(cJSON *memory)
{
    /* Tasks can start between the count and the snapshot; leave headroom. */
    UBaseType_t capacity = uxTaskGetNumberOfTasks() + 4U;
    TaskStatus_t *tasks = heap_caps_malloc(capacity * sizeof(*tasks), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!tasks) return true; /* evidence only; never fail the heartbeat for it */
    UBaseType_t count = uxTaskGetSystemState(tasks, capacity, NULL);
    qsort(tasks, count, sizeof(*tasks), least_headroom_first);
    cJSON *stacks = cJSON_AddArrayToObject(memory, "stacks");
    bool ok = stacks != NULL;
    for (UBaseType_t index = 0; ok && index < count && index < PLATFORM_STACK_REPORT_MAX; ++index) {
        cJSON *row = cJSON_CreateObject();
        /* ESP-IDF reports stack high-water marks in bytes. */
        ok = row && cJSON_AddStringToObject(row, "name", tasks[index].pcTaskName) &&
             cJSON_AddNumberToObject(row, "free_bytes", (double)tasks[index].usStackHighWaterMark) &&
             cJSON_AddItemToArray(stacks, row);
        if (!ok) cJSON_Delete(row);
    }
    heap_caps_free(tasks);
    return ok;
}
#else
static bool add_stacks(cJSON *memory)
{
    (void)memory;
    return true;
}
#endif

bool platform_memory_append(cJSON *memory)
{
    return add_pool(memory, "internal", MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) &&
           add_pool(memory, "dma", MALLOC_CAP_DMA) &&
           add_pool(memory, "psram", MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) &&
           add_failures(memory) && add_stacks(memory);
}
