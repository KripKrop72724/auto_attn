#include "zkt_rollback.h"
#include "zkt_journal_compat.h"
#include "esp_timer.h"
#include "nvs.h"
#include <string.h>

bool zj_rollback_request_valid(const ota_checkpoint_t *request)
{
    return ota_checkpoint_valid(request) &&
        !strcmp(request->journal.target_version, ZJ_BRIDGE_VERSION) &&
        (!strcmp(request->journal.state, "DOWNLOADING") ||
         !strcmp(request->journal.state, "READER_INTENT")) &&
        !request->journal.bytes_written &&
        strspn(request->journal.image_sha256, "0") != 64;
}

bool zj_rollback_same_target(const ota_checkpoint_t *left, const ota_checkpoint_t *right)
{
    if (!zj_rollback_request_valid(left) || !zj_rollback_request_valid(right)) return false;
    ota_journal_t a = left->journal, b = right->journal;
    memset(a.state, 0, sizeof(a.state));
    memset(b.state, 0, sizeof(b.state));
    return !memcmp(&a, &b, sizeof(a));
}

zj_result_t zj_rollback_commit_intent(const ota_checkpoint_t *expected,
    uint64_t deadline_us, ota_checkpoint_t *committed, int *nvs_error)
{
    if (!committed || !nvs_error) return ZJ_INVALID;
    memset(committed, 0, sizeof(*committed));
    *nvs_error = 0;
    if (!zj_rollback_request_valid(expected) || !deadline_us) return ZJ_INVALID;
    if ((uint64_t)esp_timer_get_time() >= deadline_us) return ZJ_STALE;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zone_ota", NVS_READWRITE, &handle);
    if (status != ESP_OK) { *nvs_error = status; return ZJ_IO; }
    ota_checkpoint_t current = {0};
    size_t length = sizeof(current);
    status = nvs_get_blob(handle, "journal_v1", &current, &length);
    zj_result_t result = ZJ_IO;
    if (status == ESP_ERR_NVS_NOT_FOUND) { result = ZJ_CORRUPT; goto done; }
    if (status != ESP_OK) { *nvs_error = status; goto done; }
    if (length != sizeof(current) || !ota_checkpoint_valid(&current)) {
        result = ZJ_CORRUPT; goto done;
    }
    if (!zj_rollback_same_target(expected, &current)) { result = ZJ_STALE; goto done; }
    if (!strcmp(current.journal.state, "READER_INTENT")) {
        /* A lost commit response must recover the same approved operation,
         * without rewriting NVS or trusting the caller's old generation. */
        if (current.generation < expected->generation) { result = ZJ_STALE; goto done; }
        *committed = current; result = ZJ_OK; goto done;
    }
    if (memcmp(expected, &current, sizeof(current))) { result = ZJ_STALE; goto done; }
    if (current.generation == UINT32_MAX) { result = ZJ_FULL; goto done; }
    if ((uint64_t)esp_timer_get_time() >= deadline_us) { result = ZJ_STALE; goto done; }
    ota_checkpoint_t next = current;
    ++next.generation;
    memset(next.journal.state, 0, sizeof(next.journal.state));
    memcpy(next.journal.state, "READER_INTENT", sizeof("READER_INTENT"));
    next.crc = dq_crc32(&next, offsetof(ota_checkpoint_t, crc));
    status = nvs_set_blob(handle, "journal_v1", &next, sizeof(next));
    if (status == ESP_OK) status = nvs_commit(handle);
    if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
    length = sizeof(current);
    status = nvs_get_blob(handle, "journal_v1", &current, &length);
    if (status != ESP_OK) { *nvs_error = status; result = ZJ_UNCERTAIN; goto done; }
    if (length != sizeof(current) || !ota_checkpoint_valid(&current) || memcmp(&current, &next, sizeof(next))) {
        result = ZJ_UNCERTAIN; goto done;
    }
    *committed = current;
    result = ZJ_OK;
done:
    nvs_close(handle);
    return result;
}
