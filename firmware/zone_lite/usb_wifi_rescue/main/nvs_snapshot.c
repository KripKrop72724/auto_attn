#include "nvs_snapshot.h"
#include <stdlib.h>
#include <string.h>
#include "mbedtls/sha256.h"
#include "nvs.h"

#define MAX_KEYS 256
#define MAX_VALUE 16384
typedef struct {
    char namespace_name[16];
    char key[16];
    uint8_t type;
    uint8_t value_digest[32];
} entry_t;

static int compare(const void *a, const void *b)
{
    const entry_t *left = a, *right = b;
    int result = strcmp(left->namespace_name, right->namespace_name);
    return result ? result : strcmp(left->key, right->key);
}

static esp_err_t read_value(nvs_handle_t h, const nvs_entry_info_t *info,
                            unsigned char *value, size_t *size)
{
    switch (info->type) {
    case NVS_TYPE_U8: *size = 1; return nvs_get_u8(h, info->key, (uint8_t *)value);
    case NVS_TYPE_I8: *size = 1; return nvs_get_i8(h, info->key, (int8_t *)value);
    case NVS_TYPE_U16: *size = 2; return nvs_get_u16(h, info->key, (uint16_t *)value);
    case NVS_TYPE_I16: *size = 2; return nvs_get_i16(h, info->key, (int16_t *)value);
    case NVS_TYPE_U32: *size = 4; return nvs_get_u32(h, info->key, (uint32_t *)value);
    case NVS_TYPE_I32: *size = 4; return nvs_get_i32(h, info->key, (int32_t *)value);
    case NVS_TYPE_U64: *size = 8; return nvs_get_u64(h, info->key, (uint64_t *)value);
    case NVS_TYPE_I64: *size = 8; return nvs_get_i64(h, info->key, (int64_t *)value);
    case NVS_TYPE_STR: return nvs_get_str(h, info->key, (char *)value, size);
    case NVS_TYPE_BLOB: return nvs_get_blob(h, info->key, value, size);
    default: return ESP_ERR_INVALID_STATE;
    }
}

esp_err_t rescue_nvs_snapshot(uint8_t digest[32], size_t *key_count)
{
    entry_t *entries = calloc(MAX_KEYS, sizeof(*entries));
    unsigned char *value = malloc(MAX_VALUE);
    if (!entries || !value) { free(entries); free(value); return ESP_ERR_NO_MEM; }
    nvs_iterator_t iterator = NULL;
    esp_err_t err = nvs_entry_find("nvs", NULL, NVS_TYPE_ANY, &iterator);
    size_t count = 0;
    while (err == ESP_OK) {
        nvs_entry_info_t info;
        err = nvs_entry_info(iterator, &info);
        if (err != ESP_OK) break;
        if (strcmp(info.namespace_name, "zone_cfg") ||
            (strcmp(info.key, "wifi_ssid") && strcmp(info.key, "wifi_pass"))) {
            if (count == MAX_KEYS) { err = ESP_ERR_NO_MEM; break; }
            nvs_handle_t handle;
            err = nvs_open(info.namespace_name, NVS_READONLY, &handle);
            if (err != ESP_OK) break;
            size_t size = MAX_VALUE;
            err = read_value(handle, &info, value, &size);
            nvs_close(handle);
            if (err != ESP_OK) break;
            entry_t *entry = &entries[count++];
            memcpy(entry->namespace_name, info.namespace_name, sizeof(entry->namespace_name));
            memcpy(entry->key, info.key, sizeof(entry->key));
            entry->type = info.type;
            if (mbedtls_sha256(value, size, entry->value_digest, 0)) {
                err = ESP_FAIL;
                break;
            }
        }
        err = nvs_entry_next(&iterator);
    }
    nvs_release_iterator(iterator);
    if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
    if (err == ESP_OK) {
        qsort(entries, count, sizeof(*entries), compare);
        if (mbedtls_sha256((const unsigned char *)entries, count * sizeof(*entries), digest, 0)) {
            err = ESP_FAIL;
        }
        *key_count = count;
    }
    volatile unsigned char *p = value;
    for (size_t i = 0; i < MAX_VALUE; ++i) p[i] = 0;
    memset(entries, 0, MAX_KEYS * sizeof(*entries));
    free(value);
    free(entries);
    return err;
}
