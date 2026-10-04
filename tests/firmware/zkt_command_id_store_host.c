#include "zkt_command_ids.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

enum { NONE, OPEN, READ, WRITE, FLUSH, SYNC, CLOSE, STAT, SEEK };
static unsigned fault, open_files, syncs, steps;
static size_t read_bytes;
static bool deny;
static zi_store_t store;
static bool trigger(unsigned point) { if (fault != point) return false; fault = NONE; errno = EIO; return true; }
FILE *zi_fopen(const char *path, const char *mode)
{ if (trigger(OPEN)) return NULL; FILE *file = fopen(path, mode); if (file) ++open_files; return file; }
size_t zi_fread(void *out, size_t size, size_t count, FILE *file)
{ read_bytes += size * count; if (trigger(READ)) return 0; return fread(out, size, count, file); }
size_t zi_fwrite(const void *in, size_t size, size_t count, FILE *file)
{ if (trigger(WRITE)) { assert(size == 1 && count > 1); return fwrite(in, size, count - 1, file); } return fwrite(in, size, count, file); }
int zi_fflush(FILE *file) { if (trigger(FLUSH)) return -1; return fflush(file); }
int zi_fsync(int fd) { ++syncs; if (trigger(SYNC)) return -1; return fsync(fd); }
int zi_fclose(FILE *file) { assert(open_files); --open_files; int result = fclose(file); return trigger(CLOSE) ? -1 : result; }
int zi_fstat(int fd, struct stat *st) { if (trigger(STAT)) return -1; return fstat(fd, st); }
int zi_fseek(FILE *file, long offset, int whence) { if (trigger(SEEK)) return -1; return fseek(file, offset, whence); }
static bool admit(void *context, size_t bytes)
{ (void)context; assert(bytes > 512 && bytes <= 512 + ZI_ID_BYTES); if (deny) errno = ENOSPC; return !deny; }
static void reset(const char *text)
{
    assert(!open_files); unlink("processed"); unlink("cancelled");
    if (text) { FILE *file = fopen("processed", "wb"); assert(file && fputs(text, file) >= 0 && !fclose(file)); }
    assert(zi_store_init(&store, "processed", "cancelled", admit, NULL));
    fault = NONE; deny = false; steps = syncs = 0;
}
static zj_result_t run(zi_kind_t kind, const char *id, bool remember, zi_reply_t *reply)
{
    zi_request_t request = {.kind = (uint8_t)kind, .remember = remember, .deadline_us = 10};
    assert(strlen(id) < sizeof(request.id)); strcpy(request.id, id);
    zj_result_t result;
    bool pending;
    unsigned before = steps;
    do {
        read_bytes = 0;
        pending = zi_store_step(&store, 1, steps == before ? 1 : 100, &request, reply, &result);
        assert(read_bytes <= ZI_READ_BYTES && !open_files);
        ++steps; assert(steps - before <= ZI_LIMIT_BYTES / ZI_READ_BYTES + 1);
    } while (pending);
    assert(!store.work_ticket); return result;
}
static off_t size(const char *path)
{ struct stat st; return stat(path, &st) == 0 ? st.st_size : -1; }
int main(void)
{
    zi_reply_t reply;
    reset(NULL); assert(run(ZI_PROCESSED, "A", false, &reply) == ZJ_OK && !reply.present);
    deny = true; assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_FULL && size("processed") == -1);
    deny = false; assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_OK && reply.present && syncs == 1);
    assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_OK && reply.present && syncs == 2 && size("processed") == 2);
    assert(run(ZI_CANCELLED, "A", false, &reply) == ZJ_OK && !reply.present);
    assert(run(ZI_CANCELLED, "A", true, &reply) == ZJ_OK && reply.present && size("processed") == 2);
    assert(run(ZI_PROCESSED, "B", true, &reply) == ZJ_OK && size("processed") == 4);
    assert(zi_store_init(&store, "processed", "cancelled", admit, NULL)); /* reboot */
    assert(run(ZI_PROCESSED, "A", false, &reply) == ZJ_OK && reply.present);
    for (unsigned point = OPEN; point <= SEEK; ++point) {
        reset(NULL); fault = point;
        zj_result_t result = run(ZI_PROCESSED, "A", true, &reply);
        if (point == READ || point == SEEK || point == CLOSE) {
            /* Absent input does not read/seek. Close here faults the append. */
            if (point != CLOSE) assert(result == ZJ_OK);
            else assert(result == ZJ_UNCERTAIN);
        } else assert(result != ZJ_OK);
        fault = NONE;
        if (point == WRITE) {
            assert(size("processed") == 1);
            assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_CORRUPT);
        } else assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_OK && reply.present && size("processed") == 2);
    }
    for (unsigned point = OPEN; point <= SEEK; ++point) {
        if (point == WRITE || point == FLUSH || point == SYNC) continue;
        reset("A\n"); fault = point;
        assert(run(ZI_PROCESSED, "missing", false, &reply) == ZJ_IO && !reply.present && size("processed") == 2);
    }
    reset("A\n"); fault = SYNC;
    assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_UNCERTAIN && !reply.present);
    assert(run(ZI_PROCESSED, "A", true, &reply) == ZJ_OK && syncs == 2 && size("processed") == 2);
    for (const char **bad = (const char *[]) {"A", "\n", "B\rC\n", NULL}; *bad; ++bad) {
        reset(*bad); assert(run(ZI_PROCESSED, "unknown", true, &reply) == ZJ_CORRUPT);
        assert(size("processed") == (off_t)strlen(*bad));
    }
    reset("A\r\nB\n"); assert(run(ZI_PROCESSED, "A", false, &reply) == ZJ_OK && reply.present);
    reset(NULL); FILE *file = fopen("processed", "wb"); assert(file);
    for (unsigned i = 0; i < 3000; ++i) assert(fprintf(file, "synthetic-%04u\r\n", i) == 16);
    assert(!fclose(file));
    assert(run(ZI_PROCESSED, "synthetic-2999", false, &reply) == ZJ_OK && reply.present && steps > 10);
    zi_request_t request = {.kind = ZI_PROCESSED, .deadline_us = 2, .id = "missing"}; zj_result_t result;
    read_bytes = 0; assert(zi_store_step(&store, 3, 1, &request, &reply, &result));
    assert(store.work_ticket == 3 && read_bytes <= ZI_READ_BYTES && !open_files);
    assert(!zi_store_step(&store, 4, 1, &request, &reply, &result) && result == ZJ_IO && store.work_ticket == 3);
    unlink("processed"); assert(!zi_store_step(&store, 3, 3, &request, &reply, &result) && result == ZJ_IO);
    assert(!zi_store_step(&store, 4, 3, &request, &reply, &result) && result == ZJ_STALE);
    request.id[0] = '\n'; assert(!zi_request_valid(&request));
    reset(NULL); file = fopen("processed", "wb"); assert(file);
    const char *row = "1234567890123456789012345678901\n"; assert(strlen(row) == 32);
    for (unsigned i = 0; i < ZI_LIMIT_BYTES / 32; ++i) assert(fputs(row, file) >= 0);
    assert(!fclose(file)); assert(run(ZI_PROCESSED, "new", true, &reply) == ZJ_FULL && size("processed") == ZI_LIMIT_BYTES);
    file = fopen("processed", "ab"); assert(file && fputc('x', file) != EOF && !fclose(file));
    assert(run(ZI_PROCESSED, "new", false, &reply) == ZJ_IO && reply.error == EFBIG);
    puts("bounded command receipt scans, persistence faults and replay passed");
}
