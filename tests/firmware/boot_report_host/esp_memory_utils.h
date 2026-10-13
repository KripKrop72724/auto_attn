#pragma once
#include <stdbool.h>
#include <stdint.h>
/* The harness decides which pointers a panic may read. */
bool esp_ptr_byte_accessible(const void *p);
static inline bool esp_ptr_in_drom(const void *p) { (void)p; return false; }
/* ESP32-S3 internal DRAM, 16-byte aligned as ESP-IDF requires of a stack. */
static inline bool esp_stack_ptr_is_sane(uint32_t sp)
{
    return sp >= 0x3fc88010U && sp <= 0x3fcffff0U && !(sp & 0xfU);
}
/* ESP32-S3 IRAM and flash-mapped instructions. */
static inline bool esp_ptr_executable(const void *p)
{
    uintptr_t address = (uintptr_t)p;
    return (address >= 0x40370000U && address < 0x403e0000U) || (address >= 0x42000000U && address < 0x44000000U);
}
