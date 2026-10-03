#pragma once
#include "zkt_journal_store.h"
#include "zkt_journal_compat.h"

#define ZJ_REQUEST_SLOTS 8U
#define ZJ_LIVE_RESERVED_SLOTS 3U
#define ZJ_PRIORITY_BURST 8U

typedef enum { ZJ_APPEND, ZJ_SETTLE, ZJ_PEEK, ZJ_RECLAIM, ZJ_READER_CHECK, ZJ_OTA_CHECK } zj_operation_t;
typedef struct {
    zj_operation_t operation;
    union {
        zj_observation_t observation;
        struct {
            zj_token_t token;
            uint8_t receipt_digest[32];
            char observation_id[65], payload_digest[65];
        } settlement;
        struct {
            uint32_t address, size;
            char version[32];
        } ota;
    } input;
} zj_request_t;
typedef struct {
    zj_result_t result;
    zj_compat_result_t compatibility;
    uint64_t capture_sequence;
    zj_item_t item;
} zj_reply_t;
typedef enum { ZJ_SLOT_FREE, ZJ_SLOT_QUEUED, ZJ_SLOT_RUNNING, ZJ_SLOT_DONE } zj_slot_state_t;
typedef struct {
    zj_slot_state_t state;
    bool abandoned;
    uint64_t ticket;
    zj_request_t request;
    zj_reply_t reply;
} zj_request_slot_t;
typedef struct {
    zj_request_slot_t slots[ZJ_REQUEST_SLOTS];
    uint64_t next_ticket, running_ticket, completed, refused;
    unsigned occupied, high_watermark, priority_burst;
} zj_mailbox_t;

/* All methods execute under the owner's short mailbox lock. begin/finish are
 * for one storage task only; filesystem work occurs OUTSIDE this lock. Input
 * and output are copied: timed-out callers cannot leave dangling pointers.
 * Submit proves only bounded RAM admission, never durable preservation. */
void zj_mailbox_init(zj_mailbox_t *mailbox);
bool zj_mailbox_submit(zj_mailbox_t *mailbox, const zj_request_t *request, uint64_t *ticket);
bool zj_mailbox_begin(zj_mailbox_t *mailbox, zj_request_t *request, uint64_t *ticket);
bool zj_mailbox_finish(zj_mailbox_t *mailbox, uint64_t ticket, const zj_reply_t *reply);
bool zj_mailbox_poll(zj_mailbox_t *mailbox, uint64_t ticket, zj_reply_t *reply, bool *complete);
/* Abandon only releases the reply. Accepted work still executes, including a
 * queued append. A caller retry must rely on source-occurrence deduplication. */
bool zj_mailbox_abandon(zj_mailbox_t *mailbox, uint64_t ticket);
