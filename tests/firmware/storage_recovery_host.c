/* Host regression for the one-shot blocked-queue custody transfer. Executes
 * storage_recovery_core.c against real files with fault-injecting ports and
 * injected unreadable byte ranges (fread/ferror/fclose are redirected for the
 * production unit by the test command line). */
#undef fread
#undef ferror
#undef fclose
#undef fseek
#include "storage_recovery_core.h"

#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define BLOCKED "blocked_identity.jsonl"
#define BACKUP "blocked_recovery.bak"
#define TMP "blocked_recovery.tmp"
#define ACKED "acked_uids.txt"
#define MAX_SENT 256

typedef struct {
    char generation[80], record_id[80], serial[128], reason[32];
    char bytes[SR_RECORD_MAX_BYTES + 1];
    size_t length;
} sent_t;

typedef struct {
    sent_t sent[MAX_SENT];
    unsigned count, attempts;
    unsigned retry_first;     /* RETRY this many attempts before acknowledging */
    int stop_at;              /* STOP when this many rows were acknowledged */
    int append_at;            /* Append to BLOCKED after this many rows */
    bool refuse_retire, retired_called;
    unsigned logs;
    long arm_bad_start, arm_bad_end; /* Fault armed at the first send */
} fake_t;

static const char *const k_sources[] = {BLOCKED, BACKUP, TMP};

/* Unreadable byte range of the injected fault, armed per test. With
 * g_bad_seek the range also cannot be reached by a seek, like a SPIFFS span
 * whose object index page is unreadable. */
static long g_bad_start = -1, g_bad_end = -1;
static bool g_bad_seek;
static FILE *g_faulted[8];

static void mark_faulted(FILE *file, bool faulted)
{
    for (unsigned i = 0; i < 8; ++i) {
        if (faulted && !g_faulted[i]) { g_faulted[i] = file; return; }
        if (!faulted && g_faulted[i] == file) g_faulted[i] = NULL;
    }
}

size_t sr_test_fread(void *buffer, size_t size, size_t count, FILE *file)
{
    long position = ftell(file);
    long end = position + (long)(size * count);
    if (g_bad_start >= 0 && position < g_bad_end && end > g_bad_start) {
        mark_faulted(file, true);
        return 0;
    }
    mark_faulted(file, false);
    return fread(buffer, size, count, file);
}

int sr_test_ferror(FILE *file)
{
    for (unsigned i = 0; i < 8; ++i)
        if (g_faulted[i] == file) return 1;
    return ferror(file);
}

int sr_test_fclose(FILE *file)
{
    mark_faulted(file, false);
    return fclose(file);
}

int sr_test_fseek(FILE *file, long offset, int whence)
{
    if (g_bad_seek && whence == SEEK_SET && offset >= g_bad_start && offset < g_bad_end) {
        errno = ENOENT;
        return -1;
    }
    return fseek(file, offset, whence);
}

static char *read_all(const char *path, size_t *length)
{
    FILE *file = fopen(path, "rb");
    if (!file) { *length = 0; return NULL; }
    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    fseek(file, 0, SEEK_SET);
    char *data = malloc((size_t)size + 1);
    assert(data && fread(data, 1, (size_t)size, file) == (size_t)size);
    data[size] = 0;
    fclose(file);
    *length = (size_t)size;
    return data;
}

static void write_all(const char *path, const char *data)
{
    FILE *file = fopen(path, "wb");
    assert(file && fputs(data, file) >= 0 && fclose(file) == 0);
}

static bool exists(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0;
}

