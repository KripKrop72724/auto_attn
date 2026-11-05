#include "hikvision_api.h"
#include "esp_random.h"
#include "esp_timer.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define RESPONSE_BYTES 32768
static bool integer(const cJSON *v, uint32_t *out)
{
    if (!cJSON_IsNumber(v) || v->valuedouble < 0 || v->valuedouble > 3000000000.0 ||
        (double)(uint32_t)v->valuedouble != v->valuedouble) return false;
    *out = (uint32_t)v->valuedouble;
    return true;
}
static const char *string(const cJSON *root, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(root, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}
static bool employee_valid(const char *s)
{
    if (!s || !*s || strlen(s) > 32) return false;
    /* First certified profile uses numeric strings; no integer conversion. */
    return strspn(s, "0123456789") == strlen(s);
}
static hik_result_t request_json(esp_http_client_method_t method, const char *path,
    cJSON *body, cJSON **out)
{
    *out = NULL;
    char *request = cJSON_PrintUnformatted(body);
    char *response = malloc(RESPONSE_BYTES);
    if (!request || !response) { free(request); free(response); return HIK_NETWORK; }
    size_t length;
    hik_result_t result = hik_http_request(method, path, request, response, RESPONSE_BYTES, &length);
    if (result == HIK_OK) {
        *out = cJSON_ParseWithLength(response, length);
        if (!cJSON_IsObject(*out)) { cJSON_Delete(*out); *out = NULL; result = HIK_PARSE; }
    }
    free(request); free(response);
    return result;
}
void hik_search_init(hik_search_t *s, uint32_t first, uint32_t last)
{
    memset(s, 0, sizeof(*s));
    for (unsigned i = 0; i < 4; i++) snprintf(s->search_id + i * 8, 9, "%08lx", (unsigned long)esp_random());
    s->first_serial = first; s->last_serial = last;
}
hik_result_t hik_history_response(const cJSON *request, cJSON **response)
{
    *response = NULL;
    const cJSON *cond = cJSON_GetObjectItemCaseSensitive(request, "AcsEventCond");
    const char *id = string(cond, "searchID");
    uint32_t position, size, major, minor, first, last;
    if (!id || !*id || strlen(id) > 64 ||
        !integer(cJSON_GetObjectItemCaseSensitive(cond, "searchResultPosition"), &position) || position >= 150000 ||
        !integer(cJSON_GetObjectItemCaseSensitive(cond, "maxResults"), &size) || !size || size > 20 ||
        !integer(cJSON_GetObjectItemCaseSensitive(cond, "major"), &major) || major ||
        !integer(cJSON_GetObjectItemCaseSensitive(cond, "minor"), &minor) || minor ||
        !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(cond, "picEnable"))) return HIK_CONFIGURATION;
    const cJSON *begin = cJSON_GetObjectItemCaseSensitive(cond, "beginSerialNo");
    const cJSON *end = cJSON_GetObjectItemCaseSensitive(cond, "endSerialNo");
    if ((begin || end) && (!integer(begin, &first) || !integer(end, &last) || !first || first > last))
        return HIK_CONFIGURATION;
    return request_json(HTTP_METHOD_POST, "/ISAPI/AccessControl/AcsEvent?format=json", (cJSON *)request, response);
}
static hik_result_t boundary_at(uint32_t position, uint32_t *serial, uint32_t *total)
{
    hik_search_t s; hik_search_init(&s, 1, 3000000000U);
    cJSON *body = cJSON_CreateObject();
    cJSON *cond = body ? cJSON_AddObjectToObject(body, "AcsEventCond") : NULL;
    bool valid = cond && cJSON_AddStringToObject(cond, "searchID", s.search_id) &&
        cJSON_AddNumberToObject(cond, "searchResultPosition", position) &&
        cJSON_AddNumberToObject(cond, "maxResults", 1) &&
        cJSON_AddNumberToObject(cond, "major", 0) && cJSON_AddNumberToObject(cond, "minor", 0) &&
        cJSON_AddBoolToObject(cond, "picEnable", false);
    cJSON *response = NULL;
    hik_result_t result = valid ? request_json(HTTP_METHOD_POST,
        "/ISAPI/AccessControl/AcsEvent?format=json", body, &response) : HIK_NETWORK;
    cJSON_Delete(body);
    if (result != HIK_OK) return result;
    cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "AcsEvent");
    cJSON *rows = cJSON_GetObjectItemCaseSensitive(page, "InfoList");
    uint32_t count = 0;
    const char *id = string(page, "searchID");
    const char *status = string(page, "responseStatusStrg");
    valid = id && !strcmp(id, s.search_id) && status &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), total) &&
        *total <= 150000 && count <= 1 && count == (uint32_t)cJSON_GetArraySize(rows);
    *serial = 0;
    if (valid && !*total) valid = !position && !count && !strcmp(status, "NO MATCH");
    else if (valid) valid = count == 1 && position < *total &&
        (!strcmp(status, "OK") || !strcmp(status, "MORE")) &&
        integer(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(rows, 0), "serialNo"), serial) && *serial;
    cJSON_Delete(response);
    return valid ? HIK_OK : HIK_PARSE;
}
hik_result_t hik_history_bounds(uint32_t *first, uint32_t *last, uint32_t *count)
{
    if (!first || !last || !count) return HIK_CONFIGURATION;
    *first = *last = *count = 0;
    hik_result_t result = boundary_at(0, first, count);
    if (result != HIK_OK || !*count) return result;
    uint32_t total, first_again;
    result = boundary_at(*count - 1, last, &total);
    if (result != HIK_OK) return result;
    if (total < *count || *last < *first) return HIK_BINDING;
    result = boundary_at(0, &first_again, &total);
    if (result == HIK_OK && (first_again != *first || total < *count)) return HIK_BINDING;
    return result;
}
/* Compact facts about the last events page the checks below rejected, so a
 * terminal that answers differently can be diagnosed from ADD logs alone. */
