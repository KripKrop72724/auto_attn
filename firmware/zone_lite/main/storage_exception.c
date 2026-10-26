#include "storage_exception.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* SPIFFS_ERR_FULL, as the repair reports a store without room. */
#define SX_SPIFFS_ERR_FULL (-10001)
/* SPIFFS's data bytes per page (the repair's and the inventory's spans). */
#define SX_SPAN_BYTES 251U

const char *const sx_queue_names[SX_QUEUE_COUNT] = {"/pending.jsonl", "/blocked_identity.jsonl", "/corrupt_ords.jsonl",
                                                    "/add_corrupt.jsonl", "/add_corrupt.bak"};

const char *sx_state_name(sx_state_t state)
{
    static const char *const names[SX_STATE_COUNT] = {
        "NONE", "EVALUATING", "REFUSED", "ELIGIBLE", "CONFIRMING", "CONFIRMED",
        "ROUND_DONE", "ROUND_SHORT", "UNREPAIRED", "UNHEALTHY", "ABANDONED"};
    return (unsigned)state < SX_STATE_COUNT ? names[state] : "UNKNOWN";
}

const char *sx_reason_name(sx_reason_t reason)
{
    static const char *const names[SX_REASON_COUNT] = {
        "OK", "NOT_PENDING", "ROLE", "NOT_OTA_IMAGE", "ALREADY_USED",
        "REPAIR_MISSING", "REPAIR_RESULT", "REPAIR_STAGE", "REPAIR_GUARD", "SHORT_REASON", "AREA_NOT_READY",
        "AREA_TEST", "RECLAIM_FAULT", "ROUND_RAN", "MANIFEST", "ARRIVED_WITH_ROOM", "CHECK_DELETED",
        "CUSTODY_UNHELD", "CUSTODY_LOST", "RECORDS_UNACKED", "INVENTORY_PENDING", "INVENTORY_UNEXPLAINED",
        "CUSTODY_MISSING",
        "ROOM_UNKNOWN", "CUSTODY_SHORT", "CUSTODY_BLOCKS",
        "STORAGE_UNKNOWN", "STORAGE_ERROR_CLASS", "PROBE_ERROR_CLASS", "LED_FATAL", "LED_CAUSE",
        "STORE_NOT_MOUNTED", "STORE_CHANGED",
        "HEALTH", "HEALTH_UNKNOWN",
        "ADD_DISCONNECTED", "HEARTBEAT_UNACKED", "CAPABILITY", "OTA_PRECHECK", "OTA_SLOT", "HEAP",
        "STABILIZING", "EVIDENCE_UNACKED", "RECEIPT", "BUDGET", "NVS_RECORD", "MARK_VALID"};
    return (unsigned)reason < SX_REASON_COUNT ? names[reason] : "UNKNOWN";
}

bool sx_reason_unknown(sx_reason_t reason)
{
    return reason == SX_STORAGE_UNKNOWN || reason == SX_HEALTH_UNKNOWN || reason == SX_INVENTORY_PENDING;
}

const char *sx_health_name(uint32_t bit)
{
    static const char *const names[] = {
        "STORAGE_UPGRADE", "JOURNAL_RUNTIME", "SPIFFS_INFO", "QS_OBSERVED", "QS_RECOVERY", "QS_PERSISTENCE",
        "QS_LAST_ERROR", "QS_PROBE_ERROR", "LED_LOCAL_FAILURE", "LED_FATAL", "LOCK", "ADD_CONNECTED",
        "OUTBOX_TASK", "HEARTBEAT_TASK", "OUTBOX_BUFFER", "WORKER_START", "ORDS_STARTED", "ORDS_RESOURCE",
        "ORDS_TICK", "OUTBOX_TICK", "IDENTITY", "TERMINAL_ONLINE", "TERMINAL_STABLE", "TERMINAL_COUNTS"};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i)
        if (bit == (1U << i)) return names[i];
    return "UNKNOWN";
}

static bool same(const char *a, const char *b) { return a && b && !strcmp(a, b); }

/* ---- the terms ------------------------------------------------------------ *
 * ZKT 2.7.8: each verdict below records every term it finds failing (`all`)
 * or stops at the first, as 2.7.7 did. The first failing term is the reason;
 * the mask is diagnostics only and never changes a verdict. */
