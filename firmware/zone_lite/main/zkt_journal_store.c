#include "zkt_journal_store.h"
#include "durable_queue.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CHECKPOINT_EVIDENCE_BYTES (8U + ZJ_CHECKPOINT_BYTES)
static zj_result_t read_bytes(zj_store_t *, uint64_t, uint32_t, uint8_t *, size_t);

static void put32(uint8_t *p, uint32_t v)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static void put64(uint8_t *p, uint64_t v)
{
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p)
{
    uint64_t v = 0;
    for (unsigned i = 0; i < 8; ++i) v |= (uint64_t)p[i] << (8 * i);
    return v;
}
static zj_result_t failure(zj_store_t *s, const char *operation, int error, zj_result_t result)
{
    s->last_operation = operation;
    s->last_errno = error ? error : EIO;
    return result;
}
static bool commit_file(zj_store_t *s, FILE *file, const uint8_t *bytes, size_t length,
                         bool header, bool positioned)
{
    /* Capture the first failure before close/stat can replace errno. Close is
     * still attempted exactly once; a failed close can never return success. */
    bool ok = positioned;
    if (ok && fwrite(bytes, 1, length, file) != length) {
        failure(s, header ? "segment_header_write" : "record_write", errno, ZJ_UNCERTAIN);
        ok = false;
    }
    if (ok && fflush(file) != 0) {
        failure(s, header ? "segment_header_flush" : "record_flush", errno, ZJ_UNCERTAIN);
        ok = false;
    }
    if (ok && fsync(fileno(file)) != 0) {
        failure(s, header ? "segment_header_sync" : "record_sync", errno, ZJ_UNCERTAIN);
        ok = false;
    }
    if (fclose(file) != 0 && ok) {
        failure(s, header ? "segment_header_close" : "record_close", errno, ZJ_UNCERTAIN);
        ok = false;
    }
    return ok;
}
static bool filename(const zj_store_t *s, uint64_t id, char out[144])
{
    int n = snprintf(out, 144, "%s%016llx.j", s->prefix, (unsigned long long)id);
    return n > 0 && n < 144;
}
static bool file_id(const char *name, uint64_t *id)
{
    if (strlen(name) != 18 || strcmp(name + 16, ".j")) return false;
    uint64_t value = 0;
    for (unsigned i = 0; i < 16; ++i) {
        unsigned digit = name[i] >= '0' && name[i] <= '9' ? (unsigned)(name[i] - '0') :
            name[i] >= 'a' && name[i] <= 'f' ? (unsigned)(name[i] - 'a' + 10) : 16U;
        if (digit > 15) return false;
        value = value << 4 | digit;
    }
    if (!value || value > ZJ_SEQUENCE_MAX) return false;
    *id = value;
    return true;
}
static bool load_checkpoint(zj_store_t *s, const uint8_t cp[ZJ_CHECKPOINT_BYTES])
{
    if (memcmp(cp, "ZJCP0001", 8) || get32(cp + 76) != dq_crc32(cp, 76) ||
        !get64(cp + 8) || get64(cp + 16) > ZJ_SEQUENCE_MAX ||
        get64(cp + 32) > ZJ_SEQUENCE_MAX || get32(cp + 28) || get32(cp + 72)) return false;
    s->checkpoint_revision = get64(cp + 8);
    s->read_segment = get64(cp + 16);
    s->read_offset = get32(cp + 24);
    s->last_sequence = get64(cp + 32);
    return s->read_segment || (!s->read_offset && !s->last_sequence);
}
static void recovery_pending(zj_store_t *s)
{
    s->checkpoint_recovery_pending = false;
    for (unsigned i = 0; i < s->count; ++i) {
        const zj_segment_t *segment = &s->segments[i];
        if (segment->checkpoint_evidence && (segment->id > s->read_segment ||
            (segment->id == s->read_segment && s->read_offset < segment->size)))
            s->checkpoint_recovery_pending = true;
    }
}
static zj_result_t preserve_checkpoint(zj_store_t *s, const uint8_t cp[ZJ_CHECKPOINT_BYTES])
{
    /* Preserve the exact damaged bytes before replacing a retirement cursor.
     * The fresh segment identity consumes the existing durable nonce allocator;
     * no encryption root, epoch or counter is reconstructed from file contents.
     * This opaque segment stays in the ordinary custody lane until ADD commits
     * its receipt. A reboot before reset reuses the same complete evidence. */
    uint8_t evidence[CHECKPOINT_EVIDENCE_BYTES];
    memcpy(evidence, "ZJCPE001", 8);
    memcpy(evidence + 8, cp, ZJ_CHECKPOINT_BYTES);
    for (unsigned i = 0; i < s->count; ++i) {
        const zj_segment_t *segment = &s->segments[i];
        if (!segment->checkpoint_evidence) continue;
        uint8_t existing[CHECKPOINT_EVIDENCE_BYTES];
        zj_result_t result = read_bytes(s, segment->id, 0, existing, sizeof(existing));
        if (result != ZJ_OK) return result;
        if (memcmp(existing, evidence, sizeof(existing))) continue;
        char path[144];
        if (!filename(s, segment->id, path)) return ZJ_INVALID;
        errno = 0;
        FILE *file = fopen(path, "r+b");
        if (!file) return failure(s, "checkpoint_evidence_open", errno, ZJ_IO);
        /* A previous failed fsync is not proof of durability just because a
         * read succeeds. Synchronize and close without rewriting the bytes. */
        bool ok = fsync(fileno(file)) == 0;
        int error = ok ? 0 : errno;
        if (fclose(file) != 0 && ok) { error = errno; ok = false; }
        return ok ? ZJ_OK : failure(s, "checkpoint_evidence_sync", error, ZJ_UNCERTAIN);
    }
    if (s->count == ZJ_SEGMENTS_MAX) return failure(s, "checkpoint_evidence_capacity", ENOSPC, ZJ_FULL);
    errno = 0;
    if (!s->port.admit(s->port.context, sizeof(evidence)))
        return failure(s, "checkpoint_evidence_admission", errno ? errno : ENOSPC,
                       errno && errno != ENOSPC ? ZJ_IO : ZJ_FULL);
    uint64_t id;
    if (!zj_sequence_next(&s->sequence, &id)) return failure(s, "nonce_reservation", EIO, ZJ_IO);
    char path[144];
    if (!filename(s, id, path)) return ZJ_INVALID;
    errno = 0;
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return failure(s, "checkpoint_evidence_create", errno, ZJ_IO);
    FILE *file = fdopen(fd, "wb");
    if (!file) { int error = errno; close(fd); return failure(s, "checkpoint_evidence_fdopen", error, ZJ_IO); }
    if (!commit_file(s, file, evidence, sizeof(evidence), false, true)) return ZJ_UNCERTAIN;
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size != sizeof(evidence))
        return failure(s, "checkpoint_evidence_extent", errno, ZJ_UNCERTAIN);
    s->segments[s->count++] = (zj_segment_t){.id = id, .size = sizeof(evidence), .checkpoint_evidence = true};
    return ZJ_OK;
}
static int compare_segments(const void *left, const void *right)
{
    const zj_segment_t *a = left, *b = right;
    return a->id < b->id ? -1 : a->id > b->id;
}
static zj_result_t read_header(zj_store_t *s, const zj_segment_t *segment,
                               uint8_t header[ZJ_META_BYTES], zj_metadata_t *metadata)
{
    if (segment->size < ZJ_META_BYTES) return ZJ_CORRUPT;
    char path[144];
    if (!filename(s, segment->id, path)) return ZJ_INVALID;
    errno = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return failure(s, "header_open", errno, ZJ_IO);
    bool ok = fread(header, 1, ZJ_META_BYTES, f) == ZJ_META_BYTES && !ferror(f);
    int error = ok ? 0 : errno;
    if (fclose(f) != 0 && ok) { error = errno; ok = false; }
    if (!ok) return failure(s, "header_read", error, ZJ_IO);
    return zj_metadata_decode(header, metadata) && metadata->segment_id == segment->id ? ZJ_OK : ZJ_CORRUPT;
}

