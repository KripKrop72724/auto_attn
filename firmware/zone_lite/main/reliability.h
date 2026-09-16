#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/types.h>

typedef enum { REL_SCAN_ERROR = -1, REL_SCAN_EMPTY = 0, REL_SCAN_OK = 1 } rel_scan_result_t;
rel_scan_result_t rel_count_rows(FILE *file, uint32_t *count);
bool rel_settled_eof(FILE *file, off_t boundary);
bool rel_json_syntax_valid(const char *text, size_t length);

typedef struct {
    char user_id[32];
    uint8_t status, punch;
    uint32_t timestamp;
} rel_live_record_t;
/* A hint is a previously established wire record size, never a user-record size.
 * Without a hint only one complete, unambiguous record is accepted. */
bool rel_live_frame_size(size_t length, size_t hint, size_t *record_size);
bool rel_parse_live_record(const uint8_t *data, size_t length, rel_live_record_t *out);
bool rel_identity_matches(const char *saved_serial, const char *current_serial,
                          const char *saved_fingerprint, const char *current_fingerprint);
