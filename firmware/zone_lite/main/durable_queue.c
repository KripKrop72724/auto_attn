#include "durable_queue.h"
#include <errno.h>
#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define DQ_MAGIC 0x32514c5aU
#define HEADER_BYTES 16U
uint32_t dq_crc32(const void *data, size_t length)
{
    const unsigned char *bytes = data;
    uint32_t crc = ~0U;
    while (length--) {
        crc ^= *bytes++;
        for (unsigned bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
static bool name(const durable_queue_t *q, uint32_t segment, char path[160])
{
    int n = snprintf(path, 160, "%s%08lx.q", q->prefix, (unsigned long)segment);
    return n > 0 && n < 160;
}
static bool commit(durable_queue_t *q, dq_checkpoint_t next)
{
    next.generation++;
    if (!next.generation) return false;
    next.crc = dq_crc32(&next, offsetof(dq_checkpoint_t, crc));
    if (!q->port.commit(q->port.context, &next)) {
        // An unsuccessful commit may still have reached flash. Reopen before
        // any further append; never overwrite a possibly committed record.
        q->ready = false;
        return false;
    }
    q->checkpoint = next;
    return true;
}
static bool checkpoint_valid(const dq_checkpoint_t *c)
{
    return c->version == DQ_CHECKPOINT_VERSION && c->generation &&
        c->read_segment <= c->write_segment && c->read_offset <= DQ_SEGMENT_BYTES &&
        c->write_offset <= DQ_SEGMENT_BYTES &&
        (c->read_segment != c->write_segment || c->read_offset <= c->write_offset) &&
        c->crc == dq_crc32(c, offsetof(dq_checkpoint_t, crc));
}

/* A crash after the retirement checkpoint but before unlink must not leak a
 * segment forever. Only the committed read segment authorizes reclamation. */
static bool reclaim_retired(durable_queue_t *q)
{
    char directory[128];
    const char *base = strrchr(q->prefix, '/');
    if (base) {
        size_t n = (size_t)(base - q->prefix);
        memcpy(directory, q->prefix, n); directory[n] = 0; ++base;
        if (!n) strcpy(directory, "/");
    } else { strcpy(directory, "."); base = q->prefix; }
    DIR *dir = opendir(directory);
    if (!dir) return false;
    bool ok = true;
    unsigned reclaimed = 0;
    size_t prefix_length = strlen(base);
    struct dirent *entry;
    while (reclaimed < 8) {
        errno = 0;
        entry = readdir(dir);
        if (!entry) { if (errno) ok = false; break; }
        if (strncmp(entry->d_name, base, prefix_length) ||
            strlen(entry->d_name) != prefix_length + 10 ||
            strcmp(entry->d_name + prefix_length + 8, ".q")) continue;
        uint32_t segment = 0;
        bool valid = true;
        for (unsigned i = 0; i < 8; ++i) {
            char c = entry->d_name[prefix_length + i];
            unsigned digit = c >= '0' && c <= '9' ? (unsigned)(c - '0') :
                c >= 'a' && c <= 'f' ? (unsigned)(c - 'a' + 10) : 16U;
            if (digit > 15) { valid = false; break; }
            segment = (segment << 4) | digit;
        }
        if (!valid || segment >= q->checkpoint.read_segment) continue;
        char path[160];
        if (!name(q, segment, path) || (remove(path) != 0 && errno != ENOENT)) {
            ok = false; break;
        }
        ++reclaimed;
    }
    if (closedir(dir) != 0) ok = false;
    return ok;
}
dq_result_t dq_open(durable_queue_t *q, const char *prefix, dq_port_t port)
{
    if (!q || !prefix || strlen(prefix) >= sizeof(q->prefix) || !port.load || !port.commit) return DQ_IO;
    memset(q, 0, sizeof(*q));
    strcpy(q->prefix, prefix); q->port = port;
    int loaded = port.load(port.context, &q->checkpoint);
    if (loaded < 0 || (loaded && !checkpoint_valid(&q->checkpoint))) return DQ_CORRUPT;
    if (!loaded) {
        char directory[128];
        const char *base = strrchr(prefix, '/');
        if (base) {
            size_t n = (size_t)(base - prefix);
            memcpy(directory, prefix, n); directory[n] = 0; base++;
            if (!n) strcpy(directory, "/");
        } else { strcpy(directory, "."); base = prefix; }
        DIR *dir = opendir(directory);
        if (!dir) return DQ_IO;
        struct dirent *entry;
        bool orphaned = false;
        while ((entry = readdir(dir)) != NULL) {
            if (!strncmp(entry->d_name, base, strlen(base))) { orphaned = true; break; }
        }
        closedir(dir);
        if (orphaned) return DQ_CORRUPT;
        dq_checkpoint_t initial = {.version = DQ_CHECKPOINT_VERSION, .next_sequence = 1};
        if (!commit(q, initial)) return DQ_IO;
    }
    if (!reclaim_retired(q)) return DQ_IO;
    q->ready = true;
    return DQ_OK;
}
static void encode32(unsigned char *b, uint32_t n)
{
    for (unsigned i = 0; i < 4; i++) b[i] = (unsigned char)(n >> (i * 8));
}
static uint32_t decode32(const unsigned char *b)
{
    return (uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
}
dq_result_t dq_append(durable_queue_t *q, const void *data, size_t length)
{
    if (!q || !q->ready) return DQ_IO;
    if (!data || !length || length > DQ_MAX_RECORD_BYTES) return DQ_FULL;
    if (q->port.admit && !q->port.admit(q->port.context, length + HEADER_BYTES)) return DQ_FULL;
    dq_checkpoint_t next = q->checkpoint;
    if (next.depth == UINT32_MAX || next.next_sequence == UINT32_MAX) return DQ_FULL;
    if (next.write_offset + length + 2 * HEADER_BYTES > DQ_SEGMENT_BYTES) {
        if (next.write_segment == UINT32_MAX) return DQ_FULL;
        char sealed_path[160];
        if (!name(q, next.write_segment, sealed_path)) return DQ_IO;
        FILE *sealed = fopen(sealed_path, "r+b");
        unsigned char seal[HEADER_BYTES] = {0};
        encode32(seal, DQ_MAGIC);
        bool sealed_ok = sealed && fseek(sealed, (long)next.write_offset, SEEK_SET) == 0 &&
            fwrite(seal, 1, sizeof(seal), sealed) == sizeof(seal) && fflush(sealed) == 0 && fsync(fileno(sealed)) == 0;
        if (sealed && fclose(sealed) != 0) sealed_ok = false;
        if (!sealed_ok) return DQ_IO;
        next.write_segment++; next.write_offset = 0;
    }
    char path[160];
    if (!name(q, next.write_segment, path)) return DQ_IO;
    FILE *f = fopen(path, next.write_offset ? "r+b" : "wb");
    if (!f) return DQ_IO;
    unsigned char header[HEADER_BYTES];
    encode32(header, DQ_MAGIC); encode32(header + 4, (uint32_t)length);
    encode32(header + 8, next.next_sequence); encode32(header + 12, dq_crc32(data, length));
    bool ok = fseek(f, (long)next.write_offset, SEEK_SET) == 0 &&
        fwrite(header, 1, sizeof(header), f) == sizeof(header) &&
        fwrite(data, 1, length, f) == length && fflush(f) == 0 && fsync(fileno(f)) == 0;
    if (fclose(f) != 0) ok = false;
    if (!ok) return DQ_IO;
    next.write_offset += (uint32_t)length + HEADER_BYTES;
    next.next_sequence++; next.depth++;
    return commit(q, next) ? DQ_OK : DQ_IO;
}
dq_result_t dq_peek(durable_queue_t *q, void *data, size_t capacity, size_t *length, dq_token_t *token)
{
    if (!q || !q->ready || !data || !length || !token) return DQ_IO;
    dq_checkpoint_t *c = &q->checkpoint;
    if (c->read_segment == c->write_segment && c->read_offset == c->write_offset)
        return c->depth == 0 ? DQ_EMPTY : DQ_CORRUPT;
    uint32_t segment = c->read_segment, offset = c->read_offset;
    char path[160];
    if (!name(q, segment, path)) return DQ_IO;
    FILE *f = fopen(path, "rb");
    if (!f) return DQ_IO;
    unsigned char header[HEADER_BYTES];
    bool ok = fseek(f, (long)offset, SEEK_SET) == 0 &&
        fread(header, 1, sizeof(header), f) == sizeof(header);
    if (ok && decode32(header) == DQ_MAGIC && decode32(header + 4) == 0 &&
        segment < c->write_segment) {
        if (fclose(f) != 0) return DQ_IO;
        segment++; offset = 0;
        if (!name(q, segment, path)) return DQ_IO;
        f = fopen(path, "rb");
        if (!f) return DQ_IO;
        ok = fread(header, 1, sizeof(header), f) == sizeof(header);
    }
    uint32_t n = ok ? decode32(header + 4) : 0;
    if (!ok || decode32(header) != DQ_MAGIC || !n || n > DQ_MAX_RECORD_BYTES || n > capacity ||
        offset + HEADER_BYTES + n > DQ_SEGMENT_BYTES ||
        (segment == c->write_segment && offset + HEADER_BYTES + n > c->write_offset)) {
        fclose(f); return DQ_CORRUPT;
    }
    ok = fread(data, 1, n, f) == n && !ferror(f);
    if (fclose(f) != 0) ok = false;
    if (!ok) return DQ_IO;
    if (dq_crc32(data, n) != decode32(header + 12)) return DQ_CORRUPT;
    *length = n;
    *token = (dq_token_t){segment, offset, offset + HEADER_BYTES + n,
                          decode32(header + 8), decode32(header + 12)};
    return DQ_OK;
}
dq_result_t dq_settle(durable_queue_t *q, const dq_token_t *token)
{
    if (!q || !q->ready || !token) return DQ_IO;
    dq_checkpoint_t next = q->checkpoint;
    // Revalidate the exact head after the caller releases the lock to send.
    unsigned char header[HEADER_BYTES]; char path[160];
    if (token->segment < next.read_segment || token->segment > next.read_segment + 1 ||
        (token->segment == next.read_segment && token->offset != next.read_offset) ||
        token->segment > next.write_segment || !next.depth || !name(q, token->segment, path)) return DQ_STALE;
    if (token->segment != next.read_segment) {
        char previous[160]; unsigned char seal[HEADER_BYTES];
        if (token->offset || !name(q, next.read_segment, previous)) return DQ_STALE;
        FILE *old = fopen(previous, "rb");
        bool sealed = old && fseek(old, (long)next.read_offset, SEEK_SET) == 0 &&
            fread(seal, 1, sizeof(seal), old) == sizeof(seal) &&
            decode32(seal) == DQ_MAGIC && decode32(seal + 4) == 0;
        if (old && fclose(old) != 0) return DQ_IO;
        if (!sealed) return DQ_STALE;
    }
    FILE *f = fopen(path, "rb");
    bool ok = f && fseek(f, (long)token->offset, SEEK_SET) == 0 &&
        fread(header, 1, sizeof(header), f) == sizeof(header);
    if (f && fclose(f) != 0) ok = false;
    if (!ok || decode32(header) != DQ_MAGIC || decode32(header+8) != token->sequence ||
        decode32(header+12) != token->crc || token->end != token->offset + HEADER_BYTES + decode32(header+4)) return DQ_STALE;
    uint32_t retired = next.read_segment;
    next.read_segment = token->segment; next.read_offset = token->end; next.depth--;
    if (next.depth == 0 && next.read_segment == next.write_segment &&
        next.read_offset == next.write_offset) {
        if (next.write_segment == UINT32_MAX) return DQ_FULL;
        next.write_segment++; next.read_segment = next.write_segment;
        next.write_offset = 0; next.read_offset = 0;
    }
    if (!commit(q, next)) return DQ_IO;
    if (retired != next.read_segment && !reclaim_retired(q)) {
        q->ready = false; return DQ_IO;
    }
    return DQ_OK;
}