zj_result_t zj_store_open(zj_store_t *s, const char *prefix, const zj_metadata_t *metadata,
                          uint64_t persisted_limit, zj_store_port_t port)
{
    if (!s || !prefix || !metadata || strlen(prefix) >= sizeof(s->prefix) ||
        !port.load || !port.commit || !port.reserve || !port.admit ||
        !port.crypto.seal || !port.crypto.open || !port.crypto.digest) return ZJ_INVALID;
    memset(s, 0, sizeof(*s));
    s->port = port;
    s->writer_metadata = *metadata;
    if (!zj_metadata_encode(metadata, s->writer_header) ||
        !zj_sequence_init(&s->sequence, persisted_limit, port.reserve, port.context)) return ZJ_INVALID;
    strcpy(s->prefix, prefix);
    uint8_t cp[ZJ_CHECKPOINT_BYTES];
    int loaded = port.load(port.context, cp);
    if (loaded < 0) return failure(s, "checkpoint_load", EIO, ZJ_IO);
    bool replay = loaded && (!load_checkpoint(s, cp) || s->read_segment >= persisted_limit ||
                            s->last_sequence >= persisted_limit);
    if (replay) s->read_segment = s->read_offset = s->last_sequence = s->checkpoint_revision = 0;
    char directory[112];
    const char *base = strrchr(prefix, '/');
    if (base) {
        size_t length = (size_t)(base - prefix);
        memcpy(directory, prefix, length);
        directory[length] = 0;
        if (!length) strcpy(directory, "/");
        ++base;
    } else { strcpy(directory, "."); base = prefix; }
    if (!*base) return ZJ_INVALID;
    DIR *dir = opendir(directory);
    if (!dir) return failure(s, "directory_open", errno, ZJ_IO);
    zj_result_t result = ZJ_OK;
    struct dirent *entry;
    for (;;) {
        errno = 0;
        entry = readdir(dir);
        if (!entry) {
            if (errno) result = failure(s, "directory_read", errno, ZJ_IO);
            break;
        }
        size_t base_length = strlen(base);
        if (strncmp(entry->d_name, base, base_length)) continue;
        uint64_t id;
        if (!file_id(entry->d_name + base_length, &id) || id >= persisted_limit) {
            result = failure(s, "segment_identity", EBADMSG, ZJ_CORRUPT);
            break;
        }
        if (s->count == ZJ_SEGMENTS_MAX) { result = failure(s, "segment_capacity", ENOSPC, ZJ_FULL); break; }
        char path[144];
        struct stat st;
        if (!filename(s, id, path) || stat(path, &st) != 0 || !S_ISREG(st.st_mode) ||
            st.st_size < 0 || (uint64_t)st.st_size > UINT32_MAX) {
            result = failure(s, "segment_stat", errno, ZJ_IO);
            break;
        }
        zj_segment_t segment = {.id = id, .size = (uint32_t)st.st_size};
        uint8_t header[ZJ_META_BYTES];
        zj_metadata_t parsed;
        zj_result_t checked = read_header(s, &segment, header, &parsed);
        if (checked != ZJ_OK && checked != ZJ_CORRUPT) { result = checked; break; }
        segment.metadata_valid = checked == ZJ_OK;
        if (segment.size == CHECKPOINT_EVIDENCE_BYTES) {
            uint8_t magic[8];
            checked = read_bytes(s, segment.id, 0, magic, sizeof(magic));
            if (checked != ZJ_OK) { result = checked; break; }
            segment.checkpoint_evidence = !memcmp(magic, "ZJCPE001", sizeof(magic));
        }
        if (id == s->read_segment && (s->read_offset > segment.size ||
            (segment.metadata_valid && s->read_offset < ZJ_META_BYTES))) replay = true;
        s->segments[s->count++] = segment;
    }
    if (closedir(dir) != 0 && result == ZJ_OK) result = failure(s, "directory_close", errno, ZJ_IO);
    if (result != ZJ_OK) return result;
    qsort(s->segments, s->count, sizeof(s->segments[0]), compare_segments);
    if (replay) {
        s->read_segment = s->read_offset = s->last_sequence = s->checkpoint_revision = 0;
        result = preserve_checkpoint(s, cp);
        if (result != ZJ_OK) return result;
        /* Reset means replay from the earliest retained byte, never infer a
         * destination receipt from a damaged checkpoint. Already reclaimed
         * segments are not recreated; their former ADD custody is unchanged. */
        memset(cp, 0, sizeof(cp));
        memcpy(cp, "ZJCP0001", 8);
        put64(cp + 8, 1);
        put32(cp + 76, dq_crc32(cp, 76));
        if (!port.commit(port.context, cp)) return failure(s, "checkpoint_replay_commit", EIO, ZJ_UNCERTAIN);
        if (!load_checkpoint(s, cp)) return ZJ_CORRUPT;
    }
    recovery_pending(s);
    s->ready = true;
    return ZJ_OK;
}

