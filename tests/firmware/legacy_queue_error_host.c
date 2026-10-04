#include "legacy_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
static bool fail_read, fail_close;
static unsigned commits;
static lq_checkpoint_t saved;
static size_t read_port(void *bytes, size_t size, size_t count, FILE *file)
{ if (fail_read) { errno = ENXIO; return 0; } return fread(bytes, size, count, file); }
static int close_port(FILE *file)
{ int result = fclose(file); errno = fail_close ? ENOSPC : EBUSY; return fail_close ? EOF : result; }
#define fread read_port
#define fclose close_port
#include "legacy_queue.c"
#undef fread
#undef fclose
static int load(void *context, lq_checkpoint_t *out)
{ (void)context; *out = saved; return saved.version != 0; }
static bool commit(void *context, const lq_checkpoint_t *in)
{ (void)context; ++commits; saved = *in; return true; }
int main(void)
{
    FILE *file = fopen("records", "wb");
    assert(file && fwrite("first\nlast\n", 1, 11, file) == 11 && fclose(file) == 0);
    legacy_queue_t queue = {0};
    lq_port_t port = {load, commit, NULL};
    assert(lq_open_step(&queue, "records", port) == DQ_OK);
    char bytes[32]; lq_token_t token;
    assert(lq_peek(&queue, bytes, sizeof(bytes), &token) == DQ_OK);
    fail_read = fail_close = true;
    assert(lq_settle(&queue, &token) == DQ_IO && errno == ENXIO && !commits && !saved.offset);
    fail_read = false;
    assert(lq_settle(&queue, &token) == DQ_IO && errno == ENOSPC && !commits);
    assert(lq_peek(&queue, bytes, sizeof(bytes), &token) == DQ_IO && errno == ENOSPC);
    fail_close = false;
    assert(lq_peek(&queue, bytes, sizeof(bytes), &token) == DQ_OK);
    lq_token_t wrong = token; wrong.crc ^= 1;
    assert(lq_settle(&queue, &wrong) == DQ_STALE && !commits);
    assert(lq_settle(&queue, &token) == DQ_OK && commits == 1 && saved.offset == 6);
    memset(&queue, 0, sizeof(queue));
    fail_read = fail_close = true;
    assert(lq_open_step(&queue, "records", port) == DQ_IO && errno == ENXIO && !queue.recovery_offset);
    fail_read = false;
    assert(lq_open_step(&queue, "records", port) == DQ_IO && errno == ENOSPC && !queue.recovery_offset);
    fail_close = false;
    assert(lq_open_step(&queue, "records", port) == DQ_OK && queue.checkpoint.offset == 6);
    assert(lq_peek(&queue, bytes, sizeof(bytes), &token) == DQ_OK && !strcmp(bytes, "last\n"));
}
