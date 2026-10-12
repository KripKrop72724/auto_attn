#pragma once
#include <stdint.h>
static inline intptr_t esp_cpu_get_call_addr(intptr_t return_address) { return return_address - 3; }