static zj_segment_t *writer(zj_store_t *s)
{
    if (!s->writer_id) return NULL;
    for (unsigned i = 0; i < s->count; ++i) if (s->segments[i].id == s->writer_id) return &s->segments[i];
    return NULL;
}
static zj_result_t create_segment(zj_store_t *s)
{
    if (s->count == ZJ_SEGMENTS_MAX) return failure(s, "segment_capacity", ENOSPC, ZJ_FULL);
    if (!s->port.admit(s->port.context, ZJ_META_BYTES + ZJ_RECORD_MAX)) return ZJ_FULL;
    uint64_t id;
    if (!zj_sequence_next(&s->sequence, &id)) return failure(s, "nonce_reservation", EIO, ZJ_IO);
    s->writer_metadata.segment_id = id;
    if (!zj_metadata_encode(&s->writer_metadata, s->writer_header)) return ZJ_INVALID;
    char path[144];
    if (!filename(s, id, path)) return ZJ_INVALID;
    errno = 0;
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return failure(s, "segment_create", errno, ZJ_IO);
    /* Inventory owns even a partially created file. Recovery never silently
     * discards it or reuses this sequence after a close/sync failure. */
    zj_segment_t *segment = &s->segments[s->count++];
    *segment = (zj_segment_t){.id = id};
    FILE *f = fdopen(fd, "wb");
    if (!f) { int error = errno; close(fd); return failure(s, "segment_fdopen", error, ZJ_IO); }
    bool ok = commit_file(s, f, s->writer_header, ZJ_META_BYTES, true, true);
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size < 0 || st.st_size > ZJ_SEGMENT_BYTES) {
        s->ready = false;
        return failure(s, "segment_created_stat", errno, ZJ_UNCERTAIN);
    }
    segment->size = (uint32_t)st.st_size;
    segment->metadata_valid = ok && segment->size == ZJ_META_BYTES;
    if (!segment->metadata_valid) {
        if (ok) failure(s, "segment_header_extent", EIO, ZJ_UNCERTAIN);
        return ZJ_UNCERTAIN;
    }
    s->writer_id = id;
    return ZJ_OK;
}

