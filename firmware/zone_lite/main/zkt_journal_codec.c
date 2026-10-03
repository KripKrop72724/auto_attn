#include "zkt_journal_codec.h"
#include <string.h>

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}
static void put32(uint8_t *p, uint32_t v)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static void put64(uint8_t *p, uint64_t v)
{
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static uint16_t get16(const uint8_t *p) { return (uint16_t)p[0] | (uint16_t)p[1] << 8; }
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p)
{
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)p[i] << (8 * i);
    return value;
}
static void erase(void *p, size_t n)
{
    volatile uint8_t *bytes = p;
    while (n--) *bytes++ = 0;
}
static bool nonzero(const uint8_t *p, size_t n)
{
    uint8_t bits = 0;
    while (n--) bits |= *p++;
    return bits != 0;
}
static bool identifier(const char *p, size_t capacity, size_t *length)
{
    for (size_t i = 0; i < capacity; ++i) {
        unsigned char c = (unsigned char)p[i];
        if (!c) { *length = i; return i != 0; }
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == ':' || c == '-')) return false;
    }
    return false;
}

bool zj_metadata_encode(const zj_metadata_t *m, uint8_t out[ZJ_META_BYTES])
{
    size_t serial, profile, decoder;
    if (!m || !out || !m->segment_id || m->segment_id > ZJ_SEQUENCE_MAX ||
        !nonzero(m->capture_epoch, sizeof(m->capture_epoch)) ||
        !identifier(m->terminal_serial, sizeof(m->terminal_serial), &serial) ||
        !identifier(m->decoder_profile, sizeof(m->decoder_profile), &profile) ||
        !identifier(m->decoder_version, sizeof(m->decoder_version), &decoder)) return false;
    memset(out, 0, ZJ_META_BYTES);
    memcpy(out, "ZJ270S01", 8);
    put16(out + 8, ZJ_FORMAT_VERSION);
    put16(out + 10, ZJ_META_BYTES);
    put64(out + 16, m->segment_id);
    memcpy(out + 24, m->capture_epoch, 16);
    memcpy(out + 40, m->terminal_serial, serial);
    memcpy(out + 121, m->decoder_profile, profile);
    memcpy(out + 186, m->decoder_version, decoder);
    return true;
}

bool zj_metadata_decode(const uint8_t in[ZJ_META_BYTES], zj_metadata_t *m)
{
    if (!in || !m) return false;
    memset(m, 0, sizeof(*m));
    if (memcmp(in, "ZJ270S01", 8) || get16(in + 8) != ZJ_FORMAT_VERSION ||
        get16(in + 10) != ZJ_META_BYTES) return false;
    zj_metadata_t candidate = {.segment_id = get64(in + 16)};
    memcpy(candidate.capture_epoch, in + 24, 16);
    memcpy(candidate.terminal_serial, in + 40, sizeof(candidate.terminal_serial));
    memcpy(candidate.decoder_profile, in + 121, sizeof(candidate.decoder_profile));
    memcpy(candidate.decoder_version, in + 186, sizeof(candidate.decoder_version));
    uint8_t canonical[ZJ_META_BYTES];
    if (!zj_metadata_encode(&candidate, canonical) || memcmp(canonical, in, ZJ_META_BYTES)) return false;
    *m = candidate;
    return true;
}

bool zj_observation_valid(const zj_observation_t *o)
{
    return o && o->sequence && o->sequence <= ZJ_SEQUENCE_MAX &&
        o->raw_format >= ZJ_LIVE_FRAME && o->raw_format <= ZJ_PACKET_FRAGMENT &&
        o->time_quality >= ZJ_TIME_VERIFIED && o->time_quality <= ZJ_TIME_UNKNOWN &&
        o->raw_length && o->raw_length <= ZJ_RAW_MAX &&
        /* Captured wall time may be unavailable. Never interpret that as a
         * verified punch time; the encoded terminal time remains untouched. */
        o->captured_at_seconds >= 0 &&
        ((o->source_ordinal == UINT32_MAX && !nonzero(o->source_epoch, 16)) ||
         (o->source_ordinal <= INT32_MAX && nonzero(o->source_epoch, 16) &&
          o->raw_format == ZJ_SOURCE_RECORD));
}

bool zj_record_length(const uint8_t h[ZJ_HEADER_BYTES], size_t *length)
{
    if (!h || !length || memcmp(h, "ZJO1", 4) ||
        h[6] < ZJ_LIVE_FRAME || h[6] > ZJ_PACKET_FRAGMENT ||
        h[7] < ZJ_TIME_VERIFIED || h[7] > ZJ_TIME_UNKNOWN ||
        !get64(h + 8) || get64(h + 8) > ZJ_SEQUENCE_MAX ||
        get16(h + 22)) return false;
    size_t raw = get16(h + 20);
    size_t total = ZJ_HEADER_BYTES + ZJ_FACT_BYTES + raw + ZJ_TAG_BYTES;
    if (!raw || raw > ZJ_RAW_MAX || get16(h + 4) != total) return false;
    *length = total;
    return true;
}

static void nonce_for(uint64_t sequence, uint8_t nonce[12])
{
    memcpy(nonce, "ZJ01", 4);
    put64(nonce + 4, sequence);
}

