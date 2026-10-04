#include "zkt_lease_store.h"
#include "zkt_runtime_checkpoint.h"
#include "zkt_storage_owner.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include <stdatomic.h>
#include <string.h>

#define ZL_LEASE_WAIT_US 5000000ULL
static atomic_flag caller_busy = ATOMIC_FLAG_INIT;
static uint64_t pending_ticket;
static zl_lease_record_t pending_facts;
static bool unresolved_commit;
static const uint64_t root_witness = 0x32455341454c4b5aULL;

static zj_result_t read_root(nvs_handle_t handle, int *error)
{
    uint64_t value = 0;
    size_t length = sizeof(value);
    esp_err_t status = nvs_get_blob(handle, "lease_v2_root", &value, &length);
    if (status == ESP_ERR_NVS_NOT_FOUND) return ZJ_EMPTY;
    if (status != ESP_OK) { *error = status; return ZJ_IO; }
    return length == sizeof(value) && value == root_witness ? ZJ_OK : ZJ_CORRUPT;
}

void zl_lease_checksum(zl_lease_record_t *record)
{
    if (record) record->crc = dq_crc32(record, offsetof(zl_lease_record_t, crc));
}


bool zl_lease_matches(const zl_lease_record_t *record, const char *serial,
                     uint16_t uid, const char *fingerprint)
{
    return zl_lease_valid(record) && serial && fingerprint && record->uid == uid &&
        !strcmp(record->terminal_serial, serial) && !strcmp(record->identity_fingerprint, fingerprint);
}

static bool same_facts(const zl_lease_record_t *left, const zl_lease_record_t *right)
{
    return left->active == right->active && left->uid == right->uid &&
        left->expires_epoch == right->expires_epoch &&
        !strcmp(left->terminal_serial, right->terminal_serial) &&
        !strcmp(left->identity_fingerprint, right->identity_fingerprint);
}

static zj_result_t read_record(nvs_handle_t handle, zl_lease_record_t *record, int *error)
{
    size_t length = sizeof(*record);
    esp_err_t status = nvs_get_blob(handle, "lease_v2", record, &length);
    if (status == ESP_ERR_NVS_NOT_FOUND) return ZJ_EMPTY;
    if (status != ESP_OK) { *error = status; return ZJ_IO; }
    return length == sizeof(*record) && zl_lease_valid(record) ? ZJ_OK : ZJ_CORRUPT;
}

zj_result_t zl_lease_load_boot(zl_lease_record_t *record, int *nvs_error)
{
    if (!record || !nvs_error) return ZJ_INVALID;
    memset(record, 0, sizeof(*record));
    *nvs_error = 0;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zone_lite", NVS_READONLY, &handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return ZJ_EMPTY;
    if (status != ESP_OK) { *nvs_error = status; return ZJ_IO; }
    zj_result_t result = read_record(handle, record, nvs_error);
    if (result == ZJ_EMPTY) {
        zj_result_t root = read_root(handle, nvs_error);
        result = root == ZJ_EMPTY ? ZJ_EMPTY : root == ZJ_IO ? ZJ_IO : ZJ_CORRUPT;
    }
    nvs_close(handle);
    if (result != ZJ_OK) memset(record, 0, sizeof(*record));
    return result;
}

