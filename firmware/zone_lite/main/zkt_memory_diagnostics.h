#pragma once

/* Numeric UART evidence only. These observations do not authorize an upgrade
 * or change allocation, capture, storage, or recovery policy. */
typedef enum {
    ZMD_BOOT = 0,
    ZMD_STORAGE_INIT_RETURNED,
    ZMD_STACK_RESERVE_BEGIN,
    ZMD_STACK_RESERVE_OK,
    ZMD_STACK_RESERVE_FAILED,
    ZMD_ADD_TRANSPORT_FAILURE,
    ZMD_ORDS_TRANSPORT_FAILURE,
    ZMD_STAGE_COUNT
} zmd_stage_t;

#include <stdbool.h>
#include <stdint.h>

/* Allocation-failure evidence for the heartbeat. Counters are per boot and
 * wrap modulo 2^32; incomplete means a contended update may have been lost. */
typedef struct {
    uint32_t failures, dropped;
    bool incomplete;
} zmd_counts_t;
typedef struct {
    uint32_t bytes, caps;
} zmd_sample_t;

#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
static inline void zkt_memory_diag_init(void) {}
static inline void zkt_memory_diag_report(zmd_stage_t stage, int error)
{ (void)stage; (void)error; }
static inline bool zkt_memory_diag_counts(zmd_counts_t *counts) { (void)counts; return false; }
static inline unsigned zkt_memory_diag_take(zmd_sample_t *samples, unsigned max)
{ (void)samples; (void)max; return 0; }
#else
void zkt_memory_diag_init(void);
void zkt_memory_diag_report(zmd_stage_t stage, int error);
/* False until the failure hook is registered. */
bool zkt_memory_diag_counts(zmd_counts_t *counts);
/* Takes up to max recorded failures, freeing their slots for new ones. */
unsigned zkt_memory_diag_take(zmd_sample_t *samples, unsigned max);
#endif
