#pragma once
#include "zkt_journal_capture.h"

/* Start only after persisted reader compatibility and owner readiness. No
 * call site enables this adapter in the current release. An enabled journal
 * writer must fail capture, rather than use a legacy fallback, if not ready. */
bool zj_capture_runtime_start(void);
bool zj_capture_runtime_packet(const uint8_t *packet, size_t length, const zj_capture_facts_t *facts);
bool zj_capture_runtime_health(zj_capture_health_t *health);
