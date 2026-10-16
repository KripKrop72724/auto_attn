#include "storage_inventory.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define SI_CHUNK_BYTES (SI_CHUNK_PAGES * SI_PAGE_BYTES)

#ifdef ESP_PLATFORM
/* newlib's off_t, and so st_size, is a 32-bit long here and 64-bit on the
 * host; the host test also runs this file at this width (SI_DEVICE_OFF_T). */
_Static_assert(sizeof(off_t) == 4 && (off_t)-1 < 0, "the host test's device build must match newlib's off_t");
#endif

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

/* Reads a file from position to its size unbuffered, a chunk at a time. A
 * chunk that fails is read again one SPIFFS page at a time, so a failed read
 * names its page. The page is counted unreadable and skipped, and the scan
 * continues: one damaged page never hides the rest of the file. With resume,
 * the read stops after the first chunk with an unreadable page and sets
 * *resume to where the rest begins. Returns false when the budget ran out. */
static bool scan_file(const char *path, uint32_t position, uint32_t size, unsigned char *chunk,
                      si_file_t *file, const si_ports_t *ports, uint32_t *resume)
{
    errno = 0;
    FILE *stream = fopen(path, "rb");
    if (!stream) {
        unreadable(file, position, errno, size - position);
        return true;
    }
    setvbuf(stream, NULL, _IONBF, 0);
    if (position && fseek(stream, (long)position, SEEK_SET) != 0) {
        unreadable(file, position, errno, size - position);
        position = size;
    }
    uint32_t paged = 0; /* bytes of a failed chunk still to read one page at a time */
    bool finished = true;
    while (position < size) {
        if (resume && file->first_error && !paged) {
            *resume = position;
            break;
        }
        if (!keep_going(ports)) {
            finished = false;
            break;
        }
        uint32_t step = paged ? SI_PAGE_BYTES : SI_CHUNK_BYTES;
        size_t want = size - position < step ? size - position : step;
        errno = 0;
        size_t got = fread(chunk, 1, want, stream);
        if (got != want && ferror(stream) && !paged) {
            paged = (uint32_t)want;
            clearerr(stream);
            if (fseek(stream, (long)position, SEEK_SET) == 0) continue;
            unreadable(file, position, errno, size - position);
            break;
        }
        uint32_t start = position;
        position += (uint32_t)got;
        if (got == want) {
            if (paged) paged -= (uint32_t)got;
            continue;
        }
        /* The file ended before its reported size: what remains is missing. */
        if (!ferror(stream)) {
            unreadable(file, position, ENODATA, size - position);
            break;
        }
        /* Only a page read gets here: a failed chunk was read again by page. */
        int error = errno ? errno : EIO;
        uint32_t next = position + SI_PAGE_BYTES - position % SI_PAGE_BYTES;
        if (next > size) next = size;
        unreadable(file, position, error, next - position);
        position = next;
        paged -= position - start;
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
    /* Where the rest of a damaged file begins, and the size it was read at. */
    struct {
        uint32_t resume, bytes;
    } *damaged = calloc(SI_MAX_NAMES, sizeof(*damaged));
    unsigned char *chunk = malloc(SI_CHUNK_BYTES);
    size_t listed = 0;
    if (!names || !damaged || !chunk) {
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
    /* Every file up to its first damage, then the rest of each damaged file:
     * a damaged page can cost a search of the whole store, and one damaged
     * file must not use the budget before the other files are examined. */
    bool stopped = false;
    char path[sizeof(names[0]) + 160];
    for (size_t index = 0; index < listed; ++index) {
        if (!keep_going(ports)) {
            stopped = true;
            break;
        }
        if ((size_t)snprintf(path, sizeof(path), "%s/%s", directory, names[index]) >= sizeof(path)) continue;
        si_file_t file = {0};
        snprintf(file.name, sizeof(file.name), "%s", names[index]);
        struct stat status;
        errno = 0;
        if (stat(path, &status) != 0) {
            unreadable(&file, 0, errno, 0);
        } else if (S_ISDIR(status.st_mode)) {
            continue;
        } else if (status.st_size < 0) {
            /* A size the device's 32-bit off_t cannot hold: damaged metadata. */
            unreadable(&file, 0, EOVERFLOW, 0);
        } else {
            /* Compared unsigned: (off_t)UINT32_MAX is -1 where off_t is 32-bit. */
            file.bytes = (uintmax_t)status.st_size > UINT32_MAX ? UINT32_MAX : (uint32_t)status.st_size;
            damaged[index].bytes = file.bytes;
            stopped = !scan_file(path, 0, file.bytes, chunk, &file, ports, &damaged[index].resume);
        }
        ++report->files;
        report->bytes += file.bytes;
        report->unreadable_bytes += file.unreadable_bytes;
        if (file.first_error) ++report->unreadable_files;
        keep(report, &file);
        if (stopped) break;
    }
    for (size_t index = 0; !stopped && index < listed; ++index) {
        if (!damaged[index].resume) continue;
        if (!keep_going(ports)) {
            stopped = true;
            break;
        }
        snprintf(path, sizeof(path), "%s/%s", directory, names[index]);
        struct stat status;
        /* A queue segment appended to since still holds the bytes its first
         * read was sized at, and those are mapped as one read would have. A
         * file that shrank or went cannot be: the scan is then incomplete. */
        if (stat(path, &status) != 0 || status.st_size < 0 ||
            (uintmax_t)status.st_size < damaged[index].bytes) {
            report->complete = false;
            continue;
        }
        si_file_t rest = {0};
        stopped = !scan_file(path, damaged[index].resume, damaged[index].bytes, chunk, &rest, ports, NULL);
        report->unreadable_bytes += rest.unreadable_bytes;
        for (size_t kept = 0; kept < report->count; ++kept) {
            si_file_t *entry = &report->file[kept];
            if (!strcmp(entry->name, names[index])) entry->unreadable_bytes += rest.unreadable_bytes;
        }
    }
    if (stopped) report->complete = false;
    free(chunk);
    free(damaged);
    free(names);
    report->done = true;
}
