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
/* Facts about the last events page rejected with HIK_PARSE (empty if none). */
const char *hik_history_last_reject(void);
typedef bool (*hik_range_fn)(void *, const char *raw, size_t length, uint32_t serial);
/* Every event with begin <= serialNo <= end, in whatever order the terminal
 * returns them, through one paged search session (at most max_pages pages).
 * HIK_OK only after the whole range was read with a stable total and no
 * serial seen twice. max_pages is at most 100. */
hik_result_t hik_history_range(uint32_t begin, uint32_t end, unsigned max_pages, hik_range_fn, void *);
/* totalMatches for [begin, end] from a one-row search (no rows are kept). */
hik_result_t hik_history_count(uint32_t begin, uint32_t end, uint32_t *total);
/* A bounded observation of retained history, never a coverage certificate. */
hik_result_t hik_history_bounds(uint32_t *first, uint32_t *last, uint32_t *count);
hik_result_t hik_history_response(const cJSON *request, cJSON **response);
hik_result_t hik_user_read(const char *employee, cJSON **profile);
/* Bounded profile enumeration. A caller publishes only a complete stable scan. */
hik_result_t hik_user_page(hik_search_t *, hik_record_fn, void *);
/* desired_profile is the approved installation profile, not inferred rights.
 * A failed write response requires readback before a caller retries. */
bool hik_user_matches_created_profile(const cJSON *actual, const cJSON *desired);
/* Stable-fact mode for profiles whose user records carry fields that change
 * between reads: users compare (and are fingerprinted) by employeeNo,
 * localUIRight, name and userType only. Exact comparison otherwise. */
void hik_api_use_stable_profiles(bool enabled);
cJSON *hik_profile_stable_facts(const cJSON *profile);
bool hik_profile_equal(const cJSON *a, const cJSON *b);
hik_result_t hik_user_create(const cJSON *desired_profile, bool *verified);
hik_result_t hik_user_rename(const cJSON *expected_profile, const char *name, bool *verified);
hik_result_t hik_user_edit(const cJSON *expected_profile, const char *name, bool change_admin, bool admin, bool *verified);
hik_result_t hik_user_delete(const cJSON *expected_profile, bool *verified);
