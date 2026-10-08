#include "storage_recovery_core.h"
#include "reliability.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define SR_READ_CHUNK 4096U
#define SR_UID_BYTES 32U
#define SR_SERIAL_MAX 120U

typedef struct {
    const sr_ports_t *ports;
    sr_outcome_t *outcome;
    uint8_t *uids;
    size_t uid_capacity;
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

static bool remember_uid(sr_run_t *run, const char *row, size_t length)
{
    size_t value_length = 0;
    const char *value = json_string_value(row, length, "event_uid", &value_length);
    if (!value || value_length != SR_UID_BYTES * 2) return true;
    uint8_t uid[SR_UID_BYTES];
    for (size_t i = 0; i < SR_UID_BYTES; ++i) {
        int high = hex_value(value[i * 2]), low = hex_value(value[i * 2 + 1]);
        if (high < 0 || low < 0) return true;
        uid[i] = (uint8_t)((high << 4) | low);
    }
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

/* Returns 1 when acknowledged, 0 when the caller must stop. */
static int transfer_row(sr_run_t *run, const char *generation, uint64_t offset, const char *row, size_t length)
{
    size_t json_length = length;
    if (json_length && row[json_length - 1] == '\r') json_length--;
    bool valid = json_length > 0 && !memchr(row, 0, json_length) && rel_json_syntax_valid(row, json_length);
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

/* Stream one source. Each newline-terminated row (and a final unterminated
 * tail) is sent as exact bytes. Empty rows carry no data and are skipped. */
static bool transfer_source(sr_run_t *run, const char *path, unsigned index, off_t expected_size)
{
    char generation[80];
    snprintf(generation, sizeof(generation), "storage-recovery-v1-%u-%lld", index, (long long)expected_size);
    FILE *file = fopen(path, "rb");
    if (!file) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_OPEN"); return false; }
    char *chunk = malloc(SR_READ_CHUNK);
    char *row = malloc(SR_RECORD_MAX_BYTES + 1);
    if (!chunk || !row) {
        free(chunk); free(row); fclose(file);
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_MEMORY");
        return false;
    }
    bool ok = true;
    size_t row_length = 0;
    uint64_t position = 0, row_start = 0;
    for (;;) {
        size_t got = fread(chunk, 1, SR_READ_CHUNK, file);
        for (size_t i = 0; ok && i < got; ++i, ++position) {
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
            if (!empty) {
                int sent = transfer_row(run, generation, row_start, row, row_length);
                if (sent < 0) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_UID_MEMORY"); ok = false; }
                else if (sent == 0) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_STOPPED"); ok = false; }
            }
            row_length = 0;
            row_start = position + 1;
        }
        if (!ok || got < SR_READ_CHUNK) break;
    }
    if (ok && ferror(file)) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_READ"); ok = false; }
    if (ok && row_length && !(row_length == 1 && row[0] == '\r')) {
        int sent = transfer_row(run, generation, row_start, row, row_length);
        if (sent < 0) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_UID_MEMORY"); ok = false; }
        else if (sent == 0) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_STOPPED"); ok = false; }
    }
    if (ok && position != (uint64_t)expected_size) {
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_CHANGED");
        ok = false;
    }
    free(chunk);
    free(row);
    if (fclose(file) != 0 && ok) { finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_READ"); ok = false; }
    struct stat after;
    if (ok && (stat(path, &after) != 0 || after.st_size != expected_size)) {
        finish(run, SR_INCOMPLETE, "STORAGE_RECOVERY_SOURCE_CHANGED");
        ok = false;
    }
    return ok;
}

static bool append_seen_uids(sr_run_t *run, const char *acked_path)
{
    if (!run->outcome->uids) return true;
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
    for (uint32_t i = 0; ok && i < run->outcome->uids; ++i) {
        const uint8_t *uid = run->uids + (size_t)i * SR_UID_BYTES;
        for (size_t b = 0; b < SR_UID_BYTES; ++b) {
            line[b * 2] = alphabet[uid[b] >> 4];
            line[b * 2 + 1] = alphabet[uid[b] & 0x0f];
        }
        line[SR_UID_BYTES * 2] = '\n';
        line[SR_UID_BYTES * 2 + 1] = '\0';
        ok = fputs(line, file) >= 0;
        if (ok && (i + 1) % 256 == 0) ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
        if (ok) run->outcome->uids_appended = i + 1;
    }
    if (ok) ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    if (!ok && run->outcome->uids_appended) {
        /* Only fully synced 256-row groups are counted as durable. */
        run->outcome->uids_appended -= run->outcome->uids_appended % 256;
    }
    return ok;
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
    for (size_t i = 0; i < source_count; ++i) {
        if (!present[i]) continue;
        outcome->files++;
        if (!transfer_source(&run, sources[i], (unsigned)i, sizes[i])) { free(run.uids); return; }
    }
    char message[200];
    snprintf(message, sizeof(message),
             "ADD acknowledged every retained blocked row: files=%lu rows=%lu malformed=%lu bytes=%llu uids=%lu",
             (unsigned long)outcome->files, (unsigned long)outcome->records, (unsigned long)outcome->malformed,
             (unsigned long long)outcome->bytes, (unsigned long)outcome->uids);
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
