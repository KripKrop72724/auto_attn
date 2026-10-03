#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Journal v1 is a byte protocol, never a persisted C struct. The segment's
 * immutable identity is authenticated with every record. No employee name or
 * national identity number belongs in this local envelope. */
#define ZJ_FORMAT_VERSION 1U
#define ZJ_RAW_MAX 512U
#define ZJ_META_BYTES 240U
#define ZJ_HEADER_BYTES 24U
#define ZJ_FACT_BYTES 40U
#define ZJ_TAG_BYTES 16U
#define ZJ_RECORD_MAX (ZJ_HEADER_BYTES + ZJ_FACT_BYTES + ZJ_RAW_MAX + ZJ_TAG_BYTES)
#define ZJ_SEQUENCE_MAX INT64_MAX

typedef enum { ZJ_LIVE_FRAME = 1, ZJ_SOURCE_RECORD = 2, ZJ_UNKNOWN = 3,
               ZJ_LIVE_PACKET = 4, ZJ_PACKET_FRAGMENT = 5 } zj_raw_format_t;
typedef enum { ZJ_TIME_VERIFIED = 1, ZJ_TIME_UNSYNCED = 2,
               ZJ_TIME_INVALID = 3, ZJ_TIME_UNKNOWN = 4 } zj_time_quality_t;
typedef enum { ZJ_CODEC_OK, ZJ_CODEC_INVALID, ZJ_CODEC_SMALL, ZJ_CODEC_AUTH,
               ZJ_CODEC_CRYPTO } zj_codec_result_t;

typedef struct {
    uint64_t segment_id;
    uint8_t capture_epoch[16];
    char terminal_serial[81];
    char decoder_profile[65];
    char decoder_version[33];
} zj_metadata_t;

typedef struct {
    uint64_t sequence;
    zj_raw_format_t raw_format;
    zj_time_quality_t time_quality;
    uint32_t encoded_time;
    int64_t captured_at_seconds;
    uint64_t captured_uptime_ms;
    uint8_t source_epoch[16]; /* All zero for observations without source proof. */
    uint32_t source_ordinal;  /* UINT32_MAX when no canonical source reference. */
    uint32_t identity_revision;
    uint16_t raw_length;
    uint8_t raw[ZJ_RAW_MAX];
} zj_observation_t;

/* Both operations use AES-256-GCM in the ESP adapter. The adapter must derive
 * a domain-separated key from the full metadata capture epoch and terminal.
 * Nonces are capture sequence numbers, allocated durably before encryption. */
typedef struct {
    bool (*seal)(void *context, const uint8_t metadata[ZJ_META_BYTES],
                 const uint8_t nonce[12], const uint8_t *aad, size_t aad_length,
                 const uint8_t *plain, size_t length, uint8_t *cipher,
                 uint8_t tag[ZJ_TAG_BYTES]);
    bool (*open)(void *context, const uint8_t metadata[ZJ_META_BYTES],
                 const uint8_t nonce[12], const uint8_t *aad, size_t aad_length,
                 const uint8_t *cipher, size_t length,
                 const uint8_t tag[ZJ_TAG_BYTES], uint8_t *plain);
    bool (*digest)(void *context, const uint8_t *data, size_t length, uint8_t out[32]);
    void *context;
} zj_crypto_port_t;

bool zj_metadata_encode(const zj_metadata_t *metadata, uint8_t out[ZJ_META_BYTES]);
bool zj_metadata_decode(const uint8_t in[ZJ_META_BYTES], zj_metadata_t *metadata);
bool zj_observation_valid(const zj_observation_t *observation);
zj_codec_result_t zj_record_encode(const uint8_t metadata[ZJ_META_BYTES],
                                   const zj_observation_t *observation,
                                   zj_crypto_port_t crypto, uint8_t *out,
                                   size_t capacity, size_t *length);
zj_codec_result_t zj_record_decode(const uint8_t metadata[ZJ_META_BYTES],
                                   const uint8_t *record, size_t length,
                                   zj_crypto_port_t crypto, zj_observation_t *out);
/* Only bounds/framing, never proof of authenticity or durable preservation. */
bool zj_record_length(const uint8_t header[ZJ_HEADER_BYTES], size_t *length);

/* A successful reservation persists an exclusive upper bound. On restart the
 * allocator starts there, burning the prior unused range. A failed or uncertain
 * reservation poisons this instance; reload durable state before trying again. */
typedef struct {
    uint64_t next, limit;
    bool ready;
    bool (*reserve)(void *context, uint64_t exclusive_limit);
    void *context;
} zj_sequence_t;
bool zj_sequence_init(zj_sequence_t *state, uint64_t persisted_limit,
                      bool (*reserve)(void *, uint64_t), void *context);
bool zj_sequence_next(zj_sequence_t *state, uint64_t *sequence);
