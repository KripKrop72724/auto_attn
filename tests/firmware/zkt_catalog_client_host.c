#include "zkt_catalog_client.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static zj_mailbox_t mailbox;
static uint64_t now = 1;
static bool stalled, poll_failed;
static unsigned submissions;
static uint64_t clock_us(void *context) { (void)context; return now; }
static void wait_step(void *context) { (void)context; now += 10; }
static bool submit(void *context, const zj_request_t *request, uint64_t *ticket)
{
    (void)context; ++submissions;
    return zj_mailbox_submit(&mailbox, request, ticket);
}
static bool poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    (void)context;
    if (poll_failed) return false;
    if (!stalled && !mailbox.running_ticket) {
        zj_request_t request; uint64_t running;
        if (zj_mailbox_begin(&mailbox, &request, &running)) {
            assert(request.operation == ZJ_CATALOG);
            zj_reply_t result = {.result = ZJ_OK, .catalog = {.id = 77}};
            assert(zj_mailbox_finish(&mailbox, running, &result));
        }
    }
    return zj_mailbox_poll(&mailbox, ticket, reply, complete);
}
int main(void)
{
    zc_client_t client = {0};
    zc_client_port_t port = {clock_us, wait_step, submit, poll, NULL};
    zj_mailbox_init(&mailbox);
    zc_request_t request = {.operation = ZC_RESET, .deadline_us = 100};
    zc_reply_t reply;
    assert(zc_client_call(&client, port, &request, &reply) == ZJ_OK && reply.id == 77);
    assert(!client.pending_ticket && !client.active_may_have_changed && !mailbox.occupied);
    request = (zc_request_t){.operation = ZC_ACTIVATE, .id = 77, .deadline_us = 100};
    stalled = true;
    assert(zc_client_call(&client, port, &request, &reply) == ZJ_UNCERTAIN);
    uint64_t pending = client.pending_ticket;
    assert(pending && mailbox.occupied == 1 && reply.error == ETIMEDOUT);
    unsigned before = submissions;
    request.deadline_us = 1000;
    assert(zc_client_call(&client, port, &request, &reply) == ZJ_IO && reply.error == EBUSY);
    assert(submissions == before && client.pending_ticket == pending);
    poll_failed = true;
    assert(!zc_client_drain(&client, port) && client.pending_ticket == pending);
    stalled = false; poll_failed = false;
    assert(zc_client_drain(&client, port) && !client.pending_ticket && client.active_may_have_changed);
    assert(!mailbox.occupied);
    client.active_may_have_changed = false;
    request = (zc_request_t){.operation = ZC_RESET, .deadline_us = now};
    assert(zc_client_call(&client, port, &request, &reply) == ZJ_STALE && submissions == before);
    request.deadline_us += 100;
    zj_request_t filler = {.operation = ZJ_PEEK}; uint64_t ticket;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS; ++i)
        assert(zj_mailbox_submit(&mailbox, &filler, &ticket));
    assert(zc_client_call(&client, port, &request, &reply) == ZJ_IO);
    assert(!client.pending_ticket && reply.error == EBUSY);
    puts("retained catalog timeout and admission checks passed");
}
