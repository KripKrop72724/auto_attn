#pragma once
#include "freertos/FreeRTOS.h"
typedef void *TaskHandle_t;
typedef int BaseType_t;
TaskHandle_t xTaskGetCurrentTaskHandle(void);
TaskHandle_t xTaskGetCurrentTaskHandleForCore(BaseType_t core);
char *pcTaskGetName(TaskHandle_t task);
