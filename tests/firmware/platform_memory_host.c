/* Host harness for platform_memory.c: JSON allocation routing and the exact
 * heartbeat memory evidence ADD's MemoryDiagnostics schema accepts. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "freertos/task.h"
#include "platform_memory.h"
#include "zkt_memory_diagnostics.h"

/* ---- heap ---- */
static unsigned psram_allocations, internal_allocations, failing_psram;

void *heap_caps_malloc(size_t size, uint32_t caps)
{
    if (caps & MALLOC_CAP_SPIRAM) {
        if (failing_psram) return NULL;
        ++psram_allocations;
    } else {
        ++internal_allocations;
    }
    return malloc(size);
}

void heap_caps_free(void *block) { free(block); }

static size_t pool(uint32_t caps, size_t base)
{
    if (caps & MALLOC_CAP_SPIRAM) return base * 100;
    if (caps & MALLOC_CAP_DMA) return base / 2;
    return base;
}
size_t heap_caps_get_free_size(uint32_t caps) { return pool(caps, 60000); }
size_t heap_caps_get_largest_free_block(uint32_t caps) { return pool(caps, 28000); }
size_t heap_caps_get_minimum_free_size(uint32_t caps) { return pool(caps, 20000); }

/* ---- tasks ---- */
static TaskStatus_t task_table[20];
static UBaseType_t task_count;

UBaseType_t uxTaskGetNumberOfTasks(void) { return task_count; }
UBaseType_t uxTaskGetSystemState(TaskStatus_t *tasks, UBaseType_t capacity, uint32_t *total_runtime)
{
    (void)total_runtime;
    if (capacity < task_count) return 0;
    memcpy(tasks, task_table, task_count * sizeof(*tasks));
    return task_count;
}

/* ---- allocation-failure evidence ---- */
static zmd_counts_t counts = {.failures = 7, .dropped = 2};
static zmd_sample_t pending[] = {{1024, 8}, {86, 8}, {446, 2060}};
static unsigned pending_count = 3;
static int failure_hook_ready = 1;

bool zkt_memory_diag_counts(zmd_counts_t *out)
{
    if (!failure_hook_ready) return false;
    *out = counts;
    return true;
}

unsigned zkt_memory_diag_take(zmd_sample_t *samples, unsigned max)
{
    unsigned taken = pending_count < max ? pending_count : max;
    memcpy(samples, pending, taken * sizeof(*samples));
    pending_count = 0;
    return taken;
}

/* ---- the cJSON subset ---- */
static cJSON_Hooks hooks = {malloc, free};
void cJSON_InitHooks(cJSON_Hooks *installed) { hooks = *installed; }

static cJSON *node(const char *key)
{
    cJSON *item = hooks.malloc_fn(sizeof(*item));
    if (!item) return NULL;
    memset(item, 0, sizeof(*item));
    if (key) {
        item->key = hooks.malloc_fn(strlen(key) + 1);
        strcpy(item->key, key);
    }
    return item;
}

static void attach(cJSON *parent, cJSON *child)
{
    cJSON **slot = &parent->child;
    while (*slot) slot = &(*slot)->next;
    *slot = child;
}

cJSON *cJSON_CreateObject(void) { return node(NULL); }
cJSON *cJSON_AddObjectToObject(cJSON *object, const char *key)
{
    cJSON *item = node(key);
    if (item) attach(object, item);
    return item;
}
cJSON *cJSON_AddArrayToObject(cJSON *object, const char *key)
{
    cJSON *item = cJSON_AddObjectToObject(object, key);
    if (item) item->is_array = 1;
    return item;
}
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *key, double number)
{
    cJSON *item = cJSON_AddObjectToObject(object, key);
    if (item) item->number = number;
    return item;
}
cJSON *cJSON_AddStringToObject(cJSON *object, const char *key, const char *text)
{
    cJSON *item = cJSON_AddObjectToObject(object, key);
    if (item) {
        item->is_string = 1;
        item->text = hooks.malloc_fn(strlen(text) + 1);
        strcpy(item->text, text);
    }
    return item;
}
cJSON_bool cJSON_AddItemToArray(cJSON *array, cJSON *item)
{
    if (!array || !item) return 0;
    attach(array, item);
    return 1;
}
void cJSON_Delete(cJSON *item)
{
    while (item) {
        cJSON *next = item->next;
        cJSON_Delete(item->child);
        hooks.free_fn(item->key);
        hooks.free_fn(item->text);
        hooks.free_fn(item);
        item = next;
    }
}

