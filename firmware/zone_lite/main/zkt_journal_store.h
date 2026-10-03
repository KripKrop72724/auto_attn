#pragma once
#include "zkt_journal_codec.h"

#define ZJ_SEGMENT_BYTES (64U * 1024U)
#define ZJ_SEGMENTS_MAX 256U
#define ZJ_CHECKPOINT_BYTES 80U
#define ZJ_EXCEPTION_MAX 512U

typedef enum { ZJ_OK, ZJ_EMPTY, ZJ_FULL, ZJ_IO, ZJ_CORRUPT, ZJ_STALE,
               ZJ_INVALID, ZJ_UNCERTAIN } zj_result_t;
typedef enum { ZJ_OBSERVATION, ZJ_PRESERVED_EXCEPTION } zj_item_kind_t;
typedef enum { ZJ_EXCEPTION_NONE, ZJ_EXCEPTION_METADATA, ZJ_EXCEPTION_FRAME,
               ZJ_EXCEPTION_AUTH, ZJ_EXCEPTION_TAIL, ZJ_EXCEPTION_CHECKPOINT } zj_exception_t;

typedef struct {
    uint64_t segment_id;
    uint32_t offset, end;
    uint8_t bytes_digest[32];
    uint64_t sequence;
} zj_token_t;

typedef struct {
    zj_item_kind_t kind;
    zj_exception_t exception;
    uint8_t custody_epoch[16];
    char custody_serial[81];
    zj_metadata_t metadata;
    zj_observation_t observation;
    uint16_t exception_length;
    uint8_t exception_bytes[ZJ_EXCEPTION_MAX];
    zj_token_t token;
} zj_item_t;

typedef struct {
    /* 1: exact checkpoint bytes loaded; 0: never initialized; -1: unavailable. */
    int (*load)(void *context, uint8_t checkpoint[ZJ_CHECKPOINT_BYTES]);
    bool (*commit)(void *context, const uint8_t checkpoint[ZJ_CHECKPOINT_BYTES]);
    bool (*reserve)(void *context, uint64_t exclusive_limit);
    bool (*admit)(void *context, size_t bytes);
    void *context;
    zj_crypto_port_t crypto;
} zj_store_port_t;

typedef struct { uint64_t id; uint32_t size; bool metadata_valid, checkpoint_evidence; } zj_segment_t;
typedef struct {
    char prefix[112];
    zj_store_port_t port;
    zj_metadata_t writer_metadata;
    uint8_t writer_header[ZJ_META_BYTES];
    zj_sequence_t sequence;
    zj_segment_t segments[ZJ_SEGMENTS_MAX];
    unsigned count;
    uint64_t writer_id, checkpoint_revision, read_segment, last_sequence;
    uint32_t read_offset;
    bool ready, checkpoint_recovery_pending;
    int last_errno;
    const char *last_operation;
} zj_store_t;

/* Every call belongs to the storage owner. These routines never call the
 * network and never overwrite an existing segment, even after an uncertain
 * write or interrupted rotation. Startup scans filenames/headers once; an
 * empty peek performs no filesystem operation. The caller loads the durable
 * sequence reservation and immutable capture epoch/key before opening. */
zj_result_t zj_store_open(zj_store_t *store, const char *prefix,
                          const zj_metadata_t *metadata, uint64_t persisted_limit,
                          zj_store_port_t port);
zj_result_t zj_store_append(zj_store_t *store, const zj_observation_t *observation,
                            uint64_t *capture_sequence);
zj_result_t zj_store_peek(zj_store_t *store, zj_item_t *item);
/* destination_receipt_digest is a verified ADD receipt binding this exact
 * token/content. Exceptions require preserved opaque-byte custody too. A
 * transport ACK or an Oracle retry response cannot authorize this call. */
zj_result_t zj_store_settle(zj_store_t *store, const zj_token_t *token,
                            const uint8_t destination_receipt_digest[32]);
/* At most one fully settled (or verified empty) segment per call. */
zj_result_t zj_store_reclaim_step(zj_store_t *store);
