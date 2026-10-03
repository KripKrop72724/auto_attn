#pragma once
#include "cJSON.h"
#include "zkt_journal_boot.h"

/* app_main is the single startup-controller owner. Other tasks only read its
 * bounded snapshot; capture rechecks permission for every terminal packet. */
void zj_runtime_step(void);
bool zj_runtime_health(zj_boot_t *out);
bool zj_runtime_boot_ready(void);
bool zj_runtime_writer_ready(void);
bool zj_runtime_append_diagnostics(cJSON *diagnostics);
