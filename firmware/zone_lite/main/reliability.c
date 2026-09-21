#include "reliability.h"
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

rel_id_result_t rel_id_file_contains(const char *path, const char *id, size_t max_id_length)
{
    if (!path || !id || !*id || strlen(id) >= max_id_length) return REL_ID_ERROR;
    FILE *file = fopen(path, "rb");
    if (!file) return errno == ENOENT ? REL_ID_ABSENT : REL_ID_ERROR;
    char line[128];
    rel_id_result_t result = REL_ID_ABSENT;
    while (fgets(line, sizeof(line), file)) {
        if (!strchr(line, '\n')) {
            result = REL_ID_ERROR;
            break;
        }
        size_t length = strcspn(line, "\r\n");
        line[length] = '\0';
        if (!strcmp(line, id)) { result = REL_ID_PRESENT; break; }
    }
    if (ferror(file)) result = REL_ID_ERROR;
    if (fclose(file) != 0) result = REL_ID_ERROR;
    return result;
}

bool rel_append_bounded_id(const char *path, const char *id, size_t max_bytes, size_t max_id_length)
{
    if (!path || !id || !*id || strlen(id) >= max_id_length || strlen(id) + 1 > max_bytes) {
        errno = EINVAL;
        return false;
    }
    FILE *file = rel_open_append(path);
    if (!file) return false;
    struct stat st;
    bool ok = fstat(fileno(file), &st) == 0 && st.st_size >= 0 &&
        (uintmax_t)st.st_size <= max_bytes - (strlen(id) + 1);
    if (ok) ok = fprintf(file, "%s\n", id) > 0 && fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    return ok;
}

FILE *rel_open_append(const char *path)
{
    FILE *file = fopen(path, "a+b");
    if (!file) return NULL;
    bool ok = fseek(file, 0, SEEK_END) == 0;
    long length = ok ? ftell(file) : -1;
    if (length < 0) ok = false;
    if (ok && length > 0) {
        ok = fseek(file, -1, SEEK_END) == 0 && fgetc(file) == '\n';
        if (!ok && !ferror(file)) errno = EBADMSG;
    }
    if (ok) ok = fseek(file, 0, SEEK_END) == 0;
    if (!ok) {
        int error = errno;
        (void)fclose(file);
        errno = error;
        return NULL;
    }
    return file;
}

rel_scan_result_t rel_count_rows(FILE *file, uint32_t *count)
{
    if (!file || !count) return REL_SCAN_ERROR;
    uint32_t rows = 0;
    bool partial = false;
    int c;
    while ((c = fgetc(file)) != EOF) {
        partial = true;
        if (c == '\n') {
            if (rows == UINT32_MAX) return REL_SCAN_ERROR;
            rows++;
            partial = false;
        }
    }
    if (ferror(file) || partial) return REL_SCAN_ERROR;
    *count = rows;
    return rows ? REL_SCAN_OK : REL_SCAN_EMPTY;
}

bool rel_settled_eof(FILE *file, off_t boundary)
{
    struct stat st;
    if (!file || boundary <= 0 || fstat(fileno(file), &st) != 0 ||
        boundary != st.st_size || fseeko(file, boundary - 1, SEEK_SET) != 0) return false;
    return fgetc(file) == '\n' && fgetc(file) == EOF && !ferror(file);
}

