#pragma once
#include "ota_checkpoint.h"
#include "zkt_journal_store.h"

/* An approved writer -> retained bridge operation. The existing OTA ABI is
 * retained; READER_INTENT records selection intent, never a downloaded image.
 * Only the drained storage owner writes this intent. */
bool zj_rollback_request_valid(const ota_checkpoint_t *request);
bool zj_rollback_failed_boot(const ota_checkpoint_t *request);
bool zj_rollback_same_target(const ota_checkpoint_t *left, const ota_checkpoint_t *right);
zj_result_t zj_rollback_commit_intent(const ota_checkpoint_t *expected,
    uint64_t deadline_us, ota_checkpoint_t *committed, int *nvs_error);
