#include "add_source_wire.h"
#include <string.h>

static bool uint_field(const cJSON *root, const char *name, uint32_t min, uint32_t *out)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsNumber(value) || !(value->valuedouble >= min && value->valuedouble <= UINT32_MAX)) return false;
    uint32_t integer = (uint32_t)value->valuedouble;
    if ((double)integer != value->valuedouble) return false;
    *out = integer;
    return true;
}

static bool text_field(const cJSON *root, const char *name, char *out, size_t capacity, size_t exact)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsString(value)) return false;
    size_t length = strlen(value->valuestring);
    if (!length || length >= capacity || (exact && length != exact)) return false;
    memcpy(out, value->valuestring, length + 1);
    return true;
}

static bool hex(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }
static bool optional(const cJSON *root, const char *name)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(root, name);
    return !value || cJSON_IsNull(value);
}

static bool digest_field(const cJSON *root, const char *name, char out[65], bool required)
{
    if (optional(root, name)) return !required;
    if (!text_field(root, name, out, 65, 64)) return false;
    for (size_t i = 0; i < 64; ++i) if (!hex(out[i])) return false;
    return true;
}

bool add_source_epoch_required(const char *version, bool zkt)
{
    if (!zkt || !version) return false;
    if (!strncmp(version, "zone-lite-", 10)) version += 10;
    /* These are the exact bridge/writer versions eligible for this release. */
    return !strcmp(version, "2.6.16") || !strcmp(version, "2.6.17") || !strcmp(version, "2.6.18") || !strcmp(version, "2.7.0");
}

bool add_source_epoch_read(const cJSON *root, char epoch[37], bool required)
{
    if (!root || !epoch) return false;
    epoch[0] = 0;
    if (optional(root, "source_epoch")) return !required;
    char value[37];
    if (!text_field(root, "source_epoch", value, sizeof(value), 36)) return false;
    for (size_t i = 0; i < 36; ++i) {
        bool dash = i == 8 || i == 13 || i == 18 || i == 23;
        if (dash ? value[i] != '-' : !hex(value[i])) return false;
    }
    memcpy(epoch, value, sizeof(value));
    return true;
}

const char *add_source_ack_type(const char *type)
{
    if (!type) return NULL;
    if (!strcmp(type, "reconcile_anchor")) return "reconcile_anchor_ack";
    if (!strcmp(type, "reconcile_chunk")) return "reconcile_chunk_ack";
    if (!strcmp(type, "reconcile_source_manifest")) return "reconcile_manifest_ack";
    if (!strcmp(type, "source_probe_result")) return "source_probe_ack";
    if (!strcmp(type, "source_tail_chunk")) return "source_tail_ack";
    return NULL;
}

bool add_source_epoch_write(cJSON *root, const char epoch[37])
{
    return root && epoch && (!epoch[0] || cJSON_AddStringToObject(root, "source_epoch", epoch));
}

bool add_source_ack_matches(const cJSON *root, const char *ack_type, const char epoch[37])
{
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    char received[37];
    return ack_type && epoch && epoch[0] && cJSON_IsString(type) &&
        !strcmp(type->valuestring, ack_type) && add_source_epoch_read(root, received, true) &&
        !strcmp(received, epoch);
}

