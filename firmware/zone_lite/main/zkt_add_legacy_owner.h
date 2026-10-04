#pragma once
#include "zkt_segmented_owner.h"

/* Storage-task adapters for the two existing ADD flat files. No network and
 * no borrowed data survives these calls. Other tasks use zq_legacy_* copies. */
dq_result_t add_legacy_owner_append(unsigned lane, const void *bytes, size_t length, qs_admission_t policy);
dq_result_t add_legacy_owner_peek(unsigned lane, void *bytes, size_t capacity, size_t *length, lq_token_t *token);
dq_result_t add_legacy_owner_settle(unsigned lane, const lq_token_t *token, bool custody);
