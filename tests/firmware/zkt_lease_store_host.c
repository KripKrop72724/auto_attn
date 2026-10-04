#include "zkt_storage_owner_platform.h"
#include "zkt_lease_store.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

enum { NONE, OPEN, READ, SET, COMMIT_BEFORE, COMMIT_AFTER, READBACK, CHANGED_READBACK, SHORT_READ,
    ROOT_SET, ROOT_COMMIT_BEFORE, ROOT_COMMIT_AFTER, ROOT_READ };
static unsigned fault, lease_reads, writes, submissions;
static int64_t clock_us = 1000000;
static bool present, runtime_present, stall, finish_before_wait, refuse, complete_reply, occupied;
static zl_lease_record_t durable, staged;
static runtime_checkpoint_t runtime_blob;
static zj_request_t accepted;
static zj_reply_t reply;
static uint64_t ticket_sequence, active_ticket;
static uint64_t root_value, staged_root;
static bool stage_is_root;

int64_t esp_timer_get_time(void) { return clock_us; }
void vTaskDelay(unsigned milliseconds) { clock_us += (int64_t)milliseconds * 1000; }
bool zj_runtime_checkpoint_required(void) { return true; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle)
{
    assert(!strcmp(name, "zone_lite") && (mode == NVS_READONLY || mode == NVS_READWRITE));
    *handle = 1; lease_reads = 0;
    return fault == OPEN ? 901 : ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(handle == 1); }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *name, void *out, size_t *length)
{
    assert(handle == 1);
    if (!strcmp(name, "lease_v2_root")) {
        assert(*length == sizeof(root_value));
        if (fault == ROOT_READ && present) return 908;
        if (!root_value) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, &root_value, sizeof(root_value));
        return ESP_OK;
    }
    if (!strcmp(name, "runtime_v1")) {
        assert(*length == sizeof(runtime_blob));
        if (!runtime_present) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, &runtime_blob, sizeof(runtime_blob));
        return ESP_OK;
    }
    assert(!strcmp(name, "lease_v2") && *length == sizeof(durable));
    ++lease_reads;
    if ((lease_reads == 1 && fault == READ) || (lease_reads == 2 && fault == READBACK)) return 902;
    if (!present) return ESP_ERR_NVS_NOT_FOUND;
    memcpy(out, &durable, sizeof(durable));
    if (fault == SHORT_READ) *length -= 1;
    if (fault == CHANGED_READBACK && lease_reads == 2) {
        zl_lease_record_t *changed = out;
        ++changed->uid; zl_lease_checksum(changed);
    }
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *name, const void *bytes, size_t length)
{
    assert(handle == 1);
    stage_is_root = !strcmp(name, "lease_v2_root");
    if (stage_is_root) {
        assert(length == sizeof(staged_root));
        staged_root = *(const uint64_t *)bytes;
        return fault == ROOT_SET ? 906 : ESP_OK;
    }
    assert(!strcmp(name, "lease_v2") && length == sizeof(staged));
    ++writes; staged = *(const zl_lease_record_t *)bytes;
    return fault == SET ? 903 : ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle)
{
    assert(handle == 1);
    if (fault == COMMIT_BEFORE || (stage_is_root && fault == ROOT_COMMIT_BEFORE)) return 904;
    if (stage_is_root) root_value = staged_root;
    else { durable = staged; present = true; }
    return fault == COMMIT_AFTER || (stage_is_root && fault == ROOT_COMMIT_AFTER) ? 905 : ESP_OK;
}
static void finish(void)
{
    int error;
    assert(occupied && !complete_reply);
    reply = (zj_reply_t){0};
    reply.result = zl_lease_commit(&accepted.input.lease.state,
        accepted.input.lease.deadline_us, &reply.lease, &error);
    complete_reply = true;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    assert(request->operation == ZJ_LEASE && !occupied);
    *ticket = 0;
    if (refuse) return false;
    accepted = *request; occupied = true; complete_reply = false;
    *ticket = active_ticket = ++ticket_sequence; ++submissions;
    if (finish_before_wait) finish();
    return true;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *out, bool *done)
{
    assert(occupied && ticket == active_ticket);
    *done = false;
    if (stall) return true;
    if (!complete_reply) finish();
    *out = reply; *done = true; occupied = false;
    return true;
}
static void reset_ports(void)
{
    assert(!occupied);
    fault = writes = submissions = 0;
    clock_us = 1000000;
    present = stall = finish_before_wait = refuse = false;
    durable = staged = (zl_lease_record_t){0};
    root_value = staged_root = 0; stage_is_root = false;
    runtime_present = true;
    runtime_blob = (runtime_checkpoint_t){.version = 1, .generation = 1, .history_schema = 2};
    memset(runtime_blob.source_chain, '0', 64);
    runtime_blob.crc = dq_crc32(&runtime_blob, offsetof(runtime_checkpoint_t, crc));
}

#ifndef ZL_GATEWAY_TEST
static zl_lease_record_t record(void)
{
    zl_lease_record_t result = {.version = 2, .generation = 1,
        .active = 1, .uid = 7, .expires_epoch = 1800000600, .terminal_serial = "TEST-LEASE"};
    memset(result.identity_fingerprint, 'a', 64);
    zl_lease_checksum(&result);
    return result;
}
static zj_result_t commit(zl_lease_record_t *in, zl_lease_record_t *out, int *error)
{ return zl_lease_commit(in, (uint64_t)clock_us + 5000000, out, error); }

