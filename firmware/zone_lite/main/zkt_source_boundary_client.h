#pragma once
#include "zkt_source_boundary.h"

bool zsb_runtime_required(void);
/* Use only after releasing prepared terminal buffers. These bounded calls
 * retain accepted owner work through a timeout; replay returns the first
 * committed boundary. They do not grant source/Oracle delivery permission. */
zj_result_t zsb_runtime_read(zsb_record_t *out);
zj_result_t zsb_runtime_create(const zsb_facts_t *facts, zsb_record_t *out);
