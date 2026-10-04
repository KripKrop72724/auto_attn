#include "zkt_storage_mailbox.h"
#include <string.h>

static bool priority(zj_operation_t operation)
{
    return operation == ZJ_APPEND || operation == ZJ_SETTLE;
}
static bool file_work(zj_operation_t operation)
{
    return operation == ZJ_CATALOG || operation == ZJ_COMMANDS || operation == ZJ_COMMAND_IDS;
}
static zj_request_slot_t *find(zj_mailbox_t *mailbox, uint64_t ticket)
{
    if (!mailbox || !ticket) return NULL;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        zj_request_slot_t *slot = &mailbox->slots[i];
        if (slot->state != ZJ_SLOT_FREE && slot->ticket == ticket) return slot;
    }
    return NULL;
}
static void release(zj_mailbox_t *mailbox, zj_request_slot_t *slot)
{
    memset(slot, 0, sizeof(*slot));
    --mailbox->occupied;
}
void zj_mailbox_init(zj_mailbox_t *mailbox)
{
    if (!mailbox) return;
    memset(mailbox, 0, sizeof(*mailbox));
    mailbox->next_ticket = 1;
}
bool zj_mailbox_submit(zj_mailbox_t *mailbox, const zj_request_t *request, uint64_t *ticket)
{
    if (ticket) *ticket = 0;
    if (!mailbox || !request || !ticket || (unsigned)request->operation > ZJ_COMMAND_IDS) return false;
    if ((request->operation == ZJ_CATALOG || request->operation == ZJ_COMMANDS) &&
        !zc_request_valid(&request->input.catalog)) return false;
    if (request->operation == ZJ_COMMAND_IDS && !zi_request_valid(&request->input.command_ids)) return false;
    if (request->operation == ZJ_RUNTIME_CHECKPOINT &&
        (!request->input.runtime_checkpoint.deadline_us ||
         !runtime_checkpoint_valid(&request->input.runtime_checkpoint.state))) return false;
    if (request->operation == ZJ_LEASE &&
        (!request->input.lease.deadline_us || !zl_lease_valid(&request->input.lease.state))) return false;
    if (request->operation == ZJ_OTA_CHECK &&
        (!request->input.ota.version[0] || !memchr(request->input.ota.version, 0, sizeof(request->input.ota.version))))
        return false;
    if (request->operation == ZJ_SELECT_READER) {
        uint8_t nonzero = 0;
        for (unsigned i = 0; i < 32; ++i) nonzero |= request->input.reader_selection.image_digest[i];
        if (!nonzero || !request->input.reader_selection.deadline_us) return false;
    }
    if (!mailbox->next_ticket || mailbox->occupied == ZJ_REQUEST_SLOTS ||
        (!priority(request->operation) && mailbox->occupied >= ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS)) {
        ++mailbox->refused;
        return false;
    }
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        zj_request_slot_t *slot = &mailbox->slots[i];
        if (slot->state != ZJ_SLOT_FREE) continue;
        slot->state = ZJ_SLOT_QUEUED;
        slot->ticket = mailbox->next_ticket++;
        slot->request = *request;
        *ticket = slot->ticket;
        if (++mailbox->occupied > mailbox->high_watermark) mailbox->high_watermark = mailbox->occupied;
        return true;
    }
    return false;
}
bool zj_mailbox_begin(zj_mailbox_t *mailbox, zj_request_t *request, uint64_t *ticket)
{
    if (ticket) *ticket = 0;
    if (!mailbox || !request || !ticket || mailbox->running_ticket) return false;
    zj_request_slot_t *high = NULL, *low = NULL, *resume = NULL;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        zj_request_slot_t *slot = &mailbox->slots[i];
        if (slot->state != ZJ_SLOT_QUEUED) continue;
        if (slot->ticket == mailbox->resume_ticket) { resume = slot; continue; }
        /* One retained file transaction uses the shared resume slot.
         * Delivery reads/runtime checkpoints may run between its steps;
         * catalog/command file operations wait for the recovery intent. */
        if (mailbox->resume_ticket && file_work(slot->request.operation)) continue;
        zj_request_slot_t **candidate = priority(slot->request.operation) ? &high : &low;
        if (!*candidate || slot->ticket < (*candidate)->ticket) *candidate = slot;
    }
    bool other_low = low != NULL;
    if (resume && (!low || !mailbox->yield_to_other)) low = resume;
    zj_request_slot_t *chosen = high && (!low || mailbox->priority_burst < ZJ_PRIORITY_BURST) ? high : low;
    if (!chosen) return false;
    if (chosen == high) {
        if (mailbox->priority_burst < ZJ_PRIORITY_BURST) ++mailbox->priority_burst;
    } else {
        mailbox->priority_burst = 0;
        if (resume && other_low && chosen != resume) mailbox->yield_to_other = false;
    }
    chosen->state = ZJ_SLOT_RUNNING;
    mailbox->running_ticket = chosen->ticket;
    *ticket = chosen->ticket;
    *request = chosen->request;
    return true;
}
bool zj_mailbox_finish(zj_mailbox_t *mailbox, uint64_t ticket, const zj_reply_t *reply)
{
    zj_request_slot_t *slot = find(mailbox, ticket);
    if (!slot || !reply || slot->state != ZJ_SLOT_RUNNING || mailbox->running_ticket != ticket) return false;
    mailbox->running_ticket = 0;
    if (mailbox->resume_ticket == ticket) mailbox->resume_ticket = 0;
    ++mailbox->completed;
    if (slot->abandoned) release(mailbox, slot);
    else {
        slot->reply = *reply;
        slot->state = ZJ_SLOT_DONE;
    }
    return true;
}
bool zj_mailbox_yield(zj_mailbox_t *mailbox, uint64_t ticket)
{
    zj_request_slot_t *slot = find(mailbox, ticket);
    if (!slot || slot->state != ZJ_SLOT_RUNNING || mailbox->running_ticket != ticket ||
        !file_work(slot->request.operation)) return false;
    slot->state = ZJ_SLOT_QUEUED;
    mailbox->running_ticket = 0;
    mailbox->resume_ticket = ticket;
    mailbox->yield_to_other = true;
    return true;
}
bool zj_mailbox_poll(zj_mailbox_t *mailbox, uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    if (complete) *complete = false;
    zj_request_slot_t *slot = find(mailbox, ticket);
    if (!slot || !reply || !complete || slot->abandoned) return false;
    if (slot->state == ZJ_SLOT_DONE) {
        *reply = slot->reply;
        *complete = true;
        release(mailbox, slot);
    }
    return true;
}
bool zj_mailbox_abandon(zj_mailbox_t *mailbox, uint64_t ticket)
{
    zj_request_slot_t *slot = find(mailbox, ticket);
    if (!slot) return false;
    if (slot->state == ZJ_SLOT_DONE) release(mailbox, slot);
    else slot->abandoned = true;
    return true;
}
