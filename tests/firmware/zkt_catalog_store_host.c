#include "zkt_catalog_store.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static unsigned io_calls, fail_io, handles, commits, fail_commit;
static bool uncertain;
static ft_checkpoint_t saved;
FILE *zc_fopen(const char *path, const char *mode)
{
    if (++io_calls == fail_io) { errno = EIO; return NULL; }
    FILE *file = fopen(path, mode); if (file) ++handles; return file;
}
size_t zc_fwrite(const void *bytes, size_t size, size_t count, FILE *file)
{
    bool fail = ++io_calls == fail_io;
    size_t n = fwrite(bytes, size, fail ? count / 2 : count, file);
    if (fail) errno = EIO;
    return n;
}
int zc_fflush(FILE *file)
{ if (++io_calls == fail_io) { errno = EIO; return EOF; } return fflush(file); }
int zc_fsync(int fd)
{ if (++io_calls == fail_io) { errno = EIO; return -1; } return fsync(fd); }
int zc_fclose(FILE *file)
{
    bool fail = ++io_calls == fail_io; assert(handles); --handles;
    int result = fclose(file); if (fail) { errno = EIO; return EOF; } return result;
}
static int load(void *context, ft_checkpoint_t *checkpoint)
{ (void)context; *checkpoint = saved; return saved.version ? 1 : 0; }
static bool commit(void *context, const ft_checkpoint_t *checkpoint)
{
    (void)context;
    if (++commits == fail_commit) { if (uncertain) saved = *checkpoint; return false; }
    saved = *checkpoint; return true;
}
static ft_port_t port = {load, commit, NULL};
static zc_store_t store;
static uint64_t ticket;
static zc_reply_t reply;
static void initialize(void)
{
    assert(!handles);
    unlink("active"); unlink("commit"); unlink("backup"); unlink("temporary"); unlink("stage");
    saved = (ft_checkpoint_t){0}; io_calls = fail_io = commits = fail_commit = 0; uncertain = false; ticket = 0;
    assert(zc_store_init(&store, "active", "commit", "backup", "temporary", "stage", port));
}
static zj_result_t run(zc_request_t request)
{
    request.deadline_us = 100;
    zj_result_t result;
    uint64_t work = ++ticket;
    unsigned steps = 0;
    while (zc_store_step(&store, work, 1, &request, &reply, &result)) { assert(++steps < 5000 && !handles); }
    assert(!handles); return result;
}
static zc_request_t producer(void)
{
    assert(run((zc_request_t){.operation=ZC_RESET}) == ZJ_OK);
    return (zc_request_t){.operation=ZC_APPEND, .id=reply.id, .deadline_us=100, .length=ZC_CHUNK_BYTES};
}
static void seed(const char *path, const char *value)
{
    FILE *file = fopen(path, "w"); assert(file);
    assert(fputs(value, file) >= 0 && !fclose(file));
}
static bool is(const char *path, const char *value)
{
    FILE *file = fopen(path, "r"); if (!file) return false;
    char data[64] = {0}; size_t n = fread(data, 1, sizeof(data), file);
    assert(!fclose(file)); return n == strlen(value) && !memcmp(data, value, n);
}
int main(void)
{
    initialize(); assert(run((zc_request_t){.operation=ZC_RECOVER}) == ZJ_OK);
    assert(run((zc_request_t){.operation=ZC_READ}) == ZJ_EMPTY);
    zc_request_t reset = {.operation=ZC_RESET};
    assert(zc_store_admission_bytes(&store, &reset) == 512);
    zc_request_t request = producer();
    assert(zc_store_admission_bytes(&store, &reset) == 0);
    assert(zc_store_admission_bytes(&store, &request) == ZC_CHUNK_BYTES + 512U);
    memset(request.bytes, 'N', request.length);
    for (unsigned i = 0; i < 35; ++i) { assert(run(request) == ZJ_OK); request.offset = reply.offset; }
    zc_request_t activate = request; activate.operation = ZC_ACTIVATE;
    uint64_t activation_ticket = ++ticket;
    zj_result_t result; unsigned steps = 0;
    while (zc_store_step(&store, activation_ticket, steps ? 1000 : 1, &activate, &reply, &result)) {
        /* Expiry after admission cannot interrupt the recovery obligation. */
        assert(++steps < 100 && !handles);
        zc_request_t competing = {.operation=ZC_RESET, .deadline_us=2000};
        zc_reply_t refused; zj_result_t busy;
        assert(!zc_store_step(&store, activation_ticket + 1, 1000, &competing, &refused, &busy));
        assert(busy == ZJ_IO && refused.error == EBUSY);
    }
    assert(result == ZJ_OK && steps > 15 && store.recovered);
    request = (zc_request_t){.operation=ZC_READ}; unsigned received = 0;
    do {
        assert(run(request) == ZJ_OK && reply.length <= ZC_CHUNK_BYTES);
        for (unsigned i = 0; i < reply.length; ++i) assert(reply.bytes[i] == 'N');
        received += reply.length; request.offset = reply.offset; request.revision = reply.revision;
    } while (!reply.eof);
    assert(received == ZC_CHUNK_BYTES * 35);
    assert(run((zc_request_t){.operation=ZC_RECOVER}) == ZJ_OK);
    assert(run(request) == ZJ_STALE); /* Another generation invalidates an old read cursor. */
    request = producer(); uint64_t old = request.id;
    assert(run((zc_request_t){.operation=ZC_RESET}) == ZJ_OK && reply.id != old);
    assert(run(request) == ZJ_STALE);
    request.operation = ZC_REMOVE; assert(run(request) == ZJ_STALE);
    request.operation = ZC_ACTIVATE; assert(run(request) == ZJ_STALE);
    zc_request_t expired = {.operation=ZC_RESET, .deadline_us=100};
    assert(!zc_store_step(&store, ++ticket, 100, &expired, &reply, &result) && result == ZJ_STALE);

    /* Short writes, failed flush/sync/close and opening failures poison the
     * producer; none can activate even when the bytes happen to look complete. */
    for (unsigned boundary = 1; boundary <= 5; ++boundary) {
        initialize(); seed("active", "old"); request = producer();
        memset(request.bytes, 'X', request.length); io_calls = 0; fail_io = boundary;
        assert(run(request) != ZJ_OK && reply.error);
        fail_io = 0; request.operation = ZC_ACTIVATE;
        assert(run(request) == ZJ_STALE && is("active", "old"));
        request.operation = ZC_REMOVE; assert(run(request) == ZJ_OK);
    }
    /* Retained tokens cannot overwrite a changed/partial producer file. */
    initialize(); request = producer(); seed("temporary", "unexpected");
    assert(run(request) == ZJ_STALE);
    request.operation = ZC_ACTIVATE; assert(run(request) == ZJ_STALE);

    /* Failed activation checkpoints preserve a recoverable old or new
     * generation; the unchanged bridge reader recovers uncertain commits. */
    for (unsigned boundary = 1; boundary <= 3; ++boundary) for (unsigned after = 0; after < 2; ++after) {
        initialize(); seed("active", "old"); request = producer();
        memcpy(request.bytes, "new", 3); request.length = 3;
        assert(run(request) == ZJ_OK); request.offset = reply.offset; request.operation = ZC_ACTIVATE;
        fail_commit = boundary; uncertain = after;
        assert(run(request) != ZJ_OK);
        fail_commit = 0;
        assert(ft_recover("active", "commit", "backup", port));
        assert(is("active", "old") || is("active", "new"));
    }
    initialize(); request = producer();
    store.sizes[0] = ZC_LIMIT_BYTES; request.offset = ZC_LIMIT_BYTES;
    assert(run(request) != ZJ_OK && reply.error == EFBIG);
    initialize(); seed("active", "old"); seed("backup", "ambiguous");
    assert(run((zc_request_t){.operation=ZC_RECOVER}) != ZJ_OK);
    assert(is("active", "old") && is("backup", "ambiguous"));
    puts("catalog owner token, recovery, deadline, read and write-fault checks passed");
}
