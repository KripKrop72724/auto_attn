#pragma once
#include "zkt_journal_store.h"

#define ZJ_ROOT_BYTES 160U

typedef enum { ZJ_AUTHORITY_UNKNOWN, ZJ_AUTHORITY_LEGACY, ZJ_AUTHORITY_ADD } zj_delivery_authority_t;

/* On ESP these blobs live in encrypted NVS. A write must commit; the common
 * code independently reads back the exact bytes before trusting it. No key,
 * counter or checkpoint is replaced after an unavailable/corrupt read. */
typedef struct {
    int (*read)(void *context, const char *name, uint8_t *out, size_t length);
    bool (*write)(void *context, const char *name, const uint8_t *bytes, size_t length);
    bool (*random)(void *context, uint8_t *out, size_t length);
    bool (*journal_absent)(void *context);
    void *context;
} zj_state_port_t;

typedef struct {
    zj_state_port_t port;
    uint8_t root[ZJ_ROOT_BYTES];
    uint64_t limit;
    bool ready;
} zj_state_t;

zj_result_t zj_state_open(zj_state_t *state, zj_state_port_t port, const char *terminal_serial);
bool zj_state_identity(const zj_state_t *state, uint8_t master[32], uint8_t epoch[16]);
zj_delivery_authority_t zj_state_authority(const zj_state_t *state);
/* Owner only, after proving the running writer's exact compatible rollback
 * image. Commit and read back before allocating any capture sequence. There
 * is no reverse transition; a bridge must honor this bit after rollback. */
zj_result_t zj_state_enable_add(zj_state_t *state);
bool zj_state_reserve(void *state, uint64_t exclusive_limit);
int zj_state_checkpoint_load(void *state, uint8_t checkpoint[ZJ_CHECKPOINT_BYTES]);
bool zj_state_checkpoint_commit(void *state, const uint8_t checkpoint[ZJ_CHECKPOINT_BYTES]);
void zj_state_clear(zj_state_t *state);
