#include "storage_recovery_core.h"
#include "reliability.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define SR_READ_CHUNK 4096U
#define SR_SERIAL_MAX 120U

typedef struct {
    const sr_ports_t *ports;
    sr_outcome_t *outcome;
    uint8_t *uids;
    size_t uid_capacity;
    bool scan;                 /* Measuring pass: nothing is sent or recorded */
    uint32_t scan_rows, pass_gaps;
    uint64_t pass_unreadable;
} sr_run_t;

const char *sr_result_name(sr_result_t result)
{
    switch (result) {
    case SR_NOTHING_TO_DO: return "NOTHING_TO_DO";
    case SR_COMPLETE: return "COMPLETE";
    case SR_COMPLETE_SEEN_PARTIAL: return "COMPLETE_SEEN_PARTIAL";
    case SR_INCOMPLETE: return "INCOMPLETE";
    case SR_RETIRE_PARTIAL: return "RETIRE_PARTIAL";
    case SR_REFUSED: return "REFUSED";
    }
    return "UNKNOWN";
}

static void finish(sr_run_t *run, sr_result_t result, const char *code)
{
    run->outcome->result = result;
    snprintf(run->outcome->code, sizeof(run->outcome->code), "%s", code);
}

static void logf_line(sr_run_t *run, const char *level, const char *code, const char *message)
{
    if (run->ports->log) run->ports->log(run->ports->context, level, code, message);
}

