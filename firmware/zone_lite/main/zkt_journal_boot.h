#pragma once
#include "zkt_storage_owner.h"
#include "zkt_journal_transport.h"

typedef enum { ZJ_BOOT_DISABLED, ZJ_BOOT_BRIDGE, ZJ_BOOT_WRITER } zj_boot_mode_t;
typedef enum {
    ZJ_BOOT_OFF, ZJ_BOOT_SECURITY_HOLD, ZJ_BOOT_BINDING_HOLD, ZJ_BOOT_STORAGE_WAIT,
    ZJ_BOOT_OWNER_START, ZJ_BOOT_RECOVERING, ZJ_BOOT_TRANSPORT_START,
    ZJ_BOOT_CHECKING_READER, ZJ_BOOT_READER_HOLD, ZJ_BOOT_CAPTURE_START,
    ZJ_BOOT_WRITER_DISABLED, ZJ_BOOT_READY, ZJ_BOOT_STALLED, ZJ_BOOT_QUIESCING,
    ZJ_BOOT_AUTHORITY_HOLD, ZJ_BOOT_BRIDGE_VALIDATION
} zj_boot_phase_t;
typedef struct {
    bool (*owner_start)(void *, const char *);
    bool (*owner_health)(void *, zj_owner_health_t *);
    bool (*transport_start)(void *);
    bool (*transport_health)(void *, zj_transport_health_t *);
    bool (*capture_start)(void *);
    bool (*submit)(void *, const zj_request_t *, uint64_t *);
    bool (*poll)(void *, uint64_t, zj_reply_t *, bool *);
    bool (*abandon)(void *, uint64_t);
    void *context;
} zj_boot_port_t;
typedef struct {
    uint32_t now_ms;
    zj_boot_mode_t mode;
    const char *terminal_serial;
    bool secure, storage_ready, writer_build, bridge_validation_pending;
    /* Availability permits recovery work; it never grants capture/boot proof. */
    bool storage_available;
} zj_boot_input_t;
typedef struct {
    zj_boot_mode_t mode;
    zj_boot_phase_t phase;
    bool owner_started, transport_started, capture_started, reader_ready, writer_ready;
    bool binding_changed;
    bool bridge_validation_pending;
    zj_delivery_authority_t delivery_authority;
    uint32_t sampled_ms, progress_ms, start_attempts, owner_starts, transport_starts, capture_starts;
    uint32_t proof_attempts, failures, next_attempt_ms, retry_delay_ms, ticket_started_ms;
    uint64_t ticket;
    char terminal_serial[81];
    zj_compat_result_t compatibility;
} zj_boot_t;

/* One caller owns this state. Each step is bounded by its port operations;
 * accepted requests outlive caller timeout. Never delete/restart a task that
 * may own storage. The port publishes snapshots separately for other tasks. */
void zj_boot_step(zj_boot_t *, zj_boot_port_t, const zj_boot_input_t *);
bool zj_boot_local_ready(const zj_boot_t *, uint32_t now_ms);
const char *zj_boot_phase_name(zj_boot_phase_t);
