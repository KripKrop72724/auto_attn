#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    uint32_t pc, sp, next_pc;
    const void *exc_frame;
} esp_backtrace_frame_t;
/* The harness supplies the stack the walk reads. */
bool esp_backtrace_get_next_frame(esp_backtrace_frame_t *frame);
