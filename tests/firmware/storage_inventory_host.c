/* Host harness for storage_inventory_core.c: a scan maps unreadable bytes
 * without changing anything, and never stops at the first damaged page. It
 * runs twice: with the host's off_t and with the ESP32-S3's 32-bit one. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "storage_inventory.h"

#define CHUNK_BYTES (SI_CHUNK_PAGES * SI_PAGE_BYTES)

static struct {
    FILE *stream;
    char path[512];
    long offset;
    int failed;
} open_files[8];
static struct {
    const char *name;
    long start, end;
} bad[8];
static unsigned bad_count;
static const char *unopenable, *shrunk, *resized, *changed;
static long long resized_bytes, changed_by;
static unsigned budget_reads, reads, resized_opens, changed_stats;

static int slot_of(FILE *stream)
{
    for (int i = 0; i < 8; ++i)
        if (open_files[i].stream == stream) return i;
    assert(!"untracked stream");
    return -1;
}

FILE *si_test_fopen(const char *path, const char *mode)
{
    assert(!strcmp(mode, "rb")); /* the scan only ever reads */
    if (unopenable && strstr(path, unopenable)) {
        errno = ENOENT;
        return NULL;
    }
    if (resized && strstr(path, resized)) ++resized_opens;
    FILE *stream = fopen(path, mode);
    if (!stream) return NULL;
    for (int i = 0; i < 8; ++i) {
        if (!open_files[i].stream) {
            open_files[i].stream = stream;
            snprintf(open_files[i].path, sizeof(open_files[i].path), "%s", path);
            open_files[i].offset = 0;
            open_files[i].failed = 0;
            return stream;
        }
    }
    assert(!"too many open files");
    return NULL;
}

size_t si_test_fread(void *buffer, size_t size, size_t count, FILE *stream)
{
    int i = slot_of(stream);
    long length = (long)(size * count);
    ++reads;
    for (unsigned b = 0; b < bad_count; ++b) {
        if (strstr(open_files[i].path, bad[b].name) && open_files[i].offset < bad[b].end &&
            open_files[i].offset + length > bad[b].start) {
            open_files[i].failed = 1;
            errno = EIO;
            return 0;
        }
    }
    /* The shrunk file ends at 512 bytes, whatever its size says. */
    if (shrunk && strstr(open_files[i].path, shrunk)) {
        if (open_files[i].offset >= 512) return 0;
        if (open_files[i].offset + length > 512) count = (size_t)(512 - open_files[i].offset) / size;
    }
    size_t got = fread(buffer, size, count, stream);
    open_files[i].offset += (long)(got * size);
    return got;
}

int si_test_fseek(FILE *stream, long offset, int whence)
{
    assert(whence == SEEK_SET);
    open_files[slot_of(stream)].offset = offset;
    return fseek(stream, offset, whence);
}

int si_test_ferror(FILE *stream)
{
    return open_files[slot_of(stream)].failed || ferror(stream);
}

void si_test_clearerr(FILE *stream)
{
    open_files[slot_of(stream)].failed = 0;
    clearerr(stream);
}

int si_test_fclose(FILE *stream)
{
    open_files[slot_of(stream)].stream = NULL;
    return fclose(stream);
}

long long si_test_size(const char *path, long long bytes)
{
    /* The changed file's size moves by changed_by after its first stat. */
    if (changed && strstr(path, changed) && changed_stats++) return bytes + changed_by;
    return resized && strstr(path, resized) ? resized_bytes : bytes;
}

static bool within_budget(void *context)
{
    (void)context;
    return budget_reads == 0 || --budget_reads > 0;
}

static void write_file(const char *directory, const char *name, size_t bytes)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", directory, name);
    FILE *file = fopen(path, "wb");
    assert(file);
    for (size_t i = 0; i < bytes; ++i) fputc('a' + (int)(i % 26), file);
    assert(fclose(file) == 0);
}

static const si_file_t *find(const si_report_t *report, const char *name)
{
    for (size_t i = 0; i < report->count; ++i)
        if (!strcmp(report->file[i].name, name)) return &report->file[i];
    return NULL;
}