static sr_send_t fake_send(void *context, const char *generation, const char *record_id,
                           const char *bytes, size_t length, const char *serial, const char *reason)
{
    fake_t *fake = context;
    fake->attempts++;
    if (fake->arm_bad_start >= 0) {
        g_bad_start = fake->arm_bad_start;
        g_bad_end = fake->arm_bad_end;
        fake->arm_bad_start = -1;
    }
    if (fake->stop_at >= 0 && (int)fake->count == fake->stop_at) return SR_SEND_STOP;
    if (fake->retry_first) { fake->retry_first--; return SR_SEND_RETRY; }
    if (fake->append_at >= 0 && (int)fake->count == fake->append_at) {
        FILE *file = fopen(BLOCKED, "ab");
        assert(file && fputs("{\"late\":true}\n", file) >= 0 && fclose(file) == 0);
    }
    assert(fake->count < MAX_SENT && length <= SR_RECORD_MAX_BYTES);
    sent_t *row = &fake->sent[fake->count++];
    snprintf(row->generation, sizeof(row->generation), "%s", generation);
    snprintf(row->record_id, sizeof(row->record_id), "%s", record_id);
    snprintf(row->serial, sizeof(row->serial), "%s", serial ? serial : "");
    snprintf(row->reason, sizeof(row->reason), "%s", reason);
    memcpy(row->bytes, bytes, length);
    row->bytes[length] = 0;
    row->length = length;
    return SR_SEND_ACKED;
}

static void fake_log(void *context, const char *level, const char *code, const char *message)
{
    fake_t *fake = context;
    assert(level && code && message && strlen(message) < 220);
    fake->logs++;
}

static bool fake_before_retire(void *context)
{
    fake_t *fake = context;
    fake->retired_called = true;
    /* Nothing may be removed before every row is acknowledged. */
    assert(exists(BLOCKED));
    return !fake->refuse_retire;
}

static sr_ports_t ports_for(fake_t *fake)
{
    sr_ports_t ports = {.context = fake, .send = fake_send, .log = fake_log, .before_retire = fake_before_retire};
    return ports;
}

static fake_t *new_fake(void)
{
    fake_t *fake = calloc(1, sizeof(*fake));
    assert(fake);
    fake->stop_at = -1;
    fake->append_at = -1;
    fake->arm_bad_start = -1;
    g_bad_start = g_bad_end = -1;
    g_bad_seek = false;
    return fake;
}

#define UID_A "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
#define UID_B "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
#define UID_C "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
static const char *k_blocked =
    "{\"cnic\":\"\",\"device_serial\":\"CJH9211060009\",\"event_uid\":\"" UID_A "\",\"user_id\":\"7\"}\n"
    "\n"
    "{not json at all\n"
    "{\"device_serial\":\"CJH9211060009\",\"event_uid\":\"" UID_B "\",\"user_id\":\"8\"}\r\n"
    "{\"device_serial\":\"CJH9211060009\",\"event_uid\":\"" UID_C "\",\"user\":\"tail\"}";
static const char *k_backup = "{\"device_serial\":\"CJH9211060009\",\"event_uid\":\"" UID_A "\",\"user_id\":\"7\"}\n";
static const char *k_acked_torn = UID_C "\nabc123";

static void seed(void)
{
    remove(BLOCKED); remove(BACKUP); remove(TMP); remove(ACKED);
    write_all(BLOCKED, k_blocked);
    write_all(BACKUP, k_backup);
    write_all(ACKED, k_acked_torn);
}

static void assert_unchanged(void)
{
    size_t length = 0;
    char *blocked = read_all(BLOCKED, &length);
    assert(blocked && !strcmp(blocked, k_blocked));
    char *backup = read_all(BACKUP, &length);
    assert(backup && !strcmp(backup, k_backup));
    char *acked = read_all(ACKED, &length);
    assert(acked && !strcmp(acked, k_acked_torn));
    free(blocked); free(backup); free(acked);
}

