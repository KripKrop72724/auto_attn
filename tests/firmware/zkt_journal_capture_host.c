#define main journal_store_fault_main
#include "zkt_journal_store_host.c"
#undef main
#include "zkt_journal_capture.h"

typedef struct {
    zj_store_t store;
    zj_mailbox_t mailbox;
    zj_capture_t capture;
    zj_request_t request;
    uint64_t ticket;
    uint32_t clock, operation_started, delay_ms;
    unsigned writes, refuse_write, abandon_failures;
    bool stalled, entropy_failure;
} capture_scenario_t;
static capture_scenario_t scenario;
static uint8_t packet[ZJ_PACKET_MAX], reconstructed[ZJ_PACKET_MAX];
static const zj_capture_facts_t facts = {.wall_seconds = 1700000000,
    .uptime_ms = UINT64_C(9007199254740993), .time_quality = ZJ_TIME_UNKNOWN};
static void drive(capture_scenario_t *s)
{
    if (!s->ticket) {
        if (!zj_mailbox_begin(&s->mailbox, &s->request, &s->ticket)) return;
        s->operation_started = s->clock;
    }
    if (s->stalled || (uint32_t)(s->clock - s->operation_started) < s->delay_ms) return;
    assert(s->request.operation == ZJ_APPEND);
    zj_reply_t reply = {0};
    state.full = ++s->writes == s->refuse_write;
    reply.result = zj_store_append(&s->store, &s->request.input.observation, &reply.capture_sequence);
    state.full = false;
    assert(zj_mailbox_finish(&s->mailbox, s->ticket, &reply));
    s->ticket = 0;
}
static uint32_t capture_now(void *context) { return ((capture_scenario_t *)context)->clock; }
static void capture_wait(void *context, uint32_t milliseconds)
{ capture_scenario_t *s = context; s->clock += milliseconds; drive(s); }
static bool capture_random(void *context, uint8_t *out, size_t length)
{
    capture_scenario_t *s = context;
    if (s->entropy_failure) return false;
    for (size_t i = 0; i < length; ++i) out[i] = (uint8_t)(i + 1);
    return true;
}
static bool capture_submit(void *context, const zj_request_t *request, uint64_t *ticket)
{ return zj_mailbox_submit(&((capture_scenario_t *)context)->mailbox, request, ticket); }
static bool capture_poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    capture_scenario_t *s = context;
    drive(s);
    return zj_mailbox_poll(&s->mailbox, ticket, reply, complete);
}
static bool capture_abandon(void *context, uint64_t ticket)
{
    capture_scenario_t *s = context;
    if (s->abandon_failures) { --s->abandon_failures; return false; }
    return zj_mailbox_abandon(&s->mailbox, ticket);
}
static void initialize(capture_scenario_t *s)
{
    memset(s, 0, sizeof(*s));
    reset(&s->store);
    zj_mailbox_init(&s->mailbox);
    zj_capture_port_t port = {capture_now, capture_wait, capture_random, capture_submit,
        capture_poll, capture_abandon, s, {.digest = fake_digest}};
    assert(zj_capture_init(&s->capture, port));
    for (size_t i = 0; i < sizeof(packet); ++i) packet[i] = (uint8_t)(7 + i * 23);
}
static unsigned reconstruct(capture_scenario_t *s, size_t length, size_t expected_bytes)
{
    zj_item_t item;
    unsigned records = 0;
    size_t position = 0;
    uint8_t digest[32];
    assert(fake_digest(NULL, packet, length, digest));
    zj_result_t result;
    while ((result = zj_store_peek(&s->store, &item)) == ZJ_OK) {
        assert(item.kind == ZJ_OBSERVATION);
        assert(item.observation.captured_at_seconds == facts.wall_seconds);
        assert(item.observation.captured_uptime_ms == facts.uptime_ms);
        const uint8_t *raw = item.observation.raw;
        size_t size = item.observation.raw_length;
        if (length > ZJ_RAW_MAX) {
            assert(item.observation.raw_format == ZJ_PACKET_FRAGMENT);
            assert(!memcmp(raw, "ZJF1", 4) && get32(raw + 20) == length && get32(raw + 24) == position);
            for (unsigned i = 0; i < 16; ++i) assert(raw[4 + i] == i + 1);
            assert(!memcmp(raw + 28, digest, 32));
            assert(size > ZJ_FRAGMENT_HEADER);
            raw += ZJ_FRAGMENT_HEADER; size -= ZJ_FRAGMENT_HEADER;
        } else assert(item.observation.raw_format == ZJ_LIVE_PACKET);
        assert(size <= sizeof(reconstructed) - position);
        memcpy(reconstructed + position, raw, size);
        position += size;
        settle(&s->store, &item);
        ++records;
    }
    assert(result == ZJ_EMPTY && position == expected_bytes);
    assert(!memcmp(packet, reconstructed, position));
    return records;
}
int main(void)
{
    capture_scenario_t *s = &scenario;
    size_t sizes[] = {8, 20, 512, 513, 904, 4096, ZJ_PACKET_MAX};
    for (unsigned i = 0; i < sizeof(sizes) / sizeof(*sizes); ++i) {
        initialize(s);
        size_t size = sizes[i];
        assert(zj_capture_packet(&s->capture, packet, size, &facts));
        assert(s->capture.health.committed_bytes == size && s->capture.health.packets == 1);
        reopen(&s->store);
        unsigned pieces = reconstruct(s, size, size);
        assert(pieces == (size <= 512 ? 1 : (size + ZJ_FRAGMENT_DATA - 1) / ZJ_FRAGMENT_DATA));
    }
    initialize(s);
    assert(!zj_capture_packet(&s->capture, packet, 7, &facts));
    assert(!zj_capture_packet(&s->capture, packet, ZJ_PACKET_MAX + 1, &facts));
    assert(!s->writes);
    s->entropy_failure = true;
    assert(!zj_capture_packet(&s->capture, packet, 513, &facts));
    assert(!s->writes);

    initialize(s);
    s->refuse_write = 2;
    assert(!zj_capture_packet(&s->capture, packet, 1000, &facts));
    assert(s->capture.health.committed_bytes == ZJ_FRAGMENT_DATA);
    assert(s->capture.health.last_result == ZJ_FULL && !s->capture.health.packets);
    reopen(&s->store);
    assert(reconstruct(s, 1000, ZJ_FRAGMENT_DATA) == 1);

    initialize(s);
    s->clock = UINT32_MAX - 1000;
    s->stalled = true;
    s->abandon_failures = 2;
    assert(!zj_capture_packet(&s->capture, packet, 40, &facts));
    assert(s->capture.health.timeouts == 1 && s->mailbox.occupied == 1);
    assert(s->capture.health.pending_ticket);
    uint8_t original[40]; memcpy(original, packet, 40);
    memset(packet, 0, 40); /* The timed-out caller can release/overwrite its input. */
    assert(!zj_capture_packet(&s->capture, packet, 40, &facts));
    assert(s->mailbox.occupied == 1);
    s->stalled = false;
    drive(s);
    assert(s->mailbox.occupied == 1); /* Reply remains until abandonment succeeds. */
    assert(zj_capture_packet(&s->capture, packet, 40, &facts));
    assert(!s->mailbox.occupied && !s->capture.health.pending_ticket);
    zj_item_t item;
    assert(zj_store_peek(&s->store, &item) == ZJ_OK);
    assert(!memcmp(item.observation.raw, original, 40));
    settle(&s->store, &item);
    assert(zj_store_peek(&s->store, &item) == ZJ_OK);
    assert(!memcmp(item.observation.raw, packet, 40));

    initialize(s);
    s->delay_ms = 3000;
    assert(!zj_capture_packet(&s->capture, packet, 10000, &facts));
    assert(s->clock == 15000 && s->capture.health.timeouts == 1);
    assert(s->capture.health.committed_bytes > 0 && s->capture.health.committed_bytes < 10000);
    assert(!s->capture.health.packets);
    clean();
    puts("raw packet capture, fragment preservation, bounded deadlines and reply ownership: ok");
    return 0;
}