typedef struct {
    sx_reason_t first;
    uint64_t mask;
    bool all;
} terms_t;
_Static_assert(SX_REASON_COUNT <= 53, "the terms mask is a JSON number (exact below 2^53)");

/* Records a failing term; true when the verdict stops here. */
static bool fail(terms_t *t, sx_reason_t reason)
{
    if (t->first == SX_OK) t->first = reason;
    t->mask |= 1ULL << reason;
    return !t->all;
}

const char *sx_detail_name(sx_detail_t detail)
{
    static const char *const names[SX_DETAIL_COUNT] = {
        "NONE", "INCOMPLETE", "SCAN_ERROR", "FILE_COUNT", "NOT_HELD", "OTHER_UNREADABLE", "SPAN_ERRNO",
        "FIRST_ERRNO", "SPAN_COUNT", "FIRST_OFFSET", "LAST_OFFSET", "DIGEST", "BYTES", "UNMATCHED", "HELD_READABLE"};
    return (unsigned)detail < SX_DETAIL_COUNT ? names[detail] : "UNKNOWN";
}

bool sx_repair_deferred_for_room(sx_reason_t verdict)
{
    return verdict != SX_REPAIR_MISSING && verdict != SX_NOT_PENDING && verdict != SX_REPAIR_GUARD &&
        verdict != SX_REPAIR_RESULT && verdict != SX_STORE_NOT_MOUNTED && verdict != SX_REPAIR_STAGE;
}

/* ---- the boot repair ------------------------------------------------------ */
static void repair_terms(const sx_repair_t *r, terms_t *t)
{
    if (!r || !r->ran) {
        (void)fail(t, SX_REPAIR_MISSING);
        return;
    }
    if (!r->pending_verify && fail(t, SX_NOT_PENDING)) return;
    /* The attempts guard did not act, the private mount worked, and the VFS
     * may mount the store (A6). */
    if ((r->previous_failed || !same(r->mode, "FULL")) && fail(t, SX_REPAIR_GUARD)) return;
    if ((r->mount_failed || !same(r->result, "DEFERRED")) && fail(t, SX_REPAIR_RESULT)) return;
    if (!r->storage_allowed && fail(t, SX_STORE_NOT_MOUNTED)) return;
    /* Deferred for room only (A2): the run ended full or GC-unsafe, and the
     * last resort was asked for exactly that. */
    if (((!same(r->error_stage, "full") && !same(r->error_stage, "gc_unsafe")) || !same(r->trigger, r->error_stage) ||
         r->error_code != SX_SPIFFS_ERR_FULL) &&
        fail(t, SX_REPAIR_STAGE))
        return;
    /* The redo area is ready and proved writable on this device (A4, critique 3). */
    if (!same(r->area, "READY") && fail(t, SX_AREA_NOT_READY)) return;
    /* ZKT 2.7.8: the boot runs the area test only when every other repair
     * term holds (storage_repair.c), so a test that did not run is reported
     * as the term that kept it from running; AREA_TEST only when none did.
     * The verdict is never SX_OK without a test that PASSED. */
    bool not_run = same(r->area_test, "NOT_RUN");
    if (!same(r->area_test, "PASSED")) {
        if (not_run) t->mask |= 1ULL << SX_AREA_TEST;
        else if (fail(t, SX_AREA_TEST)) return;
    }
    /* No block the unconfirmed rules allow (A3), and none taken (narrowest). */
    if (!same(r->short_reason, "candidate") && fail(t, SX_SHORT_REASON)) return;
    if ((r->rounds || r->blocks) && fail(t, SX_ROUND_RAN)) return;
    /* Nothing of the last resort faulted or waits (A5). */
    if ((r->failed || r->held_mismatch || r->records_open || r->reported || r->sources || r->store_blocked ||
         r->waiting) &&
        fail(t, SX_RECLAIM_FAULT))
        return;
    /* No manifest the confirmed boot would take as stale (critique 4). */
    if (r->manifest_active && fail(t, SX_MANIFEST)) return;
    /* The store arrived without the room the work needs: not one this image's
     * own run filled (critique 10). */
    if ((!r->mount_measured || r->erased_at_mount >= r->target) && fail(t, SX_ARRIVED_WITH_ROOM)) return;
    if ((r->check_deleted_live || r->check_delete_bad_file) && fail(t, SX_CHECK_DELETED)) return;
    /* Custody intact (C1 to C3): every custody file held, nothing lost, every
     * record acknowledged by ADD. */
    bool unheld = r->files_unheld_custody;
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES; ++i) unheld |= r->file[i].unheld;
    if (unheld && fail(t, SX_CUSTODY_UNHELD)) return;
    if ((r->gaps || r->unreadable_bytes || r->records_dropped || r->interrupted_losses || r->commands_damaged) &&
        fail(t, SX_CUSTODY_LOST))
        return;
    if (r->records_pending && fail(t, SX_RECORDS_UNACKED)) return;
    /* Room by the planner's own estimate (P1 to P3). */
    if (!r->want && fail(t, SX_ROOM_UNKNOWN)) return;
    if (r->custody_gain < r->want && fail(t, SX_CUSTODY_SHORT)) return;
    if ((!r->custody_blocks || r->custody_blocks > SX_ROUND_BLOCK_CAP) && fail(t, SX_CUSTODY_BLOCKS)) return;
    if (not_run) (void)fail(t, SX_AREA_TEST);
}