static char history_reject[160];
const char *hik_history_last_reject(void) { return history_reject; }
hik_result_t hik_history_page(hik_search_t *s, hik_record_fn fn, void *context)
{
    if (!s || !fn || !s->first_serial || s->first_serial > s->last_serial || s->last_serial > 3000000000U)
        return HIK_CONFIGURATION;
    if (s->complete) return HIK_OK;
    history_reject[0] = 0;
    /* Search positions belong to a short-lived terminal session. Resume from
     * the durable source serial, with a fresh session and position zero. */
    for (unsigned i = 0; i < 4; i++) snprintf(s->search_id + i * 8, 9, "%08lx", (unsigned long)esp_random());
    uint32_t wanted=s->previous_serial?s->previous_serial+1:s->first_serial;
    /* One-record overlap supports terminals that interpret beginSerialNo as
     * inclusive or exclusive. Only the exact predecessor may be discarded. */
    uint32_t request_begin=wanted-1;
    cJSON *body = cJSON_CreateObject();
    cJSON *cond = body ? cJSON_AddObjectToObject(body, "AcsEventCond") : NULL;
    bool valid = cond && cJSON_AddStringToObject(cond, "searchID", s->search_id) &&
        cJSON_AddNumberToObject(cond, "searchResultPosition", 0) &&
        cJSON_AddNumberToObject(cond, "maxResults", 20) &&
        cJSON_AddNumberToObject(cond, "major", 0) && cJSON_AddNumberToObject(cond, "minor", 0) &&
        cJSON_AddBoolToObject(cond, "picEnable", false) &&
        cJSON_AddNumberToObject(cond, "beginSerialNo", request_begin) &&
        cJSON_AddNumberToObject(cond, "endSerialNo", s->last_serial);
    cJSON *response = NULL;
    hik_result_t result = valid ? request_json(HTTP_METHOD_POST,
        "/ISAPI/AccessControl/AcsEvent?format=json", body, &response) : HIK_NETWORK;
    cJSON_Delete(body);
    if (result != HIK_OK) return result;
    cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "AcsEvent");
    cJSON *rows = cJSON_GetObjectItemCaseSensitive(page, "InfoList");
    const char *status = string(page, "responseStatusStrg");
    const char *id = string(page, "searchID");
    uint32_t count = 0, total = 0;
    uint32_t raw_count = 0, raw_total = 0, raw_first = 0, raw_last = 0;
    bool counts = integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &raw_count);
    counts = integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &raw_total) && counts;
    int raw_rows = cJSON_IsArray(rows) ? cJSON_GetArraySize(rows) : 0;
    if (raw_rows) {
        (void)integer(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(rows, 0), "serialNo"), &raw_first);
        (void)integer(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(rows, raw_rows - 1), "serialNo"), &raw_last);
    }
    valid = id && !strcmp(id, s->search_id) && status &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &total) &&
        count <= 20 && s->position <= 150000 && count <= total &&
        (cJSON_IsArray(rows) || (!count && !rows)) &&
        (uint32_t)cJSON_GetArraySize(rows) == count;
    uint32_t overlap=0,first_returned=0;
    if(valid && count && integer(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(rows,0),"serialNo"),&first_returned)
        && first_returned==request_begin)overlap=1;
    count-=overlap;total-=overlap;
    valid=valid && total<=150000-s->position && (!s->total_known || s->total==total+s->position);
    bool end = valid && count == total &&
        (!strcmp(status, "OK") || (!strcmp(status, "NO MATCH") && !count && !total));
    valid = valid && (end || (count && !strcmp(status, "MORE") && count < total));
    bool header_valid = valid;
    uint32_t previous = s->previous_serial;
    unsigned index=0, bad_row=0;
    cJSON *row;
    cJSON_ArrayForEach(row, rows) {
        uint32_t serial;
        if(index++<overlap)continue;
        if (!integer(cJSON_GetObjectItemCaseSensitive(row, "serialNo"), &serial) ||
            serial < s->first_serial || serial > s->last_serial || serial <= previous) {
            valid = false; bad_row = index; break;
        }
        previous = serial;
    }
    if (!valid) {
        result = HIK_PARSE;
        snprintf(history_reject, sizeof(history_reject),
            "page=%s; id=%s; status=%.12s; counts=%s; num=%lu; total=%lu; rows=%d; first=%lu; last=%lu; begin=%lu; bad_row=%u",
            header_valid ? "rows" : "header", id && !strcmp(id, s->search_id) ? "match" : "differs",
            status ? status : "none", counts ? "yes" : "no", (unsigned long)raw_count, (unsigned long)raw_total,
            cJSON_IsArray(rows) ? raw_rows : -1, (unsigned long)raw_first, (unsigned long)raw_last,
            (unsigned long)request_begin, bad_row);
    }
    else {
        index=0;
        cJSON_ArrayForEach(row, rows) {
            if(index++<overlap)continue;
            char *raw = cJSON_PrintUnformatted(row);
            bool saved = raw && fn(context, raw, strlen(raw));
            free(raw);
            if (!saved) { result = HIK_CUSTODY; break; }
        }
        if (result == HIK_OK) {
            s->total = total + s->position;
            s->previous_serial = previous; s->position += count;
            s->total_known = true; s->complete = end;
        }
    }
    cJSON_Delete(response);
    return result;
}
/* Which AcsEvent request of the last count/range call ran last, and how long
 * it took, so a failing poll can say which sub-request failed (3.1.6). */
