#pragma once
#include "zkt_storage_mailbox.h"

typedef struct {
    uint64_t (*now_us)(void *);
    void (*wait)(void *);
    bool (*submit)(void *, const zj_request_t *, uint64_t *);
    bool (*poll)(void *, uint64_t, zj_reply_t *, bool *);
    void *context;
} zc_client_port_t;
typedef struct {
    uint64_t pending_ticket;
    uint8_t pending_operation;
    bool active_may_have_changed;
    bool commands;
} zc_client_t;

/* Serialized by the catalog or command caller's lock, never the mailbox lock.
 * A timeout retains at most one reply ticket. Further use of that client waits for
 * that operation, so a timed-out activation cannot race a file reader or a
 * new producer. No request owns caller memory after submit returns. */
bool zc_client_drain(zc_client_t *client, zc_client_port_t port);
zj_result_t zc_client_call(zc_client_t *client, zc_client_port_t port,
                          const zc_request_t *request, zc_reply_t *reply);
