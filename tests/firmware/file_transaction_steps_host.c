#include "file_transaction.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static ft_checkpoint_t saved;
static unsigned commits, fail_commit, io_calls, fail_io;
static bool uncertain;
static size_t step_read_bytes;
static unsigned open_handles;
/* Only file_transaction.c is compiled with these I/O substitutions. */
FILE *fw_fopen(const char *path, const char *mode)
{
    if (++io_calls == fail_io) { errno = EIO; return NULL; }
    FILE *file = fopen(path, mode); if (file) ++open_handles; return file;
}
int fw_fseek(FILE *file, long offset, int origin)
{ if (++io_calls == fail_io) { errno = EIO; return -1; } return fseek(file, offset, origin); }
size_t fw_fread(void *out, size_t size, size_t count, FILE *file)
{
    bool short_read = ++io_calls == fail_io;
    size_t got = fread(out, size, short_read ? count / 2 : count, file);
    step_read_bytes += got * size;
    if (short_read) errno = EIO;
    return got;
}
int fw_fflush(FILE *file)
{ if (++io_calls == fail_io) { errno = EIO; return EOF; } return fflush(file); }
int fw_fsync(int descriptor)
{ if (++io_calls == fail_io) { errno = EIO; return -1; } return fsync(descriptor); }
int fw_fclose(FILE *file)
{
    bool fail = ++io_calls == fail_io;
    assert(open_handles); --open_handles;
    int result = fclose(file);
    if (fail) { errno = EIO; return EOF; }
    return result;
}
int fw_rename(const char *from, const char *to)
{ if (++io_calls == fail_io) { errno = EIO; return -1; } return rename(from, to); }
int fw_remove(const char *path)
{ if (++io_calls == fail_io) { errno = EIO; return -1; } return remove(path); }
static int load(void *context, ft_checkpoint_t *checkpoint)
{ (void)context; *checkpoint = saved; return saved.version ? 1 : 0; }
static bool commit(void *context, const ft_checkpoint_t *checkpoint)
{
    (void)context;
    if (++commits == fail_commit) { if (uncertain) saved = *checkpoint; return false; }
    saved = *checkpoint;
    return true;
}
static const ft_port_t port = {load, commit, NULL};
#define BYTES (FT_READ_SLICE_BYTES * 3U + 37U)
static unsigned char original[BYTES], replacement[BYTES];
static void seed(const char *path, const void *bytes, size_t length)
{
    FILE *file = fopen(path, "wb"); assert(file);
    assert(fwrite(bytes, 1, length, file) == length);
    assert(!fflush(file) && !fsync(fileno(file)) && !fclose(file));
}
static bool content(const char *path, const void *bytes, size_t length)
{
    FILE *file = fopen(path, "rb"); if (!file) return false;
    unsigned char buffer[BYTES + 1];
    size_t got = fread(buffer, 1, sizeof(buffer), file);
    bool ok = !ferror(file) && got == length && !memcmp(buffer, bytes, length);
    assert(!fclose(file)); return ok;
}
static void reset(bool first, size_t length)
{
    assert(!open_handles);
    unlink("active"); unlink("stage"); unlink("backup");
    saved = (ft_checkpoint_t){0}; commits = fail_commit = io_calls = fail_io = 0; uncertain = false;
    if (!first) seed("active", original, sizeof(original));
    seed("stage", replacement, length);
}
static ft_work_t begin(bool replacing)
{
    ft_work_t work;
    assert(ft_work_begin(&work, "active", "stage", "backup", sizeof(replacement), port, replacing));
    return work;
}
static ft_work_result_t step(ft_work_t *work)
{
    step_read_bytes = 0;
    ft_work_result_t result = ft_work_step(work);
    assert(step_read_bytes <= FT_READ_SLICE_BYTES);
    assert(!open_handles);
    return result;
}
static unsigned complete(ft_work_t *work, ft_work_result_t *result)
{
    unsigned steps = 0;
    do { *result = step(work); assert(++steps < 200); } while (*result == FT_WORK_PENDING);
    return steps;
}
static void preserved(size_t length)
{
    assert(content("active", original, sizeof(original)) || content("backup", original, sizeof(original)) ||
        content("active", replacement, length) || content("stage", replacement, length));
}
static void legacy_recover(bool first, size_t length)
{
    fail_io = fail_commit = 0;
    bool recovered = ft_recover("active", "stage", "backup", port);
    if (!recovered) {
        assert(first && saved.phase == 0 && content("stage", replacement, length));
        assert(ft_replace("active", "stage", "backup", sizeof(replacement), port));
    }
    assert(content("active", original, sizeof(original)) || content("active", replacement, length));
}
int main(void)
{
    memset(original, 'O', sizeof(original)); memset(replacement, 'N', sizeof(replacement));
    ft_work_result_t result;
    for (unsigned first = 0; first < 2; ++first) for (unsigned empty = 0; empty < 2; ++empty) {
        size_t length = empty ? 0 : sizeof(replacement);
        reset(first, length); ft_work_t work = begin(true);
        unsigned steps = complete(&work, &result), operations = io_calls;
        assert(result == FT_WORK_DONE && saved.phase == 0 && content("active", replacement, length));
        if (length) assert(steps > length / FT_READ_SLICE_BYTES && work.bytes_read >= length);
        assert(step(&work) == FT_WORK_DONE && io_calls == operations);
        /* Drop all RAM progress after every boundary. The unchanged reader
         * must recover the same on-disk/NVS contract, including empty files. */
        for (unsigned boundary = 0; boundary <= steps; ++boundary) {
            reset(first, length); work = begin(true);
            for (unsigned i = 0; i < boundary; ++i) (void)step(&work);
            preserved(length); legacy_recover(first, length);
        }
        for (unsigned boundary = 1; boundary <= operations; ++boundary) {
            reset(first, length); fail_io = boundary; work = begin(true);
            (void)complete(&work, &result);
            if (result == FT_WORK_FAILED) assert(work.error && work.operation);
            preserved(length); legacy_recover(first, length);
        }
        for (unsigned boundary = 1; boundary <= 3; ++boundary) for (unsigned ambiguity = 0; ambiguity < 2; ++ambiguity) {
            reset(first, length); fail_commit = boundary; uncertain = ambiguity; work = begin(true);
            (void)complete(&work, &result); assert(result == FT_WORK_FAILED);
            preserved(length); legacy_recover(first, length);
        }
    }
    reset(false, sizeof(replacement));
    ft_work_t work;
    assert(!ft_work_begin(&work, "active", "active", "backup", 100, port, true));
    assert(step(&work) == FT_WORK_FAILED);
    assert(ft_work_begin(&work, "active", "stage", "backup", 1, port, true));
    complete(&work, &result); assert(result == FT_WORK_FAILED && content("active", original, sizeof(original)));
    reset(false, sizeof(replacement)); seed("backup", "unaccounted", 11);
    work = begin(false); complete(&work, &result);
    assert(result == FT_WORK_FAILED && content("backup", "unaccounted", 11));
    saved = (ft_checkpoint_t){.version=1, .generation=1, .phase=2, .crc=123};
    work = begin(false); complete(&work, &result);
    assert(result == FT_WORK_FAILED && content("active", original, sizeof(original)));
    puts("bounded file transactions and unchanged-reader recovery passed");
}
