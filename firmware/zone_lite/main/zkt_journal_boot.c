#include "zkt_journal_boot.h"
#include <string.h>

static void increment(uint32_t *value) { if (*value != UINT32_MAX) ++*value; }
static bool due(const zj_boot_t *s, uint32_t now)
{ return !s->retry_delay_ms || (int32_t)(now - s->next_attempt_ms) >= 0; }
static void failed(zj_boot_t *s, uint32_t now)
{
    increment(&s->failures);
    s->retry_delay_ms = !s->retry_delay_ms ? 2000 :
        s->retry_delay_ms >= 30000 ? 60000 : s->retry_delay_ms * 2;
    s->next_attempt_ms = now + s->retry_delay_ms;
}
static void progressed(zj_boot_t *s, uint32_t now)
{ s->retry_delay_ms = 0; s->progress_ms = now; }
static bool fresh(uint32_t now, uint32_t sampled)
{ return (uint32_t)(now - sampled) < 45000U; }
static bool serial_valid(const char *serial)
{
    if (!serial) return false;
    size_t length = strnlen(serial, 81);
    if (!length || length > 80) return false;
    for (size_t i = 0; i < length; ++i) {
        char c = serial[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == ':' || c == '-')) return false;
    }
    return true;
}
void zj_boot_step(zj_boot_t *s, zj_boot_port_t p, const zj_boot_input_t *in)
{
    if (!s || !in || !p.owner_start || !p.owner_health || !p.transport_start ||
        !p.transport_health || !p.capture_start || !p.submit || !p.poll || !p.abandon) return;
    uint32_t now = in->now_ms;
    s->sampled_ms = now;
    s->reader_ready = s->writer_ready = false;
    s->mode = in->mode;
    s->bridge_validation_pending = in->mode == ZJ_BOOT_BRIDGE && in->bridge_validation_pending;
    if (in->mode == ZJ_BOOT_DISABLED) { s->phase = ZJ_BOOT_OFF; return; }
    if (!in->secure || (in->mode != ZJ_BOOT_BRIDGE && in->mode != ZJ_BOOT_WRITER)) {
        s->phase = ZJ_BOOT_SECURITY_HOLD; return;
    }
    if (!serial_valid(in->terminal_serial)) { s->phase = ZJ_BOOT_BINDING_HOLD; return; }
    if (s->terminal_serial[0] && strcmp(s->terminal_serial, in->terminal_serial)) s->binding_changed = true;
    if (s->binding_changed) { s->phase = ZJ_BOOT_BINDING_HOLD; return; }
    if (!s->owner_started) {
        if (!in->storage_ready && !in->storage_available) { s->phase = ZJ_BOOT_STORAGE_WAIT; return; }
        s->phase = ZJ_BOOT_OWNER_START;
        if (!due(s, now)) return;
        increment(&s->start_attempts);
        if (!p.owner_start(p.context, in->terminal_serial)) { failed(s, now); return; }
        strcpy(s->terminal_serial, in->terminal_serial);
        s->owner_started = true;
        increment(&s->owner_starts);
        progressed(s, now);
        return;
    }
    zj_owner_health_t owner = {0};
    if (!p.owner_health(p.context, &owner) || !owner.started ||
        !fresh(now, (uint32_t)(owner.sampled_uptime_us / 1000)) ||
        (owner.operation_running && (uint32_t)(now - (uint32_t)(owner.operation_started_us / 1000)) >= 15000U)) {
        s->phase = ZJ_BOOT_STALLED;
        return;
    }
    if (owner.quiescing || owner.quiesced) { s->phase = ZJ_BOOT_QUIESCING; return; }
    if (s->delivery_authority == ZJ_AUTHORITY_ADD && owner.delivery_authority == ZJ_AUTHORITY_LEGACY) {
        s->phase = ZJ_BOOT_AUTHORITY_HOLD; return;
    }
    /* Unknown never restores legacy authority. Retain an observed ADD cutover
     * while recovery is unavailable, without granting writer readiness. */
    if (owner.delivery_authority != ZJ_AUTHORITY_UNKNOWN)
        s->delivery_authority = owner.delivery_authority;
    if (!owner.ready || owner.checkpoint_recovery_pending) { s->phase = ZJ_BOOT_RECOVERING; }
    /* Delivery must start even during checkpoint recovery: the preserved
     * damaged checkpoint itself needs a committed receipt to finish recovery. */
    if (!s->transport_started) {
        s->phase = ZJ_BOOT_TRANSPORT_START;
        if (!due(s, now)) return;
        increment(&s->start_attempts);
        if (!p.transport_start(p.context)) { failed(s, now); return; }
        s->transport_started = true;
        increment(&s->transport_starts);
        progressed(s, now);
        return;
    }
    zj_transport_health_t transport = {0};
    if (!p.transport_health(p.context, &transport) || !transport.started || !fresh(now, transport.sampled_ms)) {
        s->phase = ZJ_BOOT_STALLED;
        return;
    }
    if (s->ticket) {
        s->phase = ZJ_BOOT_CHECKING_READER;
        zj_reply_t reply;
        bool complete = false;
        if (p.poll(p.context, s->ticket, &reply, &complete) && complete) {
            s->ticket = 0;
            s->compatibility = reply.compatibility;
            if (reply.result == ZJ_OK && reply.compatibility == ZJ_COMPAT_OK) progressed(s, now);
            else failed(s, now);
        } else if ((uint32_t)(now - s->ticket_started_ms) >= 5000U && p.abandon(p.context, s->ticket)) {
            s->ticket = 0;
            failed(s, now);
        }
        /* Re-read owner state next time; no stale pre-completion snapshot may
         * grant capture. An accepted proof operation can finish after timeout. */
        return;
    }
    /* The owner/transport must be able to transfer the evidence needed to
     * repair a legacy persistence incident. Health remains mandatory for
     * reader attestation, capture admission and local boot acceptance. */
    if (!in->storage_ready) { s->phase = ZJ_BOOT_STORAGE_WAIT; return; }
    if (!owner.ready || owner.checkpoint_recovery_pending) { s->phase = ZJ_BOOT_RECOVERING; return; }
    if (owner.delivery_authority == ZJ_AUTHORITY_UNKNOWN) { s->phase = ZJ_BOOT_AUTHORITY_HOLD; return; }
    if (!in->writer_build) { s->phase = ZJ_BOOT_WRITER_DISABLED; return; }
    s->reader_ready = true;
    s->compatibility = owner.compatibility;
    if (s->bridge_validation_pending) {
        /* OTA can now confirm this secure bridge's actual local reader and
         * transport. Attestation/capture wait for the next step's platform
         * VALID state, rather than relying on a race before a failed proof. */
        s->phase = ZJ_BOOT_BRIDGE_VALIDATION;
        return;
    }
    if (!owner.compatibility_checked || owner.compatibility != ZJ_COMPAT_OK) {
        s->phase = ZJ_BOOT_READER_HOLD;
        if (!due(s, now)) return;
        zj_request_t request = {.operation = ZJ_READER_CHECK};
        increment(&s->proof_attempts);
        if (!p.submit(p.context, &request, &s->ticket) || !s->ticket) {
            s->ticket = 0;
            failed(s, now);
            return;
        }
        s->ticket_started_ms = now;
        s->phase = ZJ_BOOT_CHECKING_READER;
        return;
    }
    if (in->mode == ZJ_BOOT_BRIDGE && s->delivery_authority == ZJ_AUTHORITY_LEGACY) {
        s->phase = ZJ_BOOT_READY; return;
    }
    if (s->delivery_authority != ZJ_AUTHORITY_ADD) { s->phase = ZJ_BOOT_AUTHORITY_HOLD; return; }
    if (!owner.writer_allowed) { s->phase = ZJ_BOOT_READER_HOLD; return; }
    if (!s->capture_started) {
        s->phase = ZJ_BOOT_CAPTURE_START;
        if (!due(s, now)) return;
        increment(&s->start_attempts);
        if (!p.capture_start(p.context)) { failed(s, now); return; }
        s->capture_started = true;
        increment(&s->capture_starts);
        progressed(s, now);
    }
    s->writer_ready = true;
    s->phase = ZJ_BOOT_READY;
}
bool zj_boot_local_ready(const zj_boot_t *s, uint32_t now)
{
    if (!s || !fresh(now, s->sampled_ms)) return false;
    if (s->mode == ZJ_BOOT_DISABLED) return true;
    /* A pending bridge must prove its reader before OTA marks it valid; only
     * then may the owner persist its validated-image reader attestation. */
    return s->reader_ready && (s->writer_ready || (s->mode == ZJ_BOOT_BRIDGE &&
        ((s->bridge_validation_pending && s->phase == ZJ_BOOT_BRIDGE_VALIDATION) ||
         (s->delivery_authority == ZJ_AUTHORITY_LEGACY && s->compatibility == ZJ_COMPAT_OK))));
}
const char *zj_boot_phase_name(zj_boot_phase_t phase)
{
    static const char *const names[] = {"DISABLED", "SECURITY_HOLD", "BINDING_HOLD", "STORAGE_WAIT",
        "OWNER_START", "RECOVERING", "TRANSPORT_START", "CHECKING_READER", "READER_HOLD",
        "CAPTURE_START", "WRITER_DISABLED", "READY", "STALLED", "QUIESCING", "AUTHORITY_HOLD", "BRIDGE_VALIDATION"};
    return (unsigned)phase < sizeof(names) / sizeof(names[0]) ? names[phase] : "UNKNOWN";
}
