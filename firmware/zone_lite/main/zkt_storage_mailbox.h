#pragma once
#include "zkt_journal_store.h"
#include "zkt_journal_compat.h"
#include "runtime_checkpoint.h"
#include "zkt_catalog_store.h"
#include "zkt_lease_store.h"
#include "zkt_command_ids.h"
#include "zkt_segmented_owner.h"

#define ZJ_REQUEST_SLOTS 8U
#define ZJ_LIVE_RESERVED_SLOTS 3U
#define ZJ_PRIORITY_BURST 8U

typedef enum { ZJ_APPEND, ZJ_SETTLE, ZJ_PEEK, ZJ_RECLAIM, ZJ_READER_CHECK, ZJ_OTA_CHECK,
    ZJ_SELECT_READER, ZJ_RUNTIME_CHECKPOINT, ZJ_CATALOG, ZJ_LEASE, ZJ_COMMANDS, ZJ_COMMAND_IDS,
    ZJ_SEGMENTED_QUEUE } zj_operation_t;
typedef struct {
    zj_operation_t operation;
    union {
        zj_observation_t observation;
        zc_request_t catalog;
        zi_request_t command_ids;
        zq_request_t segmented;
        struct {
            zj_token_t token;
            uint8_t receipt_digest[32];
            char observation_id[65], payload_digest[65];
        } settlement;
        struct {
            uint32_t address, size;
            char version[32];
        } ota;
        struct {
            uint8_t image_digest[32];
            uint64_t deadline_us;
        } reader_selection;
        struct {
            runtime_checkpoint_t state;
            uint64_t deadline_us;
        } runtime_checkpoint;
        struct {
            zl_lease_record_t state;
            uint64_t deadline_us;
        } lease;
    } input;
} zj_request_t;
typedef struct {
    zj_result_t result;
    zj_compat_result_t compatibility;
    uint64_t capture_sequence;
    union {
        zj_item_t item;
        zc_reply_t catalog;
        zi_reply_t command_ids;
        zq_reply_t segmented;
    };
    runtime_checkpoint_t runtime_checkpoint;
    zl_lease_record_t lease;
} zj_reply_t;
_Static_assert(sizeof(zc_request_t) <= sizeof(zj_observation_t), "Catalog requests must fit the existing bounded request envelope");
_Static_assert(sizeof(zc_reply_t) <= sizeof(zj_item_t), "Catalog replies must fit the existing bounded reply envelope");
_Static_assert(sizeof(zi_request_t) <= sizeof(zj_observation_t), "Command IDs must fit the bounded request envelope");
_Static_assert(sizeof(zi_reply_t) <= sizeof(zj_item_t), "Command ID replies must fit the bounded reply envelope");
_Static_assert(sizeof(zq_request_t) <= sizeof(zj_observation_t), "Queue copies must fit the bounded request envelope");
_Static_assert(sizeof(zq_reply_t) <= sizeof(zj_item_t), "Queue copies must fit the bounded reply envelope");
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
    uint64_t resume_ticket;
    bool yield_to_other;
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
/* Only resumable file work may yield. The copied input, ticket and caller
 * abandonment survive; live work is scheduled before the next bounded step. */
bool zj_mailbox_yield(zj_mailbox_t *mailbox, uint64_t ticket);
bool zj_mailbox_poll(zj_mailbox_t *mailbox, uint64_t ticket, zj_reply_t *reply, bool *complete);
/* Abandon only releases the reply. Accepted work still executes, including a
 * queued append. A caller retry must rely on source-occurrence deduplication. */
bool zj_mailbox_abandon(zj_mailbox_t *mailbox, uint64_t ticket);
