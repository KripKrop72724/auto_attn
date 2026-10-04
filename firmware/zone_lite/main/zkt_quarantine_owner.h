#pragma once
#include "zkt_segmented_owner.h"

/* Storage-task adapters for retained corrupt-file evidence. No append or
 * interpretation path exists; retirement requires an exact ADD receipt. */
dq_result_t zkt_quarantine_owner_peek(unsigned lane, void *bytes, size_t capacity,
                                     size_t *length, lq_token_t *token);
dq_result_t zkt_quarantine_owner_settle(unsigned lane, const lq_token_t *token, bool custody);
