#pragma once
#include <stdbool.h>
/* The harness decides which pointers a panic may read. */
bool esp_ptr_byte_accessible(const void *p);
static inline bool esp_ptr_in_drom(const void *p) { (void)p; return false; }
