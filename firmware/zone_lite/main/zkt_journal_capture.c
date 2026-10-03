#include "zkt_journal_capture.h"
#include <string.h>

#define CAPTURE_DEADLINE_MS 15000U
#define APPEND_DEADLINE_MS 5000U
static void put32(uint8_t *out, uint32_t value)
{ for (unsigned i = 0; i < 4; ++i) out[i] = (uint8_t)(value >> (i * 8)); }
static void wipe(void *data, size_t size)
{ volatile uint8_t *bytes = data; while (size--) *bytes++ = 0; }
static uint32_t clock_ms(zj_capture_t *c) { return c->port.now_ms(c->port.context); }
static bool release_reply(zj_capture_t *c)
{
    if (!c->health.pending_ticket) return true;
    if (!c->port.abandon(c->port.context, c->health.pending_ticket)) return false;
    c->health.pending_ticket = 0;
    return true;
}
bool zj_capture_init(zj_capture_t *c, zj_capture_port_t port)
{
    if (!c || !port.now_ms || !port.wait_ms || !port.random || !port.submit ||
        !port.poll || !port.abandon || !port.crypto.digest) return false;
    memset(c, 0, sizeof(*c));
    c->port = port;
    c->initialized = true;
    return true;
}
static bool append(zj_capture_t *c)
{
    uint64_t ticket = 0;
    if (!c->port.submit(c->port.context, &c->request, &ticket) || !ticket) {
        c->health.last_result = ZJ_FULL;
        return false;
    }
    c->health.pending_ticket = ticket;
    uint32_t start = clock_ms(c);
    for (;;) {
        bool complete = false;
        if (c->port.poll(c->port.context, ticket, &c->reply, &complete) && complete) {
            c->health.pending_ticket = 0;
            c->health.last_result = c->reply.result;
            if (c->reply.result != ZJ_OK) return false;
            if (!c->reply.capture_sequence) { c->health.last_result = ZJ_INVALID; return false; }
            if (!c->health.first_sequence) c->health.first_sequence = c->reply.capture_sequence;
            c->health.last_sequence = c->reply.capture_sequence;
            ++c->health.fragments;
            c->health.progress_ms = clock_ms(c);
            return true;
        }
        if ((uint32_t)(clock_ms(c) - start) >= APPEND_DEADLINE_MS ||
            (uint32_t)(clock_ms(c) - c->health.started_ms) >= CAPTURE_DEADLINE_MS) {
            c->health.last_result = ZJ_UNCERTAIN;
            ++c->health.timeouts;
            (void)release_reply(c);
            return false;
        }
        c->port.wait_ms(c->port.context, 10);
    }
}
bool zj_capture_packet(zj_capture_t *c, const uint8_t *packet, size_t length,
                        const zj_capture_facts_t *facts)
{
    if (!c || !c->initialized || c->health.running) return false;
    c->health.started_ms = clock_ms(c);
    c->health.committed_bytes = 0;
    c->health.first_sequence = c->health.last_sequence = 0;
    c->health.last_result = ZJ_INVALID;
    bool ok = false;
    uint8_t group[16] = {0}, digest[32] = {0};
    if (!packet || length < 8 || length > ZJ_PACKET_MAX || !facts || !release_reply(c)) goto done;
    c->health.running = true;
    memset(&c->request, 0, sizeof(c->request));
    c->request.operation = ZJ_APPEND;
    zj_observation_t *o = &c->request.input.observation;
    o->sequence = 1; /* Validation only. The storage owner allocates the real sequence. */
    o->source_ordinal = UINT32_MAX;
    o->captured_at_seconds = facts->wall_seconds;
    o->captured_uptime_ms = facts->uptime_ms;
    o->identity_revision = facts->identity_revision;
    o->time_quality = facts->time_quality;
    o->raw_format = length <= ZJ_RAW_MAX ? ZJ_LIVE_PACKET : ZJ_PACKET_FRAGMENT;
    if (length > ZJ_RAW_MAX) {
        if (!c->port.random(c->port.context, group, sizeof(group)) ||
            !c->port.crypto.digest(c->port.crypto.context, packet, length, digest)) goto done;
        uint8_t nonzero = 0;
        for (unsigned i = 0; i < sizeof(group); ++i) nonzero |= group[i];
        if (!nonzero) goto done;
    }
    for (size_t offset = 0; offset < length;) {
        if ((uint32_t)(clock_ms(c) - c->health.started_ms) >= CAPTURE_DEADLINE_MS) {
            c->health.last_result = ZJ_UNCERTAIN;
            ++c->health.timeouts;
            goto done;
        }
        size_t bytes = length - offset;
        if (length > ZJ_RAW_MAX) {
            if (bytes > ZJ_FRAGMENT_DATA) bytes = ZJ_FRAGMENT_DATA;
            memcpy(o->raw, "ZJF1", 4);
            memcpy(o->raw + 4, group, 16);
            put32(o->raw + 20, (uint32_t)length);
            put32(o->raw + 24, (uint32_t)offset);
            memcpy(o->raw + 28, digest, 32);
            memcpy(o->raw + ZJ_FRAGMENT_HEADER, packet + offset, bytes);
            o->raw_length = (uint16_t)(ZJ_FRAGMENT_HEADER + bytes);
        } else {
            memcpy(o->raw, packet, bytes);
            o->raw_length = (uint16_t)bytes;
        }
        if (!zj_observation_valid(o) || !append(c)) goto done;
        offset += bytes;
        c->health.committed_bytes = offset;
    }
    ++c->health.packets;
    c->health.last_result = ZJ_OK;
    ok = true;
done:
    if (!ok) ++c->health.failures;
    c->health.running = false;
    wipe(&c->request, sizeof(c->request));
    wipe(&c->reply, sizeof(c->reply));
    wipe(group, sizeof(group));
    wipe(digest, sizeof(digest));
    return ok;
}
