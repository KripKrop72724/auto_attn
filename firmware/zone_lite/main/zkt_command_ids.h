#pragma once
#include "zkt_journal_store.h"
#include "reliability.h"
#include <string.h>

#define ZI_ID_BYTES 96U
#define ZI_LIMIT_BYTES (64U * 1024U)
#define ZI_READ_BYTES 4096U
#ifndef ZI_PROCESSED_PATH
#define ZI_PROCESSED_PATH "/storage/processed_commands.txt"
#define ZI_CANCELLED_PATH "/storage/add_cancelled.txt"
#endif
typedef enum { ZI_PROCESSED, ZI_CANCELLED } zi_kind_t;
typedef struct {
    uint64_t deadline_us;
    char id[ZI_ID_BYTES];
    uint8_t kind;
    bool remember;
} zi_request_t;
typedef struct {
    bool present;
    int error;
    const char *operation;
} zi_reply_t;
typedef struct {
    char paths[2][112];
    bool (*admit)(void *, size_t);
    void *context;
    uint64_t work_ticket;
    uint32_t offset, length;
    char line[ZI_ID_BYTES + 1];
    uint8_t used;
    bool size_known;
} zi_store_t;
static inline bool zi_request_valid(const zi_request_t *request)
{
    if (!request || !request->deadline_us || request->kind > ZI_CANCELLED || !request->id[0] ||
        !memchr(request->id, 0, sizeof(request->id))) return false;
    return !strchr(request->id, '\n') && !strchr(request->id, '\r');
}
/* This sole-owner reader closes files after at most 4 KiB per step. Other
 * cache operations wait for its retained ticket; live journal work may run.
 * Existing newline-delimited IDs remain readable by the bridge and legacy
 * images. No old IDs are removed or capacity automatically reclaimed. */
bool zi_store_init(zi_store_t *store, const char *processed, const char *cancelled,
                   bool (*admit)(void *, size_t), void *context);
bool zi_store_step(zi_store_t *store, uint64_t ticket, uint64_t now_us,
                   const zi_request_t *request, zi_reply_t *reply, zj_result_t *result);
/* Exact new-image callers only. Errors and uncertain writes are never absence
 * or successful persistence. A timed-out request retains one reply per cache. */
rel_id_result_t zi_cache_contains(zi_kind_t kind, const char *id);
bool zi_cache_remember(zi_kind_t kind, const char *id);
