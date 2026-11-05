#include "hikvision_api.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned mode, received, calls;
static cJSON *person;
static bool timeout_after_write, deletion_pending;
uint32_t esp_random(void) { return ++calls; }
hik_result_t hik_http_verify_identity(void) { return HIK_OK; }
static bool saved(void *ctx, const char *raw, size_t length)
{
    if (ctx) return false;
    cJSON *r = cJSON_ParseWithLength(raw, length);
    assert(r);
    assert(cJSON_GetObjectItem(r, "serialNo")->valuedouble == 30001 + received * 2);
    received++;
    cJSON_Delete(r);
    return true;
}
hik_result_t hik_http_request(esp_http_client_method_t method, const char *path,
    const char *body, char *response, size_t capacity, size_t *length)
{
    cJSON *req = cJSON_Parse(body); assert(req);
    cJSON *out = cJSON_CreateObject();
    if (strstr(path, "/AcsEvent?") && mode >= 40) {
        /* Jhelum DS-K1T321MFWX: rows sorted by event time; boot records dated
         * 2015 sort before earlier punches, so serials arrive out of order. */
        static const unsigned order[] = {3290, 3294, 3306, 3311, 3503, 3852, 3161, 3162, 3175, 3177,
            3178, 3188, 3189, 3199, 3200, 3223, 3224, 3277, 3278, 3300, 3301, 3302, 3400, 3853, 3860,
            3861, 3862, 3863, 3864, 3865, 3866}; /* 24 from 3199: two pages */
        cJSON *cond = cJSON_GetObjectItem(req, "AcsEventCond");
        unsigned begin = (unsigned)cJSON_GetObjectItem(cond, "beginSerialNo")->valuedouble;
        unsigned end = (unsigned)cJSON_GetObjectItem(cond, "endSerialNo")->valuedouble;
        unsigned position = (unsigned)cJSON_GetObjectItem(cond, "searchResultPosition")->valuedouble;
        unsigned size = (unsigned)cJSON_GetObjectItem(cond, "maxResults")->valuedouble;
        unsigned matched[32], total = 0;
        for (unsigned i = 0; i < sizeof(order) / sizeof(order[0]); i++)
            if ((mode == 41 ? order[i] > begin : order[i] >= begin) && order[i] <= end) matched[total++] = order[i];
        if (mode == 42 && position) total--;           /* the terminal changed mid-read */
        /* Equal timestamps reordered between requests: page 2 repeats a row
         * from page 1 and the row it displaced is never returned. */
        if (mode == 45 && position) matched[position] = matched[3];
        unsigned count = position < total ? (total - position < size ? total - position : size) : 0;
        cJSON *page = cJSON_AddObjectToObject(out, "AcsEvent");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        cJSON_AddNumberToObject(page, "numOfMatches", count);
        cJSON_AddNumberToObject(page, "totalMatches", total);
        cJSON_AddStringToObject(page, "responseStatusStrg", !total ? "NO MATCH" :
            position + count == total && mode != 43 ? "OK" : "MORE");
        cJSON *rows = cJSON_AddArrayToObject(page, "InfoList");
        for (unsigned i = 0; i < count; i++) {
            cJSON *row = cJSON_CreateObject();
            cJSON_AddNumberToObject(row, "serialNo", mode == 44 && i == 3 ? begin - 5 : matched[position + i]);
            cJSON_AddItemToArray(rows, row);
        }
    } else if (strstr(path, "/AcsEvent?") && mode >= 10) {
        cJSON *cond = cJSON_GetObjectItem(req, "AcsEventCond");
        if (mode == 30) {
            assert(!strcmp(cJSON_GetObjectItem(cond, "employeeNoString")->valuestring, "00111"));
            assert(cJSON_GetObjectItem(cond, "beginSerialNo")->valuedouble == 1);
            assert(cJSON_GetObjectItem(cond, "endSerialNo")->valuedouble == 30009);
        }
        unsigned position = (unsigned)cJSON_GetObjectItem(cond, "searchResultPosition")->valuedouble;
        cJSON *page = cJSON_AddObjectToObject(out, "AcsEvent");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        unsigned total = mode == 11 ? 0 : (mode == 12 && position ? 4 : 5);
        cJSON_AddNumberToObject(page, "numOfMatches", total ? 1 : 0);
        cJSON_AddNumberToObject(page, "totalMatches", total);
        cJSON_AddStringToObject(page, "responseStatusStrg", !total ? "NO MATCH" : (position + 1 == total ? "OK" : "MORE"));
        cJSON *rows = cJSON_AddArrayToObject(page, "InfoList");
        if (total) {
            cJSON *row = cJSON_CreateObject();
            cJSON_AddNumberToObject(row, "serialNo", 30001 + 2 * position);
            cJSON_AddItemToArray(rows, row);
        }
    } else if (strstr(path, "/AcsEvent?")) {
        cJSON *cond = cJSON_GetObjectItem(req, "AcsEventCond");
        assert(cJSON_GetObjectItem(cond, "searchResultPosition")->valuedouble == 0);
        unsigned begin = (unsigned)cJSON_GetObjectItem(cond, "beginSerialNo")->valuedouble;
        unsigned offset = (begin - 30001 + 1) / 2;
        if(mode==3 && begin>=30001 && (begin-30001)%2==0)offset++; /* Exclusive lower bound. */
        unsigned count=150000-offset<20?150000-offset:20;
        cJSON *page = cJSON_AddObjectToObject(out, "AcsEvent");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        cJSON_AddNumberToObject(page, "numOfMatches", count);
        cJSON_AddNumberToObject(page, "totalMatches", (mode == 2 && offset ? 150001 : 150000) - offset);
        cJSON_AddStringToObject(page, "responseStatusStrg", offset + count == 150000 ? "OK" : "MORE");
        cJSON *list = cJSON_AddArrayToObject(page, "InfoList");
        for (unsigned i = 0; i < count; i++) {
            cJSON *r = cJSON_CreateObject();
            cJSON_AddNumberToObject(r, "serialNo", 30001 + (mode == 1 ? i : offset + i) * 2);
            cJSON_AddItemToArray(list, r);
        }
    } else if (strstr(path, "/UserInfo/Search") && mode >= 20) {
        cJSON *cond = cJSON_GetObjectItem(req, "UserInfoSearchCond");
        unsigned position = (unsigned)cJSON_GetObjectItem(cond, "searchResultPosition")->valuedouble;
        unsigned total = mode == 23 ? 0 : 33;
        unsigned count = total ? (position ? 13 : 20) : 0;
        cJSON *page = cJSON_AddObjectToObject(out, "UserInfoSearch");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        cJSON_AddNumberToObject(page, "numOfMatches", count);
        cJSON_AddNumberToObject(page, "totalMatches", mode == 21 && position ? 34 : total);
        cJSON_AddStringToObject(page, "responseStatusStrg", !total ? "NO MATCH" : position ? "OK" : "MORE");
        cJSON *list = cJSON_AddArrayToObject(page, "UserInfo");
        for (unsigned i = 0; i < count; i++) {
            cJSON *user = cJSON_CreateObject(); char employee[16];
            snprintf(employee, sizeof(employee), "%05u", mode == 22 ? 1 : position + i + 1);
            cJSON_AddStringToObject(user, "employeeNo", employee);
            cJSON_AddStringToObject(user, "name", "Test profile");
            cJSON_AddStringToObject(user, "userType", "normal");
            cJSON_AddItemToArray(list, user);
        }
    } else if (strstr(path, "/UserInfo/Search")) {
        cJSON *cond = cJSON_GetObjectItem(req, "UserInfoSearchCond");
        cJSON *page = cJSON_AddObjectToObject(out, "UserInfoSearch");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        cJSON_AddNumberToObject(page, "numOfMatches", person ? 1 : 0);
        cJSON_AddNumberToObject(page, "totalMatches", person ? 1 : 0);
        cJSON *list = cJSON_AddArrayToObject(page, "UserInfo");
        if (person) cJSON_AddItemToArray(list, cJSON_Duplicate(person, true));
    } else if (strstr(path, "/Record")) {
        assert(method == HTTP_METHOD_POST && !person);
        person = cJSON_Duplicate(cJSON_GetObjectItem(req, "UserInfo"), true);
        cJSON_AddNumberToObject(out, "statusCode", 1);
    } else if (strstr(path, "/Modify")) {
        assert(method == HTTP_METHOD_PUT && person);
        cJSON *u = cJSON_GetObjectItem(req, "UserInfo");
        assert(cJSON_GetArraySize(u) == 2); /* employee + name only */
        cJSON_ReplaceItemInObjectCaseSensitive(person, "name", cJSON_Duplicate(cJSON_GetObjectItem(u, "name"), true));
        cJSON_AddNumberToObject(out, "statusCode", 1);
    } else if (strstr(path, "/Delete")) {
        assert(method == HTTP_METHOD_PUT);
        cJSON *d = cJSON_GetObjectItem(req, "UserInfoDetail");
        assert(!strcmp(cJSON_GetObjectItem(d, "mode")->valuestring, "byEmployeeNo"));
        assert(cJSON_GetArraySize(cJSON_GetObjectItem(d, "EmployeeNoList")) == 1);
        if (!deletion_pending) { cJSON_Delete(person); person = NULL; }
        cJSON_AddNumberToObject(out, "statusCode", 1);
    } else assert(false);
    char *text = cJSON_PrintUnformatted(out); assert(text && strlen(text) < capacity);
    strcpy(response, text); *length = strlen(text);
    free(text); cJSON_Delete(out); cJSON_Delete(req);
    if (timeout_after_write && !strstr(path, "/Search") && !strstr(path, "/AcsEvent?")) return HIK_NETWORK;
    return HIK_OK;
}
static bool save_user(void *context, const char *raw, size_t length)
{
    if (context) return false;
    cJSON *user = cJSON_ParseWithLength(raw, length); assert(user);
    const char *employee = cJSON_GetObjectItem(user, "employeeNo")->valuestring;
    assert(strlen(employee) == 5 && employee[0] == '0');
    received++; cJSON_Delete(user); return true;
}
static void test_user_pages(void)
{
    hik_search_t scan; mode = 20; received = 0; hik_search_init(&scan, 1, 1);
    assert(hik_user_page(&scan, save_user, (void *)1) == HIK_CUSTODY && !scan.position);
    assert(hik_user_page(&scan, save_user, NULL) == HIK_OK && scan.position == 20 && !scan.complete);
    mode = 21;
    assert(hik_user_page(&scan, save_user, NULL) == HIK_PARSE && scan.position == 20);
    mode = 20;
    assert(hik_user_page(&scan, save_user, NULL) == HIK_OK && scan.complete && received == 33);
    mode = 22; hik_search_init(&scan, 1, 1);
    assert(hik_user_page(&scan, save_user, NULL) == HIK_PARSE && !scan.position);
    mode = 23; hik_search_init(&scan, 1, 1);
    assert(hik_user_page(&scan, save_user, NULL) == HIK_OK && scan.complete && !scan.total);
    mode = 0; received = 0;
}
static unsigned range_rows, range_max, range_seen[64];
static bool range_saved(void *ctx, const char *raw, size_t length, uint32_t serial)
{
    if (ctx) return false;
    cJSON *r = cJSON_ParseWithLength(raw, length);
    assert(r && cJSON_GetObjectItem(r, "serialNo")->valuedouble == serial);
    cJSON_Delete(r);
    assert(range_rows < 64); range_seen[range_rows++] = serial;
    if (serial > range_max) range_max = serial;
    return true;
}
static void test_unordered_ranges(void)
{
    /* Inclusive and exclusive lower bounds both deliver every later serial. */
    for (mode = 40; mode <= 41; mode++) {
        range_rows = range_max = 0;
        assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_OK);
        assert(range_max == 3866 && range_rows == (mode == 40 ? 24 : 23));
        bool found_3853 = false, found_3200 = false;
        for (unsigned i = 0; i < range_rows; i++) {
            assert(range_seen[i] >= 3199);
            found_3853 |= range_seen[i] == 3853; found_3200 |= range_seen[i] == 3200;
        }
        assert(found_3853 && found_3200);
    }
    /* The anchor window finds 3199 wherever the terminal sorts it. */
    mode = 40; range_rows = 0;
    assert(hik_history_range(3198, 3200, 1, range_saved, NULL) == HIK_OK && range_rows == 2);
    /* A total that changes between pages, a wrong completion status, or a
     * row outside the requested range is never accepted as complete. */
    mode = 42; range_rows = 0;
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_PARSE);
    assert(strstr(hik_history_last_reject(), "range=total_changed"));
    /* 3.1.6: the failing sub-request is named (page 2) with its elapsed ms. */
    assert(hik_history_last_page() == 2 && hik_history_last_request_ms() == 1);
    mode = 43;
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_PARSE);
    assert(strstr(hik_history_last_reject(), "range=completion"));
    mode = 44;
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_PARSE);
    assert(strstr(hik_history_last_reject(), "range=serial_scope"));
    mode = 45;
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_PARSE);
    assert(strstr(hik_history_last_reject(), "range=duplicate_serial"));
    /* Counts come from totalMatches of a one-row search. */
    mode = 40; uint32_t waiting = 99;
    assert(hik_history_count(3199, 3000000000U, &waiting) == HIK_OK && waiting == 24);
    assert(!hik_history_last_page() && hik_history_last_request_ms() == 1);
    assert(hik_history_count(3199, 3300, &waiting) == HIK_OK && waiting == 9);
    assert(hik_history_count(4000, 3000000000U, &waiting) == HIK_OK && !waiting);
    /* A stale reason never survives a later successful read. */
    range_rows = 0;
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, NULL) == HIK_OK);
    assert(!strlen(hik_history_last_reject()));
    /* Too many pages, custody failure, and an empty range. */
    mode = 40;
    assert(hik_history_range(1, 3000000000U, 1, range_saved, NULL) == HIK_OVERSIZED);
    assert(hik_history_range(3199, 3000000000U, 50, range_saved, (void *)1) == HIK_CUSTODY);
    range_rows = 0;
    assert(hik_history_range(4000, 3000000000U, 50, range_saved, NULL) == HIK_OK && !range_rows);
    assert(hik_history_range(0, 10, 50, range_saved, NULL) == HIK_CONFIGURATION);
    mode = 0;
}
/* 3.1.7: every AcsEvent search gets the 25 s search timeout; users,
 * profiles and writes keep 10 s. Counted per path so main() can prove that
 * each history API (page, bounds, count, range, ADD response) used it. */
