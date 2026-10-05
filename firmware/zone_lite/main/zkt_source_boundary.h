#pragma once
#include "zkt_journal_state.h"
#include "zkt_journal_compat.h"
#include "durable_queue.h"
#include <string.h>

#define ZSB_BYTES 160U

/* A first observed source boundary, not a completed migration certificate.
 * Stored in encrypted NVS by the owner. Existing evidence is immutable even
 * when a caller loses its reply, the source grows, or the connector reboots.
 * It must be joined to ADD source/legacy custody before authorizing delivery. */
typedef struct {
    uint32_t next_ordinal, record_size;
    uint8_t anchor_digest[32]; /* Digest of ordinal next_ordinal - 1. */
    uint64_t sampled_uptime_ms;
} zsb_facts_t;
typedef struct {
    zsb_facts_t facts;
    uint8_t capture_epoch[16], writer_digest[32], terminal_digest[32];
} zsb_record_t;

static inline bool zsb_nonzero(const uint8_t *p, size_t n)
{ uint8_t v = 0; while (n--) v |= *p++; return v != 0; }
static inline uint64_t zsb_get(const uint8_t *p, unsigned n)
{ uint64_t v = 0; for (unsigned i = 0; i < n; ++i) v |= (uint64_t)p[i] << (8*i); return v; }
static inline void zsb_put(uint8_t *p, uint64_t v, unsigned n)
{ for (unsigned i = 0; i < n; ++i) p[i] = (uint8_t)(v >> (8*i)); }
static inline bool zsb_facts_valid(const zsb_facts_t *f)
{
    if (!f || f->next_ordinal > INT32_MAX) return false;
    if (!f->next_ordinal) return !f->record_size && !zsb_nonzero(f->anchor_digest, 32);
    return (f->record_size == 8 || f->record_size == 16 || f->record_size == 40) &&
        zsb_nonzero(f->anchor_digest, 32);
}
/* A count sampled before preparing a terminal buffer is usable only when
 * the prepared snapshot agrees exactly. Growth during sampling is retried;
 * it must not silently choose a different occurrence boundary. */
static inline bool zsb_source_size(int32_t count, uint32_t buffer_bytes,
    uint32_t declared_bytes, uint32_t *record_size)
{
    if (!record_size) return false;
    *record_size = 0;
    if (count < 0 || buffer_bytes < 4 || declared_bytes != buffer_bytes - 4) return false;
    if (!count) return declared_bytes == 0;
    if (declared_bytes % (uint32_t)count) return false;
    uint32_t size = declared_bytes / (uint32_t)count;
    if (size != 8 && size != 16 && size != 40) return false;
    *record_size = size;
    return true;
}
static inline bool zsb_identity_valid(const zj_reader_identity_t *i)
{ return i && zsb_nonzero(i->capture_epoch, 16) && zsb_nonzero(i->image_digest, 32) &&
    zsb_nonzero(i->terminal_digest, 32); }
static inline bool zsb_decode(const uint8_t b[ZSB_BYTES], zsb_record_t *out)
{
    memset(out, 0, sizeof(*out));
    if (memcmp(b, "ZJSRC001", 8) || zsb_nonzero(b+136, 20) ||
        zsb_get(b+156, 4) != dq_crc32(b, 156)) return false;
    out->facts.next_ordinal = (uint32_t)zsb_get(b+8, 4);
    out->facts.record_size = (uint32_t)zsb_get(b+12, 4);
    memcpy(out->capture_epoch, b+16, 16);
    memcpy(out->writer_digest, b+32, 32);
    memcpy(out->terminal_digest, b+64, 32);
    memcpy(out->facts.anchor_digest, b+96, 32);
    out->facts.sampled_uptime_ms = zsb_get(b+128, 8);
    if (!zsb_facts_valid(&out->facts) || !zsb_nonzero(out->capture_epoch, 16) ||
        !zsb_nonzero(out->writer_digest, 32) || !zsb_nonzero(out->terminal_digest, 32)) {
        memset(out, 0, sizeof(*out)); return false;
    }
    return true;
}
static inline zj_result_t zsb_open(zj_state_port_t port, const zj_reader_identity_t *identity,
    const zsb_facts_t *proposed, zsb_record_t *out)
{
    if (!out) return ZJ_INVALID;
    memset(out, 0, sizeof(*out));
    if (!port.read || !port.write || !zsb_identity_valid(identity)) return ZJ_INVALID;
    uint8_t bytes[ZSB_BYTES] = {0}, verify[ZSB_BYTES] = {0};
    int found = port.read(port.context, "source_v1", bytes, sizeof(bytes));
    if (found != 0 && found != 1) return ZJ_IO;
    if (!found) {
        if (!proposed) return ZJ_EMPTY;
        if (!zsb_facts_valid(proposed)) return ZJ_INVALID;
        memcpy(bytes, "ZJSRC001", 8);
        zsb_put(bytes+8, proposed->next_ordinal, 4);
        zsb_put(bytes+12, proposed->record_size, 4);
        memcpy(bytes+16, identity->capture_epoch, 16);
        memcpy(bytes+32, identity->image_digest, 32);
        memcpy(bytes+64, identity->terminal_digest, 32);
        memcpy(bytes+96, proposed->anchor_digest, 32);
        zsb_put(bytes+128, proposed->sampled_uptime_ms, 8);
        zsb_put(bytes+156, dq_crc32(bytes, 156), 4);
        if (!port.write(port.context, "source_v1", bytes, sizeof(bytes)) ||
            port.read(port.context, "source_v1", verify, sizeof(verify)) != 1 ||
            memcmp(bytes, verify, sizeof(bytes))) return ZJ_UNCERTAIN;
    }
    if (!zsb_decode(bytes, out)) return ZJ_CORRUPT;
    if (memcmp(out->capture_epoch, identity->capture_epoch, 16) ||
        memcmp(out->writer_digest, identity->image_digest, 32) ||
        memcmp(out->terminal_digest, identity->terminal_digest, 32)) {
        memset(out, 0, sizeof(*out)); return ZJ_STALE;
    }
    return ZJ_OK;
}
