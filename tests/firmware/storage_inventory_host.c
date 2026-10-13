/* Host harness for storage_inventory_core.c: a scan maps unreadable bytes
 * without changing anything, and never stops at the first damaged page. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "storage_inventory.h"

static struct {
    FILE *stream;
    char path[512];
    long offset;
    int failed;
} open_files[8];
static struct {
    const char *name;
    long start, end;
} bad[4];
static unsigned bad_count;
static const char *unopenable, *shrunk;
static unsigned budget_pages;

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
    for (unsigned b = 0; b < bad_count; ++b) {
        if (strstr(open_files[i].path, bad[b].name) && open_files[i].offset < bad[b].end &&
            open_files[i].offset + length > bad[b].start) {
            open_files[i].failed = 1;
            errno = EIO;
            return 0;
        }
    }
    if (shrunk && strstr(open_files[i].path, shrunk) && open_files[i].offset >= 512) return 0;
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

static bool within_budget(void *context)
{
    (void)context;
    return budget_pages == 0 || --budget_pages > 0;
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

int main(int argc, char **argv)
{
    assert(argc == 2);
    const char *directory = argv[1];
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
    si_report_t report;
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
    assert(!strcmp(report.file[4].name, "acked_uids.txt") && !find(&report, "nested"));
    for (int i = 0; i < 8; ++i) assert(!open_files[i].stream); /* every stream closed */

    /* A budget that runs out leaves an incomplete scan, never a false clean one. */
    budget_pages = 10;
    const si_ports_t ports = {.keep_going = within_budget};
    si_scan(directory, &ports, &report);
    assert(report.done && !report.complete);
    budget_pages = 0;

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

    /* A directory that cannot be read is reported, not mistaken for an empty store. */
    si_scan("/nonexistent-zone-lite-storage", NULL, &report);
    assert(report.done && !report.complete && report.error == ENOENT && !report.files);
    puts("storage inventory host tests passed");
    return 0;
}