zj_result_t zj_store_append(zj_store_t *s, const zj_observation_t *observation, uint64_t *capture_sequence)
{
    if (capture_sequence) *capture_sequence = 0;
    if (!s || !s->ready || !observation || !capture_sequence) return ZJ_INVALID;
    zj_observation_t value = *observation;
    value.sequence = 1; /* The storage owner, not the caller, allocates identity. */
    if (!zj_observation_valid(&value)) return ZJ_INVALID;
    zj_segment_t *segment = writer(s);
    if (!segment || segment->size > ZJ_SEGMENT_BYTES - ZJ_RECORD_MAX) {
        s->writer_id = 0;
        zj_result_t created = create_segment(s);
        if (created != ZJ_OK) return created;
        segment = writer(s);
    }
    if (!s->port.admit(s->port.context, ZJ_RECORD_MAX)) return ZJ_FULL;
    if (!zj_sequence_next(&s->sequence, &value.sequence)) return failure(s, "nonce_reservation", EIO, ZJ_IO);
    uint8_t record[ZJ_RECORD_MAX];
    size_t length;
    if (zj_record_encode(s->writer_header, &value, s->port.crypto, record, sizeof(record), &length) != ZJ_CODEC_OK)
        return failure(s, "record_encode", EINVAL, ZJ_INVALID);
    char path[144];
    if (!filename(s, segment->id, path)) return ZJ_INVALID;
    errno = 0;
    FILE *f = fopen(path, "r+b");
    if (!f) return failure(s, "record_open", errno, ZJ_IO);
    bool positioned = fseek(f, 0, SEEK_END) == 0;
    if (!positioned) failure(s, "record_seek", errno, ZJ_UNCERTAIN);
    else if (ftell(f) != segment->size) {
        failure(s, "record_extent", errno, ZJ_UNCERTAIN);
        positioned = false;
    }
    bool ok = commit_file(s, f, record, length, false, positioned);
    if (ok) {
        segment->size += (uint32_t)length;
        *capture_sequence = value.sequence;
        return ZJ_OK;
    }
    s->writer_id = 0;
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size < 0 || st.st_size > ZJ_SEGMENT_BYTES) s->ready = false;
    else segment->size = (uint32_t)st.st_size;
    return ZJ_UNCERTAIN;
}

