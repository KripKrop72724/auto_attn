#include "zkt_storage_mailbox.h"
#include <string.h>

static bool priority(zj_operation_t operation)
{
    return operation == ZJ_APPEND || operation == ZJ_SETTLE;
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
    if (!mailbox || !request || !ticket || (unsigned)request->operation > ZJ_OTA_CHECK) return false;
    if (request->operation == ZJ_OTA_CHECK &&
        (!request->input.ota.version[0] || !memchr(request->input.ota.version, 0, sizeof(request->input.ota.version))))
        return false;
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
    zj_request_slot_t *high = NULL, *low = NULL;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        zj_request_slot_t *slot = &mailbox->slots[i];
        if (slot->state != ZJ_SLOT_QUEUED) continue;
        zj_request_slot_t **candidate = priority(slot->request.operation) ? &high : &low;
        if (!*candidate || slot->ticket < (*candidate)->ticket) *candidate = slot;
    }
    zj_request_slot_t *chosen = high && (!low || mailbox->priority_burst < ZJ_PRIORITY_BURST) ? high : low;
    if (!chosen) return false;
    if (chosen == high) {
        if (mailbox->priority_burst < ZJ_PRIORITY_BURST) ++mailbox->priority_burst;
    } else mailbox->priority_burst = 0;
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
    ++mailbox->completed;
    if (slot->abandoned) release(mailbox, slot);
    else {
        slot->reply = *reply;
        slot->state = ZJ_SLOT_DONE;
    }
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