bool add_source_parse_assignment(const cJSON *root, add_reconcile_assignment_t *out, bool required)
{
    if (!root || !out) return false;
    add_reconcile_assignment_t value = {0};
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (!cJSON_IsString(type)) return false;
    value.source_probe = !strcmp(type->valuestring, "source_probe_assignment");
    if ((!value.source_probe && strcmp(type->valuestring, "reconcile_assignment")) ||
        !add_source_epoch_read(root, value.source_epoch, required) ||
        !text_field(root, "job_id", value.job_id, sizeof(value.job_id), 36) ||
        !text_field(root, "expected_terminal_serial", value.expected_terminal_serial, sizeof(value.expected_terminal_serial), 0) ||
        !uint_field(root, "generation", 1, &value.generation)) return false;
    if (value.source_probe) {
        if (!uint_field(root, "ordinal", 0, &value.probe_ordinal)) return false;
        value.chunk_records = 1;
        *out = value;
        return true;
    }
    uint32_t count;
    if (!uint_field(root, "committed_next_ordinal", 0, &value.committed_next_ordinal) ||
        !uint_field(root, "chunk_records", 1, &count)) return false;
    value.chunk_records = count > 100 ? 100 : count;
    const cJSON *protocol = cJSON_GetObjectItemCaseSensitive(root, "protocol");
    value.stream_v2 = cJSON_IsString(protocol) && !strcmp(protocol->valuestring, "history_stream_v2");
    if (!optional(root, "assignment_id") &&
        !text_field(root, "assignment_id", value.assignment_id, sizeof(value.assignment_id), 36)) return false;
    if (!optional(root, "credit_end_ordinal") &&
        !uint_field(root, "credit_end_ordinal", value.committed_next_ordinal, &value.credit_end_ordinal)) return false;
    if (value.stream_v2 && (!value.assignment_id[0] || optional(root, "credit_end_ordinal"))) return false;
    value.max_chunks = 1;
    if (!optional(root, "max_chunks")) {
        if (!uint_field(root, "max_chunks", 1, &count)) return false;
        value.max_chunks = count > 20 ? 20 : count;
    }
    if (!optional(root, "lease_expires_epoch")) {
        const cJSON *lease = cJSON_GetObjectItemCaseSensitive(root, "lease_expires_epoch");
        if (!cJSON_IsNumber(lease) || !(lease->valuedouble >= 0 && lease->valuedouble <= 253402300799.0)) return false;
        value.lease_expires_epoch = (int64_t)lease->valuedouble;
        if ((double)value.lease_expires_epoch != lease->valuedouble) return false;
    }
    if (!optional(root, "cutoff_count")) {
        if (!uint_field(root, "cutoff_count", value.committed_next_ordinal, &value.cutoff_count)) return false;
        value.has_cutoff = true;
    }
    if (!digest_field(root, "first_anchor_digest", value.first_anchor_digest, false) ||
        !digest_field(root, "preceding_chain_digest", value.preceding_chain_digest, false) ||
        !digest_field(root, "committed_predecessor_digest", value.committed_predecessor_digest, false)) return false;
    *out = value;
    return true;
}

bool add_source_parse_chunk_ack(const cJSON *root, add_reconcile_chunk_ack_t *out, bool required)
{
    if (!root || !out) return false;
    add_reconcile_chunk_ack_t value = {0};
    if (!add_source_epoch_read(root, value.source_epoch, required) ||
        !text_field(root, "assignment_id", value.assignment_id, sizeof(value.assignment_id), 36) ||
        !text_field(root, "job_id", value.job_id, sizeof(value.job_id), 36) ||
        !uint_field(root, "generation", 1, &value.generation) ||
        !uint_field(root, "committed_next_ordinal", 0, &value.committed_next_ordinal) ||
        !digest_field(root, "resulting_chain_digest", value.resulting_chain_digest, true)) return false;
    value.continue_allowed = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "continue_allowed"));
    if ((!optional(root, "credit_end_ordinal") || value.continue_allowed) &&
        !uint_field(root, "credit_end_ordinal", value.continue_allowed ? value.committed_next_ordinal : 0, &value.credit_end_ordinal)) return false;
    value.valid = true;
    *out = value;
    return true;
}

bool add_source_parse_tail_ack(const cJSON *root, add_source_tail_ack_t *out, bool required)
{
    if (!root || !out) return false;
    add_source_tail_ack_t value = {0};
    if (!add_source_epoch_read(root, value.source_epoch, required) ||
        !text_field(root, "terminal_serial", value.terminal_serial, sizeof(value.terminal_serial), 0) ||
        !uint_field(root, "terminal_generation", 1, &value.terminal_generation) ||
        !uint_field(root, "committed_next_ordinal", 0, &value.committed_next_ordinal) ||
        !digest_field(root, "resulting_chain_digest", value.resulting_chain_digest, true)) return false;
    if (!optional(root, "exception_count") && !uint_field(root, "exception_count", 0, &value.exception_count)) return false;
    value.valid = true;
    *out = value;
    return true;
}

bool add_source_parse_coverage(const cJSON *root, add_source_coverage_t *out, bool required)
{
    if (!root || !out) return false;
    add_source_coverage_t value = {0};
    const cJSON *active = cJSON_GetObjectItemCaseSensitive(root, "active");
    if (!cJSON_IsBool(active)) return false;
    value.active = cJSON_IsTrue(active);
    if (!add_source_epoch_read(root, value.source_epoch, required && value.active) ||
        !text_field(root, "terminal_serial", value.terminal_serial, sizeof(value.terminal_serial), 0) ||
        !uint_field(root, "terminal_generation", 1, &value.terminal_generation) ||
        !uint_field(root, "source_committed_cursor", 0, &value.committed_next_ordinal) ||
        !digest_field(root, "source_committed_chain_digest", value.committed_chain_digest, true)) return false;
    *out = value;
    return true;
}