static unsigned request_ms, request_page;
unsigned hik_history_last_request_ms(void) { return request_ms; }
unsigned hik_history_last_page(void) { return request_page; }
static hik_result_t range_request(const char *search_id, uint32_t position, unsigned size,
    uint32_t begin, uint32_t end, cJSON **response)
{
    int64_t started = esp_timer_get_time();
    cJSON *body = cJSON_CreateObject();
    cJSON *cond = body ? cJSON_AddObjectToObject(body, "AcsEventCond") : NULL;
    bool valid = cond && cJSON_AddStringToObject(cond, "searchID", search_id) &&
        cJSON_AddNumberToObject(cond, "searchResultPosition", position) &&
        cJSON_AddNumberToObject(cond, "maxResults", size) &&
        cJSON_AddNumberToObject(cond, "major", 0) && cJSON_AddNumberToObject(cond, "minor", 0) &&
        cJSON_AddBoolToObject(cond, "picEnable", false) &&
        cJSON_AddNumberToObject(cond, "beginSerialNo", begin) &&
        cJSON_AddNumberToObject(cond, "endSerialNo", end);
    hik_result_t result = valid ? request_json(HTTP_METHOD_POST,
        "/ISAPI/AccessControl/AcsEvent?format=json", body, response) : HIK_NETWORK;
    int64_t elapsed = (esp_timer_get_time() - started) / 1000;
    request_ms = elapsed < 0 ? 0 : (unsigned)elapsed;
    cJSON_Delete(body);
    if (result == HIK_PARSE)
        snprintf(history_reject, sizeof(history_reject), "range=json; position=%lu; begin=%lu; end=%lu",
            (unsigned long)position, (unsigned long)begin, (unsigned long)end);
    return result;
}
static void new_search_id(char search_id[33])
{
    for (unsigned i = 0; i < 4; i++) snprintf(search_id + i * 8, 9, "%08lx", (unsigned long)esp_random());
}
hik_result_t hik_history_count(uint32_t begin, uint32_t end, uint32_t *total)
{
    if (!total || !begin || begin > end || end > 3000000000U) return HIK_CONFIGURATION;
    *total = 0; history_reject[0] = 0; request_page = 0; request_ms = 0;
    char search_id[33]; new_search_id(search_id);
    cJSON *response = NULL;
    hik_result_t result = range_request(search_id, 0, 1, begin, end, &response);
    if (result != HIK_OK) return result;
    cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "AcsEvent");
    const char *id = string(page, "searchID");
    uint32_t count = 0;
    bool valid = id && !strcmp(id, search_id) && string(page, "responseStatusStrg") &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), total) &&
        count <= 1 && count <= *total && *total <= 150000;
    cJSON_Delete(response);
    if (!valid) {
        snprintf(history_reject, sizeof(history_reject), "count=header; begin=%lu; end=%lu",
            (unsigned long)begin, (unsigned long)end);
        *total = 0;
        return HIK_PARSE;
    }
    return HIK_OK;
}
hik_result_t hik_history_range(uint32_t begin, uint32_t end, unsigned max_pages,
    hik_range_fn fn, void *context)
{
    if (!fn || !begin || begin > end || end > 3000000000U || !max_pages || max_pages > 100)
        return HIK_CONFIGURATION;
    history_reject[0] = 0; request_page = 0; request_ms = 0;
    /* One terminal search session, paged by position. Some terminals (Jhelum's
     * DS-K1T321MFWX) sort by event time, and a clock that restarts at 2015 on
     * every power-up puts boot records before older punches. So rows may come
     * in any serial order; only the requested serial range is trusted.
     * Equal timestamps may be ordered differently between page requests, so a
     * serial seen twice means another was skipped: with a stable total and no
     * repeats, every row in the range was read exactly once. */
    uint32_t *seen = malloc(sizeof(uint32_t) * 20 * max_pages);
    if (!seen) return HIK_CUSTODY;
    char search_id[33]; new_search_id(search_id);
    uint32_t position = 0, expected_total = 0, seen_count = 0;
    hik_result_t outcome = HIK_OVERSIZED;
    for (unsigned pages = 0; pages < max_pages; pages++) {
        cJSON *response = NULL;
        request_page = pages + 1;
        hik_result_t result = range_request(search_id, position, 20, begin, end, &response);
        if (result != HIK_OK) { outcome = result; break; }
        cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "AcsEvent");
        cJSON *rows = cJSON_GetObjectItemCaseSensitive(page, "InfoList");
        const char *status = string(page, "responseStatusStrg");
        const char *id = string(page, "searchID");
        uint32_t count = 0, total = 0, bad_serial = 0;
        int size = cJSON_IsArray(rows) ? cJSON_GetArraySize(rows) : 0;
        const char *reject = NULL;
        if (!id || strcmp(id, search_id)) reject = "search_id";
        else if (!status || !integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) ||
            !integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &total)) reject = "counts";
        else if (count > 20 || count > total || total > 150000 || (uint32_t)size != count ||
            (!cJSON_IsArray(rows) && rows)) reject = "rows";
        else if (pages && total != expected_total) reject = "total_changed";
        else if (position + count == total
            ? strcmp(status, "OK") && !(!total && !strcmp(status, "NO MATCH"))
            : !count || position + count > total || strcmp(status, "MORE")) reject = "completion";
        cJSON *row;
        if (!reject) cJSON_ArrayForEach(row, rows) {
            if (!integer(cJSON_GetObjectItemCaseSensitive(row, "serialNo"), &bad_serial) ||
                bad_serial < begin || bad_serial > end) { reject = "serial_scope"; break; }
            for (uint32_t i = 0; i < seen_count && !reject; i++)
                if (seen[i] == bad_serial) reject = "duplicate_serial";
            if (reject) break;
            seen[seen_count++] = bad_serial;
        }
        if (reject) {
            snprintf(history_reject, sizeof(history_reject),
                "range=%s; status=%.12s; num=%lu; total=%lu; rows=%d; position=%lu; begin=%lu; end=%lu; serial=%lu",
                reject, status ? status : "none", (unsigned long)count, (unsigned long)total, size,
                (unsigned long)position, (unsigned long)begin, (unsigned long)end, (unsigned long)bad_serial);
            cJSON_Delete(response);
            outcome = HIK_PARSE;
            break;
        }
        bool kept = true;
        cJSON_ArrayForEach(row, rows) {
            uint32_t serial = 0;
            (void)integer(cJSON_GetObjectItemCaseSensitive(row, "serialNo"), &serial);
            char *raw = cJSON_PrintUnformatted(row);
            kept = raw && fn(context, raw, strlen(raw), serial);
            free(raw);
            if (!kept) break;
        }
        cJSON_Delete(response);
        if (!kept) { outcome = HIK_CUSTODY; break; }
        expected_total = total;
        position += count;
        if (position == total) { outcome = HIK_OK; break; }
    }
    if (outcome == HIK_OVERSIZED)
        snprintf(history_reject, sizeof(history_reject), "range=page_limit; position=%lu; total=%lu; begin=%lu",
            (unsigned long)position, (unsigned long)expected_total, (unsigned long)begin);
    free(seen);
    return outcome;
}
hik_result_t hik_user_read(const char *employee, cJSON **profile)
{
    *profile = NULL;
    if (!employee_valid(employee)) return HIK_CONFIGURATION;
    hik_search_t search; hik_search_init(&search, 1, 1);
    cJSON *body = cJSON_CreateObject();
    cJSON *cond = body ? cJSON_AddObjectToObject(body, "UserInfoSearchCond") : NULL;
    cJSON *list = cond ? cJSON_AddArrayToObject(cond, "EmployeeNoList") : NULL;
    cJSON *target = cJSON_CreateObject();
    bool valid = cond && list && target && cJSON_AddStringToObject(target, "employeeNo", employee) &&
        cJSON_AddStringToObject(cond, "searchID", search.search_id) &&
        cJSON_AddNumberToObject(cond, "searchResultPosition", 0) &&
        cJSON_AddNumberToObject(cond, "maxResults", 1);
    if (list && target) cJSON_AddItemToArray(list, target); else cJSON_Delete(target);
    cJSON *response = NULL;
    hik_result_t result = valid ? request_json(HTTP_METHOD_POST,
        "/ISAPI/AccessControl/UserInfo/Search?format=json", body, &response) : HIK_NETWORK;
    cJSON_Delete(body);
    if (result != HIK_OK) return result;
    cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "UserInfoSearch");
    cJSON *users = cJSON_GetObjectItemCaseSensitive(page, "UserInfo");
    uint32_t count, total;
    const char *id = string(page, "searchID");
    if (!id || strcmp(id, search.search_id) ||
        !integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) ||
        !integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &total) ||
        count != total || count > 1 || (uint32_t)cJSON_GetArraySize(users) != count) result = HIK_PARSE;
    else if (count) {
        const cJSON *user = cJSON_GetArrayItem(users, 0);
        const char *actual = string(user, "employeeNo");
        if (!actual || strcmp(actual, employee)) result = HIK_BINDING;
        else if (!(*profile = cJSON_Duplicate(user, true))) result = HIK_NETWORK;
    }
    cJSON_Delete(response);
    return result;
}
hik_result_t hik_user_page(hik_search_t *search, hik_record_fn fn, void *context)
{
    if (!search || !fn || !search->search_id[0] || search->position > 3000) return HIK_CONFIGURATION;
    if (search->complete) return HIK_OK;
    cJSON *body = cJSON_CreateObject();
    cJSON *condition = body ? cJSON_AddObjectToObject(body, "UserInfoSearchCond") : NULL;
    bool valid = condition && cJSON_AddStringToObject(condition, "searchID", search->search_id) &&
        cJSON_AddNumberToObject(condition, "searchResultPosition", search->position) &&
        cJSON_AddNumberToObject(condition, "maxResults", 20);
    cJSON *response = NULL;
    hik_result_t result = valid ? request_json(HTTP_METHOD_POST,
        "/ISAPI/AccessControl/UserInfo/Search?format=json", body, &response) : HIK_NETWORK;
    cJSON_Delete(body);
    if (result != HIK_OK) return result;
    cJSON *page = cJSON_GetObjectItemCaseSensitive(response, "UserInfoSearch");
    cJSON *users = cJSON_GetObjectItemCaseSensitive(page, "UserInfo");
    const char *id = string(page, "searchID"), *status = string(page, "responseStatusStrg");
    uint32_t count = 0, total = 0;
    valid = id && !strcmp(id, search->search_id) && status &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &total) &&
        total <= 3000 && count <= 20 && search->position + count <= total &&
        count == (uint32_t)cJSON_GetArraySize(users) &&
        (!count || cJSON_IsArray(users)) &&
        (!search->total_known || search->total == total);
    if (valid) {
        bool last = search->position + count == total;
        valid = !total ? !strcmp(status, "NO MATCH") :
            count && !strcmp(status, last ? "OK" : "MORE");
    }
    for (uint32_t i = 0; valid && i < count; i++) {
        cJSON *user = cJSON_GetArrayItem(users, (int)i);
        const char *employee = string(user, "employeeNo"), *name = string(user, "name");
        valid = employee_valid(employee) && name && strlen(name) <= 128;
        for (uint32_t j = 0; valid && j < i; j++) {
            const char *other = string(cJSON_GetArrayItem(users, (int)j), "employeeNo");
            valid = other && strcmp(other, employee);
        }
    }
    if (!valid) result = HIK_PARSE;
    for (uint32_t i = 0; result == HIK_OK && i < count; i++) {
        char *raw = cJSON_PrintUnformatted(cJSON_GetArrayItem(users, (int)i));
        if (!raw || !fn(context, raw, strlen(raw))) result = HIK_CUSTODY;
        free(raw);
    }
    if (result == HIK_OK) {
        search->total = total; search->total_known = true;
        search->position += count; search->complete = search->position == total;
    }
    cJSON_Delete(response);
    return result;
}
static hik_result_t submit(esp_http_client_method_t method, const char *path, const char *root_name, const cJSON *value)
{
    cJSON *body = cJSON_CreateObject();
    cJSON *copy = cJSON_Duplicate(value, true);
    if (!body || !copy) { cJSON_Delete(body); cJSON_Delete(copy); return HIK_NETWORK; }
    cJSON_AddItemToObject(body, root_name, copy);
    cJSON *response = NULL;
    hik_result_t result = request_json(method, path, body, &response);
    uint32_t status;
    if (result == HIK_OK && (!integer(cJSON_GetObjectItemCaseSensitive(response, "statusCode"), &status) || status != 1)) result = HIK_HTTP_STATUS;
    cJSON_Delete(response); cJSON_Delete(body);
    return result;
}
/* Stable-fact profiles (Jhelum's DS-K1T321MFWX) report user fields that change
 * between two reads of an unchanged user. For them a user is the same user
 * when these facts match; ADD fingerprints the same facts. */
