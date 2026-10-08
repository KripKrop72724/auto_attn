#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#if defined(ZONE_LITE_QUALIFIED_READER_MATRIX) && ZONE_LITE_QUALIFIED_READER_MATRIX
#include "zkt_qualified_reader_matrix.h"
#endif

/* The caller still proves secure boot, validated slot, layout, terminal,
 * epoch and durable reader proof. A policy match alone grants no permission. */
static inline bool zj_reader_policy_image(const char *version, const uint8_t digest[32])
{
    if (!version || !digest) return false;
#if defined(ZONE_LITE_QUALIFIED_READER_MATRIX) && ZONE_LITE_QUALIFIED_READER_MATRIX
#if ZJ_READER_MATRIX_COUNT > 0
    for (unsigned i = 0; i < ZJ_READER_MATRIX_COUNT; ++i)
        if (!strcmp(version, zj_matrix_entries[i].version) &&
            !memcmp(digest, zj_matrix_entries[i].digest, 32)) return true;
#endif
    return false;
#else
    return !strcmp(version, ZJ_BRIDGE_VERSION);
#endif
}

static inline bool zj_reader_policy_target(const char *version, const char *hex)
{
    uint8_t digest[32];
    if (!hex || strnlen(hex, 65) != 64) return false;
    for (unsigned i = 0; i < 32; ++i) {
        unsigned digits[2];
        for (unsigned j = 0; j < 2; ++j) {
            unsigned char c = (unsigned char)hex[2 * i + j];
            if (c >= '0' && c <= '9') digits[j] = c - '0';
            else if (c >= 'a' && c <= 'f') digits[j] = c - 'a' + 10;
            else return false;
        }
        digest[i] = (uint8_t)((digits[0] << 4) | digits[1]);
    }
    return zj_reader_policy_image(version, digest);
}
