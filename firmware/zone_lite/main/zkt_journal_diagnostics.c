#include "zkt_journal_diagnostics.h"
#include <string.h>

static const char *result_name(zj_result_t result)
{
    switch (result) {
        case ZJ_OK: return "OK";
        case ZJ_EMPTY: return "EMPTY";
        case ZJ_FULL: return "FULL";
        case ZJ_IO: return "IO";
        case ZJ_CORRUPT: return "CORRUPT";
        case ZJ_STALE: return "STALE";
        case ZJ_INVALID: return "INVALID";
        case ZJ_UNCERTAIN: return "UNCERTAIN";
        default: return "UNKNOWN";
    }
}
static const char *owner_operation(zj_operation_t operation)
{
    switch (operation) {
        case ZJ_APPEND: return "preserving capture";
        case ZJ_SETTLE: return "committing custody receipt";
        case ZJ_PEEK: return "reading preserved capture";
        case ZJ_RECLAIM: return "reclaiming settled segment";
        case ZJ_READER_CHECK: return "checking compatible reader";
        case ZJ_OTA_CHECK: return "checking upgrade target";
        case ZJ_SELECT_READER: return "selecting compatible reader";
        case ZJ_RUNTIME_CHECKPOINT: return "committing runtime checkpoint";
        case ZJ_CATALOG: return "updating optional identity catalog";
        case ZJ_LEASE: return "committing administrator lease";
        case ZJ_COMMANDS: return "updating durable command inbox";
        default: return "unknown operation";
    }
}
static const char *delivery_operation(zj_delivery_phase_t phase)
{
    switch (phase) {
        case ZJ_DELIVERY_IDLE: return "idle or retry delay";
        case ZJ_DELIVERY_SUBMIT_READ: return "requesting preserved capture";
        case ZJ_DELIVERY_WAIT: return "waiting for storage result";
        case ZJ_DELIVERY_ENCODE: return "encoding capture evidence";
        case ZJ_DELIVERY_SEND: return "waiting for ADD custody receipt";
        case ZJ_DELIVERY_SUBMIT_SETTLE: return "requesting receipt checkpoint";
        case ZJ_DELIVERY_SUBMIT_RECLAIM: return "requesting settled reclamation";
        case ZJ_DELIVERY_ABANDON: return "releasing timed out reply";
        default: return "unknown operation";
    }
}
static bool recent32(uint64_t now, uint32_t sampled)
{
    uint32_t age = (uint32_t)now - sampled;
    return age < 45000U && age <= now;
}
/* Lift the worker's wrapping millisecond clock into this boot's 64-bit
 * uptime. Very old/ambiguous or future timestamps remain unreported. */
