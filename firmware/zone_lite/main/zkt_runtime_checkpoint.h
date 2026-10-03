#pragma once
#include "runtime_checkpoint.h"
#include "zkt_journal_store.h"

bool zj_runtime_checkpoint_required(void);

/* One terminal-session producer. Retain a timed-out ticket until its exact
 * result is collected; never enqueue a replacement over unfinished work.
 * confirmed may contain a previous call's late commit even when this call
 * returns false. Only true proves that the supplied facts were committed. */
bool zj_runtime_checkpoint_save(const runtime_checkpoint_t *proposed,
                                runtime_checkpoint_t *confirmed);

/* Storage-owner task only, under the shared local-storage lock. Preserve the
 * runtime_v1 ABI. Allocate the generation from NVS, not the caller's cache,
 * and confirm the complete blob after commit. Never repair by deleting it. */
zj_result_t zj_runtime_checkpoint_commit(const runtime_checkpoint_t *proposed,
    uint64_t deadline_us, runtime_checkpoint_t *confirmed, int *nvs_error);
