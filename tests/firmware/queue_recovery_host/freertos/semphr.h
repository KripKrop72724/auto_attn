#pragma once
#include "freertos/FreeRTOS.h"
/* Mutexes that assert on re-entry, so a lock-order slip fails the test. */
typedef int *SemaphoreHandle_t;
SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t timeout);
BaseType_t xSemaphoreGive(SemaphoreHandle_t mutex);