static void test_complete_transfer_retires_after_every_receipt(void)
{
    seed();
    fake_t *fake = new_fake();
    fake->retry_first = 2;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && !strcmp(outcome.code, "STORAGE_RECOVERY_COMPLETE"));
    assert(fake->retired_called && fake->count == 5 && outcome.records == 5 && outcome.retries == 2);
    assert(outcome.files == 2 && outcome.malformed == 1 && outcome.uids == 4 && outcome.uids_appended == 4);
    /* Exact bytes, offsets and stable labels; empty rows carry no data. */
    char generation[80];
    snprintf(generation, sizeof(generation), "storage-recovery-v1-0-%zu", strlen(k_blocked));
    assert(!strcmp(fake->sent[0].generation, generation));
    assert(!strncmp(fake->sent[0].record_id, "0:", 2));
    assert(!strcmp(fake->sent[0].serial, "CJH9211060009") && !strcmp(fake->sent[0].reason, "LEGACY_RECOVERY"));
    assert(!strcmp(fake->sent[1].bytes, "{not json at all") && !strcmp(fake->sent[1].reason, "MALFORMED"));
    assert(!fake->sent[1].serial[0]);
    assert(fake->sent[2].bytes[fake->sent[2].length - 1] == '\r');
    assert(!strcmp(fake->sent[2].reason, "LEGACY_RECOVERY"));
    assert(!strcmp(fake->sent[3].reason, "LEGACY_RECOVERY") && strstr(fake->sent[3].bytes, "tail"));
    const char *third = strstr(k_blocked, "{\"device_serial\":\"CJH9211060009\",\"event_uid\":\"" UID_B);
    char expected_id[32];
    snprintf(expected_id, sizeof(expected_id), "%ld:", (long)(third - k_blocked));
    assert(!strncmp(fake->sent[2].record_id, expected_id, strlen(expected_id)));
    assert(!strncmp(fake->sent[4].generation, "storage-recovery-v1-1-", 22));
    assert(!exists(BLOCKED) && !exists(BACKUP) && !exists(TMP));
    size_t length = 0;
    char *acked = read_all(ACKED, &length);
    /* The torn row is closed first; every retired UID is a separate row. */
    const char *expected = UID_C "\nabc123\n" UID_A "\n" UID_B "\n" UID_C "\n" UID_A "\n";
    assert(acked && !strcmp(acked, expected));
    free(acked);
    free(fake);
}

static void test_stop_leaves_every_file_unchanged(void)
{
    seed();
    fake_t *fake = new_fake();
    fake->stop_at = 2;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_INCOMPLETE && !strcmp(outcome.code, "STORAGE_RECOVERY_STOPPED"));
    assert(!fake->retired_called);
    assert_unchanged();
    free(fake);
}

static void test_refused_retirement_keeps_files(void)
{
    seed();
    fake_t *fake = new_fake();
    fake->refuse_retire = true;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_INCOMPLETE && !strcmp(outcome.code, "STORAGE_RECOVERY_PREPARE_RETIRE"));
    assert(fake->retired_called && fake->count == 5);
    assert_unchanged();
    free(fake);
}

static void test_changed_source_is_not_retired(void)
{
    seed();
    fake_t *fake = new_fake();
    fake->append_at = 1;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_INCOMPLETE && !strcmp(outcome.code, "STORAGE_RECOVERY_SOURCE_CHANGED"));
    assert(!fake->retired_called && exists(BLOCKED) && exists(BACKUP));
    size_t length = 0;
    char *acked = read_all(ACKED, &length);
    assert(acked && !strcmp(acked, k_acked_torn));
    free(acked);
    free(fake);
}

static void test_oversized_row_is_refused_without_change(void)
{
    remove(BLOCKED); remove(BACKUP); remove(TMP); remove(ACKED);
    char *large = malloc(SR_RECORD_MAX_BYTES + 16);
    assert(large);
    memset(large, 'x', SR_RECORD_MAX_BYTES + 1);
    large[SR_RECORD_MAX_BYTES + 1] = '\n';
    large[SR_RECORD_MAX_BYTES + 2] = 0;
    write_all(BLOCKED, large);
    fake_t *fake = new_fake();
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_REFUSED && !strcmp(outcome.code, "STORAGE_RECOVERY_ROW_TOO_LARGE"));
    assert(!fake->retired_called && fake->count == 0 && exists(BLOCKED) && !exists(ACKED));
    size_t length = 0;
    char *after = read_all(BLOCKED, &length);
    assert(after && !strcmp(after, large));
    free(after); free(large); free(fake);
}

