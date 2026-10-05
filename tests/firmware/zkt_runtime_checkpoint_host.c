#include "zkt_storage_owner_platform.h"
#include "zkt_runtime_checkpoint.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <string.h>

enum { NONE, OPEN, READ, SET, COMMIT_BEFORE, COMMIT_AFTER, READBACK, DIFFERENT_READBACK, SHORT_READ };
static unsigned fault, reads, writes, opens, closes, submissions;
static int64_t clock_us = 1000000;
static bool exists, expire_read, stall, finish_before_wait, refuse, lost_poll, nested_call;
static runtime_checkpoint_t durable, staged;
static esp_app_desc_t app = {.project_name = "zone_lite", .version = "2.7.0"};
static zj_request_t accepted;
static zj_reply_t reply;
static bool occupied, finished;
static uint64_t next_ticket = 1, active_ticket;

const esp_app_desc_t *esp_app_get_description(void) { return &app; }
int64_t esp_timer_get_time(void) { return clock_us; }
void vTaskDelay(unsigned milliseconds) { clock_us += (int64_t)milliseconds * 1000; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle)
{
    assert(!strcmp(name, "zone_lite") && mode == NVS_READWRITE);
    ++opens; reads = 0; *handle = 1;
    return fault == OPEN ? 901 : ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(handle == 1); ++closes; }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *name, void *out, size_t *length)
{
    assert(handle == 1 && !strcmp(name, "runtime_v1") && *length == sizeof(durable));
    ++reads;
    if (expire_read) clock_us += 6000000;
    if ((reads == 1 && fault == READ) || (reads == 2 && fault == READBACK)) return 902;
    if (!exists) return ESP_ERR_NVS_NOT_FOUND;
    memcpy(out, &durable, sizeof(durable));
    if (fault == SHORT_READ) *length -= 1;
    if (fault == DIFFERENT_READBACK && reads == 2) {
        runtime_checkpoint_t *changed = out;
        ++changed->source_cursor;
        changed->crc = dq_crc32(changed, offsetof(runtime_checkpoint_t, crc));
    }
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *name, const void *bytes, size_t length)
{
    assert(handle == 1 && !strcmp(name, "runtime_v1") && length == sizeof(staged));
    ++writes; staged = *(const runtime_checkpoint_t *)bytes;
    return fault == SET ? 903 : ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle)
{
    assert(handle == 1);
    if (fault == COMMIT_BEFORE) return 904;
    durable = staged; exists = true;
    return fault == COMMIT_AFTER ? 905 : ESP_OK;
}
static void complete(void)
{
    int error;
    assert(occupied && !finished);
    reply = (zj_reply_t){0};
    reply.result = zj_runtime_checkpoint_commit(&accepted.input.runtime_checkpoint.state,
        accepted.input.runtime_checkpoint.deadline_us, &reply.runtime_checkpoint, &error);
    finished = true;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    *ticket = 0;
    assert(request->operation == ZJ_RUNTIME_CHECKPOINT && !occupied);
    if (refuse) return false;
    accepted = *request; occupied = true; finished = false;
    *ticket = active_ticket = next_ticket++; ++submissions;
    if (finish_before_wait) complete();
    return true;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *out, bool *done)
{
    assert(occupied && ticket == active_ticket);
    *done = false;
    if (nested_call) {
        nested_call = false;
        runtime_checkpoint_t ignored;
        assert(!zj_runtime_checkpoint_save(&accepted.input.runtime_checkpoint.state, &ignored));
        assert(!runtime_checkpoint_valid(&ignored));
    }
    if (lost_poll) return false;
    if (stall) return true;
    if (!finished) complete();
    *out = reply; *done = true; occupied = false;
    return true;
}
static runtime_checkpoint_t state(void)
{
    runtime_checkpoint_t value = {.version = 1, .generation = 1, .history_schema = 2,
        .lease_active = 1, .lease_uid = 42, .lease_expiry = 1900000100, .source_cursor = 12};
    memset(value.source_chain, '0', 64);
    value.crc = dq_crc32(&value, offsetof(runtime_checkpoint_t, crc));
    return value;
}
static void checksum(runtime_checkpoint_t *value)
{ value->crc = dq_crc32(value, offsetof(runtime_checkpoint_t, crc)); }
static zj_result_t commit(runtime_checkpoint_t *value, runtime_checkpoint_t *out, int *error)
{ return zj_runtime_checkpoint_commit(value, (uint64_t)clock_us + 5000000, out, error); }

