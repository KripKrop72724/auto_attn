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

#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION
static inline void zkt_memory_diag_init(void) {}
static inline void zkt_memory_diag_report(zmd_stage_t stage, int error)
{ (void)stage; (void)error; }
#else
void zkt_memory_diag_init(void);
void zkt_memory_diag_report(zmd_stage_t stage, int error);
#endif