static void test_exact_limit_row_and_absent_files(void)
{
    remove(BLOCKED); remove(BACKUP); remove(TMP); remove(ACKED);
    fake_t *fake = new_fake();
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_NOTHING_TO_DO && fake->count == 0 && !exists(ACKED));
    write_all(TMP, "");
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_NOTHING_TO_DO && exists(TMP));
    remove(TMP);
    char *limit = malloc(SR_RECORD_MAX_BYTES + 2);
    assert(limit);
    memset(limit, 'y', SR_RECORD_MAX_BYTES);
    limit[SR_RECORD_MAX_BYTES] = '\n';
    limit[SR_RECORD_MAX_BYTES + 1] = 0;
    write_all(BLOCKED, limit);
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && fake->count == 1 && fake->sent[0].length == SR_RECORD_MAX_BYTES);
    assert(outcome.uids == 0 && !exists(BLOCKED) && !exists(ACKED));
    free(limit); free(fake);
}

static void test_repeated_runs_reuse_identical_custody_identities(void)
{
    seed();
    fake_t *first = new_fake();
    first->refuse_retire = true;
    sr_ports_t ports = ports_for(first);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    fake_t *second = new_fake();
    ports = ports_for(second);
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && first->count == second->count);
    for (unsigned i = 0; i < first->count; ++i) {
        assert(!strcmp(first->sent[i].generation, second->sent[i].generation));
        assert(!strcmp(first->sent[i].record_id, second->sent[i].record_id));
        assert(first->sent[i].length == second->sent[i].length &&
               !memcmp(first->sent[i].bytes, second->sent[i].bytes, first->sent[i].length));
    }
    free(first); free(second);
}

/* Rows of 100 bytes: {"device_serial":"CJH9211060009","event_uid":"<64 hex>","n":"NN"} padded. */
static char *write_rows(unsigned rows)
{
    size_t capacity = (size_t)rows * 128 + 1;
    char *data = calloc(1, capacity);
    assert(data);
    size_t used = 0;
    for (unsigned i = 0; i < rows; ++i) {
        char uid[65];
        for (unsigned b = 0; b < 64; ++b) uid[b] = "0123456789abcdef"[(i * 7 + b) % 16];
        uid[64] = 0;
        used += (size_t)snprintf(data + used, capacity - used,
                                 "{\"device_serial\":\"CJH9211060009\",\"event_uid\":\"%s\",\"n\":\"%05u\"}\n", uid, i);
    }
    remove(BLOCKED); remove(BACKUP); remove(TMP); remove(ACKED);
    write_all(BLOCKED, data);
    return data;
}

static unsigned count_with_prefix(const fake_t *fake, const char *prefix)
{
    unsigned count = 0;
    for (unsigned i = 0; i < fake->count; ++i)
        if (!strncmp(fake->sent[i].record_id, prefix, strlen(prefix))) count++;
    return count;
}

static void test_unreadable_region_is_skipped_reported_and_bounded(void)
{
    char *data = write_rows(60);
    size_t total = strlen(data);
    fake_t *clean = new_fake();
    clean->refuse_retire = true;
    sr_ports_t ports = ports_for(clean);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(clean->count == 60 && outcome.unreadable_bytes == 0);
    fake_t *fake = new_fake();
    g_bad_start = 2500;
    g_bad_end = 2700;
    ports = ports_for(fake);
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && outcome.gaps == 1);
    /* Probes resolve the region to 32-byte pieces around the bad bytes. */
    assert(outcome.unreadable_bytes >= 200 && outcome.unreadable_bytes <= 264);
    assert(count_with_prefix(fake, "gap:") == 1);
    const sent_t *gap = NULL;
    for (unsigned i = 0; i < fake->count; ++i)
        if (!strncmp(fake->sent[i].record_id, "gap:", 4)) gap = &fake->sent[i];
    assert(gap && strstr(gap->bytes, "\"type\":\"storage_recovery_unreadable\"") && !strcmp(gap->reason, "MALFORMED"));
    /* Every intact row keeps the exact identity of the clean run. */
    unsigned intact = 0;
    for (unsigned i = 0; i < clean->count; ++i) {
        long offset = atol(clean->sent[i].record_id);
        bool damaged = offset < 2720 && offset + (long)clean->sent[i].length > 2496;
        if (damaged) continue;
        bool found = false;
        for (unsigned j = 0; j < fake->count && !found; ++j)
            found = !strcmp(fake->sent[j].record_id, clean->sent[i].record_id);
        assert(found);
        intact++;
    }
    assert(intact >= 57);
    assert(outcome.malformed >= 1 && outcome.uids == intact);
    assert(!exists(BLOCKED));
    size_t length = 0;
    char *acked = read_all(ACKED, &length);
    assert(acked && length == (size_t)intact * 65);
    (void)total;
    free(acked); free(data); free(clean); free(fake);
}

