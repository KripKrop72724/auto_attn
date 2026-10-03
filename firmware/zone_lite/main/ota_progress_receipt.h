#pragma once
#include <stdbool.h>
#include "cJSON.h"

/* HTTP success is not transition acceptance. Identity must match this exact
 * deployment; boot/source progress additionally binds the running app digest. */
bool ota_progress_receipt_matches(const cJSON *reply, const char *deployment_id,
    const char *requested_state, const char *target_version, const char *running_digest);