static bool stable_profiles;
void hik_api_use_stable_profiles(bool enabled) { stable_profiles = enabled; }
static const char *const stable_keys[] = {"employeeNo", "localUIRight", "name", "userType"};
cJSON *hik_profile_stable_facts(const cJSON *profile)
{
    /* Sorted keys, missing facts as null: the JSON ADD hashes with
     * json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False). */
    cJSON *facts = cJSON_CreateObject();
    for (unsigned i = 0; facts && i < 4; i++) {
        const cJSON *value = cJSON_GetObjectItemCaseSensitive(profile, stable_keys[i]);
        cJSON *copy = value ? cJSON_Duplicate(value, true) : cJSON_CreateNull();
        if (!copy || !cJSON_AddItemToObject(facts, stable_keys[i], copy)) {
            cJSON_Delete(copy); cJSON_Delete(facts); facts = NULL;
        }
    }
    return facts;
}
/* Also unchanged across a rename or a delete precondition: credentials,
 * rights and validity. A terminal that disturbs them fails verification. */
static const char *const protected_keys[] = {"numOfFace", "numOfFP", "numOfCard", "doorRight", "RightPlan", "Valid"};
bool hik_profile_equal(const cJSON *a, const cJSON *b)
{
    if (!a || !b) return false;
    if (!stable_profiles) return cJSON_Compare(a, b, true);
    cJSON *left = hik_profile_stable_facts(a), *right = hik_profile_stable_facts(b);
    bool same = left && right && cJSON_Compare(left, right, true);
    cJSON_Delete(left); cJSON_Delete(right);
    for (unsigned i = 0; same && i < sizeof(protected_keys) / sizeof(protected_keys[0]); i++) {
        const cJSON *x = cJSON_GetObjectItemCaseSensitive(a, protected_keys[i]);
        const cJSON *y = cJSON_GetObjectItemCaseSensitive(b, protected_keys[i]);
        same = (!x && !y) || (x && y && cJSON_Compare(x, y, true));
    }
    return same;
}
bool hik_user_matches_created_profile(const cJSON *actual, const cJSON *desired)
{
    const cJSON *field;
    cJSON_ArrayForEach(field, desired) {
        if (!field->string) return false;
        const cJSON *observed = cJSON_GetObjectItemCaseSensitive(actual, field->string);
        if (cJSON_Compare(field, observed, true)) continue;
        /* This exact pilot firmware canonicalizes disabled validity dates to
         * the epoch. Accept only that observed normalization, not arbitrary
         * changed rights, enabled validity, or other field differences. */
        if (strcmp(field->string, "Valid") ||
            !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(field, "enable"))) return false;
        cJSON *canonical = cJSON_Duplicate(field, true);
        if (!canonical) return false;
        const char *dates[] = {"beginTime", "endTime"};
        bool valid = true;
        for (unsigned i = 0; i < 2; i++) {
            cJSON *epoch = cJSON_CreateString("1970-01-01T00:00:00");
            if (!epoch || !cJSON_ReplaceItemInObjectCaseSensitive(canonical, dates[i], epoch)) {
                cJSON_Delete(epoch); valid = false; break;
            }
        }
        valid = valid && cJSON_Compare(canonical, observed, true);
        cJSON_Delete(canonical);
        if (!valid) return false;
    }
    return true;
}
hik_result_t hik_user_create(const cJSON *desired, bool *verified)
{
    *verified = false;
    const char *employee = string(desired, "employeeNo");
    if (!employee_valid(employee) || !string(desired, "name") ||
        !string(desired, "userType") || strcmp(string(desired, "userType"), "normal")) return HIK_CONFIGURATION;
    hik_result_t result = hik_http_verify_identity();
    cJSON *before = NULL;
    if (result == HIK_OK) result = hik_user_read(employee, &before);
    if (result != HIK_OK) return result;
    if (before) { cJSON_Delete(before); return HIK_BINDING; } /* no blind create retry */
    result = submit(HTTP_METHOD_POST, "/ISAPI/AccessControl/UserInfo/Record?format=json", "UserInfo", desired);
    cJSON *after = NULL;
    hik_result_t read = hik_user_read(employee, &after);
    if (read == HIK_OK && after) {
        *verified = hik_user_matches_created_profile(after, desired);
    }
    cJSON_Delete(after);
    return *verified ? HIK_OK : result == HIK_OK ? HIK_CUSTODY : result;
}
hik_result_t hik_user_rename(const cJSON *expected, const char *name, bool *verified)
{
    return hik_user_edit(expected, name, false, false, verified);
}
hik_result_t hik_user_edit(const cJSON *expected, const char *name, bool change_admin, bool admin, bool *verified)
{
    *verified = false;
    const char *employee = string(expected, "employeeNo");
    if (!employee_valid(employee) || !name || !*name || strlen(name) > 128) return HIK_CONFIGURATION;
    hik_result_t result = hik_http_verify_identity();
    cJSON *before = NULL;
    if (result == HIK_OK) result = hik_user_read(employee, &before);
    if (result != HIK_OK) return result;
    if (!before || !hik_profile_equal(before, expected)) { cJSON_Delete(before); return HIK_BINDING; }
    cJSON *update = cJSON_CreateObject();
    bool valid = update && cJSON_AddStringToObject(update, "employeeNo", employee) && cJSON_AddStringToObject(update, "name", name);
    if (change_admin) valid = valid && cJSON_AddBoolToObject(update, "localUIRight", admin);
    result = valid ? submit(HTTP_METHOD_PUT, "/ISAPI/AccessControl/UserInfo/Modify?format=json", "UserInfo", update) : HIK_NETWORK;
    cJSON_Delete(update);
    cJSON *after = NULL;
    hik_result_t read = hik_user_read(employee, &after);
    cJSON *new_name = cJSON_CreateString(name);
    bool changed = new_name && cJSON_ReplaceItemInObjectCaseSensitive(before, "name", new_name);
    if (!changed) cJSON_Delete(new_name);
    if (changed && change_admin) {
        cJSON *right = cJSON_CreateBool(admin);
        changed = right && cJSON_ReplaceItemInObjectCaseSensitive(before, "localUIRight", right);
        if (!changed) cJSON_Delete(right);
    }
    *verified = changed && read == HIK_OK && after && hik_profile_equal(before, after);
    cJSON_Delete(after); cJSON_Delete(before);
    return *verified ? HIK_OK : result == HIK_OK ? HIK_CUSTODY : result;
}
hik_result_t hik_user_delete(const cJSON *expected, bool *verified)
{
    *verified = false;
    const char *employee = string(expected, "employeeNo");
    if (!employee_valid(employee)) return HIK_CONFIGURATION;
    hik_result_t result = hik_http_verify_identity();
    cJSON *before = NULL;
    if (result == HIK_OK) result = hik_user_read(employee, &before);
    if (result != HIK_OK) return result;
    if (!before) { *verified = true; return HIK_OK; }
    if (!hik_profile_equal(before, expected)) { cJSON_Delete(before); return HIK_BINDING; }
    cJSON_Delete(before);
    cJSON *value = cJSON_CreateObject();
    cJSON *list = value ? cJSON_AddArrayToObject(value, "EmployeeNoList") : NULL;
    cJSON *target = cJSON_CreateObject();
    bool valid = list && target && cJSON_AddStringToObject(target, "employeeNo", employee) &&
        cJSON_AddStringToObject(value, "mode", "byEmployeeNo");
    if (target && list) cJSON_AddItemToArray(list, target); else cJSON_Delete(target);
    result = valid ? submit(HTTP_METHOD_PUT, "/ISAPI/AccessControl/UserInfoDetail/Delete?format=json", "UserInfoDetail", value) : HIK_NETWORK;
    cJSON_Delete(value);
    cJSON *after = NULL;
    hik_result_t read = hik_user_read(employee, &after);
    *verified = read == HIK_OK && !after;
    cJSON_Delete(after);
    return *verified ? HIK_OK : result == HIK_OK ? HIK_CUSTODY : result;
}