zj_codec_result_t zj_record_encode(const uint8_t metadata[ZJ_META_BYTES],
                                   const zj_observation_t *o, zj_crypto_port_t crypto,
                                   uint8_t *out, size_t capacity, size_t *length)
{
    zj_metadata_t m;
    if (length) *length = 0;
    if (!out || !length || !crypto.seal || !zj_observation_valid(o) ||
        !zj_metadata_decode(metadata, &m)) return ZJ_CODEC_INVALID;
    size_t total = ZJ_HEADER_BYTES + ZJ_FACT_BYTES + o->raw_length + ZJ_TAG_BYTES;
    if (capacity < total) { *length = total; return ZJ_CODEC_SMALL; }
    uint8_t plain[ZJ_FACT_BYTES + ZJ_RAW_MAX] = {0};
    uint8_t aad[ZJ_META_BYTES + ZJ_HEADER_BYTES];
    uint8_t nonce[12];
    memset(out, 0, total);
    memcpy(out, "ZJO1", 4);
    put16(out + 4, (uint16_t)total);
    out[6] = (uint8_t)o->raw_format;
    out[7] = (uint8_t)o->time_quality;
    put64(out + 8, o->sequence);
    put32(out + 16, o->encoded_time);
    put16(out + 20, o->raw_length);
    put64(plain, (uint64_t)o->captured_at_seconds);
    put64(plain + 8, o->captured_uptime_ms);
    memcpy(plain + 16, o->source_epoch, 16);
    put32(plain + 32, o->source_ordinal);
    put32(plain + 36, o->identity_revision);
    memcpy(plain + ZJ_FACT_BYTES, o->raw, o->raw_length);
    memcpy(aad, metadata, ZJ_META_BYTES);
    memcpy(aad + ZJ_META_BYTES, out, ZJ_HEADER_BYTES);
    nonce_for(o->sequence, nonce);
    bool ok = crypto.seal(crypto.context, metadata, nonce, aad, sizeof(aad), plain,
                          ZJ_FACT_BYTES + o->raw_length, out + ZJ_HEADER_BYTES,
                          out + total - ZJ_TAG_BYTES);
    erase(plain, sizeof(plain));
    if (!ok) { erase(out, total); return ZJ_CODEC_CRYPTO; }
    *length = total;
    return ZJ_CODEC_OK;
}

zj_codec_result_t zj_record_decode(const uint8_t metadata[ZJ_META_BYTES],
                                   const uint8_t *record, size_t length,
                                   zj_crypto_port_t crypto, zj_observation_t *out)
{
    if (!out) return ZJ_CODEC_INVALID;
    memset(out, 0, sizeof(*out));
    size_t total;
    zj_metadata_t m;
    if (!record || !crypto.open || length < ZJ_HEADER_BYTES ||
        !zj_metadata_decode(metadata, &m) || !zj_record_length(record, &total) ||
        length != total) return ZJ_CODEC_INVALID;
    uint8_t aad[ZJ_META_BYTES + ZJ_HEADER_BYTES];
    uint8_t plain[ZJ_FACT_BYTES + ZJ_RAW_MAX] = {0};
    uint8_t nonce[12];
    memcpy(aad, metadata, ZJ_META_BYTES);
    memcpy(aad + ZJ_META_BYTES, record, ZJ_HEADER_BYTES);
    nonce_for(get64(record + 8), nonce);
    if (!crypto.open(crypto.context, metadata, nonce, aad, sizeof(aad),
                     record + ZJ_HEADER_BYTES, total - ZJ_HEADER_BYTES - ZJ_TAG_BYTES,
                     record + total - ZJ_TAG_BYTES, plain)) {
        erase(plain, sizeof(plain));
        return ZJ_CODEC_AUTH;
    }
    zj_observation_t candidate = {
        .sequence = get64(record + 8), .raw_format = record[6], .time_quality = record[7],
        .encoded_time = get32(record + 16), .captured_uptime_ms = get64(plain + 8),
        .source_ordinal = get32(plain + 32), .identity_revision = get32(plain + 36),
        .raw_length = get16(record + 20),
    };
    uint64_t wall_time = get64(plain);
    bool valid = wall_time <= INT64_MAX;
    candidate.captured_at_seconds = valid ? (int64_t)wall_time : 0;
    memcpy(candidate.source_epoch, plain + 16, 16);
    memcpy(candidate.raw, plain + ZJ_FACT_BYTES, candidate.raw_length);
    erase(plain, sizeof(plain));
    valid = valid && zj_observation_valid(&candidate);
    if (valid) *out = candidate;
    erase(&candidate, sizeof(candidate));
    return valid ? ZJ_CODEC_OK : ZJ_CODEC_INVALID;
}

bool zj_sequence_init(zj_sequence_t *state, uint64_t persisted_limit,
                      bool (*reserve)(void *, uint64_t), void *context)
{
    if (!state) return false;
    memset(state, 0, sizeof(*state));
    if (!reserve || !persisted_limit || persisted_limit > (uint64_t)ZJ_SEQUENCE_MAX + 1) return false;
    *state = (zj_sequence_t){.next = persisted_limit, .limit = persisted_limit,
                             .ready = true, .reserve = reserve, .context = context};
    return true;
}

bool zj_sequence_next(zj_sequence_t *state, uint64_t *sequence)
{
    if (sequence) *sequence = 0;
    if (!state || !sequence || !state->ready || !state->next || state->next > ZJ_SEQUENCE_MAX) return false;
    if (state->next == state->limit) {
        const uint64_t block = 256;
        uint64_t limit = state->next > (uint64_t)ZJ_SEQUENCE_MAX + 1 - block ?
            (uint64_t)ZJ_SEQUENCE_MAX + 1 : state->next + block;
        if (!state->reserve(state->context, limit)) { state->ready = false; return false; }
        state->limit = limit;
    }
    *sequence = state->next++;
    return true;
}
