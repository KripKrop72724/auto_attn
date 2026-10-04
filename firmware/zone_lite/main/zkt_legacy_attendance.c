#include "zkt_legacy_attendance.h"
#include "zkt_storage_owner.h"
#include "zkt_runtime_checkpoint.h"
#include "reliability.h"
#include "nvs.h"
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

typedef struct {
    const char *path, *backup, *temporary, *key;
    legacy_queue_t queue;
    bool initialized;
} lane_t;
static lane_t lanes[] = {
    {.path=ZOL_PENDING_PATH, .backup=ZOL_PENDING_BACKUP_PATH, .temporary=ZOL_PENDING_TEMP_PATH, .key="ords_pending"},
    {.path=ZOL_BLOCKED_PATH, .backup=ZOL_BLOCKED_BACKUP_PATH, .temporary=ZOL_BLOCKED_TEMP_PATH, .key="blocked"}};
static atomic_bool pending_empty;

bool zol_pending_verified_empty(void)
{ return atomic_load_explicit(&pending_empty, memory_order_acquire); }
dq_result_t zol_append(unsigned lane, const void *bytes, size_t length, qs_admission_t policy)
{
    if (lane == ZOL_PENDING) atomic_store_explicit(&pending_empty, false, memory_order_release);
    return zq_attendance_legacy_append(lane, bytes, length, policy);
}
static int load(void *context, lq_checkpoint_t *checkpoint)
{
    lane_t *lane = context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("legacy_queues", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t size = sizeof(*checkpoint);
    result = nvs_get_blob(handle, lane->key, checkpoint, &size);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && size == sizeof(*checkpoint) ? 1 : -1;
}
static bool commit(void *context, const lq_checkpoint_t *checkpoint)
{
    lane_t *lane = context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("legacy_queues", NVS_READWRITE, &handle);
    if (result != ESP_OK) return false;
    result = nvs_set_blob(handle, lane->key, checkpoint, sizeof(*checkpoint));
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}
static bool restore(lane_t *lane)
{
    lq_invalidate_empty(&lane->queue);
    struct stat st;
    if (stat(lane->path, &st) == 0) return true;
    if (errno != ENOENT) return false;
    const char *generations[] = {lane->backup, lane->temporary};
    for (unsigned i = 0; i < 2; ++i) {
        if (stat(generations[i], &st) == 0) return rename(generations[i], lane->path) == 0;
        if (errno != ENOENT) return false;
    }
    return true;
}
static lane_t *begin(unsigned index)
{
    if (index >= 2 || !zj_runtime_checkpoint_required() || !zj_owner_is_current_task() || !qs_local_read_begin()) return NULL;
    return &lanes[index];
}
static dq_result_t prepare(lane_t *lane)
{
    if (!lane->initialized) {
        if (!restore(lane)) return DQ_IO;
        lane->initialized = true;
    }
    if (lane->queue.ready) return DQ_OK;
    /* One bounded consumed-prefix check, never a synchronous whole-file scan. */
    return lq_open_step(&lane->queue, lane->path, (lq_port_t){load, commit, lane});
}
static dq_result_t finish(lane_t *lane, dq_result_t result, bool failed_write, int error)
{
    if (lane == &lanes[ZOL_PENDING]) atomic_store_explicit(&pending_empty,
        result == DQ_EMPTY && lane->queue.ready && lane->queue.empty_cached, memory_order_release);
    qs_local_end(!failed_write, failed_write ? (error ? error : EIO) : 0);
    return result;
}
static dq_result_t retire(lane_t *lane)
{
    dq_result_t result = lq_reclaim(&lane->queue);
    if (result == DQ_OK) {
        /* A failed restore after removal remains an obligation. A later read
         * must retry it before trusting the now-absent active generation. */
        lane->initialized = restore(lane);
        if (!lane->initialized) return DQ_IO;
    }
    return result;
}
dq_result_t zol_owner_append(unsigned index, const void *bytes, size_t length, qs_admission_t policy)
{
    if (!bytes || !length || length > DQ_MAX_RECORD_BYTES - 2 || (unsigned)policy > QS_ADMIT_RECOVERY ||
        memchr(bytes, 0, length) || memchr(bytes, '\n', length)) return DQ_IO;
    lane_t *lane = begin(index);
    if (!lane) return DQ_PENDING;
    dq_result_t result = prepare(lane);
    bool attempted = false;
    int error = 0;
    if (result != DQ_OK) goto done;
    if (!qs_local_admit_locked(policy, length + 1)) {
        result = errno == ENOSPC ? DQ_FULL : DQ_PENDING;
        goto done;
    }
    lq_invalidate_empty(&lane->queue);
    attempted = true;
    errno = 0;
    FILE *file = rel_open_append(lane->path);
    bool ok = file && fwrite(bytes, 1, length, file) == length && fputc('\n', file) != EOF &&
        fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (!ok) error = errno ? errno : EIO;
    if (file && fclose(file) != 0) { ok = false; if (!error) error = errno ? errno : EIO; }
    result = ok ? DQ_OK : DQ_IO;
done:
    return finish(lane, result, attempted && result != DQ_OK, error);
}
dq_result_t zol_owner_peek(unsigned index, void *bytes, size_t capacity, size_t *length, lq_token_t *token)
{
    if (length) *length = 0;
    if (token) memset(token, 0, sizeof(*token));
    if (!bytes || capacity < 2 || capacity > DQ_MAX_RECORD_BYTES + 1 || !length || !token) return DQ_IO;
    lane_t *lane = begin(index);
    if (!lane) return DQ_PENDING;
    dq_result_t result = prepare(lane);
    if (result != DQ_OK) goto done;
    if (lane->queue.empty_cached) { result = DQ_EMPTY; goto done; }
    result = lq_peek(&lane->queue, bytes, capacity, token);
    if (result == DQ_OK) *length = token->end - token->offset;
    else if (result == DQ_EMPTY) {
        dq_result_t reclaimed = retire(lane);
        if (reclaimed == DQ_EMPTY) lane->queue.empty_cached = true;
        else result = reclaimed == DQ_OK ? DQ_PENDING : reclaimed;
    }
done:
    return finish(lane, result, false, 0);
}
dq_result_t zol_owner_settle(unsigned index, const lq_token_t *token, bool custody)
{
    if (!token || token->end <= token->offset) return DQ_IO;
    lane_t *lane = begin(index);
    if (!lane) return DQ_PENDING;
    errno = 0;
    dq_result_t result = custody ? lq_settle_evidence(&lane->queue, token) : lq_settle(&lane->queue, token);
    if (result == DQ_OK) {
        result = retire(lane);
        if (result == DQ_STALE) result = DQ_OK; /* Another retained row remains. */
    }
    int error = errno;
    return finish(lane, result, result == DQ_IO, error);
}
