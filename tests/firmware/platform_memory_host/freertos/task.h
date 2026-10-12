#pragma once
#include "freertos/FreeRTOS.h"
typedef struct {
    const char *pcTaskName;
    uint32_t usStackHighWaterMark;
} TaskStatus_t;
UBaseType_t uxTaskGetNumberOfTasks(void);
UBaseType_t uxTaskGetSystemState(TaskStatus_t *tasks, UBaseType_t capacity, uint32_t *total_runtime);