static void test_unreadable_over_limit_refuses_before_sending(void)
{
    char *data = write_rows(400);
    fake_t *fake = new_fake();
    g_bad_start = 4000;
    g_bad_end = 4000 + (long)SR_UNREADABLE_LIMIT + 1024;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_REFUSED && !strcmp(outcome.code, "STORAGE_RECOVERY_UNREADABLE_LIMIT"));
    assert(fake->count == 0 && !fake->retired_called && !exists(ACKED));
    size_t length = 0;
    char *after = read_all(BLOCKED, &length);
    assert(after && !strcmp(after, data));
    free(after); free(data); free(fake);
}

static void test_region_seen_only_by_transfer_pass_is_not_retired(void)
{
    char *data = write_rows(60);
    fake_t *fake = new_fake();
    fake->arm_bad_start = 6000; /* Past the first 4096-byte read */
    fake->arm_bad_end = 6100;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_INCOMPLETE && !strcmp(outcome.code, "STORAGE_RECOVERY_SOURCE_CHANGED"));
    assert(!fake->retired_called && exists(BLOCKED) && !exists(ACKED));
    free(data); free(fake);
}

static void test_unreachable_offsets_are_skipped_like_unreadable_bytes(void)
{
    char *data = write_rows(80);
    fake_t *fake = new_fake();
    g_bad_start = 4200;
    g_bad_end = 4500;
    g_bad_seek = true;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && outcome.gaps == 1);
    assert(outcome.unreadable_bytes >= 300 && outcome.unreadable_bytes <= 364);
    assert(count_with_prefix(fake, "gap:") == 1 && !exists(BLOCKED));
    free(data); free(fake);
}

static void test_unreadable_tail_reaches_eof(void)
{
    char *data = write_rows(30);
    long total = (long)strlen(data);
    fake_t *fake = new_fake();
    g_bad_start = total - 90;
    g_bad_end = total;
    sr_ports_t ports = ports_for(fake);
    sr_outcome_t outcome;
    sr_transfer_and_retire(k_sources, 3, ACKED, &ports, &outcome);
    assert(outcome.result == SR_COMPLETE && outcome.gaps == 1 && outcome.unreadable_bytes <= 128);
    assert(outcome.uids == 29 && !exists(BLOCKED));
    free(data); free(fake);
}

int main(void)
{
    test_complete_transfer_retires_after_every_receipt();
    test_stop_leaves_every_file_unchanged();
    test_refused_retirement_keeps_files();
    test_changed_source_is_not_retired();
    test_oversized_row_is_refused_without_change();
    test_exact_limit_row_and_absent_files();
    test_repeated_runs_reuse_identical_custody_identities();
    test_unreadable_region_is_skipped_reported_and_bounded();
    test_unreadable_over_limit_refuses_before_sending();
    test_region_seen_only_by_transfer_pass_is_not_retired();
    test_unreachable_offsets_are_skipped_like_unreadable_bytes();
    test_unreadable_tail_reaches_eof();
    puts("storage recovery custody tests passed");
    return 0;
}