static unsigned search_requests, other_requests;
hik_result_t hik_http_request_timeout(esp_http_client_method_t method, const char *path,
    const char *body, int timeout_ms, char *response, size_t capacity, size_t *length)
{
    bool search = !strncmp(path, "/ISAPI/AccessControl/AcsEvent", 29);
    assert(timeout_ms == (search ? HIK_HTTP_SEARCH_TIMEOUT_MS : HIK_HTTP_TIMEOUT_MS));
    if (search) search_requests++; else other_requests++;
    return hik_http_request(method, path, body, response, capacity, length);
}
int main(void)
{
    test_unordered_ranges();
    test_user_pages();
    hik_search_t s;
    for(unsigned semantics=0;semantics<2;semantics++){
        mode=semantics?3:0;received=0;hik_search_init(&s,30001,330000);
        while(!s.complete)assert(hik_history_page(&s,saved,NULL)==HIK_OK);
        assert(received==150000 && s.position==150000);
    }
    for (mode = 1; mode <= 2; mode++) {
        received = 0; hik_search_init(&s, 30001, 330000);
        assert(hik_history_page(&s, saved, NULL) == HIK_OK);
        assert(hik_history_page(&s, saved, NULL) == HIK_PARSE);
        assert(s.position == 20 && received == 20);
    }
    mode = 0; hik_search_init(&s, 30001, 330000);
    assert(hik_history_page(&s, saved, &s) == HIK_CUSTODY && s.position == 0);
    cJSON *desired = cJSON_Parse("{\"employeeNo\":\"00111\",\"name\":\"Test\",\"userType\":\"normal\",\"doorRight\":\"1\"}");
    bool verified;
    timeout_after_write = true;
    assert(hik_user_create(desired, &verified) == HIK_OK && verified);
    assert(hik_user_create(desired, &verified) == HIK_BINDING && !verified);
    assert(hik_user_rename(desired, "Renamed", &verified) == HIK_OK && verified);
    assert(hik_user_rename(desired, "Stale", &verified) == HIK_BINDING && !verified);
    cJSON *expected = cJSON_Duplicate(person, true);
    deletion_pending = true;
    assert(hik_user_delete(expected, &verified) != HIK_OK && !verified);
    deletion_pending = false;
    assert(hik_user_delete(expected, &verified) == HIK_OK && verified);
    assert(hik_user_delete(expected, &verified) == HIK_OK && verified);
    cJSON_Delete(expected); cJSON_Delete(desired);
    uint32_t first, last, count;
    mode = 10;
    assert(hik_history_bounds(&first, &last, &count) == HIK_OK);
    assert(first == 30001 && last == 30009 && count == 5);
    mode = 11;
    assert(hik_history_bounds(&first, &last, &count) == HIK_OK && !first && !last && !count);
    mode = 12;
    assert(hik_history_bounds(&first, &last, &count) != HIK_OK);
    cJSON *bad = cJSON_Parse("{\"AcsEventCond\":{\"searchID\":\"test\",\"searchResultPosition\":0,\"maxResults\":20,\"major\":5,\"minor\":0,\"picEnable\":false}}");
    cJSON *response = NULL;
    assert(hik_history_response(bad, &response) == HIK_CONFIGURATION && !response);
    cJSON_Delete(bad);
    mode = 30;
    cJSON *filtered = cJSON_Parse("{\"AcsEventCond\":{\"searchID\":\"filtered\",\"searchResultPosition\":0,\"maxResults\":20,\"major\":0,\"minor\":0,\"picEnable\":false,\"beginSerialNo\":1,\"endSerialNo\":30009,\"employeeNoString\":\"00111\"}}");
    assert(hik_history_response(filtered, &response) == HIK_OK && response);
    cJSON_Delete(response); cJSON_Delete(filtered);
    assert(HIK_HTTP_TIMEOUT_MS == 10000 && HIK_HTTP_SEARCH_TIMEOUT_MS == 25000);
    unsigned before = search_requests, users = other_requests;
    mode = 10;
    assert(hik_history_bounds(&first, &last, &count) == HIK_OK && search_requests == before + 3);
    mode = 0; received = 0; before = search_requests; hik_search_init(&s, 30001, 330000);
    assert(hik_history_page(&s, saved, NULL) == HIK_OK && search_requests == before + 1);
    before = search_requests; uint32_t total = 0;
    (void)hik_history_count(1, 3000000000U, &total); assert(search_requests == before + 1);
    assert(other_requests == users && users > 0);
    puts("150000 bounded source records, custody failures and verified CRUD passed");
}