zj_result_t zl_lease_commit(const zl_lease_record_t *proposed, uint64_t deadline_us,
                          zl_lease_record_t *confirmed, int *nvs_error)
{
    if (!confirmed || !nvs_error) return ZJ_INVALID;
    memset(confirmed, 0, sizeof(*confirmed));
    *nvs_error = 0;
    if (!zl_lease_valid(proposed) || !deadline_us) return ZJ_INVALID;
    if ((uint64_t)esp_timer_get_time() >= deadline_us) return ZJ_STALE;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zone_lite", NVS_READWRITE, &handle);
    if (status != ESP_OK) { *nvs_error = status; return ZJ_IO; }
    zl_lease_record_t current = {0};
    zj_result_t result = read_record(handle, &current, nvs_error);
    if (result == ZJ_EMPTY) {
        if (proposed->generation != 1 || !proposed->active) { result = ZJ_CORRUPT; goto done; }
        zj_result_t root = read_root(handle, nvs_error);
        if (root != ZJ_EMPTY) { result = root == ZJ_IO ? ZJ_IO : ZJ_CORRUPT; goto done; }
        runtime_checkpoint_t legacy = {0};
        size_t length = sizeof(legacy);
        status = nvs_get_blob(handle, "runtime_v1", &legacy, &length);
        if (status != ESP_OK) { *nvs_error = status; result = ZJ_IO; goto done; }
        if (length != sizeof(legacy) || !runtime_checkpoint_valid(&legacy) || legacy.lease_active) {
            result = ZJ_CORRUPT;
            goto done;
        }
    } else if (result != ZJ_OK) goto done;
    else {
        if (proposed->generation == current.generation && same_facts(proposed, &current)) {
            goto witness; /* Commit succeeded, acknowledgement was lost. */
        }
        if (current.generation == UINT32_MAX) { result = ZJ_FULL; goto done; }
        if (proposed->generation != current.generation + 1) { result = ZJ_STALE; goto done; }
        if (current.active && !zl_lease_matches(&current, proposed->terminal_serial,
                                               proposed->uid, proposed->identity_fingerprint)) {
            result = ZJ_INVALID;
            goto done;
        }
        /* A retained inactive record still belongs to the same terminal. */
        if (strcmp(current.terminal_serial, proposed->terminal_serial)) { result = ZJ_INVALID; goto done; }
    }
    if ((uint64_t)esp_timer_get_time() >= deadline_us) { result = ZJ_STALE; goto done; }
    status = nvs_set_blob(handle, "lease_v2", proposed, sizeof(*proposed));
    if (status == ESP_OK) status = nvs_commit(handle);
    if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
    result = read_record(handle, &current, nvs_error);
    if (result != ZJ_OK || memcmp(&current, proposed, sizeof(current))) {
        result = ZJ_UNCERTAIN;
        goto done;
    }
witness:
    /* Never acknowledge a first elevation without an independent presence
     * witness. A subsequently missing record cannot become a new empty root.
     * If this write is interrupted, exact replay finishes it before success. */
    result = read_root(handle, nvs_error);
    if (result == ZJ_EMPTY) {
        if ((uint64_t)esp_timer_get_time() >= deadline_us) { result = ZJ_UNCERTAIN; goto done; }
        status = nvs_set_blob(handle, "lease_v2_root", &root_witness, sizeof(root_witness));
        if (status == ESP_OK) status = nvs_commit(handle);
        if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
        result = read_root(handle, nvs_error);
    }
    if (result != ZJ_OK) { result = ZJ_UNCERTAIN; goto done; }
    *confirmed = current;
    result = ZJ_OK;
done:
    nvs_close(handle);
    return result;
}

static bool collect(uint64_t deadline, zl_lease_record_t *confirmed)
{
    do {
        zj_reply_t reply;
        bool complete = false;
        if (zj_owner_poll(pending_ticket, &reply, &complete) && complete) {
            pending_ticket = 0;
            bool valid = reply.result == ZJ_OK && zl_lease_valid(&reply.lease) &&
                reply.lease.generation == pending_facts.generation && same_facts(&pending_facts, &reply.lease);
            if (valid) *confirmed = reply.lease;
            unresolved_commit = !valid && (unresolved_commit || reply.result == ZJ_UNCERTAIN);
            if (!unresolved_commit) memset(&pending_facts, 0, sizeof(pending_facts));
            return valid;
        }
        if ((uint64_t)esp_timer_get_time() >= deadline) break;
        vTaskDelay(pdMS_TO_TICKS(20));
    } while ((uint64_t)esp_timer_get_time() < deadline);
    return false;
}

bool zl_lease_save(const zl_lease_record_t *proposed, zl_lease_record_t *confirmed)
{
    if (!confirmed) return false;
    memset(confirmed, 0, sizeof(*confirmed));
    if (!zl_lease_valid(proposed) || !zj_runtime_checkpoint_required() ||
        atomic_flag_test_and_set_explicit(&caller_busy, memory_order_acquire)) return false;
    bool saved = false;
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - ZL_LEASE_WAIT_US) goto done;
    uint64_t deadline = now + ZL_LEASE_WAIT_US;
    if (pending_ticket && !collect(deadline, confirmed)) goto done;
    if ((uint64_t)esp_timer_get_time() >= deadline) goto done;
    if (unresolved_commit) {
        /* A failed commit/readback can already be durable. Resolve those exact
         * facts before considering another generation or a clear request. */
        zj_request_t retry = {.operation = ZJ_LEASE,
            .input.lease = {.state = pending_facts, .deadline_us = deadline}};
        if (!zj_owner_submit(&retry, &pending_ticket)) { pending_ticket = 0; goto done; }
        if (collect(deadline, confirmed))
            saved = proposed->generation == confirmed->generation && same_facts(proposed, confirmed);
        goto done;
    }
    zj_request_t request = {.operation = ZJ_LEASE,
        .input.lease = {.state = *proposed, .deadline_us = deadline}};
    if (!zj_owner_submit(&request, &pending_ticket)) { pending_ticket = 0; goto done; }
    pending_facts = *proposed;
    saved = collect(deadline, confirmed);
done:
    atomic_flag_clear_explicit(&caller_busy, memory_order_release);
    return saved;
}
