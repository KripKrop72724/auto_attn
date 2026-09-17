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
    if (strstr(path, "/AcsEvent?") && mode >= 10) {
        cJSON *cond = cJSON_GetObjectItem(req, "AcsEventCond");
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
        cJSON *page = cJSON_AddObjectToObject(out, "AcsEvent");
        cJSON_AddStringToObject(page, "searchID", cJSON_GetObjectItem(cond, "searchID")->valuestring);
        cJSON_AddNumberToObject(page, "numOfMatches", 20);
        cJSON_AddNumberToObject(page, "totalMatches", (mode == 2 && offset ? 150001 : 150000) - offset);
        cJSON_AddStringToObject(page, "responseStatusStrg", offset + 20 == 150000 ? "OK" : "MORE");
        cJSON *list = cJSON_AddArrayToObject(page, "InfoList");
        for (unsigned i = 0; i < 20; i++) {
            cJSON *r = cJSON_CreateObject();
            cJSON_AddNumberToObject(r, "serialNo", 30001 + (mode == 1 ? i : offset + i) * 2);
            cJSON_AddItemToArray(list, r);
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
int main(void)
{
    hik_search_t s;
    hik_search_init(&s, 30001, 330000);
    while (!s.complete) assert(hik_history_page(&s, saved, NULL) == HIK_OK);
    assert(received == 150000 && s.position == 150000);
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
    puts("150000 bounded source records, custody failures and verified CRUD passed");
}