static void print(const cJSON *item, char *out, size_t size, int in_array)
{
    size_t length = strlen(out);
    if (!in_array && item->key) length += (size_t)snprintf(out + length, size - length, "\"%s\":", item->key);
    if (item->is_string) {
        snprintf(out + length, size - length, "\"%s\"", item->text);
    } else if (item->child || item->is_array || !item->key) {
        int array = item->is_array;
        snprintf(out + length, size - length, "%c", array ? '[' : '{');
        for (const cJSON *child = item->child; child; child = child->next) {
            print(child, out, size, array);
            if (child->next) strncat(out, ",", size - strlen(out) - 1);
        }
        strncat(out, array ? "]" : "}", size - strlen(out) - 1);
    } else {
        snprintf(out + length, size - length, "%.0f", item->number);
    }
}

char *cJSON_PrintUnformatted(const cJSON *item)
{
    char *out = calloc(1, 8192);
    print(item, out, 8192, 1); /* a printed root never carries its own key */
    return out;
}

static char *memory_json(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *memory = cJSON_AddObjectToObject(root, "memory");
    assert(platform_memory_append(memory));
    char *text = cJSON_PrintUnformatted(memory);
    cJSON_Delete(root);
    return text;
}

int main(void)
{
    /* JSON trees go to PSRAM once the hooks are installed. */
    platform_memory_init();
    unsigned before = psram_allocations;
    cJSON *probe = cJSON_CreateObject();
    assert(psram_allocations == before + 1 && internal_allocations == 0);
    cJSON_Delete(probe);
    failing_psram = 1;
    probe = cJSON_CreateObject();
    assert(probe && internal_allocations == 1); /* internal RAM only as a fallback */
    cJSON_Delete(probe);
    failing_psram = 0;

    /* Twenty tasks: only the sixteen with the least headroom are reported. */
    static char names[20][16];
    for (unsigned index = 0; index < 20; ++index) {
        snprintf(names[index], sizeof(names[index]), "task%02u", index);
        task_table[index] = (TaskStatus_t){names[index], 4000 - 100 * index};
    }
    task_count = 20;
    char *text = memory_json();
    printf("%s\n", text);
    fflush(stdout);
    const char *expected_prefix =
        "{\"internal_free_bytes\":60000,\"internal_largest_block_bytes\":28000,\"internal_min_free_bytes\":20000,"
        "\"dma_free_bytes\":30000,\"dma_largest_block_bytes\":14000,\"dma_min_free_bytes\":10000,"
        "\"psram_free_bytes\":6000000,\"psram_largest_block_bytes\":2800000,\"psram_min_free_bytes\":2000000,"
        "\"alloc_failures\":7,\"alloc_failures_dropped\":2,"
        "\"recent_alloc_failures\":[{\"bytes\":1024,\"caps\":8},{\"bytes\":86,\"caps\":8},{\"bytes\":446,\"caps\":2060}],"
        "\"stacks\":[{\"name\":\"task19\",\"free_bytes\":2100},";
    assert(strncmp(text, expected_prefix, strlen(expected_prefix)) == 0);
    assert(strstr(text, "\"task04\"") && !strstr(text, "\"task03\""));
    free(text);

    /* Samples are reported once; a family without the failure hook omits
     * the counters instead of reporting false zeros. */
    failure_hook_ready = 0;
    text = memory_json();
    assert(!strstr(text, "alloc_failures") && strstr(text, "\"stacks\""));
    free(text);
    puts("platform memory host tests passed");
    return 0;
}
