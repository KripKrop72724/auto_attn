#include "zkt_catalog_store.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

bool zc_store_init(zc_store_t *store, const char *active, const char *commit,
                   const char *backup, const char *temporary, const char *stage, ft_port_t port)
{
    if (!store || !port.load || !port.commit) return false;
    memset(store, 0, sizeof(*store));
    const char *paths[] = {active, commit, backup, temporary, stage};
    for (unsigned i = 0; i < 5; ++i) {
        if (!paths[i] || !paths[i][0] || strlen(paths[i]) >= FT_PATH_BYTES) return false;
        for (unsigned j = 0; j < i; ++j) if (!strcmp(paths[i], paths[j])) return false;
        strcpy(store->paths[i], paths[i]);
    }
    store->port = port;
    store->next_id = 1;
    store->limit = ZC_LIMIT_BYTES;
    store->allow_first_recovery = true;
    return true;
}
static zj_result_t failed(zc_store_t *store, const char *operation, int error)
{
    store->error = error ? error : EIO;
    store->operation = operation;
    return error == ENOSPC ? ZJ_FULL : error == ESTALE ? ZJ_STALE : ZJ_IO;
}
size_t zc_store_admission_bytes(const zc_store_t *store, const zc_request_t *request)
{
    if (!store || !request || request->file >= 2) return SIZE_MAX;
    if (request->operation == ZC_RESET) {
        struct stat st;
        if (stat(store->paths[3 + request->file], &st) == 0 && S_ISREG(st.st_mode)) return 0;
        return 512U;
    }
    return request->operation == ZC_APPEND ? request->length + 512U : 0;
}
static bool transaction_begin(zc_store_t *store, bool replacement)
{
    return ft_work_begin(&store->transaction, store->paths[0], store->paths[1], store->paths[2],
        store->limit, store->port, replacement);
}
enum { ZC_WORK_RECOVER = 1, ZC_WORK_FIRST, ZC_WORK_MOVE, ZC_WORK_REPLACE };
static bool transaction_step(zc_store_t *store, const zc_request_t *request, zj_result_t *result)
{
    if (store->work_phase == ZC_WORK_MOVE) {
        /* The recovered transaction is idle. Only the complete producer file
         * selected by its live token can become a canonical recovery intent. */
        if (remove(store->paths[1]) != 0 && errno != ENOENT) {
            *result = failed(store, "catalog_clear_uncommitted", errno); return false;
        }
        if (rename(store->paths[3 + request->file], store->paths[1]) != 0) {
            *result = failed(store, "catalog_prepare_rename", errno); return false;
        }
        store->poisoned[request->file] = true; /* Never append to the renamed file. */
        if (!transaction_begin(store, true)) {
            *result = failed(store, "catalog_transaction_begin", EINVAL); return false;
        }
        store->work_phase = ZC_WORK_REPLACE;
        return true;
    }
    ft_work_result_t progress = ft_work_step(&store->transaction);
    if (progress == FT_WORK_PENDING) return true;
    if (progress == FT_WORK_FAILED && store->work_phase == ZC_WORK_RECOVER && store->allow_first_recovery) {
        /* A canonical commit file is renamed here only after the complete
         * producer has closed it. Match the existing first-install reader. */
        if (!transaction_begin(store, true)) {
            *result = failed(store, "catalog_recovery_begin", EINVAL); return false;
        }
        store->work_phase = ZC_WORK_FIRST;
        return true;
    }
    if (progress == FT_WORK_FAILED) {
        *result = failed(store, store->transaction.operation, store->transaction.error);
        return false;
    }
    if (request->operation == ZC_ACTIVATE && store->work_phase != ZC_WORK_REPLACE) {
        store->work_phase = ZC_WORK_MOVE;
        return true;
    }
    store->recovered = true;
    ++store->revision;
    *result = ZJ_OK;
    return false;
}
static zj_result_t stream_write(zc_store_t *store, const zc_request_t *request, zc_reply_t *reply)
{
    unsigned slot = request->file;
    bool reset = request->operation == ZC_RESET;
    if (reset && (!store->next_id || store->next_id == UINT64_MAX)) return failed(store, "catalog_id_exhausted", EOVERFLOW);
    if (!reset && (store->ids[slot] != request->id || store->poisoned[slot] || store->sizes[slot] != request->offset))
        return failed(store, "catalog_stale_producer", ESTALE);
    if (!reset && (store->sizes[slot] > store->limit || request->length > store->limit - store->sizes[slot]))
        return failed(store, "catalog_size_limit", EFBIG);
    const char *path = store->paths[3 + slot];
    store->poisoned[slot] = true;
    FILE *file = fopen(path, reset ? "wb" : "r+b");
    if (!file) return failed(store, "catalog_stage_open", errno);
    int error = 0;
    const char *operation = "catalog_stage_seek";
    if (!reset) {
        struct stat st;
        if (fstat(fileno(file), &st) != 0) { error = errno; operation = "catalog_stage_stat"; }
        else if (st.st_size < 0 || (uint64_t)st.st_size != request->offset) error = ESTALE;
        else if (fseek(file, (long)request->offset, SEEK_SET) != 0) error = errno;
        if (!error) {
            operation = "catalog_stage_write";
            errno = 0;
            if (fwrite(request->bytes, 1, request->length, file) != request->length) error = errno ? errno : EIO;
        }
    }
    if (!error) { operation = "catalog_stage_flush"; if (fflush(file) != 0) error = errno ? errno : EIO; }
    if (!error) { operation = "catalog_stage_sync"; if (fsync(fileno(file)) != 0) error = errno ? errno : EIO; }
    int closed = fclose(file);
    if (!error && closed != 0) { operation = "catalog_stage_close"; error = errno ? errno : EIO; }
    if (error) return failed(store, operation, error);
    if (reset) { store->ids[slot] = store->next_id++; store->sizes[slot] = 0; }
    else store->sizes[slot] += request->length;
    store->poisoned[slot] = false;
    reply->id = store->ids[slot];
    reply->offset = store->sizes[slot];
    return ZJ_OK;
}
static zj_result_t read_active(zc_store_t *store, const zc_request_t *request, zc_reply_t *reply)
{
    if (!store->recovered || (request->offset && request->revision != store->revision))
        return failed(store, "catalog_reader_revision", ESTALE);
    FILE *file = fopen(store->paths[0], "rb");
    if (!file) return errno == ENOENT ? ZJ_EMPTY : failed(store, "catalog_read_open", errno);
    struct stat st;
    int error = 0;
    const char *operation = "catalog_read_stat";
    if (fstat(fileno(file), &st) != 0) error = errno;
    else if (st.st_size < 0 || (uint64_t)st.st_size > store->limit || request->offset > (uint64_t)st.st_size) error = EINVAL;
    if (!error) {
        reply->total = (uint32_t)st.st_size;
        operation = "catalog_read_seek";
        if (fseek(file, (long)request->offset, SEEK_SET) != 0) error = errno;
    }
    if (!error) {
        size_t bytes = reply->total - request->offset;
        if (bytes > ZC_CHUNK_BYTES) bytes = ZC_CHUNK_BYTES;
        operation = "catalog_read_bytes";
        errno = 0;
        if (fread(reply->bytes, 1, bytes, file) != bytes || ferror(file)) error = errno ? errno : EIO;
        else {
            reply->length = (uint16_t)bytes;
            reply->offset = request->offset + (uint32_t)bytes;
            reply->eof = reply->offset == reply->total;
            reply->revision = store->revision;
        }
    }
    int closed = fclose(file);
    if (!error && closed != 0) { operation = "catalog_read_close"; error = errno ? errno : EIO; }
    return error ? failed(store, operation, error) : ZJ_OK;
}
bool zc_store_step(zc_store_t *store, uint64_t ticket, uint64_t now_us,
                   const zc_request_t *request, zc_reply_t *reply, zj_result_t *result)
{
    if (!store || !reply || !result) return false;
    memset(reply, 0, sizeof(*reply));
    store->error = 0;
    store->operation = "catalog_request";
    *result = ZJ_INVALID;
    if (!ticket || !zc_request_valid(request)) return false;
    if (store->work_ticket && store->work_ticket != ticket) {
        *result = failed(store, "catalog_transaction_busy", EBUSY);
    } else if (!store->work_ticket && now_us >= request->deadline_us) {
        *result = failed(store, "catalog_deadline", ESTALE);
    } else if (request->operation == ZC_RECOVER || request->operation == ZC_ACTIVATE) {
        if (!store->work_ticket) {
            if (request->operation == ZC_ACTIVATE && (store->ids[request->file] != request->id ||
                store->poisoned[request->file] || store->sizes[request->file] != request->offset)) {
                *result = failed(store, "catalog_stale_producer", ESTALE); goto done;
            }
            if (store->revision == UINT64_MAX || !transaction_begin(store, false)) {
                *result = failed(store, "catalog_recovery_begin", EINVAL); goto done;
            }
            store->recovered = false;
            store->work_ticket = ticket;
            store->work_phase = ZC_WORK_RECOVER;
        }
        if (transaction_step(store, request, result)) return true;
        store->work_ticket = 0;
    } else if (request->operation == ZC_RESET || request->operation == ZC_APPEND) {
        *result = stream_write(store, request, reply);
    } else if (request->operation == ZC_REMOVE) {
        if (store->ids[request->file] != request->id) *result = failed(store, "catalog_stale_producer", ESTALE);
        else if (remove(store->paths[3 + request->file]) != 0 && errno != ENOENT) *result = failed(store, "catalog_stage_remove", errno);
        else { store->poisoned[request->file] = true; *result = ZJ_OK; }
    } else *result = read_active(store, request, reply);
done:
    reply->error = store->error;
    reply->operation = store->operation;
    return false;
}
