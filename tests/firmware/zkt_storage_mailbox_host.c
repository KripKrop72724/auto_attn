#include "zkt_storage_mailbox.h"
#include <assert.h>
#include <string.h>

int main(void)
{
    static zj_mailbox_t mailbox;
    zj_mailbox_init(&mailbox);
    zj_request_t request = {.operation = ZJ_PEEK}, work;
    zj_reply_t reply = {.result = ZJ_OK, .capture_sequence = 17}, obtained;
    uint64_t tickets[ZJ_REQUEST_SLOTS], running, refused;
    bool complete;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS; ++i)
        assert(zj_mailbox_submit(&mailbox, &request, &tickets[i]));
    assert(!zj_mailbox_submit(&mailbox, &request, &refused) && !refused);
    request.operation = ZJ_APPEND;
    request.input.observation.raw[0] = 77;
    for (unsigned i = ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS; i < ZJ_REQUEST_SLOTS; ++i)
        assert(zj_mailbox_submit(&mailbox, &request, &tickets[i]));
    request.input.observation.raw[0] = 0;
    assert(!zj_mailbox_submit(&mailbox, &request, &refused));
    assert(mailbox.high_watermark == ZJ_REQUEST_SLOTS && mailbox.refused == 2);
    assert(zj_mailbox_begin(&mailbox, &work, &running));
    assert(running == tickets[ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS]);
    assert(work.input.observation.raw[0] == 77 && work.operation == ZJ_APPEND);
    assert(!zj_mailbox_begin(&mailbox, &work, &refused));
    assert(zj_mailbox_poll(&mailbox, running, &obtained, &complete) && !complete);
    assert(zj_mailbox_abandon(&mailbox, running));
    assert(!zj_mailbox_submit(&mailbox, &request, &refused));
    assert(!zj_mailbox_finish(&mailbox, running + 1, &reply));
    assert(zj_mailbox_finish(&mailbox, running, &reply));
    assert(!zj_mailbox_poll(&mailbox, running, &obtained, &complete));
    assert(zj_mailbox_submit(&mailbox, &request, &refused) && refused > tickets[ZJ_REQUEST_SLOTS - 1]);
    assert(!zj_mailbox_abandon(&mailbox, running));

    /* A timed-out QUEUED append still executes; its slot cannot be recycled
     * while a storage operation might hold a pointer to its copied inputs. */
    zj_mailbox_init(&mailbox);
    assert(zj_mailbox_submit(&mailbox, &request, &running));
    assert(zj_mailbox_abandon(&mailbox, running) && mailbox.occupied == 1);
    uint64_t ticket;
    assert(zj_mailbox_begin(&mailbox, &work, &ticket) && ticket == running);
    assert(zj_mailbox_finish(&mailbox, running, &reply) && mailbox.occupied == 0);

    /* Continuous live producers cannot starve a pending delivery peek. */
    zj_mailbox_init(&mailbox);
    request.operation = ZJ_PEEK;
    assert(zj_mailbox_submit(&mailbox, &request, &tickets[0]));
    bool peek_seen = false;
    for (unsigned i = 0; i <= ZJ_PRIORITY_BURST; ++i) {
        request.operation = ZJ_APPEND;
        assert(zj_mailbox_submit(&mailbox, &request, &ticket));
        assert(zj_mailbox_begin(&mailbox, &work, &running));
        if (work.operation == ZJ_PEEK) peek_seen = true;
        assert(zj_mailbox_finish(&mailbox, running, &reply));
        /* Completed replies occupy their slot until explicitly collected. */
        unsigned occupied = mailbox.occupied;
        assert(zj_mailbox_poll(&mailbox, running, &obtained, &complete) && complete);
        assert(mailbox.occupied == occupied - 1 && obtained.capture_sequence == 17);
        assert(!zj_mailbox_poll(&mailbox, running, &obtained, &complete));
    }
    assert(peek_seen);
    zj_mailbox_init(&mailbox);
    mailbox.next_ticket = UINT64_MAX;
    assert(zj_mailbox_submit(&mailbox, &request, &ticket) && ticket == UINT64_MAX);
    assert(!zj_mailbox_submit(&mailbox, &request, &ticket));
    zj_mailbox_init(&mailbox);
    request = (zj_request_t){.operation = ZJ_OTA_CHECK};
    assert(!zj_mailbox_submit(&mailbox, &request, &ticket) && !mailbox.occupied);
    memset(request.input.ota.version, 'a', sizeof(request.input.ota.version));
    assert(!zj_mailbox_submit(&mailbox, &request, &ticket) && !mailbox.occupied);
    strcpy(request.input.ota.version, ZJ_WRITER_VERSION);
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS; ++i)
        assert(zj_mailbox_submit(&mailbox, &request, &tickets[i]));
    assert(!zj_mailbox_submit(&mailbox, &request, &ticket));
    request.operation = ZJ_APPEND;
    assert(zj_mailbox_submit(&mailbox, &request, &ticket));
    assert(zj_mailbox_begin(&mailbox, &work, &running) && running == ticket);
    return 0;
}
