#include "hikvision_commands.h"
#include "hikvision_http.h"
#include "zone_config.h"
#include "mbedtls/sha256.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

static const char *text(const cJSON *root, const char *key)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(root, key);
    return cJSON_IsString(value) ? value->valuestring : NULL;
}
static void digest(const char *value, char out[65])
{
    unsigned char bytes[32];
    mbedtls_sha256((const unsigned char *)value, strlen(value), bytes, 0);
    for (unsigned i = 0; i < 32; i++) snprintf(out + 2 * i, 3, "%02x", bytes[i]);
}
static bool state_matches(const cJSON *profile, const char *expected)
{
    char *raw = cJSON_PrintUnformatted(profile), hash[65];
    if (!raw) return false;
    digest(raw, hash); free(raw);
    return !strcmp(hash, expected);
}
static bool credential_free(const cJSON *profile)
{
    const char *keys[] = {"numOfCard", "numOfFP", "numOfFace"};
    for (unsigned i = 0; i < 3; i++) {
        const cJSON *n = cJSON_GetObjectItemCaseSensitive(profile, keys[i]);
        if (!cJSON_IsNumber(n) || n->valuedouble != 0) return false;
    }
    return true;
}
static bool deletion_qualified(const cJSON *profile)
{
    /* Face removal and retained punches verified on the exact pilot firmware.
     * Fingerprint/card deletion consequences still require hardware evidence. */
    const cJSON *face = cJSON_GetObjectItemCaseSensitive(profile, "numOfFace");
    const cJSON *finger = cJSON_GetObjectItemCaseSensitive(profile, "numOfFP");
    const cJSON *card = cJSON_GetObjectItemCaseSensitive(profile, "numOfCard");
    return cJSON_IsNumber(face) && (face->valuedouble == 0 || face->valuedouble == 1) &&
        cJSON_IsNumber(finger) && finger->valuedouble == 0 &&
        cJSON_IsNumber(card) && card->valuedouble == 0;
}
static bool regular(const cJSON *profile)
{
    const char *type = text(profile, "userType");
    return type && !strcmp(type, "normal") &&
        cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(profile, "localUIRight"));
}
static cJSON *new_profile(const add_command_t *command)
{
    /* Exact regular-user template exercised by the disposable profile test.
     * No biometric, card, password, administrator, or door-control command. */
    cJSON *profile = cJSON_Parse("{\"userType\":\"normal\",\"Valid\":{\"enable\":false,\"beginTime\":\"2026-01-01T00:00:00\",\"endTime\":\"2036-01-01T00:00:00\",\"timeType\":\"local\"},\"doorRight\":\"1\",\"RightPlan\":[{\"doorNo\":1,\"planTemplateNo\":\"1\"}]}");
    if (!profile || !cJSON_AddStringToObject(profile, "employeeNo", command->user_id) ||
        !cJSON_AddStringToObject(profile, "name", command->name)) {
        cJSON_Delete(profile); return NULL;
    }
    return profile;
}
static bool contains_desired(const cJSON *actual, const cJSON *desired)
{
    const cJSON *field;
    cJSON_ArrayForEach(field, desired) {
        if (!field->string || !cJSON_Compare(field, cJSON_GetObjectItemCaseSensitive(actual, field->string), true)) return false;
    }
    return true;
}
hik_result_t hik_profile_command(const add_command_t *command, cJSON **receipt)
{
    *receipt = NULL;
    const zone_config_t *cfg = zone_config_get();
    if (strcmp(cfg->hik_profile, "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1")) return HIK_CONFIGURATION;
    bool create = !strcmp(command->command_type, "CREATE_USER");
    bool update = !strcmp(command->command_type, "UPDATE_USER");
    bool remove = !strcmp(command->command_type, "DELETE_USER");
    if ((!create && !update && !remove) || !command->user_id[0] ||
        strlen(command->user_id) > 32 || strspn(command->user_id, "0123456789") != strlen(command->user_id) ||
        strcmp(command->uid, command->user_id) || strcmp(command->expected_serial, cfg->hik_expected_serial) ||
        ((create || update) && (!command->has_name || !command->name[0] || strlen(command->name) > 128)) ||
        (create && (!command->has_privilege || command->privilege != 0)) ||
        (!create && (!command->has_expected_terminal_identity_fingerprint ||
            !command->has_expected_terminal_state_fingerprint || !command->has_expected_name ||
            !command->has_expected_privilege)) ||
        (update && (!command->has_privilege || (command->privilege != 0 && command->privilege != 14) ||
            (command->expected_privilege != 0 && command->expected_privilege != 14))) ||
        (remove && command->expected_privilege != 0)) return HIK_CONFIGURATION;
    char identity[65], binding[180];
    int n = snprintf(binding, sizeof(binding), "%s\n%s", cfg->hik_expected_serial, command->user_id);
    if (n < 0 || (size_t)n >= sizeof(binding)) return HIK_CONFIGURATION;
    digest(binding, identity);
    if (!create && strcmp(identity, command->expected_terminal_identity_fingerprint)) return HIK_BINDING;
    hik_result_t result = hik_http_verify_identity();
    cJSON *before = NULL, *after = NULL, *desired = NULL;
    if (result == HIK_OK) result = hik_user_read(command->user_id, &before);
    if (result != HIK_OK) goto done;
    bool verified = false;
    if (create) {
        desired = new_profile(command);
        if (!desired) { result = HIK_NETWORK; goto done; }
        if (before) {
            /* Lost write ACK: read the reserved employee before retrying. Never
             * adopt a profile with credentials or elevated local access. */
            verified = regular(before) && credential_free(before) && contains_desired(before, desired);
            result = verified ? HIK_OK : HIK_BINDING;
        } else result = hik_user_create(desired, &verified);
    } else if (remove && !before) {
        verified = true; /* Targeted deletion already completed; no write. */
    } else if (!before) result = HIK_BINDING;
    else if (update) {
        if (!text(before, "userType") || strcmp(text(before, "userType"), "normal")) {
            result = HIK_CONFIGURATION; goto done;
        }
        bool original = state_matches(before, command->expected_terminal_state_fingerprint);
        if (!original && text(before, "name") && !strcmp(text(before, "name"), command->name)) {
            /* A retry may observe the verified desired name. Restoring only the
             * old name must reproduce the complete original profile fingerprint. */
            cJSON *prior = cJSON_Duplicate(before, true);
            cJSON *old_name = cJSON_CreateString(command->expected_name);
            bool replaced = prior && old_name && cJSON_ReplaceItemInObjectCaseSensitive(prior, "name", old_name);
            if (!replaced) cJSON_Delete(old_name);
            cJSON *old_right = cJSON_CreateBool(command->expected_privilege == 14);
            bool right_replaced = prior && old_right && cJSON_ReplaceItemInObjectCaseSensitive(prior, "localUIRight", old_right);
            if (!right_replaced) cJSON_Delete(old_right);
            verified = replaced && right_replaced &&
                cJSON_IsBool(cJSON_GetObjectItemCaseSensitive(before, "localUIRight")) &&
                cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(before, "localUIRight")) == (command->privilege == 14) &&
                state_matches(prior, command->expected_terminal_state_fingerprint);
            cJSON_Delete(prior);
            result = verified ? HIK_OK : HIK_BINDING;
        } else if (!original) result = HIK_BINDING;
        else result = hik_user_edit(before, command->name,
            command->privilege != command->expected_privilege, command->privilege == 14, &verified);
    } else if (!state_matches(before, command->expected_terminal_state_fingerprint) || !regular(before)) {
        result = HIK_BINDING;
    } else if (!deletion_qualified(before)) {
        result = HIK_CONFIGURATION;
    } else result = hik_user_delete(before, &verified);
    if (!verified) goto done;
    result = hik_user_read(command->user_id, &after);
    if (result != HIK_OK) goto done;
    if ((remove && after) || (!remove && !after)) { result = HIK_CUSTODY; goto done; }
    if (create && (!regular(after) || !credential_free(after) || !contains_desired(after, desired))) {
        result = HIK_BINDING; goto done;
    }
    if (update) {
        cJSON *expected_after = cJSON_Duplicate(before, true);
        cJSON *name = cJSON_CreateString(command->name);
        bool changed = expected_after && name && cJSON_ReplaceItemInObjectCaseSensitive(expected_after, "name", name);
        if (!changed) cJSON_Delete(name);
        cJSON *right = cJSON_CreateBool(command->privilege == 14);
        bool right_changed = expected_after && right && cJSON_ReplaceItemInObjectCaseSensitive(expected_after, "localUIRight", right);
        if (!right_changed) cJSON_Delete(right);
        bool same = changed && right_changed && cJSON_Compare(expected_after, after, true);
        cJSON_Delete(expected_after);
        if (!same) { result = HIK_BINDING; goto done; }
    }
    *receipt = cJSON_CreateObject();
    if (!*receipt || !cJSON_AddBoolToObject(*receipt, "verified", true) ||
        !cJSON_AddStringToObject(*receipt, "verified_terminal_identity_fingerprint", identity)) {
        result = HIK_NETWORK; goto done;
    }
    if (remove && !cJSON_AddBoolToObject(*receipt, "user_absent", true)) {
        result = HIK_NETWORK; goto done;
    }
    if (after) {
        char *raw = cJSON_PrintUnformatted(after), hash[65];
        if (!raw) { result = HIK_NETWORK; goto done; }
        digest(raw, hash); free(raw);
        if (!cJSON_AddStringToObject(*receipt, "verified_terminal_state_fingerprint", hash)) result = HIK_NETWORK;
    }
done:
    cJSON_Delete(before); cJSON_Delete(after); cJSON_Delete(desired);
    if (result != HIK_OK) { cJSON_Delete(*receipt); *receipt = NULL; }
    return result;
}