sx_reason_t sx_repair_verdict(const sx_repair_t *r)
{
    terms_t t = {0};
    repair_terms(r, &t);
    return t.first;
}

/* ---- the inventory against the held spans (C4, critique 8; C5, critique 7) --- */
static const sx_repair_file_t *repair_file(const sx_repair_t *r, const char *bare)
{
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES; ++i)
        if (r->file[i].name[0] == '/' && !strcmp(r->file[i].name + 1, bare)) return &r->file[i];
    return NULL;
}

static const sx_inventory_file_t *inventory_file(const sx_inventory_t *v, const char *slashed)
{
    if (!slashed || slashed[0] != '/') return NULL;
    for (uint32_t i = 0; i < v->count && i < SX_INVENTORY_FILES; ++i)
        if (!strcmp(v->file[i].name, slashed + 1)) return &v->file[i];
    return NULL;
}

/* Every byte the VFS could not read is one of the spans the repair holds on
 * flash, and every span it holds is unreadable: the same set. Each span read
 * failed with EIO or ENOENT (ZKT 2.7.8: ESP-IDF reads ENOENT for a span whose
 * index entry points at a deleted page, SPIFFS_ERR_DELETED; Peshawar-02's
 * 2.7.7 refused on it); any other errno, a failed open or stat, or a file
 * that ended early still refuses. Returns why not, or SX_DETAIL_NONE. */
static sx_detail_t explain(const sx_inventory_file_t *f, const sx_repair_file_t *held)
{
    if (!held || !held->blocked_spans) return SX_DETAIL_NOT_HELD;
    if (f->other_unreadable) return SX_DETAIL_OTHER_UNREADABLE;
    if (f->span_errors_other || f->span_errors_enoent > f->unreadable_spans) return SX_DETAIL_SPAN_ERRNO;
    /* The first failed read was a span read (nothing else failed), so its
     * errno is one the spans counted. */
    if (!(f->first_error == EIO && f->span_errors_enoent < f->unreadable_spans) &&
        !(f->first_error == ENOENT && f->span_errors_enoent))
        return SX_DETAIL_FIRST_ERRNO;
    if (f->unreadable_spans != held->blocked_spans) return SX_DETAIL_SPAN_COUNT;
    if (f->first_error_offset != held->blocked_offset) return SX_DETAIL_FIRST_OFFSET;
    if (f->last_error_offset != held->blocked_last) return SX_DETAIL_LAST_OFFSET;
    if (f->span_digest != held->blocked_digest) return SX_DETAIL_DIGEST;
    if (f->unreadable_bytes > held->blocked_spans * SX_SPAN_BYTES ||
        f->unreadable_bytes <= (held->blocked_spans - 1U) * SX_SPAN_BYTES)
        return SX_DETAIL_BYTES;
    return SX_DETAIL_NONE;
}

