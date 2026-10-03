#pragma once
#include "cJSON.h"
#include "zkt_journal_boot.h"

/* app_main is the single startup-controller owner. Other tasks only read its
 * bounded snapshot; capture rechecks permission for every terminal packet. */
void zj_runtime_step(void);
bool zj_runtime_health(zj_boot_t *out);
bool zj_runtime_boot_ready(void);
bool zj_runtime_writer_ready(void);
/* Legacy images retain their path. A bridge must first prove it has never
 * transferred authority; an unknown/stale/recovering snapshot is not proof. */
bool zj_runtime_legacy_capture_allowed(void);
/* Includes a bridge after cutover and any new image whose authority is still
 * unknown. This requirement never grants permission to write or ACK. */
bool zj_runtime_raw_source_required(void);
bool zj_runtime_append_diagnostics(cJSON *diagnostics);