int main(void)
{
    reset_ports();
    zl_lease_record_t proposed = record(), confirmed;
    int error;
    assert(zl_lease_valid(&proposed));
    assert(zl_lease_load_boot(&confirmed, &error) == ZJ_EMPTY && !zl_lease_valid(&confirmed));
    runtime_present = false;
    assert(commit(&proposed, &confirmed, &error) == ZJ_IO && !writes);
    runtime_present = true; runtime_blob.lease_active = 1;
    runtime_blob.crc = dq_crc32(&runtime_blob, offsetof(runtime_checkpoint_t, crc));
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && !writes);
    reset_ports();
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && writes == 1);
    assert(zl_lease_load_boot(&confirmed, &error) == ZJ_OK && confirmed.active);
    assert(zl_lease_matches(&confirmed, "TEST-LEASE", 7, proposed.identity_fingerprint));
    assert(!zl_lease_matches(&confirmed, "OTHER-TERMINAL", 7, proposed.identity_fingerprint));
    unsigned prior = writes;
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && writes == prior); /* lost response */
    ++proposed.generation; proposed.uid = 8; zl_lease_checksum(&proposed);
    assert(commit(&proposed, &confirmed, &error) == ZJ_INVALID && writes == prior);
    proposed.uid = 7; memset(proposed.identity_fingerprint, 'b', 64); zl_lease_checksum(&proposed);
    assert(commit(&proposed, &confirmed, &error) == ZJ_INVALID && writes == prior);
    proposed = durable; ++proposed.generation; proposed.active = 0; proposed.expires_epoch = 0; zl_lease_checksum(&proposed);
    runtime_blob.crc ^= 1; /* independent source checkpoint damage cannot erase the lease */
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK && !confirmed.active);
    ++proposed.generation; proposed.uid = 8; proposed.active = 1; proposed.expires_epoch = 1800000600;
    memset(proposed.identity_fingerprint, 'b', 64); zl_lease_checksum(&proposed);
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK);
    zl_lease_record_t good = durable;
    ++proposed.generation; proposed.expires_epoch += 1; zl_lease_checksum(&proposed);
    prior = writes; durable.crc ^= 1;
    assert(zl_lease_load_boot(&confirmed, &error) == ZJ_CORRUPT && !zl_lease_valid(&confirmed));
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    assert(durable.crc == (good.crc ^ 1)); durable = good;
    for (fault = OPEN; fault <= CHANGED_READBACK; ++fault) {
        durable = good;
        zj_result_t result = commit(&proposed, &confirmed, &error);
        assert(result == (fault <= READ ? ZJ_IO : ZJ_UNCERTAIN));
        assert(!zl_lease_valid(&confirmed));
    }
    fault = NONE;
    assert(commit(&proposed, &confirmed, &error) == ZJ_OK); /* retry a possibly committed write */
    prior = writes; present = false;
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    assert(zl_lease_load_boot(&confirmed, &error) == ZJ_CORRUPT); /* absence is not an empty root after reboot */
    proposed = record();
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    proposed = durable;
    present = true; fault = SHORT_READ;
    assert(commit(&proposed, &confirmed, &error) == ZJ_CORRUPT && writes == prior);
    fault = NONE;
    assert(zl_lease_commit(&proposed, (uint64_t)clock_us, &confirmed, &error) == ZJ_STALE && writes == prior);
    ++proposed.generation; zl_lease_checksum(&proposed);
    durable.generation = UINT32_MAX; zl_lease_checksum(&durable);
    assert(commit(&proposed, &confirmed, &error) == ZJ_FULL && writes == prior);
    reset_ports(); proposed = record(); stall = finish_before_wait = true;
    assert(!zl_lease_save(&proposed, &confirmed) && occupied && submissions == 1);
    assert(!zl_lease_save(&proposed, &confirmed) && occupied && submissions == 1);
    stall = false; refuse = true;
    assert(!zl_lease_save(&proposed, &confirmed) && !occupied && confirmed.active && submissions == 1);
    refuse = false; finish_before_wait = false;
    assert(zl_lease_save(&proposed, &confirmed) && writes == 1); /* exact replay does not mutate */
    proposed.generation = 2; proposed.active = 0; proposed.expires_epoch = 0; zl_lease_checksum(&proposed);
    assert(zl_lease_save(&proposed, &confirmed) && !confirmed.active);
    for (unsigned failure = ROOT_SET; failure <= ROOT_READ; ++failure) {
        reset_ports(); proposed = record(); fault = failure;
        assert(commit(&proposed, &confirmed, &error) == ZJ_UNCERTAIN && !zl_lease_valid(&confirmed));
        assert(present && durable.active && writes == 1);
        fault = NONE;
        assert(commit(&proposed, &confirmed, &error) == ZJ_OK && root_value && writes == 1);
        present = false;
        assert(zl_lease_load_boot(&confirmed, &error) == ZJ_CORRUPT);
    }
    return 0;
}
#endif