static void note_why(sx_why_t *why, sx_detail_t detail, const char *file)
{
    if (!why || why->detail != SX_DETAIL_NONE) return;
    why->detail = detail;
    snprintf(why->file, sizeof(why->file), "%s", file ? file : "");
    /* It is reported as text: a damaged directory entry's bytes are not. */
    for (char *c = why->file; *c; ++c)
        if ((unsigned char)*c < 0x20 || (unsigned char)*c > 0x7e) *c = '?';
}

static void inventory_terms(const sx_repair_t *r, const sx_inventory_t *v, terms_t *t, sx_why_t *why)
{
    if (!r || !r->ran) {
        (void)fail(t, SX_REPAIR_MISSING);
        return;
    }
    if (!v || !v->done) {
        (void)fail(t, SX_INVENTORY_PENDING);
        return;
    }
    if (!v->complete || v->error || v->unreadable_files > v->count) {
        note_why(why, !v->complete ? SX_DETAIL_INCOMPLETE : v->error ? SX_DETAIL_SCAN_ERROR : SX_DETAIL_FILE_COUNT,
                 NULL);
        if (fail(t, SX_INVENTORY_UNEXPLAINED)) return;
    }
    uint32_t matched = 0;
    bool unexplained = false;
    for (uint32_t i = 0; i < v->count && i < SX_INVENTORY_FILES; ++i) {
        const sx_inventory_file_t *f = &v->file[i];
        if (!f->first_error) continue;
        sx_detail_t detail = explain(f, repair_file(r, f->name));
        if (detail != SX_DETAIL_NONE) {
            note_why(why, detail, f->name);
            unexplained = true;
            if (!t->all) break;
            continue;
        }
        matched++;
    }
    if (!unexplained && matched != v->unreadable_files) {
        note_why(why, SX_DETAIL_UNMATCHED, NULL);
        unexplained = true;
    }
    if (unexplained && fail(t, SX_INVENTORY_UNEXPLAINED)) return;
    bool missing = false;
    unexplained = false;
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES; ++i) {
        const sx_repair_file_t *held = &r->file[i];
        const sx_inventory_file_t *f = inventory_file(v, held->name);
        if (held->blocked_spans && (!f || !f->first_error) && !unexplained) {
            note_why(why, SX_DETAIL_HELD_READABLE, held->name + (held->name[0] == '/'));
            unexplained = true;
            if (fail(t, SX_INVENTORY_UNEXPLAINED)) return;
        }
        /* A custody file the repair kept is still there, at least its size. */
        if (held->custody && !held->removed && (!f || f->bytes < held->size) && !missing) {
            missing = true;
            if (fail(t, SX_CUSTODY_MISSING)) return;
        }
    }
}

sx_reason_t sx_inventory_verdict(const sx_repair_t *r, const sx_inventory_t *v)
{
    terms_t t = {0};
    inventory_terms(r, v, &t, NULL);
    return t.first;
}

sx_reason_t sx_inventory_explain(const sx_repair_t *r, const sx_inventory_t *v, sx_why_t *why)
{
    if (why) memset(why, 0, sizeof(*why));
    terms_t t = {0};
    inventory_terms(r, v, &t, why);
    return t.first;
}

/* ---- runtime storage evidence (S1 to S3, H2) -------------------------------- */
bool sx_probe_error_room(const char *operation, int error)
{
    if (same(operation, "persistence_capacity")) return error == ENOSPC;
    /* SPIFFS can refuse a fresh name with ENOENT when no page is free (13FL);
     * after the open, ENOENT means a file that was just written vanished. */
    if (same(operation, "persistence_open_write")) return error == ENOSPC || error == ENOENT;
    if (same(operation, "persistence_write") || same(operation, "persistence_sync") ||
        same(operation, "persistence_close_write"))
        return error == ENOSPC;
    return false;
}

bool sx_last_error_room(const char *operation, int error)
{
    static const char *const writes[] = {"capacity_admission", "local_write_commit", "segment_append",
                                         "segment_settle", "segment_recovery", "legacy_append", "legacy_retire"};
    if (error != ENOSPC) return false;
    for (size_t i = 0; i < sizeof(writes) / sizeof(writes[0]); ++i)
        if (same(operation, writes[i])) return true;
    return false;
}

