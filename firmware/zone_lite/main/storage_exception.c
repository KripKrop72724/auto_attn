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
        "ADD_DISCONNECTED", "CAPABILITY", "OTA_PRECHECK", "OTA_SLOT", "HEAP",
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

/* ---- the boot repair ------------------------------------------------------ */
sx_reason_t sx_repair_verdict(const sx_repair_t *r)
{
    if (!r || !r->ran) return SX_REPAIR_MISSING;
    if (!r->pending_verify) return SX_NOT_PENDING;
    /* The attempts guard did not act, the private mount worked, and the VFS
     * may mount the store (A6). */
    if (r->previous_failed || !same(r->mode, "FULL")) return SX_REPAIR_GUARD;
    if (r->mount_failed || !same(r->result, "DEFERRED")) return SX_REPAIR_RESULT;
    if (!r->storage_allowed) return SX_STORE_NOT_MOUNTED;
    /* Deferred for room only (A2): the run ended full or GC-unsafe, and the
     * last resort was asked for exactly that. */
    if ((!same(r->error_stage, "full") && !same(r->error_stage, "gc_unsafe")) || !same(r->trigger, r->error_stage) ||
        r->error_code != SX_SPIFFS_ERR_FULL)
        return SX_REPAIR_STAGE;
    /* The redo area is ready and proved writable on this device (A4, critique 3). */
    if (!same(r->area, "READY")) return SX_AREA_NOT_READY;
    if (!same(r->area_test, "PASSED")) return SX_AREA_TEST;
    /* No block the unconfirmed rules allow (A3), and none taken (narrowest). */
    if (!same(r->short_reason, "candidate")) return SX_SHORT_REASON;
    if (r->rounds || r->blocks) return SX_ROUND_RAN;
    /* Nothing of the last resort faulted or waits (A5). */
    if (r->failed || r->held_mismatch || r->records_open || r->reported || r->sources || r->store_blocked ||
        r->waiting)
        return SX_RECLAIM_FAULT;
    /* No manifest the confirmed boot would take as stale (critique 4). */
    if (r->manifest_active) return SX_MANIFEST;
    /* The store arrived without the room the work needs: not one this image's
     * own run filled (critique 10). */
    if (!r->mount_measured || r->erased_at_mount >= r->target) return SX_ARRIVED_WITH_ROOM;
    if (r->check_deleted_live || r->check_delete_bad_file) return SX_CHECK_DELETED;
    /* Custody intact (C1 to C3): every custody file held, nothing lost, every
     * record acknowledged by ADD. */
    if (r->files_unheld_custody) return SX_CUSTODY_UNHELD;
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES; ++i)
        if (r->file[i].unheld) return SX_CUSTODY_UNHELD;
    if (r->gaps || r->unreadable_bytes || r->records_dropped || r->interrupted_losses || r->commands_damaged)
        return SX_CUSTODY_LOST;
    if (r->records_pending) return SX_RECORDS_UNACKED;
    /* Room by the planner's own estimate (P1 to P3). */
    if (!r->want) return SX_ROOM_UNKNOWN;
    if (r->custody_gain < r->want) return SX_CUSTODY_SHORT;
    if (!r->custody_blocks || r->custody_blocks > SX_ROUND_BLOCK_CAP) return SX_CUSTODY_BLOCKS;
    return SX_OK;
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
 * flash, and every span it holds is unreadable: the same set. */
static bool explained(const sx_inventory_file_t *f, const sx_repair_file_t *held)
{
    return held && held->blocked_spans && f->first_error == EIO && !f->other_unreadable && !f->span_errors_other &&
        f->unreadable_spans == held->blocked_spans && f->first_error_offset == held->blocked_offset &&
        f->last_error_offset == held->blocked_last && f->span_digest == held->blocked_digest &&
        f->unreadable_bytes <= held->blocked_spans * SX_SPAN_BYTES &&
        f->unreadable_bytes > (held->blocked_spans - 1U) * SX_SPAN_BYTES;
}

