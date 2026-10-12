#pragma once

#include <stdbool.h>

#include "cJSON.h"

/* Route every cJSON tree to PSRAM. Call once, before any JSON is built: a
 * heartbeat alone makes about 200 cJSON allocations every 15 s. */
void platform_memory_init(void);

/* Add each capability pool's free, largest and minimum-ever free bytes, the
 * allocation-failure counters and recent failures, and the tasks with the
 * least stack headroom to a heartbeat's diagnostics.memory object. */
bool platform_memory_append(cJSON *memory);
