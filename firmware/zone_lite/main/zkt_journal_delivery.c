#include "zkt_journal_delivery.h"
#include <string.h>

#define OWNER_DEADLINE_MS 5000U
#define ADD_ACK_DEADLINE_MS 15000U
static void erase(void *data, size_t length)
{
    volatile uint8_t *bytes = data;
    while (length--) *bytes++ = 0;
}
static uint32_t now(zj_delivery_t *d) { return d->port.now_ms(d->port.context); }
static void phase(zj_delivery_t *d, zj_delivery_phase_t next)
{
    d->health.phase = next;
    d->health.phase_started_ms = now(d);
}
static void clear_item(zj_delivery_t *d)
{
    erase(&d->item, sizeof(d->item));
    erase(&d->request, sizeof(d->request));
    erase(&d->reply, sizeof(d->reply));
    erase(&d->expected, sizeof(d->expected));
    erase(d->receipt_digest, sizeof(d->receipt_digest));
    erase(d->payload, sizeof(d->payload));
}
static void idle(zj_delivery_t *d, uint32_t delay)
{
    clear_item(d);
    d->verified_empty = false;
    d->health.wait_started_ms = now(d);
    d->health.wait_ms = delay;
    phase(d, ZJ_DELIVERY_IDLE);
}
static void retry(zj_delivery_t *d)
{
    unsigned exponent = d->health.consecutive_failures ? d->health.consecutive_failures - 1 : 0;
    if (exponent > 6) exponent = 6;
    uint32_t ceiling = 1000U << exponent;
    if (ceiling > 60000U) ceiling = 60000U;
    uint32_t delay = ceiling / 2 + d->port.random(d->port.context) % (ceiling / 2 + 1);
    idle(d, delay);
}
static void failed(zj_delivery_t *d, const char *operation, zj_result_t result)
{
    ++d->health.failures;
    if (d->health.consecutive_failures < UINT32_MAX) ++d->health.consecutive_failures;
    d->health.last_failure = operation;
    d->health.last_result = result;
}
static void submit(zj_delivery_t *d, zj_operation_t operation)
{
    d->request.operation = operation;
    uint64_t ticket = 0;
    if (!d->port.submit(d->port.context, &d->request, &ticket) || !ticket) {
        ++d->health.refused;
        failed(d, "owner_admission", ZJ_FULL);
        retry(d);
        return;
    }
    d->health.pending_ticket = ticket;
    d->health.pending_operation = operation;
    d->health.progress_ms = now(d);
    phase(d, ZJ_DELIVERY_WAIT);
}
bool zj_delivery_init(zj_delivery_t *d, zj_delivery_port_t port)
{
    if (!d || !port.now_ms || !port.random || !port.connected || !port.submit ||
        !port.poll || !port.abandon || !port.send || !port.crypto.digest) return false;
    memset(d, 0, sizeof(*d));
    d->port = port;
    d->initialized = true;
    idle(d, 0);
    return true;
}
void zj_delivery_step(zj_delivery_t *d)
{
    if (!d || !d->initialized) return;
    switch (d->health.phase) {
        case ZJ_DELIVERY_IDLE:
            if ((uint32_t)(now(d) - d->health.wait_started_ms) < d->health.wait_ms) return;
            phase(d, d->port.connected(d->port.context) ? ZJ_DELIVERY_SUBMIT_READ : ZJ_DELIVERY_SUBMIT_RECLAIM);
            break;
        case ZJ_DELIVERY_SUBMIT_READ:
            submit(d, ZJ_PEEK);
            break;
        case ZJ_DELIVERY_WAIT: {
            bool complete = false;
            bool polled = d->port.poll(d->port.context, d->health.pending_ticket, &d->reply, &complete);
            if (!polled || !complete) {
                if ((uint32_t)(now(d) - d->health.phase_started_ms) >= OWNER_DEADLINE_MS) {
                    ++d->health.timeouts;
                    failed(d, "owner_deadline", ZJ_UNCERTAIN);
                    phase(d, ZJ_DELIVERY_ABANDON);
                }
                return;
            }
            d->health.pending_ticket = 0;
            d->health.progress_ms = now(d);
            d->health.last_result = d->reply.result;
            if (d->reply.result == ZJ_EMPTY && d->health.pending_operation == ZJ_PEEK) {
                d->verified_empty = true;
                phase(d, ZJ_DELIVERY_SUBMIT_RECLAIM);
            } else if (d->reply.result == ZJ_EMPTY && d->health.pending_operation == ZJ_RECLAIM) {
                if (d->verified_empty) d->health.consecutive_failures = 0;
                idle(d, d->port.connected(d->port.context) ? 250 : 1000);
            } else if (d->reply.result != ZJ_OK) {
                failed(d, d->health.pending_operation == ZJ_SETTLE ? "retirement" : "journal_read_or_reclaim", d->reply.result);
                retry(d);
            } else if (d->health.pending_operation == ZJ_PEEK) {
                d->item = d->reply.item;
                erase(&d->reply, sizeof(d->reply));
                phase(d, ZJ_DELIVERY_ENCODE);
            } else if (d->health.pending_operation == ZJ_SETTLE) {
                ++d->health.settled;
                d->health.consecutive_failures = 0;
                clear_item(d);
                phase(d, ZJ_DELIVERY_SUBMIT_RECLAIM);
            } else {
                ++d->health.reclaimed;
                idle(d, 0);
            }
            break;
        }
        case ZJ_DELIVERY_ENCODE:
            if (zj_custody_encode(&d->item, d->port.crypto, d->payload, sizeof(d->payload), &d->expected))
                phase(d, ZJ_DELIVERY_SEND);
            else {
                failed(d, "custody_encode", ZJ_INVALID);
                retry(d);
            }
            break;
        case ZJ_DELIVERY_SEND: {
            if (!d->port.connected(d->port.context)) { idle(d, 1000); break; }
            memset(d->receipt_digest, 0, sizeof(d->receipt_digest));
            bool committed = d->port.send(d->port.context, d->payload, ADD_ACK_DEADLINE_MS, d->receipt_digest);
            uint8_t proof = 0;
            for (unsigned i = 0; i < sizeof(d->receipt_digest); ++i) proof |= d->receipt_digest[i];
            d->health.progress_ms = now(d);
            if (committed && proof) {
                ++d->health.receipts;
                phase(d, ZJ_DELIVERY_SUBMIT_SETTLE);
            } else {
                failed(d, "add_custody", ZJ_IO);
                retry(d);
            }
            break;
        }
        case ZJ_DELIVERY_SUBMIT_SETTLE:
            d->request.input.settlement.token = d->item.token;
            memcpy(d->request.input.settlement.receipt_digest, d->receipt_digest, 32);
            memcpy(d->request.input.settlement.observation_id, d->expected.observation_id, 65);
            memcpy(d->request.input.settlement.payload_digest, d->expected.payload_digest, 65);
            submit(d, ZJ_SETTLE);
            break;
        case ZJ_DELIVERY_SUBMIT_RECLAIM:
            submit(d, ZJ_RECLAIM);
            break;
        case ZJ_DELIVERY_ABANDON:
            /* Accepted work may still finish. Do not discard the ticket until
             * the owner accepts abandonment of its reply. The owner keeps its
             * copied request slot until that operation actually finishes. */
            if (d->port.abandon(d->port.context, d->health.pending_ticket)) {
                d->health.pending_ticket = 0;
                d->health.progress_ms = now(d);
                retry(d);
            }
            break;
    }
}
