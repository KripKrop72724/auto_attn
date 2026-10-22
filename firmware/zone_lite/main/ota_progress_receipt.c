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
    /* A recovered writer attempt still names the writer artifact. Bind the
     * rollback acknowledgement to ADD's stored, verified bridge boot evidence;
     * a terminal state or an echoed caller digest cannot retire the intent. */
    if (!strcmp(requested_state, "ROLLED_BACK") && !strcmp(target_version, "2.7.0") &&
        (!running_digest || strlen(running_digest) != 64 ||
         strspn(running_digest, "0123456789abcdef") != 64 ||
         !same(reply, "rollback_application_sha256", running_digest))) return false;
    return true;
}

bool ota_progress_receipt_exception(const cJSON *reply, const char *deployment_id, const char *target_version,
    const char *running_digest)
{
    if (!ota_progress_receipt_matches(reply, deployment_id, "BOOTED_PENDING", target_version, running_digest)) return false;
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(reply, "state");
    return cJSON_IsString(state) && !strcmp(state->valuestring, "BOOTED_PENDING") &&
        cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(reply, "confirm")) &&
        same(reply, "error_code", "STORAGE_REPAIR_EXCEPTION") &&
        cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(reply, "storage_exception_allowed"));
}

bool ota_progress_receipt_terminal(const cJSON *reply, const char *deployment_id, const char *target_version)
{
    static const char *const ended[] = {"FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"};
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(reply, "schema_version");
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(reply, "state");
    if (!reply || !cJSON_IsNumber(schema) || schema->valuedouble != 1 || !cJSON_IsString(state) ||
        !same(reply, "deployment_id", deployment_id) || !same(reply, "target_version", target_version)) return false;
    for (size_t i = 0; i < sizeof(ended) / sizeof(ended[0]); ++i)
        if (!strcmp(state->valuestring, ended[i])) return true;
    return false;
}
