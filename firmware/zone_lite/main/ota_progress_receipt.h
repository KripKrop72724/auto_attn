#pragma once
#include <stdbool.h>
#include "cJSON.h"

/* HTTP success is not transition acceptance. Identity must match this exact
 * deployment; boot/source progress additionally binds the running app digest. */
bool ota_progress_receipt_matches(const cJSON *reply, const char *deployment_id,
    const char *requested_state, const char *target_version, const char *running_digest);

/* ZKT 2.7.7: ADD accepted BOOTED_PENDING under the confirm-first storage
 * exception for this exact deployment and image: confirm, the echoed
 * error_code STORAGE_REPAIR_EXCEPTION, and storage_exception_allowed (the
 * release available or HIL-only and the campaign active: pausing the
 * campaign is ADD's veto). */
bool ota_progress_receipt_exception(const cJSON *reply, const char *deployment_id, const char *target_version,
    const char *running_digest);
/* ZKT 2.7.7: the receipt names this deployment and target and says ADD has
 * ended it (FAILED, ROLLED_BACK, CANCELLED, SUPERSEDED, RELEASE_REVOKED): an
 * exception device's RECONCILING journal is then cleared. */
bool ota_progress_receipt_terminal(const cJSON *reply, const char *deployment_id, const char *target_version);