static void runtime_terms(const sx_repair_t *r, const sx_runtime_t *rt, terms_t *t)
{
    if (!rt) {
        (void)fail(t, SX_STORAGE_UNKNOWN);
        return;
    }
    if (!rt->spiffs_mounted && fail(t, SX_STORE_NOT_MOUNTED)) return;
    if (!rt->qs_known) {
        if (fail(t, SX_STORAGE_UNKNOWN)) return;
    } else {
        if ((!rt->qs_observed || !rt->qs_available) && fail(t, SX_STORAGE_ERROR_CLASS)) return;
        if (rt->probe_error && !sx_probe_error_room(rt->probe_operation, rt->probe_error) &&
            fail(t, SX_PROBE_ERROR_CLASS))
            return;
        if (rt->last_error && !sx_last_error_room(rt->last_operation, rt->last_error) &&
            fail(t, SX_STORAGE_ERROR_CLASS))
            return;
    }
    if (!rt->led_known) {
        if (fail(t, SX_STORAGE_UNKNOWN)) return;
    } else {
        if (rt->led_fatal && fail(t, SX_LED_FATAL)) return;
        /* Every LOCAL_FAILURE incident of this boot was a store write refused
         * for room, or a read of a reclaim queue the repair reports blocked
         * (the inventory proved its unreadable spans are exactly the held
         * ones). */
        bool cause = rt->local_failures != rt->room_failures + rt->held_read_failures;
        for (unsigned q = 0; q < SX_QUEUE_COUNT && !cause; ++q) {
            if (!(rt->held_read_queues & (1U << q))) continue;
            const sx_repair_file_t *held = r ? repair_file(r, sx_queue_names[q] + 1) : NULL;
            /* A read that starts past the last held span did not fail on one. */
            cause = !held || !held->blocked_spans || rt->held_read_last[q] > held->blocked_last + SX_SPAN_BYTES - 1U;
        }
        if ((cause || rt->held_read_queues >> SX_QUEUE_COUNT) && fail(t, SX_LED_CAUSE)) return;
    }
    /* Every other boot-health term (3.7). */
    if (!rt->health_known) {
        if (fail(t, SX_HEALTH_UNKNOWN)) return;
    } else if ((rt->health_blockers & ~(uint32_t)SX_HEALTH_EXCEPTED) && fail(t, SX_HEALTH)) {
        return;
    }
    /* ADD and the OTA path (D1, D2, D4, critique 6). */
    if (!rt->add_connected && fail(t, SX_ADD_DISCONNECTED)) return;
    /* D2 (design 14.2 item 1): ADD acknowledged a heartbeat within
     * SX_HEARTBEAT_ACK_SECONDS. The log and the receipt prove the log and
     * HTTPS paths; the heartbeat is the channel that carries the floor, the
     * confirm reason and the exception diagnostics the operator reads. */
    if (!rt->heartbeat_acked && fail(t, SX_HEARTBEAT_UNACKED)) return;
    if (!rt->capability_accepted && fail(t, SX_CAPABILITY)) return;
    if (!rt->ota_precheck_ok && fail(t, SX_OTA_PRECHECK)) return;
    if (!rt->passive_slot_ok && fail(t, SX_OTA_SLOT)) return;
    if (rt->internal_largest < SX_INTERNAL_FLOOR_BYTES) (void)fail(t, SX_HEAP);
}

sx_reason_t sx_runtime_verdict(const sx_repair_t *r, const sx_inventory_t *v, const sx_runtime_t *rt)
{
    (void)v;
    terms_t t = {0};
    runtime_terms(r, rt, &t);
    return t.first;
}

