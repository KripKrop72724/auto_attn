#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <pthread.h>

typedef int esp_err_t;
typedef unsigned nvs_handle_t;
typedef struct { char project_name[32], version[32]; } esp_app_desc_t;
const esp_app_desc_t *esp_app_get_description(void);
typedef pthread_mutex_t *SemaphoreHandle_t;
typedef pthread_t *TaskHandle_t;
typedef uint8_t StackType_t;
typedef struct { unsigned used; } StaticTask_t;
#define DRAM_ATTR
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define ESP_ERR_INVALID_SIZE 2
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define pdTRUE 1
#define pdPASS 1
#define portMAX_DELAY UINT32_MAX
#define pdMS_TO_TICKS(ms) (ms)
int64_t esp_timer_get_time(void);
void esp_fill_random(void *, size_t);
void *heap_caps_calloc(size_t, size_t, unsigned);
void heap_caps_free(void *);
void mbedtls_platform_zeroize(void *, size_t);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
void xSemaphoreGive(SemaphoreHandle_t);
void vSemaphoreDelete(SemaphoreHandle_t);
int xTaskCreate(void (*)(void *), const char *, unsigned, void *, unsigned, TaskHandle_t *);
TaskHandle_t xTaskCreateStatic(void (*)(void *), const char *, unsigned, void *, unsigned, StackType_t *, StaticTask_t *);
void vTaskDelay(unsigned);
unsigned ulTaskNotifyTake(int, unsigned);
void xTaskNotifyGive(TaskHandle_t);
TaskHandle_t xTaskGetCurrentTaskHandle(void);
esp_err_t nvs_open(const char *, int, nvs_handle_t *);
void nvs_close(nvs_handle_t);
esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *);
esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t);
esp_err_t nvs_commit(nvs_handle_t);