sx_reason_t sx_inventory_verdict(const sx_repair_t *r, const sx_inventory_t *v)
{
    if (!r || !r->ran) return SX_REPAIR_MISSING;
    if (!v || !v->done) return SX_INVENTORY_PENDING;
    if (!v->complete || v->error || v->unreadable_files > v->count) return SX_INVENTORY_UNEXPLAINED;
    uint32_t matched = 0;
    for (uint32_t i = 0; i < v->count && i < SX_INVENTORY_FILES; ++i) {
        const sx_inventory_file_t *f = &v->file[i];
        if (!f->first_error) continue;
        if (!explained(f, repair_file(r, f->name))) return SX_INVENTORY_UNEXPLAINED;
        matched++;
    }
    if (matched != v->unreadable_files) return SX_INVENTORY_UNEXPLAINED;
    for (uint32_t i = 0; i < r->file_count && i < SX_REPAIR_FILES; ++i) {
        const sx_repair_file_t *held = &r->file[i];
        const sx_inventory_file_t *f = inventory_file(v, held->name);
        if (held->blocked_spans && (!f || !f->first_error)) return SX_INVENTORY_UNEXPLAINED;
        /* A custody file the repair kept is still there, at least its size. */
        if (held->custody && !held->removed && (!f || f->bytes < held->size)) return SX_CUSTODY_MISSING;
    }
    return SX_OK;
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

sx_reason_t sx_runtime_verdict(const sx_repair_t *r, const sx_inventory_t *v, const sx_runtime_t *rt)
{
    (void)v;
    if (!rt) return SX_STORAGE_UNKNOWN;
    if (!rt->spiffs_mounted) return SX_STORE_NOT_MOUNTED;
    if (!rt->qs_known) return SX_STORAGE_UNKNOWN;
    if (!rt->qs_observed || !rt->qs_available) return SX_STORAGE_ERROR_CLASS;
    if (rt->probe_error && !sx_probe_error_room(rt->probe_operation, rt->probe_error)) return SX_PROBE_ERROR_CLASS;
    if (rt->last_error && !sx_last_error_room(rt->last_operation, rt->last_error)) return SX_STORAGE_ERROR_CLASS;
    if (!rt->led_known) return SX_STORAGE_UNKNOWN;
    if (rt->led_fatal) return SX_LED_FATAL;
    /* Every LOCAL_FAILURE incident of this boot was a store write refused for
     * room, or a read of a reclaim queue the repair reports blocked (the
     * inventory proved its unreadable spans are exactly the held ones). */
    if (rt->local_failures != rt->room_failures + rt->held_read_failures) return SX_LED_CAUSE;
    for (unsigned q = 0; q < SX_QUEUE_COUNT; ++q) {
        if (!(rt->held_read_queues & (1U << q))) continue;
        const sx_repair_file_t *held = r ? repair_file(r, sx_queue_names[q] + 1) : NULL;
        if (!held || !held->blocked_spans) return SX_LED_CAUSE;
        /* A read that starts past the last held span did not fail on one. */
        if (rt->held_read_last[q] > held->blocked_last + SX_SPAN_BYTES - 1U) return SX_LED_CAUSE;
    }
    if (rt->held_read_queues >> SX_QUEUE_COUNT) return SX_LED_CAUSE;
    /* Every other boot-health term (3.7). */
    if (!rt->health_known) return SX_HEALTH_UNKNOWN;
    if (rt->health_blockers & ~(uint32_t)SX_HEALTH_EXCEPTED) return SX_HEALTH;
    /* ADD and the OTA path (D1, D4, critique 6). */
    if (!rt->add_connected) return SX_ADD_DISCONNECTED;
    if (!rt->capability_accepted) return SX_CAPABILITY;
    if (!rt->ota_precheck_ok) return SX_OTA_PRECHECK;
    if (!rt->passive_slot_ok) return SX_OTA_SLOT;
    if (rt->internal_largest < SX_INTERNAL_FLOOR_BYTES) return SX_HEAP;
    return SX_OK;
}

sx_reason_t sx_evaluate(const sx_input_t *in)
{
    if (!in) return SX_REPAIR_MISSING;
    if (!in->production_role) return SX_ROLE;
    if (!in->running_pending) return SX_NOT_PENDING;
    if (!in->ota_deployment) return SX_NOT_OTA_IMAGE;
    if (in->record_terminal) return SX_ALREADY_USED;
    sx_reason_t reason = sx_repair_verdict(in->repair);
    if (reason == SX_OK) reason = sx_inventory_verdict(in->repair, in->inventory);
    if (reason == SX_OK) reason = sx_runtime_verdict(in->repair, in->inventory, in->runtime);
    return reason;
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

sx_next_t sx_next_step(const sx_record_t *record, bool time_known, uint32_t now_epoch, uint32_t *wait_s)
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
    if (!time_known || !record->restart_epoch) return SX_NEXT_WAIT;
    uint32_t backoff = record->restarts <= 1 ? SX_RETRY_SECONDS_2 : SX_RETRY_SECONDS_3;
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