static void evaluate_terms(const sx_input_t *in, terms_t *t, sx_why_t *why)
{
    if (!in) {
        (void)fail(t, SX_REPAIR_MISSING);
        return;
    }
    if (!in->production_role && fail(t, SX_ROLE)) return;
    if (!in->running_pending && fail(t, SX_NOT_PENDING)) return;
    if (!in->ota_deployment && fail(t, SX_NOT_OTA_IMAGE)) return;
    if (in->record_terminal && fail(t, SX_ALREADY_USED)) return;
    /* Each part's own first failing term, in order: the repair, then the
     * inventory, then the runtime (2.7.7's order). */
    terms_t part = {.all = t->all};
    repair_terms(in->repair, &part);
    t->mask |= part.mask;
    if (part.first != SX_OK && fail(t, part.first)) return;
    part = (terms_t){.all = t->all};
    inventory_terms(in->repair, in->inventory, &part, why);
    t->mask |= part.mask;
    if (part.first != SX_OK && fail(t, part.first)) return;
    part = (terms_t){.all = t->all};
    runtime_terms(in->repair, in->runtime, &part);
    t->mask |= part.mask;
    if (part.first != SX_OK) (void)fail(t, part.first);
}

sx_reason_t sx_evaluate(const sx_input_t *in)
{
    terms_t t = {0};
    evaluate_terms(in, &t, NULL);
    return t.first;
}

sx_reason_t sx_evaluate_terms(const sx_input_t *in, uint64_t *mask, sx_why_t *why)
{
    if (why) memset(why, 0, sizeof(*why));
    terms_t t = {.all = true};
    evaluate_terms(in, &t, why);
    if (mask) *mask = t.mask;
    return t.first;
}

/* ---- the stable window ------------------------------------------------------- */
bool sx_window_same(const sx_window_t *w, const sx_store_t *store)
{
    return w && store && w->open && w->store.used == store->used && w->store.total == store->total &&
        w->store.writes == store->writes;
}

sx_reason_t sx_window_step(sx_window_t *w, sx_reason_t verdict, uint32_t now_s, const sx_store_t *store)
{
    if (!w || !store) return SX_STABILIZING;
    if (sx_reason_unknown(verdict)) return verdict;
    if (verdict != SX_OK) {
        w->open = false;
        return verdict;
    }
    if (!sx_window_same(w, store)) {
        bool moved = w->open;
        w->open = true;
        w->since_s = now_s;
        w->store = *store;
        return moved ? SX_STORE_CHANGED : SX_STABILIZING;
    }
    return now_s - w->since_s >= SX_STABLE_SECONDS ? SX_OK : SX_STABILIZING;
}