static uint32_t crc32_bytes(const char *data, size_t length)
{
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < length; ++i) {
        crc ^= (unsigned char)data[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

/* Locate a top-level string value written by cJSON_PrintUnformatted. The row
 * is already syntax-checked; a missing or unusual value simply yields false. */
static const char *json_string_value(const char *text, size_t length, const char *key, size_t *value_length)
{
    char pattern[40];
    int written = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    if (written <= 0 || (size_t)written >= sizeof(pattern)) return NULL;
    size_t pattern_length = (size_t)written;
    for (size_t i = 0; i + pattern_length <= length; ++i) {
        if (memcmp(text + i, pattern, pattern_length)) continue;
        size_t p = i + pattern_length;
        while (p < length && (text[p] == ' ' || text[p] == '\t')) p++;
        if (p >= length || text[p] != ':') continue;
        p++;
        while (p < length && (text[p] == ' ' || text[p] == '\t')) p++;
        if (p >= length || text[p] != '"') return NULL;
        size_t start = ++p;
        while (p < length && text[p] != '"' && text[p] != '\\') p++;
        if (p >= length || text[p] != '"') return NULL;
        *value_length = p - start;
        return text + start;
    }
    return NULL;
}

static bool parse_uid(const char *row, size_t length, uint8_t uid[SR_UID_BYTES])
{
    size_t value_length = 0;
    const char *value = json_string_value(row, length, "event_uid", &value_length);
    if (!value || value_length != SR_UID_BYTES * 2) return false;
    for (size_t i = 0; i < SR_UID_BYTES; ++i) {
        int high = hex_value(value[i * 2]), low = hex_value(value[i * 2 + 1]);
        if (high < 0 || low < 0) return false;
        uid[i] = (uint8_t)((high << 4) | low);
    }
    return true;
}

static bool row_valid(const char *row, size_t *json_length)
{
    if (*json_length && row[*json_length - 1] == '\r') (*json_length)--;
    return *json_length > 0 && !memchr(row, 0, *json_length) && rel_json_syntax_valid(row, *json_length);
}

bool sr_row_event_uid(const char *row, size_t length, uint8_t uid[SR_UID_BYTES])
{
    return row && row_valid(row, &length) && parse_uid(row, length, uid);
}

static bool remember_uid(sr_run_t *run, const char *row, size_t length)
{
    uint8_t uid[SR_UID_BYTES];
    if (!parse_uid(row, length, uid)) return true;
    if (run->outcome->uids == run->uid_capacity) {
        size_t next = run->uid_capacity ? run->uid_capacity * 2 : 4096;
        uint8_t *grown = realloc(run->uids, next * SR_UID_BYTES);
        if (!grown) return false;
        run->uids = grown;
        run->uid_capacity = next;
    }
    memcpy(run->uids + (size_t)run->outcome->uids * SR_UID_BYTES, uid, SR_UID_BYTES);
    run->outcome->uids++;
    return true;
}

/* Returns 1 when acknowledged (or counted by a scan), 0 when the caller must
 * stop, and -1 when the UID set cannot grow. */
static int transfer_row(sr_run_t *run, const char *generation, uint64_t offset, const char *row, size_t length)
{
    size_t json_length = length;
    bool valid = row_valid(row, &json_length);
    if (run->scan) {
        run->scan_rows++;
        return 1;
    }
    char serial[SR_SERIAL_MAX + 1] = {0};
    if (valid) {
        size_t serial_length = 0;
        const char *value = json_string_value(row, json_length, "device_serial", &serial_length);
        bool printable = value && serial_length > 0 && serial_length <= SR_SERIAL_MAX;
        for (size_t i = 0; printable && i < serial_length; ++i)
            printable = value[i] > 32 && value[i] < 127;
        if (printable) memcpy(serial, value, serial_length);
        if (!remember_uid(run, row, json_length)) return -1;
    } else {
        run->outcome->malformed++;
    }
    char record_id[80];
    snprintf(record_id, sizeof(record_id), "%llu:%lu:%08lx", (unsigned long long)offset,
             (unsigned long)length, (unsigned long)crc32_bytes(row, length));
    for (;;) {
        run->outcome->sends++;
        sr_send_t sent = run->ports->send(run->ports->context, generation, record_id, row, length,
                                          serial[0] ? serial : NULL, valid ? "LEGACY_RECOVERY" : "MALFORMED");
        if (sent == SR_SEND_ACKED) break;
        if (sent == SR_SEND_STOP) return 0;
        run->outcome->retries++;
    }
    run->outcome->records++;
    run->outcome->bytes += length;
    if (run->outcome->records % SR_PROGRESS_INTERVAL == 0) {
        char message[160];
        snprintf(message, sizeof(message), "Transferred %lu blocked rows (%llu bytes) to ADD custody; nothing removed yet",
                 (unsigned long)run->outcome->records, (unsigned long long)run->outcome->bytes);
        logf_line(run, "INFO", "STORAGE_RECOVERY_PROGRESS", message);
    }
    return 1;
}

/* Every unreadable region is counted against the approved limit. A scan logs
 * it; the transfer pass hands ADD a durable record of its exact location.
 * Returns 1 to continue, 0 to stop (send refused), -2 over the limit. */
static int report_gap(sr_run_t *run, const char *generation, uint64_t offset, uint64_t length, int error)
{
    run->pass_gaps++;
    run->pass_unreadable += length;
    char message[200];
    if (run->pass_unreadable > SR_UNREADABLE_LIMIT) {
        snprintf(message, sizeof(message), "More than %u unreadable bytes (region at offset %llu); nothing will be removed",
                 (unsigned)SR_UNREADABLE_LIMIT, (unsigned long long)offset);
        logf_line(run, "ERROR", "STORAGE_RECOVERY_UNREADABLE_LIMIT", message);
        return -2;
    }
    if (run->scan) {
        if (run->pass_gaps <= 20) {
            snprintf(message, sizeof(message), "Unreadable region in %s at offset %llu length %llu errno %d",
                     generation, (unsigned long long)offset, (unsigned long long)length, error);
            logf_line(run, "WARN", "STORAGE_RECOVERY_UNREADABLE_REGION", message);
        }
        return 1;
    }
    char record_id[80], raw[240];
    snprintf(record_id, sizeof(record_id), "gap:%llu:%llu", (unsigned long long)offset, (unsigned long long)length);
    int written = snprintf(raw, sizeof(raw),
        "{\"errno\":%d,\"generation\":\"%s\",\"length\":%llu,\"offset\":%llu,\"type\":\"storage_recovery_unreadable\"}",
        error, generation, (unsigned long long)length, (unsigned long long)offset);
    if (written <= 0 || (size_t)written >= sizeof(raw)) return 0;
    for (;;) {
        run->outcome->sends++;
        sr_send_t sent = run->ports->send(run->ports->context, generation, record_id, raw, (size_t)written,
                                          NULL, "MALFORMED");
        if (sent == SR_SEND_ACKED) break;
        if (sent == SR_SEND_STOP) return 0;
        run->outcome->retries++;
    }
    run->outcome->gaps++;
    run->outcome->unreadable_bytes += length;
    return 1;
}

typedef enum { SR_READ_OK, SR_READ_GAP, SR_READ_EOF, SR_READ_FAIL } sr_read_t;

typedef struct {
    const char *path;
    FILE *file;
    uint64_t position, size, clean;
    bool probing;
    int error;
} sr_reader_t;

/* Unbuffered, so one fread is one filesystem read of exactly these bytes.
 * Returns 1 when open at the offset, 0 when the offset cannot be reached (a
 * SPIFFS seek resolves the index page of that span, which may be unreadable)
 * and -1 when the source cannot be opened at all. */
static int open_at(sr_reader_t *reader, uint64_t offset, FILE **out)
{
    *out = NULL;
    errno = 0;
    FILE *file = fopen(reader->path, "rb");
    if (!file) {
        reader->error = errno ? errno : EIO;
        return -1;
    }
    if (setvbuf(file, NULL, _IONBF, 0) != 0) {
        reader->error = errno ? errno : EIO;
        fclose(file);
        return -1;
    }
    errno = 0;
    if (fseek(file, (long)offset, SEEK_SET) != 0) {
        reader->error = errno ? errno : EIO;
        fclose(file);
        return 0;
    }
    *out = file;
    return 1;
}

/* Returns 1 when the piece read (got 0 is EOF), 0 when it is unreadable and
 * -1 when the source cannot be opened at all. */
static int read_piece(sr_reader_t *reader, uint64_t offset, char *buffer, size_t length, size_t *got)
{
    *got = 0;
    FILE *file = NULL;
    int opened = open_at(reader, offset, &file);
    if (opened <= 0) return opened;
    errno = 0;
    *got = fread(buffer, 1, length, file);
    int readable = !ferror(file);
    if (!readable) reader->error = errno ? errno : EIO;
    fclose(file);
    return readable;
}

/* Sequential reads until a read fails. The bytes of a failed read are then
 * read in SR_PROBE_BYTES pieces; an unreadable piece starts a region that is
 * extended until a probe reads again, EOF, or the limit. */
static sr_read_t reader_next(sr_reader_t *reader, char *buffer, size_t capacity, size_t *got,
                             uint64_t *gap_offset, uint64_t *gap_length)
{
    *got = 0;
    if (reader->position >= reader->size) return SR_READ_EOF;
    size_t want = capacity;
    if ((uint64_t)want > reader->size - reader->position) want = (size_t)(reader->size - reader->position);
    if (!reader->probing && !reader->file) {
        int opened = open_at(reader, reader->position, &reader->file);
        if (opened < 0) return SR_READ_FAIL;
        if (!opened) {
            reader->probing = true;
            reader->clean = 0;
        }
    }
    if (!reader->probing) {
        errno = 0;
        size_t n = fread(buffer, 1, want, reader->file);
        if (!ferror(reader->file)) {
            if (n == 0) return SR_READ_EOF;
            reader->position += n;
            *got = n;
            return SR_READ_OK;
        }
        reader->error = errno ? errno : EIO;
        fclose(reader->file);
        reader->file = NULL;
        reader->probing = true;
        reader->clean = 0;
    }
    size_t length = want < SR_PROBE_BYTES ? want : SR_PROBE_BYTES;
    size_t n = 0;
    int readable = read_piece(reader, reader->position, buffer, length, &n);
    if (readable < 0) return SR_READ_FAIL;
    if (readable) {
        if (n == 0) return SR_READ_EOF;
        reader->position += n;
        reader->clean += n;
        if (reader->clean >= SR_READ_CHUNK) reader->probing = false;
        *got = n;
        return SR_READ_OK;
    }
    uint64_t start = reader->position;
    int error = reader->error;
    for (;;) {
        uint64_t next = reader->position + SR_PROBE_BYTES;
        reader->position = next < reader->size ? next : reader->size;
        if (reader->position >= reader->size || reader->position - start > SR_UNREADABLE_LIMIT) break;
        char byte;
        size_t one = 0;
        readable = read_piece(reader, reader->position, &byte, 1, &one);
        if (readable < 0) return SR_READ_FAIL;
        if (readable) break;
    }
    reader->clean = 0;
    reader->error = error;
    *gap_offset = start;
    *gap_length = reader->position - start;
    return SR_READ_GAP;
}

static bool row_sent(sr_run_t *run, int sent)
{
    if (sent > 0) return true;
    if (sent < 0) finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_UID_MEMORY");
    else finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_STOPPED");
    return false;
}

/* Stream one source. Each newline-terminated row (and a final unterminated
 * tail) is sent as exact bytes. Empty rows carry no data and are skipped. A
 * row cut by an unreadable region is sent as its exact readable fragments. */
static bool transfer_source(sr_run_t *run, const char *path, unsigned index, off_t expected_size)
{
    char generation[80];
    snprintf(generation, sizeof(generation), "storage-recovery-v1-%u-%lld", index, (long long)expected_size);
    char *chunk = malloc(SR_READ_CHUNK);
    char *row = malloc(SR_RECORD_MAX_BYTES + 1);
    if (!chunk || !row) {
        free(chunk); free(row);
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_MEMORY");
        return false;
    }
    sr_reader_t reader = {.path = path, .size = (uint64_t)expected_size};
    bool ok = true;
    size_t row_length = 0;
    uint64_t row_start = 0;
    while (ok) {
        size_t got = 0;
        uint64_t gap_offset = 0, gap_length = 0, chunk_start = reader.position;
        sr_read_t read = reader_next(&reader, chunk, SR_READ_CHUNK, &got, &gap_offset, &gap_length);
        if (read == SR_READ_EOF) break;
        if (read == SR_READ_FAIL) {
            finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_READ");
            ok = false;
            break;
        }
        if (read == SR_READ_GAP) {
            if (row_length && !row_sent(run, transfer_row(run, generation, row_start, row, row_length))) {
                ok = false;
                break;
            }
            int reported = report_gap(run, generation, gap_offset, gap_length, reader.error);
            if (reported == -2) finish(run, SR_REFUSED, "STORAGE_RECOVERY_UNREADABLE_LIMIT");
            else if (reported == 0) finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_STOPPED");
            if (reported <= 0) {
                ok = false;
                break;
            }
            row_length = 0;
            row_start = reader.position;
            continue;
        }
        for (size_t i = 0; ok && i < got; ++i) {
            if (chunk[i] != '\n') {
                if (row_length == SR_RECORD_MAX_BYTES) {
                    finish(run, SR_REFUSED, "STORAGE_RECOVERY_ROW_TOO_LARGE");
                    ok = false;
                    break;
                }
                row[row_length++] = chunk[i];
                continue;
            }
            bool empty = row_length == 0 || (row_length == 1 && row[0] == '\r');
            if (!empty && !row_sent(run, transfer_row(run, generation, row_start, row, row_length))) ok = false;
            row_length = 0;
            row_start = chunk_start + i + 1;
        }
    }
    if (reader.file) fclose(reader.file);
    if (ok && row_length && !(row_length == 1 && row[0] == '\r'))
        ok = row_sent(run, transfer_row(run, generation, row_start, row, row_length));
    if (ok && reader.position != (uint64_t)expected_size) {
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_CHANGED");
        ok = false;
    }
    free(chunk);
    free(row);
    struct stat after;
    if (ok && (stat(path, &after) != 0 || after.st_size != expected_size)) {
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_CHANGED");
        ok = false;
    }
    return ok;
}

bool sr_append_uids(const char *acked_path, const uint8_t *uids, uint32_t count, uint32_t *appended)
{
    *appended = 0;
    if (!count) return true;
    errno = 0;
    FILE *file = rel_open_append(acked_path);
    bool terminate_torn_row = false;
    if (!file && errno == EBADMSG) {
        /* Close an interrupted legacy row first so no UID is concatenated to it. */
        file = fopen(acked_path, "ab");
        terminate_torn_row = file != NULL;
    }
    if (!file) return false;
    bool ok = !terminate_torn_row || fputc('\n', file) != EOF;
    static const char alphabet[] = "0123456789abcdef";
    char line[SR_UID_BYTES * 2 + 2];
    for (uint32_t i = 0; ok && i < count; ++i) {
        const uint8_t *uid = uids + (size_t)i * SR_UID_BYTES;
        for (size_t b = 0; b < SR_UID_BYTES; ++b) {
            line[b * 2] = alphabet[uid[b] >> 4];
            line[b * 2 + 1] = alphabet[uid[b] & 0x0f];
        }
        line[SR_UID_BYTES * 2] = '\n';
        line[SR_UID_BYTES * 2 + 1] = '\0';
        ok = fputs(line, file) >= 0;
        if (ok && (i + 1) % 256 == 0) ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
        if (ok) *appended = i + 1;
    }
    if (ok) ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    if (!ok && *appended) {
        /* Only fully synced 256-row groups are counted as durable. */
        *appended -= *appended % 256;
    }
    return ok;
}

static bool append_seen_uids(sr_run_t *run, const char *acked_path)
{
    return sr_append_uids(acked_path, run->uids, run->outcome->uids, &run->outcome->uids_appended);
}

void sr_transfer_and_retire(const char *const *sources, size_t source_count,
                            const char *acked_path, const sr_ports_t *ports,
                            sr_outcome_t *outcome)
{
    if (!outcome) return;
    memset(outcome, 0, sizeof(*outcome));
    sr_run_t run = {.ports = ports, .outcome = outcome};
    if (!sources || !source_count || source_count > 8 || !acked_path || !ports || !ports->send) {
        finish(&run, SR_REFUSED, "STORAGE_RECOVERY_INVALID_REQUEST");
        return;
    }
    off_t sizes[8] = {0};
    bool present[8] = {false};
    for (size_t i = 0; i < source_count; ++i) {
        struct stat st;
        errno = 0;
        if (stat(sources[i], &st) == 0) {
            if (!S_ISREG(st.st_mode)) { finish(&run, SR_REFUSED, "STORAGE_RECOVERY_SOURCE_TYPE"); return; }
            sizes[i] = st.st_size;
            present[i] = st.st_size > 0;
        } else if (errno != ENOENT) {
            finish(&run, SR_REFUSED, "STORAGE_RECOVERY_SOURCE_STAT");
            return;
        }
    }
    bool any = false;
    for (size_t i = 0; i < source_count; ++i) any = any || present[i];
    if (!any) { finish(&run, SR_NOTHING_TO_DO, "STORAGE_RECOVERY_NOTHING_TO_DO"); return; }
    /* Measure every unreadable region before sending anything. */
    run.scan = true;
    for (size_t i = 0; i < source_count; ++i) {
        if (present[i] && !transfer_source(&run, sources[i], (unsigned)i, sizes[i])) return;
    }
    char message[220];
    uint32_t scan_gaps = run.pass_gaps;
    uint64_t scan_unreadable = run.pass_unreadable;
    snprintf(message, sizeof(message), "Scan found %lu rows and %lu unreadable regions (%llu bytes, limit %u); transfer starts",
             (unsigned long)run.scan_rows, (unsigned long)scan_gaps, (unsigned long long)scan_unreadable,
             (unsigned)SR_UNREADABLE_LIMIT);
    logf_line(&run, scan_gaps ? "WARN" : "INFO", "STORAGE_RECOVERY_SCAN", message);
    run.scan = false;
    run.pass_gaps = 0;
    run.pass_unreadable = 0;
    for (size_t i = 0; i < source_count; ++i) {
        if (!present[i]) continue;
        outcome->files++;
        if (!transfer_source(&run, sources[i], (unsigned)i, sizes[i])) { free(run.uids); return; }
    }
    if (run.pass_gaps != scan_gaps || run.pass_unreadable != scan_unreadable) {
        /* The same bytes must be unreadable in both passes. */
        free(run.uids);
        finish(&run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_CHANGED");
        return;
    }
    snprintf(message, sizeof(message),
             "ADD acknowledged every readable blocked row: files=%lu rows=%lu malformed=%lu bytes=%llu uids=%lu unreadable=%llu",
             (unsigned long)outcome->files, (unsigned long)outcome->records, (unsigned long)outcome->malformed,
             (unsigned long long)outcome->bytes, (unsigned long)outcome->uids,
             (unsigned long long)outcome->unreadable_bytes);
    logf_line(&run, "INFO", "STORAGE_RECOVERY_CUSTODY_COMPLETE", message);
    if (ports->before_retire && !ports->before_retire(ports->context)) {
        free(run.uids);
        finish(&run, SR_INCOMPLETE, "STORAGE_RECOVERY_PREPARE_RETIRE");
        return;
    }
    bool removed_all = true;
    for (size_t i = 0; i < source_count; ++i) {
        if (!present[i]) continue;
        errno = 0;
        if (remove(sources[i]) != 0 && errno != ENOENT) removed_all = false;
    }
    bool seen = append_seen_uids(&run, acked_path);
    free(run.uids);
    run.uids = NULL;
    if (!removed_all) finish(&run, SR_RETIRE_PARTIAL, "STORAGE_RECOVERY_RETIRE_PARTIAL");
    else if (!seen) finish(&run, SR_COMPLETE_SEEN_PARTIAL, "STORAGE_RECOVERY_SEEN_PARTIAL");
    else finish(&run, SR_COMPLETE, "STORAGE_RECOVERY_COMPLETE");
}
