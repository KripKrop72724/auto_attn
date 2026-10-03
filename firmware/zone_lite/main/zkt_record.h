#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZKT_RECORD_DECODER_VERSION 2U
typedef struct {
    uint16_t attendance_uid; /* Source evidence, never an enrollment UID claim. */
    char user_id[32];
    uint32_t encoded_time;
    uint8_t status, punch;
} zkt_record_t;

/* Decode fields only. Calendar/clock quality and employee resolution are
 * separate checks. Callers retain the raw bytes even on a false result. */
bool zkt_record_decode(const uint8_t *raw, size_t length, zkt_record_t *out);
/* Missing source identity is a preserved hold, never a current-roster lookup.
 * Clock/layout qualification remains a separate prerequisite. */
bool zkt_record_identity_missing(const uint8_t *raw, size_t length);
uint32_t zkt_record_size(uint32_t bytes, uint32_t count,
                         const uint32_t *sizes, size_t size_count);
bool zkt_record_range(uint32_t prepared_bytes, uint32_t record_size,
                       uint32_t first, uint32_t end, uint32_t *offset, uint32_t *length);
const char *zkt_model_profile(const char *model);