/* ---- the record ------------------------------------------------------------- */
static uint32_t crc32(uint32_t crc, const void *data, size_t length)
{
    const uint8_t *p = data;
    crc = ~crc;
    for (size_t i = 0; i < length; ++i) {
        crc ^= p[i];
        for (int k = 0; k < 8; ++k) crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}

void sx_record_seal(sx_record_t *record)
{
    if (!record) return;
    record->version = SX_RECORD_VERSION;
    record->crc = crc32(0, record, offsetof(sx_record_t, crc));
}

bool sx_record_valid(const sx_record_t *record)
{
    return record && record->version == SX_RECORD_VERSION && record->state < SX_STATE_COUNT &&
        record->crc == crc32(0, record, offsetof(sx_record_t, crc));
}

uint32_t sx_deployment_crc(const char *deployment_id)
{
    return deployment_id && deployment_id[0] ? crc32(0, deployment_id, strlen(deployment_id)) : 0;
}

bool sx_record_confirmed(const sx_record_t *record, uint32_t image_tag)
{
    if (!sx_record_valid(record) || record->image_tag != image_tag) return false;
    switch ((sx_state_t)record->state) {
    case SX_STATE_CONFIRMING:
    case SX_STATE_CONFIRMED:
    case SX_STATE_ROUND_DONE:
    case SX_STATE_ROUND_SHORT:
    case SX_STATE_UNREPAIRED:
    case SX_STATE_UNHEALTHY: return true;
    default: return false;
    }
}

bool sx_exception_active(const sx_record_t *record, uint32_t image_tag, bool running_pending)
{
    return !running_pending && sx_record_confirmed(record, image_tag);
}

bool sx_record_ended(const sx_record_t *record, uint32_t deployment_crc)
{
    return sx_record_valid(record) && record->deployment_crc == deployment_crc &&
        (record->state == SX_STATE_ABANDONED || record->state == SX_STATE_UNREPAIRED ||
         record->state == SX_STATE_UNHEALTHY);
}

bool sx_record_retire_other(sx_record_t *record, uint32_t image_tag, uint32_t deployment_crc)
{
    if (!sx_record_confirmed(record, image_tag) || record->deployment_crc == deployment_crc) return false;
    record->state = SX_STATE_ABANDONED;
    sx_record_seal(record);
    return true;
}

bool sx_record_clear_on_ordinary(const sx_record_t *record, uint32_t image_tag)
{
    return sx_record_valid(record) && record->image_tag == image_tag && record->state != SX_STATE_NONE;
}

/* ---- every boot ------------------------------------------------------------ */
sx_boot_t sx_boot_action(const char *journal_state, bool running_pending, const sx_record_t *record,
                         uint32_t image_tag, uint32_t deployment_crc)
{
    if (!journal_state || !journal_state[0]) return SX_BOOT_ORDINARY;
    if (!strcmp(journal_state, "READY_TO_BOOT")) return running_pending ? SX_BOOT_CONFIRM : SX_BOOT_RECONCILE;
    if (!strcmp(journal_state, "RECONCILING") && !running_pending && sx_record_valid(record) &&
        record->image_tag == image_tag && sx_record_ended(record, deployment_crc) && !record->reported &&
        (record->state == SX_STATE_UNREPAIRED || record->state == SX_STATE_UNHEALTHY))
        return SX_BOOT_REPORT_ENDED;
    return SX_BOOT_ORDINARY;
}

/* ---- after a round boot ----------------------------------------------------- */
const char *sx_outcome_name(sx_outcome_t outcome)
{
    static const char *const names[SX_ROUND_COUNT] = {
        "OK", "TIME", "CAP", "REMOUNT", "STORE_BLOCKED", "AREA_UNREADABLE", "HOLD", "WAITING", "DEFERRED",
        "MISMATCH", "GAPS", "AREA", "GUARD", "NO_CANDIDATE", "FAILED"};
    return (unsigned)outcome < SX_ROUND_COUNT ? names[outcome] : "UNKNOWN";
}

bool sx_outcome_retryable(sx_outcome_t outcome) { return outcome > SX_ROUND_OK && outcome <= SX_ROUND_DEFERRED; }

sx_outcome_t sx_round_outcome(const sx_repair_t *a)
{
    if (!a || !a->ran) return SX_ROUND_DEFERRED;
    if (a->held_mismatch) return SX_ROUND_MISMATCH;
    if (same(a->area, "FOREIGN") || same(a->area, "ABSENT") || same(a->area, "OVERLAP")) return SX_ROUND_AREA;
    if (same(a->result, "REPORT_ONLY")) return SX_ROUND_GUARD;
    bool repaired = same(a->result, "REPAIRED") || same(a->result, "CLEAN") || same(a->result, "REPAIRED_WITH_GAPS");
    if (repaired && a->storage_allowed && !a->waiting && !a->store_blocked) return SX_ROUND_OK;
    if (a->store_blocked || (same(a->result, "NOT_RUN") && same(a->error_stage, "unmounted")))
        return SX_ROUND_STORE_BLOCKED;
    /* 2.7.6 decision 5 (c) on exception devices: spans that wait for byte
     * sources keep the VFS off; a later boot with room puts them back. */
    if (a->waiting) return SX_ROUND_WAITING;
    if (same(a->area, "UNREADABLE") || same(a->area, "NO_MEMORY")) return SX_ROUND_AREA_UNREADABLE;
    if (same(a->error_stage, "remount")) return SX_ROUND_REMOUNT;
    if (a->gaps) return SX_ROUND_GAPS;
    if (same(a->short_reason, "hold")) return SX_ROUND_HOLD;
    /* Only after progress: a round that freed nothing will not next time. */
    if (same(a->short_reason, "time") || same(a->result, "STOPPED"))
        return a->pages_freed ? SX_ROUND_TIME : SX_ROUND_FAILED;
    if (same(a->short_reason, "cap")) return a->pages_freed ? SX_ROUND_CAP : SX_ROUND_FAILED;
    if (same(a->short_reason, "candidate")) return SX_ROUND_NO_CANDIDATE;
    if (same(a->result, "DEFERRED") || same(a->result, "NOT_RUN"))
        return a->pages_freed ? SX_ROUND_DEFERRED : SX_ROUND_FAILED;
    return SX_ROUND_FAILED;
}

bool sx_record_after_boot(sx_record_t *r, uint32_t image_tag, bool running_pending, bool previous_crashed,
                          sx_outcome_t outcome)
{
    if (!sx_exception_active(r, image_tag, running_pending)) return false;
    sx_state_t state = (sx_state_t)r->state;
    bool round = state == SX_STATE_CONFIRMING || state == SX_STATE_CONFIRMED || state == SX_STATE_ROUND_SHORT;
    if (!round && state != SX_STATE_ROUND_DONE) return false;
    bool changed = false;
    if (previous_crashed && r->crashes < UINT8_MAX) {
        r->crashes++;
        changed = true;
    }
    if (round) {
        r->state = (uint8_t)(outcome == SX_ROUND_OK ? SX_STATE_ROUND_DONE : SX_STATE_ROUND_SHORT);
        r->reason = (uint8_t)outcome;
        if (r->round_boots < UINT8_MAX) r->round_boots++;
        changed = true;
    }
    if (changed) sx_record_seal(r);
    return changed;
}

sx_next_t sx_next_step(const sx_record_t *record, bool time_known, uint32_t now_epoch, uint32_t uptime_s,
                       uint32_t *wait_s)
{
    if (wait_s) *wait_s = 0;
    if (!sx_record_valid(record)) return SX_NEXT_NONE;
    sx_state_t state = (sx_state_t)record->state;
    if ((state == SX_STATE_CONFIRMED || state == SX_STATE_CONFIRMING) && !record->round_boots) {
        /* The confirming boot: its one restart, at a capture safepoint (also
         * when the CONFIRMED record could not be written: CONFIRMING on a
         * valid image is confirmed). */
        return record->restarts ? SX_NEXT_UNREPAIRED : SX_NEXT_RESTART;
    }
    if (state != SX_STATE_ROUND_SHORT && state != SX_STATE_CONFIRMED) return SX_NEXT_NONE;
    /* CONFIRMED with a round boot behind it: that boot could not record its
     * outcome (an NVS write failed); a further boot cannot tell more. */
    if (state == SX_STATE_CONFIRMED) return SX_NEXT_UNREPAIRED;
    if (record->crashes || !sx_outcome_retryable((sx_outcome_t)record->reason) || record->restarts >= SX_MAX_RESTARTS)
        return SX_NEXT_UNREPAIRED;
    uint32_t backoff = record->restarts <= 1 ? SX_RETRY_SECONDS_2 : SX_RETRY_SECONDS_3;
    if (!time_known || !record->restart_epoch) {
        /* The wall clock is unknown now, or was when the restart was counted:
         * the backoff runs on this boot's own clock. SX_MAX_RESTARTS still
         * bounds the total, so a reset before the wait ends only repeats the
         * wait; it can never loop (design 14.2 item 4). */
        if (uptime_s >= backoff) return SX_NEXT_RESTART;
        if (wait_s) *wait_s = backoff - uptime_s;
        return SX_NEXT_WAIT;
    }
    uint32_t due = record->restart_epoch + backoff;
    if (now_epoch >= due) return SX_NEXT_RESTART;
    if (wait_s) *wait_s = due - now_epoch;
    return SX_NEXT_WAIT;
}

/* ---- the OTA floor ------------------------------------------------------------- */
bool sx_version_redo_aware(const char *version)
{
    if (!version) return false;
    if (!strncmp(version, "zone-lite-", 10)) version += 10;
    unsigned long part[3] = {0, 0, 0};
    const char *p = version;
    for (int i = 0; i < 3; ++i) {
        if (*p < '0' || *p > '9') return false;
        char *end = NULL;
        part[i] = strtoul(p, &end, 10);
        if (end == p || part[i] > 9999) return false;
        p = end;
        if (i < 2) {
            if (*p != '.') return false;
            ++p;
        }
    }
    if (*p) return false;
    if (part[0] != 2) return part[0] > 2;
    if (part[1] != 7) return part[1] > 7;
    return part[2] >= 6;
}