static bool add_time(cJSON *object, const char *name, uint64_t now, uint32_t sampled)
{
    uint32_t age = (uint32_t)now - sampled;
    return age >= 0x80000000U || age > now || cJSON_AddNumberToObject(object, name, (double)(now - age));
}
static bool replace(cJSON *object, const char *name, cJSON *value)
{
    if (!value) return false;
    if (cJSON_ReplaceItemInObjectCaseSensitive(object, name, value)) return true;
    cJSON_Delete(value);
    return false;
}
static cJSON *worker(cJSON *workers, const char *name, const char *state, const char *operation,
                     uint32_t starts)
{
    cJSON *entry = cJSON_CreateObject();
    if (!entry) return NULL;
    if (!cJSON_AddItemToArray(workers, entry)) { cJSON_Delete(entry); return NULL; }
    if (!cJSON_AddStringToObject(entry, "name", name) || !cJSON_AddStringToObject(entry, "state", state) ||
        !cJSON_AddStringToObject(entry, "operation", operation) ||
        !cJSON_AddNumberToObject(entry, "restart_count", starts ? starts - 1U : 0U)) return NULL;
    return entry;
}
static bool queue(cJSON *queues, const char *name, bool empty, const char *reason,
                  bool bytes_known, uint64_t bytes)
{
    cJSON *entry = cJSON_CreateObject();
    if (!entry) return false;
    if (!cJSON_AddItemToArray(queues, entry)) { cJSON_Delete(entry); return false; }
    return cJSON_AddStringToObject(entry, "name", name) &&
        cJSON_AddBoolToObject(entry, "count_known", empty) &&
        cJSON_AddStringToObject(entry, "count_reason", reason) &&
        (!empty || cJSON_AddNumberToObject(entry, "records", 0)) &&
        (!bytes_known || cJSON_AddNumberToObject(entry, "bytes", (double)bytes));
}
bool zj_diagnostics_append(cJSON *diagnostics, const zj_boot_t *boot,
                           bool recent, bool legacy, const zj_diagnostics_snapshot_t *sample,
                           uint64_t now)
{
    cJSON *workers = cJSON_GetObjectItemCaseSensitive(diagnostics, "workers");
    cJSON *queues = cJSON_GetObjectItemCaseSensitive(diagnostics, "queues");
    cJSON *storage = cJSON_GetObjectItemCaseSensitive(diagnostics, "storage");
    if (!cJSON_IsArray(workers) || !cJSON_IsArray(queues) || !cJSON_IsObject(storage)) return false;
    if (!legacy) {
        cJSON *entry;
        cJSON_ArrayForEach(entry, workers) {
            cJSON *name = cJSON_GetObjectItemCaseSensitive(entry, "name");
            if (!cJSON_IsString(name)) return false;
            const char *renamed = !strcmp(name->valuestring, "add_delivery") ? "legacy_add_delivery" :
                !strcmp(name->valuestring, "ords_delivery") ? "legacy_ords_delivery" : NULL;
            if (renamed && !replace(entry, "name", cJSON_CreateString(renamed))) return false;
        }
    }
    const zj_owner_health_t *owner = &sample->owner;
    const zj_transport_health_t *transport = &sample->transport;
    const zj_capture_health_t *capture = &sample->capture;
    bool owner_fresh = recent && sample->owner_observed && owner->sampled_uptime_us / 1000 <= now &&
        now - owner->sampled_uptime_us / 1000 < 45000U;
    bool owner_stalled = owner_fresh && owner->operation_running &&
        (owner->operation_started_us / 1000 > now || now - owner->operation_started_us / 1000 >= 15000U);
    bool owner_ready = owner_fresh && owner->started && owner->ready && !owner_stalled &&
        !owner->recovering && !owner->quiescing && !owner->checkpoint_recovery_pending;
    bool append_failed = owner->append_observed && owner->last_append_result != ZJ_OK;
    const char *owner_state = !recent || !sample->owner_observed ? "UNKNOWN" : !owner->started ? "STOPPED" :
        !owner_fresh || owner_stalled ? "FAULT" : owner->quiesced ? "STOPPED" :
        !owner_ready || append_failed ? "WAITING_RESOURCE" : "RUNNING";
    cJSON *entry = worker(workers, "storage_owner", owner_state,
        owner->recovering ? "recovering journal" : owner->quiescing ? "finishing accepted storage work" :
        owner->operation_running ? owner_operation(owner->operation) : "idle", boot->owner_starts);
    if (!entry || !cJSON_AddNumberToObject(entry, "pending_requests", owner->occupied) ||
        !cJSON_AddNumberToObject(entry, "completed_operations", (double)owner->completed) ||
        !cJSON_AddNumberToObject(entry, "failures", (double)owner->failures) ||
        !cJSON_AddNumberToObject(entry, "refusals", (double)owner->refused) ||
        !cJSON_AddNumberToObject(entry, "max_operation_ms", (double)(owner->max_operation_us / 1000)) ||
        (owner_fresh && (!cJSON_AddNumberToObject(entry, "last_activity_uptime_ms", (double)(owner->sampled_uptime_us / 1000)) ||
            !cJSON_AddNumberToObject(entry, "last_progress_uptime_ms", (double)(owner->progress_uptime_us / 1000)))) ||
        (owner->operation_running && !cJSON_AddNumberToObject(entry, "operation_started_uptime_ms", (double)(owner->operation_started_us / 1000)))) return false;

    bool capture_started = recent && boot->capture_started && sample->capture_observed;
    bool capture_stalled = capture_started && capture->running && (uint32_t)((uint32_t)now - capture->started_ms) >= 15000U;
    bool capture_ready = capture_started && boot->writer_ready && owner_ready;
    const char *capture_state = !recent ? "UNKNOWN" : !boot->capture_started ? "STOPPED" :
        !sample->capture_observed ? "UNKNOWN" : capture_stalled ? "FAULT" :
        !capture_ready || (!capture->running && capture->failures && capture->last_result != ZJ_OK) ? "WAITING_RESOURCE" : "RUNNING";
    if (!legacy) {
    entry = worker(workers, "capture", capture_state, capture->running ? "preserving terminal packet" :
        capture_ready ? "waiting for terminal packet" : "waiting for writer permission", boot->capture_starts);
    if (!entry || !cJSON_AddStringToObject(entry, "execution_model", "ON_DEMAND") ||
        !cJSON_AddNumberToObject(entry, "sampled_uptime_ms", (double)now) ||
        !cJSON_AddNumberToObject(entry, "pending_requests", capture->pending_ticket ? 1 : 0) ||
        !cJSON_AddNumberToObject(entry, "completed_operations", (double)capture->packets) ||
        !cJSON_AddNumberToObject(entry, "failures", (double)capture->failures) ||
        !cJSON_AddNumberToObject(entry, "timeouts", (double)capture->timeouts) ||
        (capture_started && !add_time(entry, "last_activity_uptime_ms", now, capture->sampled_ms)) ||
        ((capture->packets || capture->fragments) && !add_time(entry, "last_progress_uptime_ms", now, capture->progress_ms)) ||
        (capture->running && !add_time(entry, "operation_started_uptime_ms", now, capture->started_ms))) return false;
    }

    bool transport_fresh = recent && sample->transport_observed && recent32(now, transport->sampled_ms);
    const zj_delivery_health_t *delivery = &transport->delivery;
    uint32_t deadline = delivery->phase == ZJ_DELIVERY_SEND ? 20000U : 15000U;
    bool transport_stalled = transport_fresh && delivery->phase != ZJ_DELIVERY_IDLE &&
        (uint32_t)((uint32_t)now - delivery->phase_started_ms) >= deadline;
    const char *transport_state = !recent || !sample->transport_observed ? "UNKNOWN" : !transport->started ? "STOPPED" :
        !transport_fresh || transport_stalled ? "FAULT" : delivery->phase == ZJ_DELIVERY_SEND ||
        (delivery->consecutive_failures && delivery->last_failure && !strcmp(delivery->last_failure, "add_custody")) ? "WAITING_NETWORK" :
        delivery->consecutive_failures ? "WAITING_RESOURCE" : "RUNNING";
    entry = worker(workers, legacy ? "journal_add_delivery" : "add_delivery", transport_state,
        delivery_operation(delivery->phase), boot->transport_starts);
    if (!entry || !cJSON_AddNumberToObject(entry, "pending_requests", delivery->pending_ticket ? 1 : 0) ||
        !cJSON_AddNumberToObject(entry, "completed_operations", (double)delivery->settled) ||
        !cJSON_AddNumberToObject(entry, "failures", (double)delivery->failures) ||
        !cJSON_AddNumberToObject(entry, "consecutive_failures", delivery->consecutive_failures) ||
        !cJSON_AddNumberToObject(entry, "timeouts", (double)delivery->timeouts) ||
        !cJSON_AddNumberToObject(entry, "refusals", (double)delivery->refused) ||
        (transport_fresh && !add_time(entry, "last_activity_uptime_ms", now, transport->sampled_ms)) ||
        (sample->transport_observed && !add_time(entry, "last_progress_uptime_ms", now, delivery->progress_ms)) ||
        (delivery->phase != ZJ_DELIVERY_IDLE && !add_time(entry, "operation_started_uptime_ms", now, delivery->phase_started_ms))) return false;

    const char *durability = !owner_fresh ? "UNKNOWN" : !owner_ready ? "DEGRADED" :
        append_failed ? owner->last_append_result == ZJ_FULL ? "FULL" : "DEGRADED" : "HEALTHY";
    cJSON *journal = cJSON_AddObjectToObject(diagnostics, "journal_storage");
    if (!journal || !cJSON_AddBoolToObject(journal, "observed", sample->owner_observed) ||
        !cJSON_AddBoolToObject(journal, "fresh", owner_fresh) ||
        !cJSON_AddBoolToObject(journal, "ready", owner_ready) ||
        !cJSON_AddStringToObject(journal, "durability", durability) ||
        !cJSON_AddBoolToObject(journal, "checkpoint_recovery_pending", owner->checkpoint_recovery_pending) ||
        !cJSON_AddNumberToObject(journal, "mailbox_capacity", ZJ_REQUEST_SLOTS) ||
        !cJSON_AddNumberToObject(journal, "mailbox_high_watermark", owner->high_watermark) ||
        !cJSON_AddNumberToObject(journal, "pending_appends", owner->pending_appends) ||
        (owner_fresh && !cJSON_AddNumberToObject(journal, "sampled_uptime_ms", (double)(owner->sampled_uptime_us / 1000))) ||
        (owner_fresh && owner->inventory_known && !cJSON_AddNumberToObject(journal, "segments", owner->journal_segments)) ||
        (owner->append_observed && (!cJSON_AddStringToObject(journal, "last_append_result", result_name(owner->last_append_result)) ||
            !cJSON_AddNumberToObject(journal, "last_append_uptime_ms", (double)(owner->last_append_uptime_us / 1000)))) ||
        (owner->failed_operation && !cJSON_AddStringToObject(journal, "last_failure_operation", owner->failed_operation)) ||
        (owner->failures && (!cJSON_AddNumberToObject(journal, "last_filesystem_error", owner->filesystem_error) ||
            !cJSON_AddNumberToObject(journal, "last_nvs_error", owner->nvs_error)))) return false;

    /* Never let a legacy filesystem probe promote a broken journal. Preserve
     * an independently reported legacy fault; this path can only downgrade. */
    if (!legacy && strcmp(durability, "HEALTHY")) {
        cJSON *original = cJSON_GetObjectItemCaseSensitive(storage, "durability");
        if ((!cJSON_IsString(original) || !strcmp(original->valuestring, "HEALTHY") || !strcmp(original->valuestring, "UNKNOWN")) &&
            !replace(storage, "durability", cJSON_CreateString(durability))) return false;
        if (!replace(storage, "persistence_verified", cJSON_CreateBool(false)) ||
            !replace(storage, "recovery_complete", cJSON_CreateBool(false))) return false;
    }
    bool empty = owner_ready && owner->inventory_known && owner->verified_empty && !owner->operation_running && !owner->pending_appends;
    return queue(queues, "journal", empty, !owner_fresh ? "STALE_OWNER" : owner->pending_appends ? "PENDING_APPEND" :
            empty ? "VERIFIED_EMPTY" : "NONEMPTY_OR_UNVERIFIED", owner_fresh && owner->inventory_known, owner->journal_bytes) &&
        queue(queues, "legacy_migration", false, "UNVERIFIED_MIGRATION", false, 0);
}
