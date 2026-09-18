#include "hikvision_api.h"
#include "esp_random.h"
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
hik_result_t hik_history_page(hik_search_t *s, hik_record_fn fn, void *context)
{
    if (!s || !fn || !s->first_serial || s->first_serial > s->last_serial || s->last_serial > 3000000000U)
        return HIK_CONFIGURATION;
    if (s->complete) return HIK_OK;
    /* Search positions belong to a short-lived terminal session. Resume from
     * the durable source serial, with a fresh session and position zero. */
    for (unsigned i = 0; i < 4; i++) snprintf(s->search_id + i * 8, 9, "%08lx", (unsigned long)esp_random());
    cJSON *body = cJSON_CreateObject();
    cJSON *cond = body ? cJSON_AddObjectToObject(body, "AcsEventCond") : NULL;
    bool valid = cond && cJSON_AddStringToObject(cond, "searchID", s->search_id) &&
        cJSON_AddNumberToObject(cond, "searchResultPosition", 0) &&
        cJSON_AddNumberToObject(cond, "maxResults", 20) &&
        cJSON_AddNumberToObject(cond, "major", 0) && cJSON_AddNumberToObject(cond, "minor", 0) &&
        cJSON_AddBoolToObject(cond, "picEnable", false) &&
        cJSON_AddNumberToObject(cond, "beginSerialNo", s->previous_serial ? s->previous_serial + 1 : s->first_serial) &&
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
    valid = id && !strcmp(id, s->search_id) && status &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "numOfMatches"), &count) &&
        integer(cJSON_GetObjectItemCaseSensitive(page, "totalMatches"), &total) &&
        count <= 20 && s->position <= 150000 && total <= 150000 - s->position && count <= total &&
        (cJSON_IsArray(rows) || (!count && !rows)) &&
        (uint32_t)cJSON_GetArraySize(rows) == count && (!s->total_known || s->total == total + s->position);
    bool end = valid && count == total &&
        (!strcmp(status, "OK") || (!strcmp(status, "NO MATCH") && !count && !total));
    valid = valid && (end || (count && !strcmp(status, "MORE") && count < total));
    uint32_t previous = s->previous_serial;
    cJSON *row;
    cJSON_ArrayForEach(row, rows) {
        uint32_t serial;
        if (!integer(cJSON_GetObjectItemCaseSensitive(row, "serialNo"), &serial) ||
            serial < s->first_serial || serial > s->last_serial || serial <= previous) { valid = false; break; }
        previous = serial;
    }
    if (!valid) result = HIK_PARSE;
    else {
        cJSON_ArrayForEach(row, rows) {
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
        *verified = true;
        const cJSON *field;
        cJSON_ArrayForEach(field, desired) {
            if (!field->string || !cJSON_Compare(field, cJSON_GetObjectItemCaseSensitive(after, field->string), true)) *verified = false;
        }
    }
    cJSON_Delete(after);
    return *verified ? HIK_OK : result == HIK_OK ? HIK_CUSTODY : result;
}
hik_result_t hik_user_rename(const cJSON *expected, const char *name, bool *verified)
{
    *verified = false;
    const char *employee = string(expected, "employeeNo");
    if (!employee_valid(employee) || !name || !*name || strlen(name) > 128) return HIK_CONFIGURATION;
    hik_result_t result = hik_http_verify_identity();
    cJSON *before = NULL;
    if (result == HIK_OK) result = hik_user_read(employee, &before);
    if (result != HIK_OK) return result;
    if (!before || !cJSON_Compare(before, expected, true)) { cJSON_Delete(before); return HIK_BINDING; }
    cJSON *update = cJSON_CreateObject();
    bool valid = update && cJSON_AddStringToObject(update, "employeeNo", employee) && cJSON_AddStringToObject(update, "name", name);
    result = valid ? submit(HTTP_METHOD_PUT, "/ISAPI/AccessControl/UserInfo/Modify?format=json", "UserInfo", update) : HIK_NETWORK;
    cJSON_Delete(update);
    cJSON *after = NULL;
    hik_result_t read = hik_user_read(employee, &after);
    cJSON *new_name = cJSON_CreateString(name);
    bool changed = new_name && cJSON_ReplaceItemInObjectCaseSensitive(before, "name", new_name);
    if (!changed) cJSON_Delete(new_name);
    *verified = changed && read == HIK_OK && after && cJSON_Compare(before, after, true);
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
    if (!cJSON_Compare(before, expected, true)) { cJSON_Delete(before); return HIK_BINDING; }
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
