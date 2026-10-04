#include "zkt_command_ids.h"
#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

bool zi_store_init(zi_store_t *store, const char *processed, const char *cancelled,
                   bool (*admit)(void *, size_t), void *context)
{
    if (!store || !admit || !processed || !*processed || !cancelled || !*cancelled ||
        !strcmp(processed, cancelled) || strlen(processed) >= sizeof(store->paths[0]) ||
        strlen(cancelled) >= sizeof(store->paths[1])) return false;
    memset(store, 0, sizeof(*store));
    strcpy(store->paths[0], processed); strcpy(store->paths[1], cancelled);
    store->admit = admit; store->context = context;
    return true;
}
static bool finish(zi_store_t *store, zi_reply_t *reply, zj_result_t *result,
                   zj_result_t status, const char *operation, int error)
{
    store->work_ticket = 0;
    reply->operation = operation; reply->error = error; *result = status;
    return false;
}
static bool persist(zi_store_t *store, const zi_request_t *request,
                    zi_reply_t *reply, zj_result_t *result, bool existing)
{
    size_t length = strlen(request->id) + 1;
    if (!existing && length > ZI_LIMIT_BYTES - store->length)
        return finish(store, reply, result, ZJ_FULL, "command_id_capacity", ENOSPC);
    if (!existing && !store->admit(store->context, length + 512U)) {
        int error = errno ? errno : ENOSPC;
        return finish(store, reply, result, error == ENOSPC ? ZJ_FULL : ZJ_IO, "command_id_admission", error);
    }
    /* A previous complete append may have failed sync/close. Replaying a
     * remembered ID must prove sync again before claiming durable success. */
    FILE *file = fopen(store->paths[request->kind], existing ? "r+b" : "a+b");
    if (!file) return finish(store, reply, result, ZJ_IO, "command_id_append_open", errno);
    struct stat st;
    int error = 0;
    const char *operation = "command_id_append_stat";
    if (fstat(fileno(file), &st) != 0) error = errno;
    else if (st.st_size < 0 || (uint64_t)st.st_size != store->length) error = ESTALE;
    bool attempted = false;
    if (!error && !existing) {
        char line[ZI_ID_BYTES]; memcpy(line, request->id, length - 1); line[length - 1] = '\n';
        operation = "command_id_append_write"; attempted = true; errno = 0;
        if (fwrite(line, 1, length, file) != length) error = errno ? errno : EIO;
    }
    if (!error) { operation = "command_id_append_flush"; attempted = true; if (fflush(file) != 0) error = errno ? errno : EIO; }
    if (!error) { operation = "command_id_append_sync"; if (fsync(fileno(file)) != 0) error = errno ? errno : EIO; }
    int closed = fclose(file);
    if (!error && closed != 0) { operation = "command_id_append_close"; error = errno ? errno : EIO; }
    reply->present = !error;
    return finish(store, reply, result, error ? (attempted ? ZJ_UNCERTAIN : ZJ_IO) : ZJ_OK, operation, error);
}
bool zi_store_step(zi_store_t *store, uint64_t ticket, uint64_t now_us,
                   const zi_request_t *request, zi_reply_t *reply, zj_result_t *result)
{
    if (!store || !reply || !result) return false;
    memset(reply, 0, sizeof(*reply)); *result = ZJ_INVALID;
    if (!ticket || !zi_request_valid(request)) return false;
    if (store->work_ticket && store->work_ticket != ticket) {
        reply->operation = "command_id_busy"; reply->error = EBUSY; *result = ZJ_IO; return false;
    }
    if (!store->work_ticket) {
        if (now_us >= request->deadline_us)
            return finish(store, reply, result, ZJ_STALE, "command_id_deadline", ETIMEDOUT);
        store->offset = store->length = store->used = 0; store->size_known = false;
        store->work_ticket = ticket;
    }
    FILE *file = fopen(store->paths[request->kind], "rb");
    if (!file) {
        int error = errno;
        if (error != ENOENT || store->size_known)
            return finish(store, reply, result, ZJ_IO, "command_id_read_open", error);
        if (request->remember) return persist(store, request, reply, result, false);
        return finish(store, reply, result, ZJ_OK, "command_id_absent", 0);
    }
    struct stat st;
    int error = 0;
    const char *operation = "command_id_read_stat";
    if (fstat(fileno(file), &st) != 0) error = errno;
    else if (!S_ISREG(st.st_mode) || st.st_size < 0 || (uint64_t)st.st_size > ZI_LIMIT_BYTES) error = EFBIG;
    else if (store->size_known && (uint64_t)st.st_size != store->length) error = ESTALE;
    if (!error) { store->length = (uint32_t)st.st_size; store->size_known = true; }
    uint8_t bytes[ZI_READ_BYTES];
    size_t count = !error ? store->length - store->offset : 0;
    if (count > sizeof(bytes)) count = sizeof(bytes);
    if (!error) { operation = "command_id_read_seek"; if (fseek(file, (long)store->offset, SEEK_SET) != 0) error = errno; }
    if (!error) {
        operation = "command_id_read_bytes"; errno = 0;
        if (fread(bytes, 1, count, file) != count || ferror(file)) error = errno ? errno : EIO;
    }
    int closed = fclose(file);
    if (!error && closed != 0) { operation = "command_id_read_close"; error = errno ? errno : EIO; }
    if (error) return finish(store, reply, result, ZJ_IO, operation, error);
    for (size_t index = 0; index < count; ++index) {
        uint8_t byte = bytes[index];
        if (!byte || (byte != '\n' && store->used == sizeof(store->line) - 1))
            return finish(store, reply, result, ZJ_CORRUPT, "command_id_line", EBADMSG);
        if (byte == '\n') {
            if (store->used && store->line[store->used - 1] == '\r') --store->used;
            store->line[store->used] = 0;
            if (!store->used || store->used >= ZI_ID_BYTES || strchr(store->line, '\r'))
                return finish(store, reply, result, ZJ_CORRUPT, "command_id_line", EBADMSG);
            if (!strcmp(store->line, request->id)) {
                if (request->remember) return persist(store, request, reply, result, true);
                reply->present = true;
                return finish(store, reply, result, ZJ_OK, "command_id_present", 0);
            }
            store->used = 0;
        } else store->line[store->used++] = (char)byte;
    }
    store->offset += (uint32_t)count;
    if (store->offset != store->length) return true;
    if (store->used) return finish(store, reply, result, ZJ_CORRUPT, "command_id_tail", EBADMSG);
    if (request->remember) return persist(store, request, reply, result, false);
    return finish(store, reply, result, ZJ_OK, "command_id_absent", 0);
}
