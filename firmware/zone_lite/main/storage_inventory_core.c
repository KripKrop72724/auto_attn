#include "storage_inventory.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static bool keep_going(const si_ports_t *ports)
{
    return !ports || !ports->keep_going || ports->keep_going(ports->context);
}

/* Unreadable files first, then the largest: the ones a repair must handle. */
static bool before(const si_file_t *a, const si_file_t *b)
{
    if ((a->first_error != 0) != (b->first_error != 0)) return a->first_error != 0;
    return a->bytes > b->bytes;
}

static void keep(si_report_t *report, const si_file_t *file)
{
    size_t at = report->count;
    while (at > 0 && before(file, &report->file[at - 1])) --at;
    if (at >= SI_MAX_FILES) return;
    size_t last = report->count < SI_MAX_FILES ? report->count : SI_MAX_FILES - 1;
    memmove(&report->file[at + 1], &report->file[at], (last - at) * sizeof(report->file[0]));
    report->file[at] = *file;
    if (report->count < SI_MAX_FILES) ++report->count;
}

static void unreadable(si_file_t *file, uint32_t offset, int error, uint32_t length)
{
    if (!file->first_error) {
        file->first_error = error ? error : EIO;
        file->first_error_offset = offset;
    }
    file->unreadable_bytes += length;
}

/* Reads a file to its size one SPIFFS page at a time, unbuffered, so a failed
 * read names its page. The page is counted unreadable and skipped, and the
 * scan continues: one damaged page never hides the rest of the file. */
static bool scan_file(const char *path, uint32_t size, si_file_t *file, const si_ports_t *ports)
{
    errno = 0;
    FILE *stream = fopen(path, "rb");
    if (!stream) {
        unreadable(file, 0, errno, size);
        return true;
    }
    setvbuf(stream, NULL, _IONBF, 0);
    unsigned char page[SI_PAGE_BYTES];
    uint32_t position = 0;
    bool finished = true;
    while (position < size) {
        if (!keep_going(ports)) {
            finished = false;
            break;
        }
        size_t want = size - position < sizeof(page) ? size - position : sizeof(page);
        errno = 0;
        size_t got = fread(page, 1, want, stream);
        position += (uint32_t)got;
        if (got == want) continue;
        /* The file ended before its reported size: what remains is missing. */
        if (!ferror(stream)) {
            unreadable(file, position, ENODATA, size - position);
            break;
        }
        int error = errno ? errno : EIO;
        uint32_t next = position + SI_PAGE_BYTES - position % SI_PAGE_BYTES;
        if (next > size) next = size;
        unreadable(file, position, error, next - position);
        position = next;
        clearerr(stream);
        if (position < size && fseek(stream, (long)position, SEEK_SET) != 0) {
            unreadable(file, position, errno, size - position);
            break;
        }
    }
    fclose(stream);
    return finished;
}

void si_scan(const char *directory, const si_ports_t *ports, si_report_t *report)
{
    memset(report, 0, sizeof(*report));
    report->complete = true;
    errno = 0;
    DIR *dir = directory ? opendir(directory) : NULL;
    if (!dir) {
        report->error = errno ? errno : EIO;
        report->complete = false;
        report->done = true;
        return;
    }
    /* Names first: no file is opened while the directory is being listed. */
    char (*names)[sizeof(report->file[0].name)] = calloc(SI_MAX_NAMES, sizeof(*names));
    size_t listed = 0;
    if (!names) {
        report->error = ENOMEM;
        report->complete = false;
    } else {
        for (struct dirent *entry; (entry = readdir(dir)) != NULL;) {
            if (listed == SI_MAX_NAMES) {
                report->complete = false;
                break;
            }
            /* SPIFFS names fit; a longer one is cut and then fails stat. */
            size_t length = 0;
            while (length + 1 < sizeof(names[0]) && entry->d_name[length]) {
                names[listed][length] = entry->d_name[length];
                ++length;
            }
            names[listed++][length] = '\0';
        }
    }
    closedir(dir);
    for (size_t index = 0; index < listed; ++index) {
        if (!keep_going(ports)) {
            report->complete = false;
            break;
        }
        char path[sizeof(names[0]) + 160];
        if ((size_t)snprintf(path, sizeof(path), "%s/%s", directory, names[index]) >= sizeof(path)) continue;
        si_file_t file = {0};
        snprintf(file.name, sizeof(file.name), "%s", names[index]);
        struct stat status;
        errno = 0;
        if (stat(path, &status) != 0) {
            unreadable(&file, 0, errno, 0);
        } else if (S_ISDIR(status.st_mode)) {
            continue;
        } else {
            file.bytes = status.st_size > (off_t)UINT32_MAX ? UINT32_MAX : (uint32_t)status.st_size;
            if (!scan_file(path, file.bytes, &file, ports)) report->complete = false;
        }
        ++report->files;
        report->bytes += file.bytes;
        report->unreadable_bytes += file.unreadable_bytes;
        if (file.first_error) ++report->unreadable_files;
        keep(report, &file);
    }
    free(names);
    report->done = true;
}