int main(void)
{
    assert(zj_runtime_checkpoint_required());
    strcpy(app.version, "2.6.17"); assert(zj_runtime_checkpoint_required());
    strcpy(app.version, "2.6.15"); assert(!zj_runtime_checkpoint_required());
    strcpy(app.version, "2.7.0"); strcpy(app.project_name, "zone_hikvision");
    assert(!zj_runtime_checkpoint_required()); strcpy(app.project_name, "zone_lite");
    runtime_checkpoint_t proposed = state(), confirmed;
    int error;
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && !error);
    assert(confirmed.generation == 1 && !memcmp(&durable, &confirmed, sizeof(durable)));
    proposed.source_cursor = 13; checksum(&proposed);
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && confirmed.generation == 2);
    unsigned prior = writes;
    for (fault = OPEN; fault <= DIFFERENT_READBACK; ++fault) {
        zj_result_t result = commit(&proposed, &confirmed, &error);
        assert(result == (fault <= READ ? ZJ_IO : ZJ_UNCERTAIN));
        assert(!runtime_checkpoint_valid(&confirmed));
        assert((error != 0) == (fault != DIFFERENT_READBACK));
    }
    fault = NONE;
    uint32_t actual = durable.generation;
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && confirmed.generation == actual + 1);
    assert(writes > prior); prior = writes;
    runtime_checkpoint_t good = durable;
    durable.crc ^= 1;
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    assert(durable.crc == (good.crc ^ 1)); durable = good;
    durable.history_schema = 99; checksum(&durable);
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    durable = good; fault = SHORT_READ;
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    fault = NONE; durable.generation = UINT32_MAX; checksum(&durable);
    assert(commit(&proposed, &confirmed, &error) == ZJ_FULL && writes == prior);
    durable = good; proposed.generation = durable.generation + 2; checksum(&proposed);
    assert(commit(&proposed, &confirmed, &error) == ZJ_STALE && writes == prior);
    exists = false;
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    exists = true; proposed = state(); expire_read = true;
    assert(commit(&proposed, &confirmed, &error) == ZJ_STALE && writes == prior);
    expire_read = false;
    assert(zj_runtime_checkpoint_commit(&proposed, (uint64_t)clock_us, &confirmed, &error) == ZJ_STALE);
    assert(writes == prior && closes + 1 == opens); /* Failed open has no handle. */

    /* Only one retained request, even across repeated caller timeouts. A late
     * prior commit can update the committed cache but cannot certify new facts. */
    exists = false; proposed = state(); stall = finish_before_wait = true;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed));
    assert(!runtime_checkpoint_valid(&confirmed) && occupied && submissions == 1);
    proposed.source_cursor = 40; checksum(&proposed);
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed) && submissions == 1);
    stall = false; refuse = true;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed));
    assert(confirmed.source_cursor == 12 && confirmed.generation == 1 && !occupied && submissions == 1);
    refuse = false; nested_call = true;
    assert(zj_runtime_checkpoint_save(&proposed, &confirmed));
    assert(confirmed.source_cursor == 40 && confirmed.generation == 2 && submissions == 2);
    lost_poll = true;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed) && occupied && submissions == 3);
    lost_poll = false; refuse = true;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed) && !occupied);
    assert(confirmed.generation == 3 && confirmed.source_cursor == 40);
    refuse = false; finish_before_wait = false; stall = true;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed) && occupied);
    prior = writes; stall = false;
    assert(!zj_runtime_checkpoint_save(&proposed, &confirmed));
    assert(!occupied && !runtime_checkpoint_valid(&confirmed) && writes == prior);
    assert(zj_runtime_checkpoint_save(&proposed, &confirmed));
    assert(confirmed.generation == 4);
    return 0;
}
