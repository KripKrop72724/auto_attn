#include "zkt_custody_wire.h"
#include "cJSON.h"
#include <stdio.h>
#include <string.h>

static const cJSON *unique(const cJSON *object, const char *name)
{
    if (!cJSON_IsObject(object)) return NULL;
    const cJSON *found = NULL;
    for (const cJSON *child = object->child; child; child = child->next) {
        if (child->string && !strcmp(child->string, name)) {
            if (found) return NULL;
            found = child;
        }
    }
    return found;
}
static const char *string(const cJSON *object, const char *name)
{
    const cJSON *value = unique(object, name);
    return cJSON_IsString(value) ? value->valuestring : NULL;
}
static bool equal(const cJSON *object, const char *name, const char *expected)
{
    const char *value = string(object, name);
    return value && !strcmp(value, expected);
}
static bool uuid(const char *value)
{
    if (!value || strlen(value) != 36) return false;
    for (unsigned i = 0; i < 36; ++i) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (value[i] != '-') return false; }
        else if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return false;
    }
    return true;
}
bool zj_custody_verify(const cJSON *ack, const zj_custody_expected_t *expected,
                        zj_crypto_port_t crypto, uint8_t receipt_digest[32])
{
    if (receipt_digest) memset(receipt_digest, 0, 32);
    if (!ack || !expected || !receipt_digest || !crypto.digest ||
        strlen(expected->observation_id) != 64 || strlen(expected->payload_digest) != 64) return false;
    const cJSON *version = unique(ack, "schema_version"), *items = unique(ack, "items");
    if (!equal(ack, "type", "zkt_observation_ack") || !cJSON_IsNumber(version) || version->valuedouble != 1 ||
        !cJSON_IsTrue(unique(ack, "committed")) || !equal(ack, "delivery_authority", "ADD") ||
        !equal(ack, "oracle_completion", "NOT_ASSERTED") || !cJSON_IsArray(items) || cJSON_GetArraySize(items) != 1)
        return false;
    const cJSON *row = cJSON_GetArrayItem(items, 0), *index = unique(row, "index");
    const char *receipt = string(row, "receipt_id"), *custody = string(row, "custody");
    if (!cJSON_IsNumber(index) || index->valuedouble != 0 || !uuid(receipt) || !custody ||
        (strcmp(custody, "PRESERVED_UNRESOLVED") && strcmp(custody, "PRESERVED_EXCEPTION")) ||
        !equal(row, "observation_id", expected->observation_id) || !equal(row, "payload_digest", expected->payload_digest))
        return false;
    char material[320];
    int length = snprintf(material, sizeof(material), "[\"zkt-add-custody-v1\",\"%s\",\"%s\",\"%s\",\"%s\"]",
        expected->observation_id, expected->payload_digest, receipt, custody);
    return length > 0 && (size_t)length < sizeof(material) &&
        crypto.digest(crypto.context, (const uint8_t *)material, (size_t)length, receipt_digest);
}
