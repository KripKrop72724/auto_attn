#pragma once
#include "add_connector.h"
#include "cJSON.h"

/* Pure bounded parsers. A missing epoch is only accepted for legacy senders. */
bool add_source_epoch_required(const char *version, bool zkt);
bool add_source_epoch_read(const cJSON *root, char epoch[37], bool required);
bool add_source_epoch_write(cJSON *root, const char epoch[37]);
bool add_source_parse_assignment(const cJSON *, add_reconcile_assignment_t *, bool);
bool add_source_parse_chunk_ack(const cJSON *, add_reconcile_chunk_ack_t *, bool);
bool add_source_parse_tail_ack(const cJSON *, add_source_tail_ack_t *, bool);
bool add_source_parse_coverage(const cJSON *, add_source_coverage_t *, bool);
const char *add_source_ack_type(const char *request_type);
bool add_source_ack_matches(const cJSON *, const char *ack_type, const char epoch[37]);
