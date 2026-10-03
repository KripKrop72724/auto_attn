#include "zkt_journal_store.h"
#include "durable_queue.h"
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static unsigned calls, fail_at;
static bool uncertain;
static bool fails(void) { return ++calls == fail_at; }
static FILE *fault_fopen(const char *path, const char *mode)
{
    if (fails()) { errno = EIO; return NULL; }
    return fopen(path, mode);
}
static int fault_open(const char *path, int flags, int mode)
{
    if (fails()) { errno = EIO; return -1; }
    return open(path, flags, mode);
}
static FILE *fault_fdopen(int fd, const char *mode)
{
    if (fails()) { errno = EIO; return NULL; }
    return fdopen(fd, mode);
}
static size_t fault_fwrite(const void *data, size_t size, size_t count, FILE *file)
{
    if (fails()) { (void)fwrite(data, size, count / 2, file); errno = EIO; return count / 2; }
    return fwrite(data, size, count, file);
}
static size_t fault_fread(void *data, size_t size, size_t count, FILE *file)
{
    if (fails()) { errno = EIO; return 0; }
    return fread(data, size, count, file);
}
static int fault_fflush(FILE *file)
{
    if (fails()) { errno = EIO; return EOF; }
    return fflush(file);
}
static int fault_fsync(int fd)
{
    if (fails()) { errno = EIO; return -1; }
    return fsync(fd);
}
static int fault_fclose(FILE *file)
{
    bool failed = fails();
    int result = fclose(file);
    if (failed) { errno = EIO; return EOF; }
    return result;
}
static int fault_fseek(FILE *file, long offset, int origin)
{
    if (fails()) { errno = EIO; return -1; }
    return fseek(file, offset, origin);
}
static int fault_stat(const char *path, struct stat *st)
{
    if (fails()) { errno = EIO; return -1; }
    return stat(path, st);
}
static int fault_unlink(const char *path)
{
    if (fails()) { errno = EIO; return -1; }
    return unlink(path);
}
static DIR *fault_opendir(const char *path)
{
    if (fails()) { errno = EIO; return NULL; }
    return opendir(path);
}
static struct dirent *fault_readdir(DIR *dir)
{
    if (fails()) { errno = EIO; return NULL; }
    return readdir(dir);
}
static int fault_closedir(DIR *dir)
{
    bool failed = fails();
    int result = closedir(dir);
    if (failed) { errno = EIO; return -1; }
    return result;
}
#define fopen fault_fopen
#define open(...) fault_open(__VA_ARGS__)
#define fdopen fault_fdopen
#define fwrite fault_fwrite
#define fread fault_fread
#define fflush fault_fflush
#define fsync fault_fsync
#define fclose fault_fclose
#define fseek fault_fseek
#define stat(...) fault_stat(__VA_ARGS__)
#define unlink fault_unlink
#define opendir fault_opendir
#define readdir fault_readdir
#define closedir fault_closedir
#include "zkt_journal_store.c"
#undef fopen
#undef open
#undef fdopen
#undef fwrite
#undef fread
#undef fflush
#undef fsync
#undef fclose
#undef fseek
#undef stat
#undef unlink
#undef opendir
#undef readdir
#undef closedir

typedef struct {
    uint64_t limit;
    bool exists, full;
    uint8_t checkpoint[ZJ_CHECKPOINT_BYTES];
} state_t;
static state_t state;
static const zj_metadata_t metadata = {.segment_id = 1, .capture_epoch = {1},
    .terminal_serial = "TEST-TERMINAL", .decoder_profile = "G3-v1", .decoder_version = "1"};
static int load(void *context, uint8_t *checkpoint)
{
    state_t *s = context;
    if (fails()) return -1;
    memcpy(checkpoint, s->checkpoint, ZJ_CHECKPOINT_BYTES);
    return s->exists;
}
static bool save(void *context, const uint8_t *checkpoint)
{
    state_t *s = context;
    bool failed = fails();
    if (!failed || uncertain) { memcpy(s->checkpoint, checkpoint, ZJ_CHECKPOINT_BYTES); s->exists = true; }
    return !failed;
}
static bool reserve(void *context, uint64_t limit)
{
    state_t *s = context;
    assert(limit > s->limit);
    bool failed = fails();
    if (!failed || uncertain) s->limit = limit;
    return !failed;
}
static bool admit(void *context, size_t bytes)
{
    state_t *s = context;
    assert(bytes <= ZJ_META_BYTES + ZJ_RECORD_MAX);
    return !s->full;
}
/* Deterministic fault port for filesystem tests. The real mbedTLS adapter has
 * separate independent AES-GCM vectors; this is deliberately not encryption. */
