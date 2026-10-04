/* Reuse the fault-injectable actual filesystem port, not a fake queue. */
#define main journal_store_fault_main
#include "zkt_journal_store_host.c"
#undef main
#include "zkt_journal_delivery.h"

typedef struct {
    zj_store_t store;
    zj_mailbox_t mailbox;
    zj_delivery_t delivery;
    zj_request_t running;
    uint64_t running_ticket;
    uint32_t clock;
    bool online, stall, uncertain_checkpoint, reject_proof, immediate_owner;
    unsigned send_delay_ms, polls, observations, observation_delay_ms;
    zj_delivery_phase_t observed_phase;
    unsigned sends, lost_acks, abandon_failures, abandons, submissions, canonical_count;
    char canonical[4][ZJ_CUSTODY_PAYLOAD_MAX];
} scenario_t;
static scenario_t scenario;
static void owner_step(scenario_t *s);

static uint32_t delivery_now(void *context) { return ((scenario_t *)context)->clock; }
static uint32_t delivery_random(void *context) { (void)context; return 123; }
static bool delivery_connected(void *context) { return ((scenario_t *)context)->online; }
static bool delivery_submit(void *context, const zj_request_t *request, uint64_t *ticket)
{
    scenario_t *s = context;
    ++s->submissions;
    bool accepted = zj_mailbox_submit(&s->mailbox, request, ticket);
    if (accepted && s->immediate_owner) owner_step(s);
    return accepted;
}
static bool delivery_poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    scenario_t *s = context;
    ++s->polls;
    return zj_mailbox_poll(&s->mailbox, ticket, reply, complete);
}
static bool delivery_abandon(void *context, uint64_t ticket)
{
    scenario_t *s = context;
    ++s->abandons;
    if (s->abandon_failures) { --s->abandon_failures; return false; }
    return zj_mailbox_abandon(&s->mailbox, ticket);
}
static bool delivery_send(void *context, const char *payload, uint32_t timeout, uint8_t receipt[32])
{
    scenario_t *s = context;
    assert(s->online && timeout == 15000);
    assert(!s->running_ticket && !s->mailbox.running_ticket);
    if (s->observations) assert(s->observed_phase == ZJ_DELIVERY_SEND);
    ++s->sends;
    unsigned index = 0;
    for (; index < s->canonical_count; ++index) if (!strcmp(payload, s->canonical[index])) break;
    if (index == s->canonical_count) {
        assert(index < 4 && strlen(payload) < sizeof(s->canonical[index]));
        strcpy(s->canonical[index], payload);
        ++s->canonical_count;
    }
    /* The simulated server commits before dropping its response. Replay
     * identity and payload must remain byte-for-byte unchanged. */
    s->clock += s->send_delay_ms;
    if (s->lost_acks) { --s->lost_acks; return false; }
    if (!s->reject_proof) receipt[0] = (uint8_t)(index + 1);
    return true;
}
static bool uncertain_save(void *context, const uint8_t *checkpoint)
{
    assert(save(context, checkpoint));
    if (scenario.uncertain_checkpoint) {
        scenario.uncertain_checkpoint = false;
        return false;
    }
    return true;
}
static void owner_step(scenario_t *s)
{
    if (!s->running_ticket && !zj_mailbox_begin(&s->mailbox, &s->running, &s->running_ticket)) return;
    if (s->stall) return;
    zj_reply_t reply = {0};
    switch (s->running.operation) {
        case ZJ_READER_CHECK:
        case ZJ_OTA_CHECK:
        case ZJ_SELECT_READER:
        case ZJ_RUNTIME_CHECKPOINT:
        case ZJ_CATALOG:
        case ZJ_LEASE:
        case ZJ_COMMANDS:
        case ZJ_COMMAND_IDS:
            assert(!"Delivery must not grant its own reader/writer compatibility");
            reply.result = ZJ_INVALID;
            break;
        case ZJ_APPEND:
            reply.result = zj_store_append(&s->store, &s->running.input.observation, &reply.capture_sequence);
            break;
        case ZJ_PEEK:
            reply.result = zj_store_peek(&s->store, &reply.item);
            break;
        case ZJ_SETTLE: {
            zj_custody_expected_t expected;
            char payload[ZJ_CUSTODY_PAYLOAD_MAX];
            reply.result = zj_store_peek(&s->store, &reply.item);
            if (reply.result != ZJ_OK) break;
            assert(zj_custody_encode(&reply.item, s->store.port.crypto, payload, sizeof(payload), &expected));
            if (strcmp(expected.observation_id, s->running.input.settlement.observation_id) ||
                strcmp(expected.payload_digest, s->running.input.settlement.payload_digest)) {
                reply.result = ZJ_STALE;
                break;
            }
            reply.result = zj_store_settle(&s->store, &s->running.input.settlement.token,
                                          s->running.input.settlement.receipt_digest);
            break;
        }
        case ZJ_RECLAIM:
            reply.result = zj_store_reclaim_step(&s->store);
            break;
    }
    assert(zj_mailbox_finish(&s->mailbox, s->running_ticket, &reply));
    s->running_ticket = 0;
}
static void initialize(scenario_t *s)
{
    memset(s, 0, sizeof(*s));
    reset(&s->store);
    s->store.port.commit = uncertain_save;
    s->online = true;
    s->send_delay_ms = 15000;
    zj_mailbox_init(&s->mailbox);
    zj_delivery_port_t p = {delivery_now, delivery_random, delivery_connected,
        delivery_submit, delivery_poll, delivery_abandon, delivery_send, s,
        {.digest = fake_digest}};
    assert(zj_delivery_init(&s->delivery, p));
}
static void step(scenario_t *s)
{
    zj_delivery_step(&s->delivery);
    owner_step(s);
    s->clock += 10;
    assert(s->mailbox.occupied <= ZJ_REQUEST_SLOTS);
}
static void steps(scenario_t *s, unsigned count) { while (count--) step(s); }
static void until(scenario_t *s, zj_delivery_phase_t phase)
{
    for (unsigned i = 0; i < 20000; ++i) {
        if (s->delivery.health.phase == phase) return;
        step(s);
    }
    assert(!"phase deadline");
}
static void drained(scenario_t *s)
{
    for (unsigned i = 0; i < 20000; ++i) {
        if (!s->store.count && !s->mailbox.occupied && s->delivery.health.phase == ZJ_DELIVERY_IDLE) return;
        step(s);
    }
    assert(!"drain deadline");
}
static void observe(const zj_delivery_t *delivery, void *context)
{
    scenario_t *s = context;
    ++s->observations;
    s->observed_phase = delivery->health.phase;
    s->clock += s->observation_delay_ms;
}
static void cadence(void)
{
    scenario_t *s = &scenario;
    initialize(s);
    assert(append(&s->store, 'J', 40) == ZJ_OK);
    assert(append(&s->store, 'K', 40) == ZJ_OK);
    assert(append(&s->store, 'L', 40) == ZJ_OK);
    s->send_delay_ms = 0;
    s->immediate_owner = true;
    for (unsigned i = 0; i < 30 && s->delivery.health.settled < 3; ++i) {
        unsigned sent = s->sends, observed = s->observations;
        uint32_t delay = zj_delivery_pump(&s->delivery, observe, s);
        assert(s->sends <= sent + 1); /* Even a fast sender yields every exchange. */
        assert(s->observations <= observed + 9); /* Eight transitions plus final sample. */
        assert(delay >= 1 && delay <= 100);
        s->clock += delay < 10 ? 10 : delay; /* Model a 100-Hz task scheduler. */
    }
    assert(s->delivery.health.settled == 3 && s->canonical_count == 3);
    /* Multiple pending records in one segment must not incur a 250-ms empty
     * delay after each receipt merely because the segment cannot be deleted. */
    assert(s->clock < 250);
    for (unsigned i = 0; i < 20; ++i)
        s->clock += zj_delivery_pump(&s->delivery, observe, s);
    unsigned io_before = calls, sends_before = s->sends;
    for (unsigned i = 0; i < 20; ++i)
        s->clock += zj_delivery_pump(&s->delivery, observe, s);
    assert(calls == io_before && s->sends == sends_before);

    initialize(s);
    assert(append(&s->store, 'M', 40) == ZJ_OK);
    assert(zj_delivery_pump(&s->delivery, observe, s) == 1);
    assert(s->polls == 1 && !s->sends && s->mailbox.occupied == 1);
    assert(zj_delivery_pump(&s->delivery, observe, s) == 1);
    assert(s->polls == 2); /* Waiting owner is polled once per activation. */
    owner_step(s);
    s->send_delay_ms = 5;
    assert(zj_delivery_pump(&s->delivery, observe, s) == 1);
    assert(s->sends == 1 && s->delivery.health.phase == ZJ_DELIVERY_SUBMIT_SETTLE);
    assert(s->observed_phase == ZJ_DELIVERY_SUBMIT_SETTLE);

    initialize(s);
    s->clock = UINT32_MAX;
    s->delivery.health.wait_ms = 0;
    s->observation_delay_ms = 2;
    assert(zj_delivery_pump(&s->delivery, observe, s) == 1);
    assert(s->delivery.health.phase == ZJ_DELIVERY_SUBMIT_READ && !s->submissions);
    assert(s->observations == 2); /* Budget holds across monotonic wrap. */
    s->observation_delay_ms = 0;
    s->delivery.health.phase = ZJ_DELIVERY_IDLE;
    s->delivery.health.wait_started_ms = UINT32_MAX - 99;
    s->delivery.health.wait_ms = 600;
    s->clock = 100;
    assert(zj_delivery_pump(&s->delivery, observe, s) == 100);
    assert(!s->submissions && !s->sends); /* Backoff is not shortened. */
    s->delivery.health.wait_ms = 220;
    assert(zj_delivery_pump(&s->delivery, observe, s) == 20);
    assert(zj_delivery_pump(NULL, NULL, NULL) == 100);
}
int main(void)
{
    scenario_t *s = &scenario;
    zj_item_t item;
    initialize(s);
    assert(append(&s->store, 'A', 40) == ZJ_OK);
    s->online = false;
    steps(s, 1000);
    assert(!s->sends && !state.exists && s->store.count == 1);
    assert(zj_store_peek(&s->store, &item) == ZJ_OK && item.observation.raw[0] == 'A');
    s->online = true;
    s->lost_acks = 2;
    until(s, ZJ_DELIVERY_SEND);
    step(s);
    assert(!state.exists && s->store.count == 1);
    assert(zj_store_peek(&s->store, &item) == ZJ_OK && item.observation.raw[0] == 'A');
    drained(s);
    assert(s->sends == 3 && s->canonical_count == 1 && s->delivery.health.settled == 1);
    assert(s->delivery.health.failures == 2 && !s->delivery.health.consecutive_failures);
    unsigned io_before = calls;
    steps(s, 1000);
    assert(calls == io_before); /* Verified empty polling performs no file scan. */

    initialize(s);
    assert(append(&s->store, 'B', 40) == ZJ_OK);
    s->reject_proof = true;
    until(s, ZJ_DELIVERY_SEND);
    step(s);
    assert(!state.exists && !s->delivery.health.receipts);
    assert(zj_store_peek(&s->store, &item) == ZJ_OK);
    s->reject_proof = false;
    drained(s);
    assert(s->canonical_count == 1 && s->sends == 2);

    initialize(s);
    assert(append(&s->store, 'C', 40) == ZJ_OK);
    s->uncertain_checkpoint = true;
    until(s, ZJ_DELIVERY_SUBMIT_SETTLE);
    step(s); /* Persistence succeeds, but reports uncertainty. */
    assert(state.exists && !s->uncertain_checkpoint);
    step(s); /* Caller receives uncertainty, never a successful retirement. */
    assert(!s->delivery.health.settled && s->delivery.health.failures == 1);
    assert(!s->mailbox.occupied);
    reopen(&s->store); /* Recovery accepts the committed retirement. */
    drained(s);
    assert(s->sends == 1 && s->canonical_count == 1);

    initialize(s);
    assert(append(&s->store, 'D', 40) == ZJ_OK);
    s->clock = UINT32_MAX - 1000; /* Deadline crosses the millisecond wrap. */
    s->delivery.health.wait_ms = 0;
    s->stall = true;
    s->abandon_failures = 3;
    until(s, ZJ_DELIVERY_ABANDON);
    assert(s->delivery.health.timeouts == 1 && s->mailbox.occupied == 1);
    uint64_t ticket = s->delivery.health.pending_ticket;
    unsigned submitted = s->submissions;
    for (unsigned i = 0; i < 3; ++i) {
        step(s);
        assert(s->delivery.health.pending_ticket == ticket && s->submissions == submitted);
    }
    step(s);
    assert(!s->delivery.health.pending_ticket && s->mailbox.occupied == 1);
    assert(!s->sends && !state.exists);
    s->stall = false;
    drained(s);
    assert(s->canonical_count == 1 && s->sends == 1 && !s->mailbox.occupied);

    initialize(s);
    assert(append(&s->store, 'E', 40) == ZJ_OK);
    until(s, ZJ_DELIVERY_SEND);
    s->online = false;
    step(s);
    assert(!s->sends && !state.exists);
    s->online = true;
    drained(s);
    assert(s->canonical_count == 1 && s->delivery.health.settled == 1);

    initialize(s);
    assert(append(&s->store, 'F', 40) == ZJ_OK);
    assert(append(&s->store, 'G', 40) == ZJ_OK);
    s->stall = true;
    steps(s, 12000); /* Repeated owner timeouts cannot grow RAM without bound. */
    assert(!s->sends && !state.exists && s->delivery.health.timeouts >= 5);
    assert(s->mailbox.occupied == ZJ_REQUEST_SLOTS - ZJ_LIVE_RESERVED_SLOTS);
    assert(s->delivery.health.refused && s->mailbox.high_watermark <= ZJ_REQUEST_SLOTS);
    s->stall = false;
    drained(s);
    assert(s->canonical_count == 2 && s->sends == 2 && s->delivery.health.settled == 2);
    cadence();
    clean();
    puts("journal delivery replay, checkpoint recovery, bounded timeouts and wrap: ok");
    return 0;
}
