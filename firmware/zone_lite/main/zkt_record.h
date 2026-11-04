#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZKT_RECORD_DECODER_VERSION 2U
/* MB40-VL/ID (platform ZAM180_TFT) appends a 9-byte tail (0xff or 0x00, then
 * ASCII "255" and zeros) to the 40-byte record. Fields are read only from the
 * 40-byte prefix; digests, lengths and uploads keep all 49 original bytes. */
#define ZKT_RECORD_TAILED_BYTES 49U
#define ZKT_RECORD_MAX_BYTES ZKT_RECORD_TAILED_BYTES
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
/* A count-matched 49-byte width is ambiguous only when the payload is also a
 * whole number of 8-byte units: an 8/16/40-byte log that grew between the
 * count and the prepared read. A record read at the 49-byte stride must then
 * carry the observed tail. True for every other width and payload. */
bool zkt_record_width_proven(uint32_t bytes, uint32_t size, const uint8_t *record);
bool zkt_record_range(uint32_t prepared_bytes, uint32_t record_size,
                       uint32_t first, uint32_t end, uint32_t *offset, uint32_t *length);
const char *zkt_model_profile(const char *model);
