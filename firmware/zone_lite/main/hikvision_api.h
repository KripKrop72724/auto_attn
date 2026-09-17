#pragma once
#include "hikvision_http.h"
#include "cJSON.h"

typedef struct {
    char search_id[33];
    uint32_t position, total, first_serial, last_serial, previous_serial;
    bool total_known, complete;
} hik_search_t;
typedef bool (*hik_record_fn)(void *, const char *, size_t);
void hik_search_init(hik_search_t *, uint32_t first_serial, uint32_t last_serial);
hik_result_t hik_history_page(hik_search_t *, hik_record_fn, void *);
/* A bounded observation of retained history, never a coverage certificate. */
hik_result_t hik_history_bounds(uint32_t *first, uint32_t *last, uint32_t *count);
hik_result_t hik_history_response(const cJSON *request, cJSON **response);
hik_result_t hik_user_read(const char *employee, cJSON **profile);
/* desired_profile is the approved installation profile, not inferred rights.
 * A failed write response requires readback before a caller retries. */
hik_result_t hik_user_create(const cJSON *desired_profile, bool *verified);
hik_result_t hik_user_rename(const cJSON *expected_profile, const char *name, bool *verified);
hik_result_t hik_user_delete(const cJSON *expected_profile, bool *verified);
