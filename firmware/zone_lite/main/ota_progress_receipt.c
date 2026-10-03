#include "ota_progress_receipt.h"
#include <string.h>

static bool same(const cJSON *root, const char *name, const char *expected)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    return expected && expected[0] && cJSON_IsString(item) && !strcmp(item->valuestring, expected);
}

bool ota_progress_receipt_matches(const cJSON *reply, const char *deployment_id,
    const char *requested_state, const char *target_version, const char *running_digest)
{
    if (!reply || !requested_state || !requested_state[0]) return false;
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(reply, "schema_version");
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(reply, "state");
    if (!cJSON_IsNumber(schema) || schema->valuedouble != 1 || !cJSON_IsString(state) ||
        !same(reply, "deployment_id", deployment_id) || !same(reply, "target_version", target_version)) return false;
    /* A lost final success response/clear can leave the local journal at
     * RECONCILING. Only the same attested deployment's SUCCEEDED result is an
     * allowed forward terminal acknowledgement; FAILED/CANCELLED never are. */
    if (strcmp(state->valuestring, requested_state) &&
        (strcmp(requested_state, "RECONCILING") || strcmp(state->valuestring, "SUCCEEDED"))) return false;
    bool boot = !strcmp(requested_state, "BOOTED_PENDING") ||
        !strcmp(requested_state, "RECONCILING") || !strcmp(requested_state, "SUCCEEDED");
    if (boot && (!running_digest || strlen(running_digest) != 64 ||
        strspn(running_digest, "0123456789abcdef") != 64 ||
        !same(reply, "application_sha256", running_digest))) return false;
    return true;
}
