#include "storage_repair_plan.h"

#include <stdio.h>
#include <string.h>

#include "reliability.h"

uint32_t srp_crc32(uint32_t crc, const void *data, size_t length)
{
    /* The legacy queue's prefix CRC: zlib CRC-32, chained. */
    const unsigned char *bytes = data;
    crc = ~crc;
    while (length--) {
        crc ^= *bytes++;
        for (unsigned bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}

/* ---- classes -------------------------------------------------------- */
typedef struct {
    const char *name;
    srp_class_t cls;
    srp_fill_t fill;
    uint8_t flags;
    const char *lq_namespace, *lq_key;
} srp_known_t;

/* Every file the 2.4.12 to 2.7.x images keep under /storage. */
static const srp_known_t KNOWN[] = {
    {"/pending.jsonl", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, SRP_APPEND_TARGET, "legacy_queues", "ords_pending"},
    {"/pending.bak", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/pending.tmp", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/blocked_identity.jsonl", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, SRP_APPEND_TARGET, "legacy_queues", "blocked"},
    {"/blocked_recovery.bak", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/blocked_recovery.tmp", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/add_live.jsonl", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, SRP_APPEND_TARGET, "add_legacy", "live"},
    {"/add_live.bak", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/add_live.tmp", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/add_pending.jsonl", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, SRP_APPEND_TARGET, "add_legacy", "reconcile"},
    {"/add_pending.bak", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/add_pending.tmp", SRP_CLASS_ATTENDANCE, SRP_FILL_JSON_ROWS, 0, NULL, NULL},
    {"/corrupt_ords.jsonl", SRP_CLASS_EVIDENCE, SRP_FILL_JSON_ROWS, SRP_APPEND_TARGET, "legacy_queues", "old_qo"},
    {"/add_corrupt.jsonl", SRP_CLASS_EVIDENCE, SRP_FILL_JSON_ROWS, 0, "legacy_queues", "old_qa"},
    {"/add_corrupt.bak", SRP_CLASS_EVIDENCE, SRP_FILL_JSON_ROWS, 0, "legacy_queues", "old_qb"},
    {"/add_commands.jsonl", SRP_CLASS_OPERATIONAL, SRP_FILL_BINARY, SRP_COMMAND_FAMILY, NULL, NULL},
    {"/add_commands.tmp", SRP_CLASS_OPERATIONAL, SRP_FILL_BINARY, SRP_COMMAND_FAMILY, NULL, NULL},
    {"/add_commands.bak", SRP_CLASS_OPERATIONAL, SRP_FILL_BINARY, SRP_COMMAND_FAMILY, NULL, NULL},
    {"/add_commands.producer", SRP_CLASS_OPERATIONAL, SRP_FILL_BINARY, SRP_COMMAND_FAMILY, NULL, NULL},
    {"/add_commands.spare", SRP_CLASS_OPERATIONAL, SRP_FILL_BINARY, SRP_COMMAND_FAMILY, NULL, NULL},
    {"/processed_commands.txt", SRP_CLASS_OPERATIONAL, SRP_FILL_ID_LINES, SRP_APPEND_TARGET, NULL, NULL},
    {"/add_cancelled.txt", SRP_CLASS_OPERATIONAL, SRP_FILL_ID_LINES, SRP_APPEND_TARGET, NULL, NULL},
    {"/add_live.pos", SRP_CLASS_OPERATIONAL, SRP_FILL_CURSOR, 0, NULL, NULL},
    {"/add_pending.pos", SRP_CLASS_OPERATIONAL, SRP_FILL_CURSOR, 0, NULL, NULL},
    {"/acked_uids.txt", SRP_CLASS_DEDUPE, SRP_FILL_ID_LINES, SRP_APPEND_TARGET, NULL, NULL},
    {"/add_identities.enc", SRP_CLASS_CATALOG, SRP_FILL_BINARY, SRP_CATALOG_FAMILY, NULL, NULL},
    {"/add_identities.tmp", SRP_CLASS_CATALOG, SRP_FILL_BINARY, SRP_CATALOG_FAMILY, NULL, NULL},
    {"/add_identities.stage", SRP_CLASS_CATALOG, SRP_FILL_BINARY, SRP_CATALOG_FAMILY, NULL, NULL},
    {"/add_identities.backup", SRP_CLASS_CATALOG, SRP_FILL_BINARY, SRP_CATALOG_FAMILY, NULL, NULL},
    {"/add_identities.commit", SRP_CLASS_CATALOG, SRP_FILL_BINARY, SRP_CATALOG_FAMILY, NULL, NULL},
    {"/persistence-probe", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
    {"/persistence-probe-2", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
    {"/repair-probe", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
    {"/repair-probe-2", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
    {"/add_live.pos.tmp", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
    {"/add_pending.pos.tmp", SRP_CLASS_EXPENDABLE, SRP_FILL_BINARY, 0, NULL, NULL},
};

static bool hex_run(const char *text, size_t length)
{
    for (size_t i = 0; i < length; ++i) {
        char c = text[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

srp_kind_t srp_classify(const char *name)
{
    srp_kind_t kind = {SRP_CLASS_UNKNOWN, SRP_FILL_BINARY, 0, NULL, NULL};
    if (!name) return kind;
    for (size_t i = 0; i < sizeof(KNOWN) / sizeof(KNOWN[0]); ++i) {
        if (!strcmp(name, KNOWN[i].name)) {
            kind.cls = KNOWN[i].cls;
            kind.fill = KNOWN[i].fill;
            kind.flags = KNOWN[i].flags;
            kind.lq_namespace = KNOWN[i].lq_namespace;
            kind.lq_key = KNOWN[i].lq_key;
            return kind;
        }
    }
    size_t length = strlen(name);
    /* durable_queue segments: /q<lane><8 hex>.q (queue_store.c names). */
    if (length == 13 && name[0] == '/' && name[1] == 'q' && strchr("lboirhe", name[2]) && name[2] &&
        hex_run(name + 3, 8) && !strcmp(name + 11, ".q")) {
        kind.cls = SRP_CLASS_ATTENDANCE;
        kind.flags = SRP_SEGMENT;
        return kind;
    }
    /* Journal segments: /zktj<16 hex>.j (zkt_journal_store.c). */
    if (length == 23 && !strncmp(name, "/zktj", 5) && hex_run(name + 5, 16) && !strcmp(name + 21, ".j")) {
        kind.cls = SRP_CLASS_EVIDENCE;
        kind.flags = SRP_SEGMENT;
        return kind;
    }
    return kind;
}

const char *srp_class_name(srp_class_t cls)
{
    switch (cls) {
    case SRP_CLASS_ATTENDANCE: return "attendance";
    case SRP_CLASS_EVIDENCE: return "evidence";
    case SRP_CLASS_OPERATIONAL: return "operational";
    case SRP_CLASS_DEDUPE: return "dedupe";
    case SRP_CLASS_CATALOG: return "catalog";
    case SRP_CLASS_EXPENDABLE: return "expendable";
    case SRP_CLASS_UNKNOWN: break;
    }
    return "unknown";
}

unsigned srp_class_priority(srp_class_t cls)
{
    switch (cls) {
    case SRP_CLASS_ATTENDANCE: return 0;
    case SRP_CLASS_EVIDENCE: return 1;
    case SRP_CLASS_OPERATIONAL: return 2;
    case SRP_CLASS_DEDUPE: return 3;
    case SRP_CLASS_UNKNOWN: return 4;
    case SRP_CLASS_CATALOG: return 5;
    case SRP_CLASS_EXPENDABLE: return 6;
    }
    return 4;
}

/* ---- fill ------------------------------------------------------------ */
static bool complete_object(const uint8_t *row, size_t length)
{
    return length >= 2 && row[0] == '{' && !memchr(row, 0, length) &&
        rel_json_syntax_valid((const char *)row, length);
}

void srp_fill(srp_fill_t fill, const uint8_t *prefix, size_t prefix_length, const uint8_t *suffix,
              size_t suffix_length, bool suffix_complete, uint8_t *out, size_t length)
{
    if (!out || !length) return;
    switch (fill) {
    case SRP_FILL_ID_LINES:
        memset(out, '\n', length);
        return;
    case SRP_FILL_CURSOR:
        memset(out, '0', length);
        out[length - 1] = '\n';
        return;
    case SRP_FILL_JSON_ROWS:
        memset(out, 0, length);
        /* A readable row whose newline was lost keeps its own boundary. */
        if (prefix && prefix_length && complete_object(prefix, prefix_length)) out[0] = '\n';
        /* A complete row after the gap starts on its own line. */
        if (suffix && suffix_complete && suffix_length >= 3 && suffix[suffix_length - 1] == '\n' &&
            complete_object(suffix, suffix_length - 1))
            out[length - 1] = '\n';
        return;
    case SRP_FILL_BINARY:
        break;
    }
    memset(out, 0, length);
}

/* ---- cursors -------------------------------------------------------- */
srp_cursor_verdict_t srp_cursor_verdict(int loaded, const lq_checkpoint_t *checkpoint, bool exists,
                                        uint32_t size, bool prefix_has_gap, const srp_bytes_t *bytes)
{
    if (loaded != 1 || !checkpoint) return SRP_CURSOR_KEEP;
    if (!lq_checkpoint_valid(checkpoint)) return SRP_CURSOR_CORRUPT;
    uint32_t offset = checkpoint->offset;
    if (!offset) return SRP_CURSOR_KEEP;
    if (!exists || offset > size) return SRP_CURSOR_BEYOND_FILE;
    if (prefix_has_gap) return SRP_CURSOR_PREFIX_GAP;
    if (!bytes || !bytes->read) return SRP_CURSOR_PREFIX_GAP;
    uint8_t chunk[512];
    uint32_t crc = 0, position = 0;
    uint8_t last = 0;
    while (position < offset) {
        size_t n = offset - position < sizeof(chunk) ? offset - position : sizeof(chunk);
        if (!bytes->read(bytes->context, position, chunk, n)) return SRP_CURSOR_PREFIX_GAP;
        crc = srp_crc32(crc, chunk, n);
        last = chunk[n - 1];
        position += (uint32_t)n;
    }
    /* lq_open_step checks the boundary of a version 1 cursor, then the CRC. */
    if (checkpoint->version == 1 && last != '\n') return SRP_CURSOR_NOT_ROW_BOUNDARY;
    if (crc != checkpoint->prefix_crc) return SRP_CURSOR_PREFIX_MISMATCH;
    return SRP_CURSOR_KEEP;
}

const char *srp_cursor_verdict_name(srp_cursor_verdict_t verdict)
{
    switch (verdict) {
    case SRP_CURSOR_KEEP: return "KEEP";
    case SRP_CURSOR_CORRUPT: return "CORRUPT";
    case SRP_CURSOR_BEYOND_FILE: return "BEYOND_FILE";
    case SRP_CURSOR_NOT_ROW_BOUNDARY: return "NOT_ROW_BOUNDARY";
    case SRP_CURSOR_PREFIX_MISMATCH: return "PREFIX_MISMATCH";
    case SRP_CURSOR_PREFIX_GAP: return "PREFIX_GAP";
    }
    return "UNKNOWN";
}

/* ---- records -------------------------------------------------------- */
static bool sanitized_name(const char *name, char *out, size_t size)
{
    size_t used = 0;
    const char *p = name && name[0] == '/' ? name + 1 : name;
    if (!p || !*p || size < 2) return false;
    for (; *p; ++p) {
        if (used + 1 >= size) return false;
        char c = *p;
        bool keep = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
        out[used++] = keep ? c : '_';
    }
    out[used] = 0;
    return true;
}

static bool bounded_name(const char *name)
{
    return name && memchr(name, 0, SRP_NAME_BYTES) != NULL && name[0];
}

bool srp_gap_record_id(const srp_gap_t *gap, char *out, size_t size)
{
    char file[SRP_NAME_BYTES];
    if (!gap || !out || !bounded_name(gap->name) || !sanitized_name(gap->name, file, sizeof(file))) return false;
    int n = snprintf(out, size, "gap:%s:%lu:%lu:%lu", file, (unsigned long)gap->offset, (unsigned long)gap->length,
                     (unsigned long)gap->generation);
    return n > 0 && (size_t)n < size;
}

bool srp_reset_record_id(const srp_reset_t *reset, char *out, size_t size)
{
    char file[SRP_NAME_BYTES];
    if (!reset || !out || !bounded_name(reset->name) || !sanitized_name(reset->name, file, sizeof(file))) return false;
    int n = snprintf(out, size, "cursor:%s:%lu:%lu", file, (unsigned long)reset->previous.generation,
                     (unsigned long)reset->generation);
    return n > 0 && (size_t)n < size;
}

static bool printable_name(const char *name)
{
    for (const char *p = name; *p; ++p)
        if ((unsigned char)*p < 0x20 || (unsigned char)*p > 0x7e || *p == '"' || *p == '\\') return false;
    return true;
}

const char *srp_gap_cause_name(srp_gap_cause_t cause)
{
    switch (cause) {
    case SRP_GAP_SALVAGE: return "SALVAGE";
    case SRP_GAP_CHECK_INTERRUPTED: return "CHECK_INTERRUPTED";
    case SRP_GAP_RESTORE_INTERRUPTED: return "RESTORE_INTERRUPTED";
    case SRP_GAP_CONTENT_UNVERIFIED: return "CONTENT_UNVERIFIED";
    case SRP_GAP_CHECK_CHANGED: return "CHECK_CHANGED";
    case SRP_GAP_REFORMAT_INTERRUPTED: return "REFORMAT_INTERRUPTED";
    case SRP_GAP_UNHELD_LOST: return "UNHELD_LOST";
    }
    return "UNKNOWN";
}

size_t srp_gap_evidence(const srp_gap_t *gap, char *out, size_t size)
{
    if (!gap || !out || !bounded_name(gap->name) || !printable_name(gap->name)) return 0;
    int n = snprintf(out, size,
        "{\"type\":\"storage_repair_unreadable\",\"schema\":1,\"file\":\"%s\",\"offset\":%lu,\"length\":%lu,"
        "\"file_size\":%lu,\"spiffs_error\":%ld,\"cause\":\"%s\",\"class\":\"%s\",\"merged\":%s,\"run\":%lu}",
        gap->name, (unsigned long)gap->offset, (unsigned long)gap->length, (unsigned long)gap->file_size,
        (long)gap->spiffs_error, srp_gap_cause_name((srp_gap_cause_t)gap->cause),
        srp_class_name((srp_class_t)gap->cls), (gap->flags & SRP_GAP_MERGED) ? "true" : "false",
        (unsigned long)gap->generation);
    return n > 0 && (size_t)n < size ? (size_t)n : 0;
}

size_t srp_reset_evidence(const srp_reset_t *reset, char *out, size_t size)
{
    if (!reset || !out || !bounded_name(reset->name) || !printable_name(reset->name)) return 0;
    const lq_checkpoint_t *cp = &reset->previous;
    int n = snprintf(out, size,
        "{\"type\":\"storage_repair_cursor_reset\",\"schema\":1,\"file\":\"%s\",\"verdict\":\"%s\",\"run\":%lu,"
        "\"checkpoint\":{\"version\":%lu,\"generation\":%lu,\"offset\":%lu,\"prefix_crc\":%lu,\"crc\":%lu}}",
        reset->name, srp_cursor_verdict_name((srp_cursor_verdict_t)reset->verdict),
        (unsigned long)reset->generation, (unsigned long)cp->version, (unsigned long)cp->generation,
        (unsigned long)cp->offset, (unsigned long)cp->prefix_crc, (unsigned long)cp->crc);
    return n > 0 && (size_t)n < size ? (size_t)n : 0;
}

bool srp_gaps_add(srp_gaps_t *gaps, const srp_gap_t *gap)
{
    if (!gaps || !gap || !bounded_name(gap->name) || gaps->count > SRP_MAX_GAPS) return false;
    if (gaps->count < SRP_MAX_GAPS) {
        gaps->gap[gaps->count++] = *gap;
        return true;
    }
    /* Full: widen this file's last unacknowledged record of the same run. */
    for (uint32_t i = gaps->count; i-- > 0;) {
        srp_gap_t *old = &gaps->gap[i];
        if ((old->flags & SRP_RECORD_ACKED) || old->generation != gap->generation || strcmp(old->name, gap->name))
            continue;
        uint64_t start = old->offset < gap->offset ? old->offset : gap->offset;
        uint64_t old_end = (uint64_t)old->offset + old->length, new_end = (uint64_t)gap->offset + gap->length;
        uint64_t end = old_end > new_end ? old_end : new_end;
        if (end - start > UINT32_MAX) return false;
        old->offset = (uint32_t)start;
        old->length = (uint32_t)(end - start);
        old->flags |= SRP_GAP_MERGED;
        if (gap->file_size > old->file_size) old->file_size = gap->file_size;
        return true;
    }
    for (uint32_t i = 0; i < gaps->count; ++i) {
        if (gaps->gap[i].flags & SRP_RECORD_ACKED) {
            gaps->gap[i] = *gap;
            return true;
        }
    }
    return false;
}

bool srp_resets_add(srp_resets_t *resets, const srp_reset_t *reset)
{
    if (!resets || !reset || !bounded_name(reset->name) || resets->count > SRP_MAX_RESETS) return false;
    if (resets->count < SRP_MAX_RESETS) {
        resets->reset[resets->count++] = *reset;
        return true;
    }
    for (uint32_t i = 0; i < resets->count; ++i) {
        if (resets->reset[i].flags & SRP_RECORD_ACKED) {
            resets->reset[i] = *reset;
            return true;
        }
    }
    return false;
}

uint32_t srp_gaps_unacked(const srp_gaps_t *gaps)
{
    uint32_t unacked = 0;
    for (uint32_t i = 0; gaps && i < gaps->count && i < SRP_MAX_GAPS; ++i)
        if (!(gaps->gap[i].flags & SRP_RECORD_ACKED)) ++unacked;
    return unacked;
}

uint32_t srp_resets_unacked(const srp_resets_t *resets)
{
    uint32_t unacked = 0;
    for (uint32_t i = 0; resets && i < resets->count && i < SRP_MAX_RESETS; ++i)
        if (!(resets->reset[i].flags & SRP_RECORD_ACKED)) ++unacked;
    return unacked;
}

/* ---- journal -------------------------------------------------------- */
const char *srp_phase_name(srp_phase_t phase)
{
    switch (phase) {
    case SRP_PHASE_IDLE: return "IDLE";
    case SRP_PHASE_SURVEY: return "SURVEY";
    case SRP_PHASE_NORMALIZE: return "NORMALIZE";
    case SRP_PHASE_CHECK: return "CHECK";
    case SRP_PHASE_RESTORE: return "RESTORE";
    case SRP_PHASE_FINISH: return "FINISH";
    case SRP_PHASE_REFORMAT: return "REFORMAT";
    }
    return "UNKNOWN";
}

void srp_state_seal(srp_state_t *state)
{
    if (!state) return;
    state->version = SRP_STATE_VERSION;
    state->crc = srp_crc32(0, state, offsetof(srp_state_t, crc));
}

bool srp_state_valid(const srp_state_t *state)
{
    return state && state->version == SRP_STATE_VERSION && state->phase <= SRP_PHASE_REFORMAT &&
        state->crc == srp_crc32(0, state, offsetof(srp_state_t, crc));
}

void srp_manifest_seal(srp_manifest_t *manifest)
{
    if (!manifest) return;
    manifest->version = SRP_STATE_VERSION;
    manifest->crc = srp_crc32(0, manifest, offsetof(srp_manifest_t, crc));
}

bool srp_manifest_valid(const srp_manifest_t *manifest)
{
    if (!manifest || manifest->version != SRP_STATE_VERSION || manifest->count > SRP_MANIFEST_FILES ||
        manifest->crc != srp_crc32(0, manifest, offsetof(srp_manifest_t, crc))) return false;
    for (uint32_t i = 0; i < manifest->count; ++i)
        if (!bounded_name(manifest->file[i].name)) return false;
    return true;
}

/* ---- policy --------------------------------------------------------- */
const char *srp_mode_name(srp_mode_t mode)
{
    switch (mode) {
    case SRP_MODE_FULL: return "FULL";
    case SRP_MODE_NORMALIZE_ONLY: return "NORMALIZE_ONLY";
    case SRP_MODE_REPORT_ONLY: return "REPORT_ONLY";
    }
    return "UNKNOWN";
}

srp_policy_t srp_policy(const srp_policy_input_t *input)
{
    srp_policy_t policy = {SRP_MODE_FULL, false, false};
    if (!input) {
        policy.mode = SRP_MODE_REPORT_ONLY;
        return policy;
    }
    uint32_t failures = input->failures;
    uint32_t incomplete = input->incomplete;
    if (input->disabled) {
        if (input->same_image) {
            policy.mode = SRP_MODE_REPORT_ONLY;
            return policy;
        }
        policy.clear_disable = true;
        failures = 0;
    }
    if (failures >= SRP_GUARD_FAILURES) {
        policy.disable = true;
        policy.mode = SRP_MODE_REPORT_ONLY;
        return policy;
    }
    if (input->pending_verify && incomplete) policy.mode = SRP_MODE_NORMALIZE_ONLY;
    return policy;
}

/* ---- budget --------------------------------------------------------- */
uint64_t srp_budget(srp_budget_file_t *files, size_t count, uint64_t budget)
{
    uint64_t held = 0;
    for (size_t i = 0; files && i < count; ++i) files[i].hold = false;
    /* Smallest first within each class: more whole files fit. */
    for (unsigned priority = 0; priority <= 6; ++priority) {
        for (;;) {
            size_t best = count;
            for (size_t i = 0; i < count; ++i) {
                if (files[i].hold || srp_class_priority((srp_class_t)files[i].cls) != priority) continue;
                if (files[i].size > budget - held) continue;
                if (best == count || files[i].size < files[best].size) best = i;
            }
            if (best == count) break;
            files[best].hold = true;
            held += files[best].size;
        }
    }
    return held;
}