static void tag_for(const uint8_t *aad, size_t aad_length, const uint8_t *cipher,
                     size_t length, uint8_t tag[ZJ_TAG_BYTES])
{
    uint32_t value = dq_crc32(aad, aad_length) ^ dq_crc32(cipher, length);
    for (unsigned i = 0; i < ZJ_TAG_BYTES; ++i) tag[i] = (uint8_t)(value >> ((i % 4) * 8));
}
static bool seal(void *context, const uint8_t *meta, const uint8_t *nonce,
                 const uint8_t *aad, size_t aad_length, const uint8_t *plain,
                 size_t length, uint8_t *cipher, uint8_t *tag)
{
    (void)context; (void)meta; (void)nonce;
    for (size_t i = 0; i < length; ++i) cipher[i] = plain[i] ^ 0xa5;
    tag_for(aad, aad_length, cipher, length, tag);
    return true;
}
static bool open_record(void *context, const uint8_t *meta, const uint8_t *nonce,
                        const uint8_t *aad, size_t aad_length, const uint8_t *cipher,
                        size_t length, const uint8_t *tag, uint8_t *plain)
{
    uint8_t expected[ZJ_TAG_BYTES];
    tag_for(aad, aad_length, cipher, length, expected);
    if (memcmp(expected, tag, ZJ_TAG_BYTES)) return false;
    return seal(context, meta, nonce, aad, aad_length, cipher, length, plain, expected);
}
static bool fake_digest(void *context, const uint8_t *bytes, size_t length, uint8_t out[32])
{
    (void)context;
    uint32_t value = dq_crc32(bytes, length);
    for (unsigned i = 0; i < 32; ++i) out[i] = (uint8_t)(value >> ((i % 4) * 8));
    return true;
}
static zj_store_port_t port(void)
{
    return (zj_store_port_t){load, save, reserve, admit, &state, {seal, open_record, fake_digest, NULL}};
}
static void clean(void)
{
    DIR *dir = opendir(".");
    assert(dir);
    struct dirent *entry;
    while ((entry = readdir(dir))) if (!strncmp(entry->d_name, "fault-journal-", 14)) assert(!unlink(entry->d_name));
    assert(!closedir(dir));
}
static void reset(zj_store_t *store)
{
    fail_at = calls = 0;
    uncertain = false;
    clean();
    memset(&state, 0, sizeof(state));
    state.limit = 1;
    assert(zj_store_open(store, "fault-journal-", &metadata, state.limit, port()) == ZJ_OK);
}
static void reopen(zj_store_t *store)
{
    fail_at = 0;
    assert(zj_store_open(store, "fault-journal-", &metadata, state.limit, port()) == ZJ_OK);
}
static zj_result_t append(zj_store_t *store, unsigned char value, unsigned raw_length)
{
    zj_observation_t observation = {.raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
        .source_ordinal = UINT32_MAX, .raw_length = (uint16_t)raw_length};
    memset(observation.raw, value, raw_length);
    uint64_t sequence;
    return zj_store_append(store, &observation, &sequence);
}
static void settle(zj_store_t *store, const zj_item_t *item)
{
    const uint8_t simulated_committed_add_receipt[32] = {1};
    assert(zj_store_settle(store, &item->token, simulated_committed_add_receipt) == ZJ_OK);
}
static unsigned drain(zj_store_t *store, unsigned counts[256])
{
    zj_item_t item;
    unsigned exceptions = 0;
    zj_result_t result;
    while ((result = zj_store_peek(store, &item)) == ZJ_OK) {
        if (item.kind == ZJ_OBSERVATION) ++counts[item.observation.raw[0]];
        else { assert(item.exception_length); ++exceptions; }
        settle(store, &item);
    }
    assert(result == ZJ_EMPTY);
    return exceptions;
}
static void damaged_checkpoint(zj_store_t *store, uint8_t damaged[ZJ_CHECKPOINT_BYTES], unsigned byte)
{
    reset(store);
    assert(append(store, 'A', 40) == ZJ_OK);
    assert(append(store, 'B', 40) == ZJ_OK);
    zj_item_t item;
    assert(zj_store_peek(store, &item) == ZJ_OK);
    settle(store, &item);
    state.checkpoint[byte] ^= 1;
    memcpy(damaged, state.checkpoint, ZJ_CHECKPOINT_BYTES);
}
static void check_replay(zj_store_t *store, const uint8_t damaged[ZJ_CHECKPOINT_BYTES])
{
    assert(store->checkpoint_recovery_pending);
    unsigned retained = 0;
    for (unsigned i = 0; i < store->count; ++i) if (store->segments[i].size) ++retained;
    zj_result_t reclaimed = zj_store_reclaim_step(store);
    assert(reclaimed == ZJ_EMPTY || reclaimed == ZJ_OK); /* A failed open can leave an empty file. */
    unsigned after = 0;
    for (unsigned i = 0; i < store->count; ++i) if (store->segments[i].size) ++after;
    assert(retained == after);
    unsigned observations[256] = {0}, evidence = 0;
    zj_item_t item;
    zj_result_t result;
    while ((result = zj_store_peek(store, &item)) == ZJ_OK) {
        if (item.kind == ZJ_OBSERVATION) {
            ++observations[item.observation.raw[0]];
            assert(item.observation.sequence == (item.observation.raw[0] == 'A' ? 2U : 3U));
        } else if (item.exception == ZJ_EXCEPTION_CHECKPOINT) {
            assert(item.exception_length == CHECKPOINT_EVIDENCE_BYTES);
            assert(!memcmp(item.exception_bytes, "ZJCPE001", 8));
            assert(!memcmp(item.exception_bytes + 8, damaged, ZJ_CHECKPOINT_BYTES));
            ++evidence;
        }
        settle(store, &item);
    }
    assert(result == ZJ_EMPTY && observations['A'] == 1 && observations['B'] == 1 && evidence == 1);
    assert(!store->checkpoint_recovery_pending);
    uint64_t reserved = state.limit;
    reopen(store);
    assert(!store->checkpoint_recovery_pending && zj_store_peek(store, &item) == ZJ_EMPTY);
    assert(append(store, 'C', 40) == ZJ_OK);
    assert(zj_store_peek(store, &item) == ZJ_OK && item.observation.sequence > reserved);
}
static void checkpoint_recovery_tests(zj_store_t *store)
{
    uint8_t damaged[ZJ_CHECKPOINT_BYTES];
    for (unsigned byte = 0; byte < ZJ_CHECKPOINT_BYTES; ++byte) {
        damaged_checkpoint(store, damaged, byte);
        reopen(store);
        /* Recovery and its pending evidence survive another immediate reboot. */
        reopen(store);
        check_replay(store, damaged);
    }
    for (unsigned reused = 0; reused < 2; ++reused) {
        for (unsigned persisted = 0; persisted < 2; ++persisted) {
            for (unsigned boundary = 1; boundary <= 64; ++boundary) {
                damaged_checkpoint(store, damaged, 30);
                if (reused) {
                    reopen(store);
                    memcpy(state.checkpoint, damaged, sizeof(damaged));
                }
                calls = 0; fail_at = boundary; uncertain = persisted;
                zj_result_t result = zj_store_open(store, "fault-journal-", &metadata, state.limit, port());
                assert(result == ZJ_OK || !store->ready);
                reopen(store);
                check_replay(store, damaged);
            }
        }
    }
    damaged_checkpoint(store, damaged, 30);
    state.full = true;
    assert(zj_store_open(store, "fault-journal-", &metadata, state.limit, port()) == ZJ_FULL);
    assert(!store->ready && !memcmp(state.checkpoint, damaged, sizeof(damaged)));
    state.full = false;
    reopen(store);
    check_replay(store, damaged);
    /* A correctly checksummed but impossible cursor also replays safely. */
    damaged_checkpoint(store, damaged, 30);
    put32(state.checkpoint + 28, 0);
    put32(state.checkpoint + 24, UINT32_MAX);
    put32(state.checkpoint + 76, dq_crc32(state.checkpoint, 76));
    memcpy(damaged, state.checkpoint, sizeof(damaged));
    reopen(store);
    check_replay(store, damaged);
}
int main(void)
{
    static zj_store_t store;
    zj_item_t item;
    reset(&store);
    unsigned io_before = calls;
    for (unsigned i = 0; i < 100; ++i) assert(zj_store_peek(&store, &item) == ZJ_EMPTY);
    assert(calls == io_before);
    assert(append(&store, 'A', 40) == ZJ_OK);
    assert(append(&store, 'B', 40) == ZJ_OK);
    assert(zj_store_peek(&store, &item) == ZJ_OK && item.observation.raw[0] == 'A');
    zj_token_t old = item.token;
    settle(&store, &item);
    const uint8_t receipt[32] = {1};
    assert(zj_store_settle(&store, &old, receipt) == ZJ_STALE);
    state.full = true;
    assert(append(&store, 'C', 40) == ZJ_FULL);
    state.full = false;
    reopen(&store);
    assert(zj_store_peek(&store, &item) == ZJ_OK && item.observation.raw[0] == 'B');
    settle(&store, &item);
    io_before = calls;
    assert(zj_store_peek(&store, &item) == ZJ_EMPTY && calls == io_before);

    const char *failed_stages[] = {"record_write", "record_flush", "record_sync", "record_close"};
    for (unsigned stage = 0; stage < 4; ++stage) {
        reset(&store);
        assert(append(&store, 'A', 40) == ZJ_OK);
        calls = 0;
        fail_at = stage + 3; /* open, seek, write, flush, sync, close */
        assert(append(&store, 'B', 40) == ZJ_UNCERTAIN);
        assert(store.last_errno == EIO && !strcmp(store.last_operation, failed_stages[stage]));
        reopen(&store);
        assert(zj_store_peek(&store, &item) == ZJ_OK && item.observation.raw[0] == 'A');
    }

    for (unsigned target = 1; target <= 30; ++target) {
        reset(&store);
        assert(append(&store, 'A', 40) == ZJ_OK);
        calls = 0; fail_at = target;
        zj_result_t result = zj_store_open(&store, "fault-journal-", &metadata, state.limit, port());
        assert(result == ZJ_OK || !store.ready);
        reopen(&store);
        assert(zj_store_peek(&store, &item) == ZJ_OK && item.observation.raw[0] == 'A');
        assert(zj_store_reclaim_step(&store) == ZJ_EMPTY);
    }

    /* Restart at every write/open/sync/close/checkpoint boundary. A failed
     * append may exist, or leave preserved opaque bytes, but earlier successful
     * captures always remain available. No failed reservation is reused. */
    for (unsigned persisted = 0; persisted < 2; ++persisted) {
        for (unsigned target = 1; target <= 30; ++target) {
            reset(&store);
            assert(append(&store, 'A', 40) == ZJ_OK);
            assert(append(&store, 'B', 40) == ZJ_OK);
            reopen(&store); /* Force a fresh segment rotation. */
            uncertain = persisted;
            calls = 0; fail_at = target;
            zj_result_t result = append(&store, 'C', 40);
            reopen(&store);
            assert(append(&store, 'D', 40) == ZJ_OK);
            unsigned counts[256] = {0};
            drain(&store, counts);
            assert(counts['A'] == 1 && counts['B'] == 1 && counts['D'] == 1);
            assert(counts['C'] <= 1 && (result != ZJ_OK || counts['C'] == 1));
        }
        for (unsigned target = 1; target <= 35; ++target) {
            reset(&store);
            assert(append(&store, 'A', 40) == ZJ_OK);
            assert(append(&store, 'B', 40) == ZJ_OK);
            assert(zj_store_peek(&store, &item) == ZJ_OK);
            uncertain = persisted;
            calls = 0; fail_at = target;
            (void)zj_store_settle(&store, &item.token, receipt);
            reopen(&store);
            unsigned counts[256] = {0};
            drain(&store, counts);
            assert(counts['A'] <= 1 && counts['B'] == 1);
        }
    }
    /* An authentic record after a corrupt record in the same segment remains
     * an observation. The corrupt prefix receives its own opaque-byte custody. */
    reset(&store);
    assert(append(&store, 'A', 40) == ZJ_OK);
    assert(append(&store, 'B', 40) == ZJ_OK);
    char path[144];
    assert(filename(&store, store.segments[0].id, path));
    FILE *f = fopen(path, "r+b");
    assert(f && !fseek(f, ZJ_META_BYTES + ZJ_HEADER_BYTES, SEEK_SET));
    int byte = fgetc(f);
    assert(byte != EOF && !fseek(f, -1, SEEK_CUR) && fputc(byte ^ 1, f) != EOF && !fclose(f));
    reopen(&store);
    unsigned counts[256] = {0};
    assert(drain(&store, counts) >= 1 && !counts['A'] && counts['B'] == 1);
    /* Corrupted retirement state never formats or removes retained evidence. */
    state.checkpoint[30] ^= 1;
    reopen(&store);
    assert(store.checkpoint_recovery_pending);
    struct stat st;
    assert(!stat(path, &st) && st.st_size > ZJ_META_BYTES);
    checkpoint_recovery_tests(&store);
    /* Rotations are append-only, and each GC step removes at most one file. */
    reset(&store);
    for (unsigned i = 0; i < 250; ++i) assert(append(&store, 'R', ZJ_RAW_MAX) == ZJ_OK);
    assert(store.count >= 3);
    reopen(&store);
    memset(counts, 0, sizeof(counts));
    assert(!drain(&store, counts) && counts['R'] == 250);
    while (store.count) {
        unsigned before = store.count;
        assert(zj_store_reclaim_step(&store) == ZJ_OK && store.count + 1 == before);
    }
    assert(zj_store_reclaim_step(&store) == ZJ_EMPTY);
    reopen(&store);
    assert(zj_store_peek(&store, &item) == ZJ_EMPTY);
    clean();
    puts("Journal rotation, custody retirement, opaque recovery, and injected I/O failures passed");
    return 0;
}