static zj_result_t read_bytes(zj_store_t *s, uint64_t id, uint32_t offset, uint8_t *out, size_t length)
{
    char path[144];
    if (!filename(s, id, path)) return ZJ_INVALID;
    errno = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return failure(s, "record_read_open", errno, ZJ_IO);
    bool ok = fseek(f, (long)offset, SEEK_SET) == 0 && fread(out, 1, length, f) == length && !ferror(f);
    int error = ok ? 0 : errno;
    if (fclose(f) != 0 && ok) { error = errno; ok = false; }
    return ok ? ZJ_OK : failure(s, "record_read", error, ZJ_IO);
}

zj_result_t zj_store_peek(zj_store_t *s, zj_item_t *item)
{
    if (!s || !s->ready || !item) return ZJ_INVALID;
    memset(item, 0, sizeof(*item));
    memcpy(item->custody_epoch, s->writer_metadata.capture_epoch, sizeof(item->custody_epoch));
    memcpy(item->custody_serial, s->writer_metadata.terminal_serial, sizeof(item->custody_serial));
    zj_segment_t *segment = NULL;
    uint32_t offset = 0;
    for (unsigned i = 0; i < s->count; ++i) {
        zj_segment_t *candidate = &s->segments[i];
        if (candidate->id < s->read_segment) continue;
        offset = candidate->id == s->read_segment ? s->read_offset :
            candidate->metadata_valid ? ZJ_META_BYTES : 0;
        if (offset < candidate->size) { segment = candidate; break; }
    }
    if (!segment) return ZJ_EMPTY;
    uint8_t metadata[ZJ_META_BYTES];
    zj_result_t header = read_header(s, segment, metadata, &item->metadata);
    if (header != ZJ_OK && header != ZJ_CORRUPT) return header;
    size_t remaining = segment->size - offset;
    size_t length = remaining < ZJ_EXCEPTION_MAX ? remaining : ZJ_EXCEPTION_MAX;
    uint8_t record[ZJ_RECORD_MAX];
    item->kind = ZJ_PRESERVED_EXCEPTION;
    item->exception = header == ZJ_OK ? ZJ_EXCEPTION_FRAME :
        segment->checkpoint_evidence ? ZJ_EXCEPTION_CHECKPOINT : ZJ_EXCEPTION_METADATA;
    if (header == ZJ_OK && remaining >= ZJ_HEADER_BYTES) {
        zj_result_t read = read_bytes(s, segment->id, offset, record, ZJ_HEADER_BYTES);
        if (read != ZJ_OK) return read;
        size_t framed;
        if (zj_record_length(record, &framed)) {
            if (framed <= remaining) {
                read = read_bytes(s, segment->id, offset, record, framed);
                if (read != ZJ_OK) return read;
                zj_codec_result_t decoded = zj_record_decode(metadata, record, framed, s->port.crypto, &item->observation);
                if (decoded == ZJ_CODEC_OK && item->observation.sequence > s->last_sequence) {
                    length = framed;
                    item->kind = ZJ_OBSERVATION;
                    item->exception = ZJ_EXCEPTION_NONE;
                } else item->exception = ZJ_EXCEPTION_AUTH;
            } else item->exception = ZJ_EXCEPTION_TAIL;
        }
    } else if (header == ZJ_OK) item->exception = ZJ_EXCEPTION_TAIL;
    if (item->kind == ZJ_PRESERVED_EXCEPTION) {
        /* Untrusted lengths never skip opaque bytes. A corrupt record may
         * span multiple custody items; every byte is kept until its receipt.
         * Following segments remain independently readable. */
        /* Preserve the bad prefix, then retry framing at the next possible
         * record. Never swallow a later valid observation into an opaque
         * exception just because an earlier length/tag was corrupted. The
         * next peek authenticates the candidate before interpreting it. */
        if (header == ZJ_OK && remaining > 4) {
            uint8_t window[ZJ_EXCEPTION_MAX + 3];
            size_t scan = remaining < sizeof(window) ? remaining : sizeof(window);
            zj_result_t scanned = read_bytes(s, segment->id, offset, window, scan);
            if (scanned != ZJ_OK) return scanned;
            for (size_t i = 1; i < length && i + 4 <= scan; ++i) {
                if (!memcmp(window + i, "ZJO1", 4)) { length = i; break; }
            }
        }
        zj_result_t read = read_bytes(s, segment->id, offset, item->exception_bytes, length);
        if (read != ZJ_OK) return read;
        item->exception_length = (uint16_t)length;
        if (!s->port.crypto.digest(s->port.crypto.context, item->exception_bytes, length, item->token.bytes_digest))
            return failure(s, "exception_digest", EIO, ZJ_IO);
    } else {
        if (!s->port.crypto.digest(s->port.crypto.context, record, length, item->token.bytes_digest))
            return failure(s, "record_digest", EIO, ZJ_IO);
        item->token.sequence = item->observation.sequence;
    }
    item->token.segment_id = segment->id;
    item->token.offset = offset;
    item->token.end = offset + (uint32_t)length;
    return ZJ_OK;
}