typedef struct { const char *p, *end; } json_cursor_t;
static void whitespace(json_cursor_t *c)
{
    while (c->p < c->end && strchr(" \t\r\n", *c->p) && *c->p) c->p++;
}
static bool json_string(json_cursor_t *c)
{
    if (c->p == c->end || *c->p++ != '"') return false;
    while (c->p < c->end) {
        unsigned char ch = (unsigned char)*c->p++;
        if (ch == '"') return true;
        if (ch < 0x20) return false;
        if (ch == '\\') {
            if (c->p == c->end) return false;
            ch = (unsigned char)*c->p++;
            if (ch == 'u') {
                for (int i = 0; i < 4; i++) {
                    if (c->p == c->end || !isxdigit((unsigned char)*c->p++)) return false;
                }
            } else if (!ch || !strchr("\"\\/bfnrt", ch)) return false;
        }
    }
    return false;
}
static bool json_value(json_cursor_t *c, unsigned depth)
{
    whitespace(c);
    if (c->p == c->end || depth > 32) return false;
    if (*c->p == '"') return json_string(c);
    if (*c->p == '{' || *c->p == '[') {
        bool object = *c->p++ == '{';
        char close = object ? '}' : ']';
        whitespace(c);
        if (c->p < c->end && *c->p == close) { c->p++; return true; }
        for (;;) {
            if (object) {
                if (!json_string(c)) return false;
                whitespace(c);
                if (c->p == c->end || *c->p++ != ':') return false;
            }
            if (!json_value(c, depth + 1)) return false;
            whitespace(c);
            if (c->p == c->end) return false;
            char next = *c->p++;
            if (next == close) return true;
            if (next != ',') return false;
            whitespace(c);
        }
    }
    const char *words[] = {"true", "false", "null"};
    for (unsigned i = 0; i < 3; i++) {
        size_t n = strlen(words[i]);
        if ((size_t)(c->end - c->p) >= n && !memcmp(c->p, words[i], n)) {
            c->p += n; return true;
        }
    }
    if (*c->p == '-') c->p++;
    if (c->p == c->end) return false;
    if (*c->p == '0') c->p++;
    else {
        if (*c->p < '1' || *c->p > '9') return false;
        do { c->p++; } while (c->p < c->end && isdigit((unsigned char)*c->p));
    }
    if (c->p < c->end && *c->p == '.') {
        c->p++;
        if (c->p == c->end || !isdigit((unsigned char)*c->p)) return false;
        while (c->p < c->end && isdigit((unsigned char)*c->p)) c->p++;
    }
    if (c->p < c->end && (*c->p == 'e' || *c->p == 'E')) {
        c->p++;
        if (c->p < c->end && (*c->p == '+' || *c->p == '-')) c->p++;
        if (c->p == c->end || !isdigit((unsigned char)*c->p)) return false;
        while (c->p < c->end && isdigit((unsigned char)*c->p)) c->p++;
    }
    return true;
}
bool rel_json_syntax_valid(const char *text, size_t length)
{
    if (!text) return false;
    json_cursor_t c = {text, text + length};
    if (!json_value(&c, 0)) return false;
    whitespace(&c);
    return c.p == c.end;
}

bool rel_live_frame_size(size_t length, size_t hint, size_t *record_size)
{
    if (!record_size) return false;
    if (hint == 12 || hint == 32 || hint == 36 || hint == 52) {
        if (!length || length % hint) return false;
        *record_size = hint;
        return true;
    }
    // 36 bytes could be one extended record or three 12-byte records.
    // Length alone cannot establish that format for a new session.
    if (length != 12 && length != 32 && length != 52) return false;
    *record_size = length;
    return true;
}
bool rel_parse_live_record(const uint8_t *data, size_t length, rel_live_record_t *out)
{
    if (!data || !out || (length != 12 && length != 32 && length != 36 && length != 52)) return false;
    memset(out, 0, sizeof(*out));
    size_t base;
    if (length == 12) {
        uint32_t id = (uint32_t)data[0] | (uint32_t)data[1] << 8 |
                      (uint32_t)data[2] << 16 | (uint32_t)data[3] << 24;
        snprintf(out->user_id, sizeof(out->user_id), "%lu", (unsigned long)id);
        base = 4;
    } else {
        size_t n = 0;
        while (n < 24 && data[n]) { out->user_id[n] = (char)data[n]; n++; }
        while (n && isspace((unsigned char)out->user_id[n - 1])) out->user_id[--n] = 0;
        base = 24;
    }
    const uint8_t *t = data + base + 2;
    if (t[1] < 1 || t[1] > 12 || t[2] < 1 || t[2] > 31 ||
        t[3] > 23 || t[4] > 59 || t[5] > 59) return false;
    uint64_t timestamp = (((((uint64_t)t[0] * 12 + t[1] - 1) * 31 + t[2] - 1)
                           * 24 + t[3]) * 60 + t[4]) * 60 + t[5];
    if (timestamp > UINT32_MAX) return false;
    out->timestamp = (uint32_t)timestamp;
    out->status = data[base]; out->punch = data[base + 1];
    return out->user_id[0] != 0;
}
bool rel_identity_matches(const char *saved_serial, const char *current_serial,
                          const char *saved_fingerprint, const char *current_fingerprint)
{
    return saved_serial && current_serial && saved_serial[0] &&
        !strcmp(saved_serial, current_serial) && saved_fingerprint && current_fingerprint &&
        strlen(saved_fingerprint) == 64 && strlen(current_fingerprint) == 64 &&
        !strcmp(saved_fingerprint, current_fingerprint);
}