static void subdirectory(char *path, size_t size, const char *directory, const char *name)
{
    snprintf(path, size, "%s/%s", directory, name);
    assert(mkdir(path, 0700) == 0);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    const char *directory = argv[1];
    si_report_t report;

    /* A healthy store reports every file's exact size and nothing unreadable.
     * On the device 2.7.4 reported each file as 4294967295 bytes, unreadable
     * past its real end (13FL, 16 October 2026: 4 files, 17179869180 bytes). */
    char clean[512];
    subdirectory(clean, sizeof(clean), directory, "clean");
    write_file(clean, "empty.txt", 0);
    write_file(clean, "page.bin", SI_PAGE_BYTES);
    write_file(clean, "chunk.bin", CHUNK_BYTES);
    write_file(clean, "segment.jsonl", 3 * CHUNK_BYTES + 77);
    reads = 0;
    si_scan(clean, NULL, &report);
    assert(report.done && report.complete && !report.error);
    assert(report.files == 4 && report.count == 4);
    assert(report.bytes == SI_PAGE_BYTES + CHUNK_BYTES + 3 * CHUNK_BYTES + 77);
    assert(!report.unreadable_files && !report.unreadable_bytes);
    assert(!strcmp(report.file[0].name, "segment.jsonl") && report.file[0].bytes == 3 * CHUNK_BYTES + 77);
    assert(!strcmp(report.file[1].name, "chunk.bin") && report.file[1].bytes == CHUNK_BYTES);
    assert(!strcmp(report.file[2].name, "page.bin") && report.file[2].bytes == SI_PAGE_BYTES);
    assert(!strcmp(report.file[3].name, "empty.txt") && report.file[3].bytes == 0);
    for (size_t i = 0; i < report.count; ++i) assert(!report.file[i].first_error && !report.file[i].unreadable_bytes);
    /* A file that reads back is read a chunk at a time, not a page at a time:
     * 6 reads and budget checks here instead of 66. (newlib on the device
     * still fills an unbuffered stream one byte per read() underneath.) */
    assert(reads == 1 + 1 + 4);

    write_file(directory, "blocked_identity.jsonl", 4000);
    write_file(directory, "acked_uids.txt", 600);
    write_file(directory, "persistence-probe", 32);
    write_file(directory, "big.bin", 10000);
    write_file(directory, "pending_events.jsonl", 2000);
    char nested[512];
    snprintf(nested, sizeof(nested), "%s/nested", directory);
    assert(mkdir(nested, 0700) == 0);

    /* A damaged region is mapped page by page and the read continues past it;
     * a file that cannot be opened is wholly unreadable; a file that ends
     * before its size is missing the rest. */
    bad[bad_count++] = (typeof(bad[0])){"blocked_identity.jsonl", 1000, 1300};
    unopenable = "persistence-probe";
    shrunk = "pending_events.jsonl";
    si_scan(directory, NULL, &report);
    assert(report.done && report.complete && !report.error);
    assert(report.files == 5 && report.count == 5 && report.unreadable_files == 3);
    assert(report.bytes == 4000 + 600 + 32 + 10000 + 2000);
    const si_file_t *blocked = find(&report, "blocked_identity.jsonl");
    assert(blocked && blocked->bytes == 4000 && blocked->first_error == EIO);
    assert(blocked->first_error_offset == 768 && blocked->unreadable_bytes == 768);
    const si_file_t *probe = find(&report, "persistence-probe");
    assert(probe && probe->first_error == ENOENT && probe->first_error_offset == 0 && probe->unreadable_bytes == 32);
    const si_file_t *pending = find(&report, "pending_events.jsonl");
    assert(pending && pending->first_error == ENODATA && pending->first_error_offset == 512);
    assert(pending->unreadable_bytes == 2000 - 512);
    assert(report.unreadable_bytes == 768 + 32 + 1488);
    /* Unreadable files first, then the largest. */
    assert(!strcmp(report.file[0].name, "blocked_identity.jsonl") && !strcmp(report.file[1].name, "pending_events.jsonl"));
    assert(!strcmp(report.file[2].name, "persistence-probe") && !strcmp(report.file[3].name, "big.bin"));
    assert(!strcmp(report.file[4].name, "acked_uids.txt"));
    assert(!find(&report, "nested") && !find(&report, "clean"));
    for (int i = 0; i < 8; ++i) assert(!open_files[i].stream); /* every stream closed */

    /* A budget that runs out leaves an incomplete scan, never a false clean one. */
    budget_reads = 10;
    const si_ports_t ports = {.keep_going = within_budget};
    si_scan(directory, &ports, &report);
    assert(report.done && !report.complete);
    budget_reads = 0;

    /* At most 16 files are reported, unreadable ones always among them. */
    for (int i = 0; i < 20; ++i) {
        char name[32];
        snprintf(name, sizeof(name), "segment-%02d.bin", i);
        write_file(directory, name, (size_t)(5000 + i));
    }
    si_scan(directory, NULL, &report);
    assert(report.complete && report.files == 25 && report.count == SI_MAX_FILES && report.unreadable_files == 3);
    assert(find(&report, "blocked_identity.jsonl") && find(&report, "persistence-probe") &&
           find(&report, "pending_events.jsonl") && find(&report, "big.bin") && !find(&report, "acked_uids.txt"));

    /* Damage across a chunk boundary is mapped page by page, as is a damaged
     * byte in a later chunk; whole chunks resume once a chunk reads back. */
    char spans[512];
    subdirectory(spans, sizeof(spans), directory, "spans");
    write_file(spans, "journal.bin", 6 * CHUNK_BYTES);
    bad[bad_count++] = (typeof(bad[0])){"journal.bin", CHUNK_BYTES - 10, CHUNK_BYTES + 10};
    bad[bad_count++] = (typeof(bad[0])){"journal.bin", 4 * CHUNK_BYTES + 300, 4 * CHUNK_BYTES + 301};
    reads = 0;
    si_scan(spans, NULL, &report);
    const si_file_t *journal = find(&report, "journal.bin");
    assert(report.complete && report.files == 1 && report.unreadable_files == 1);
    assert(report.bytes == 6 * CHUNK_BYTES && report.unreadable_bytes == 3 * SI_PAGE_BYTES);
    assert(journal && journal->bytes == 6 * CHUNK_BYTES && journal->first_error == EIO);
    assert(journal->first_error_offset == CHUNK_BYTES - SI_PAGE_BYTES);
    assert(journal->unreadable_bytes == 3 * SI_PAGE_BYTES);
    assert(reads < 6 * SI_CHUNK_PAGES);
    for (int i = 0; i < 8; ++i) assert(!open_files[i].stream);

    /* Every file is examined before any damaged one is mapped past its first
     * damaged chunk: a damaged page can cost a search of the whole store, so
     * one damaged file used to spend the budget before the rest were read. */
    char survey[512];
    subdirectory(survey, sizeof(survey), directory, "survey");
    static const char *const queues[] = {"q0.jsonl", "q1.jsonl", "q2.jsonl", "q3.jsonl"};
    for (int i = 0; i < 4; ++i) {
        write_file(survey, queues[i], 64 * CHUNK_BYTES);
        bad[bad_count++] = (typeof(bad[0])){queues[i], CHUNK_BYTES, 64 * CHUNK_BYTES};
    }
    /* Enough for "." and "..", then per file its turn, a chunk, a failed chunk
     * and its pages, and a few reads more: not for mapping a whole file. */
    budget_reads = 2 + 4 * (1 + 1 + 1 + SI_CHUNK_PAGES) + 8;
    si_scan(survey, &ports, &report);
    budget_reads = 0;
    assert(report.done && !report.complete && report.files == 4 && report.unreadable_files == 4);
    assert(report.bytes == 4ULL * 64 * CHUNK_BYTES && report.unreadable_bytes >= 4 * CHUNK_BYTES);
    for (int i = 0; i < 4; ++i) {
        const si_file_t *queue = find(&report, queues[i]);
        assert(queue && queue->bytes == 64 * CHUNK_BYTES && queue->first_error == EIO);
        assert(queue->first_error_offset == CHUNK_BYTES && queue->unreadable_bytes >= CHUNK_BYTES);
    }
    /* With the budget, the rest of every damaged file is mapped page by page. */
    si_scan(survey, NULL, &report);
    assert(report.complete && report.files == 4 && report.unreadable_bytes == 4ULL * 63 * CHUNK_BYTES);
    for (int i = 0; i < 4; ++i) {
        const si_file_t *queue = find(&report, queues[i]);
        assert(queue && queue->first_error_offset == CHUNK_BYTES && queue->unreadable_bytes == 63 * CHUNK_BYTES);
    }
    /* A segment appended to in between still holds the bytes its first read
     * was sized at: the rest is mapped as one read would have mapped it. */
    changed = "q2.jsonl";
    changed_by = SI_PAGE_BYTES;
    changed_stats = 0;
    si_scan(survey, NULL, &report);
    assert(report.complete && report.unreadable_bytes == 4ULL * 63 * CHUNK_BYTES);
    assert(find(&report, "q2.jsonl")->unreadable_bytes == 63 * CHUNK_BYTES);
    /* One that shrank cannot be mapped: incomplete, never complete with the
     * rest of a damaged file left unexamined. */
    changed_by = -(long long)CHUNK_BYTES;
    changed_stats = 0;
    si_scan(survey, NULL, &report);
    changed = NULL;
    assert(!report.complete && report.unreadable_bytes == 3ULL * 63 * CHUNK_BYTES + CHUNK_BYTES);
    assert(find(&report, "q2.jsonl")->unreadable_bytes == CHUNK_BYTES);
    for (int i = 0; i < 8; ++i) assert(!open_files[i].stream);

    /* A size the device's 32-bit off_t cannot hold arrives negative there: the
     * file's metadata is damaged, so it is reported and never opened. */
    char meta[512];
    subdirectory(meta, sizeof(meta), directory, "meta");
    write_file(meta, "header.bin", 100);
    resized = "header.bin";
    resized_bytes = 0x80000000LL + 4096;
    si_scan(meta, NULL, &report);
    const si_file_t *header = find(&report, "header.bin");
#ifdef SI_DEVICE_OFF_T
    assert(report.complete && report.files == 1 && report.unreadable_files == 1);
    assert(!report.bytes && !report.unreadable_bytes && !resized_opens);
    assert(header && !header->bytes && header->first_error == EOVERFLOW);
    assert(!header->first_error_offset && !header->unreadable_bytes);
#else
    /* The host's off_t holds it; the file then ends at its real 100 bytes. */
    assert(header && header->bytes == 0x80001000U && header->first_error == ENODATA);
    assert(header->first_error_offset == 100 && header->unreadable_bytes == 0x80001000U - 100);
    /* A size above 4 GiB is clamped to the largest the report holds. */
    resized_bytes = 0x100000000LL + 7;
    si_scan(meta, NULL, &report);
    header = find(&report, "header.bin");
    assert(header && header->bytes == UINT32_MAX && header->first_error == ENODATA);
    assert(header->first_error_offset == 100 && header->unreadable_bytes == UINT32_MAX - 100);
    /* No width makes a negative size real. */
    resized_bytes = -1;
    resized_opens = 0;
    si_scan(meta, NULL, &report);
    header = find(&report, "header.bin");
    assert(report.unreadable_files == 1 && !report.bytes && !resized_opens);
    assert(header && !header->bytes && header->first_error == EOVERFLOW && !header->unreadable_bytes);
#endif
    resized = NULL;
    for (int i = 0; i < 8; ++i) assert(!open_files[i].stream);

    /* A directory that cannot be read is reported, not mistaken for an empty store. */
    si_scan("/nonexistent-zone-lite-storage", NULL, &report);
    assert(report.done && !report.complete && report.error == ENOENT && !report.files);
    puts("storage inventory host tests passed");
    return 0;
}