zj_result_t zj_store_settle(zj_store_t *s, const zj_token_t *token, const uint8_t receipt[32])
{
    if (!s || !s->ready || !token || !receipt) return ZJ_INVALID;
    uint8_t present = 0;
    for (unsigned i = 0; i < 32; ++i) present |= receipt[i];
    if (!present || s->checkpoint_revision == UINT64_MAX) return ZJ_INVALID;
    zj_item_t item;
    zj_result_t result = zj_store_peek(s, &item);
    if (result != ZJ_OK) return result;
    const zj_token_t *actual = &item.token;
    if (token->segment_id != actual->segment_id || token->offset != actual->offset ||
        token->end != actual->end || token->sequence != actual->sequence ||
        memcmp(token->bytes_digest, actual->bytes_digest, sizeof(token->bytes_digest))) return ZJ_STALE;
    uint8_t cp[ZJ_CHECKPOINT_BYTES] = {0};
    memcpy(cp, "ZJCP0001", 8);
    put64(cp + 8, s->checkpoint_revision + 1);
    put64(cp + 16, token->segment_id);
    put32(cp + 24, token->end);
    put64(cp + 32, token->sequence ? token->sequence : s->last_sequence);
    memcpy(cp + 40, receipt, 32);
    put32(cp + 76, dq_crc32(cp, 76));
    if (!s->port.commit(s->port.context, cp)) {
        s->ready = false;
        return failure(s, "retirement_checkpoint", EIO, ZJ_UNCERTAIN);
    }
    if (!load_checkpoint(s, cp)) { s->ready = false; return ZJ_CORRUPT; }
    recovery_pending(s);
    return ZJ_OK;
}

zj_result_t zj_store_reclaim_step(zj_store_t *s)
{
    if (!s || !s->ready) return ZJ_INVALID;
    for (unsigned i = 0; i < s->count; ++i) {
        zj_segment_t *segment = &s->segments[i];
        bool empty = segment->size == 0 || (segment->metadata_valid && segment->size == ZJ_META_BYTES);
        bool retired = segment->id < s->read_segment ||
            (segment->id == s->read_segment && segment->size <= s->read_offset);
        if (!empty && !retired) continue;
        char path[144];
        if (!filename(s, segment->id, path)) return ZJ_INVALID;
        if (s->writer_id == segment->id) s->writer_id = 0;
        errno = 0;
        if (unlink(path) != 0 && errno != ENOENT) return failure(s, "segment_reclaim", errno, ZJ_IO);
        memmove(segment, segment + 1, (s->count - i - 1) * sizeof(*segment));
        --s->count;
        return ZJ_OK;
    }
    return ZJ_EMPTY;
}
