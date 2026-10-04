#pragma once
#include "zkt_storage_mailbox.h"
#include "zkt_custody_wire.h"

typedef enum {
    ZJ_DELIVERY_IDLE, ZJ_DELIVERY_SUBMIT_READ, ZJ_DELIVERY_WAIT,
    ZJ_DELIVERY_ENCODE, ZJ_DELIVERY_SEND, ZJ_DELIVERY_SUBMIT_SETTLE,
    ZJ_DELIVERY_SUBMIT_RECLAIM, ZJ_DELIVERY_ABANDON
} zj_delivery_phase_t;
typedef struct {
    uint32_t (*now_ms)(void *context);
    uint32_t (*random)(void *context);
    bool (*connected)(void *context);
    bool (*submit)(void *context, const zj_request_t *request, uint64_t *ticket);
    bool (*poll)(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete);
    bool (*abandon)(void *context, uint64_t ticket);
    bool (*send)(void *context, const char *payload, uint32_t timeout_ms, uint8_t receipt_digest[32]);
    void *context;
    zj_crypto_port_t crypto;
} zj_delivery_port_t;
typedef struct {
    zj_delivery_phase_t phase;
    zj_operation_t pending_operation;
    uint32_t phase_started_ms, progress_ms, wait_started_ms, wait_ms, consecutive_failures;
    uint64_t pending_ticket, receipts, settled, reclaimed, failures, timeouts, refused;
    zj_result_t last_result;
    const char *last_failure;
} zj_delivery_health_t;
typedef struct {
    zj_delivery_port_t port;
    zj_delivery_health_t health;
    zj_item_t item;
    zj_request_t request;
    zj_reply_t reply;
    zj_custody_expected_t expected;
    uint8_t receipt_digest[32];
    char payload[ZJ_CUSTODY_PAYLOAD_MAX];
    bool initialized, verified_empty;
} zj_delivery_t;

bool zj_delivery_init(zj_delivery_t *delivery, zj_delivery_port_t port);
/* One bounded submission, poll, encoding or network exchange per step. The
 * caller owns this state and must never run concurrent steps. No filesystem
 * handle or storage lock crosses the network port. */
void zj_delivery_step(zj_delivery_t *delivery);
/* Drive at most eight transitions and one network exchange. The two-ms
 * cooperative budget is checked between operations; it cannot preempt an
 * individual owner/network port call. Observe each phase before entering it
 * so a blocked SEND remains visible. Return a suggested delay in ms; the
 * task must always yield at least one scheduler tick. */
uint32_t zj_delivery_pump(zj_delivery_t *delivery,
    void (*observe)(const zj_delivery_t *delivery, void *context), void *context);
