#pragma once
#include <stdbool.h>
/* The subset of ESP-IDF 5.5's panic_info_t the hook reads. */
typedef enum {
    PANIC_EXCEPTION_DEBUG, PANIC_EXCEPTION_IWDT, PANIC_EXCEPTION_TWDT, PANIC_EXCEPTION_ABORT, PANIC_EXCEPTION_FAULT,
} panic_exception_t;
typedef struct {
    int core;
    panic_exception_t exception;
    const void *addr;
} panic_info_t;
extern bool g_panic_abort;
extern char *g_panic_abort_details;
