#pragma once
#include "file_transaction.h"
#include "zkt_journal_store.h"

#define ZC_CHUNK_BYTES 512U
#define ZC_LIMIT_BYTES (2U * 1024U * 1024U)
#ifndef ZC_ACTIVE_PATH
#define ZC_ACTIVE_PATH "/storage/add_identities.enc"
#define ZC_COMMIT_PATH "/storage/add_identities.commit"
#define ZC_BACKUP_PATH "/storage/add_identities.backup"
#define ZC_TEMP_PATH "/storage/add_identities.tmp"
#define ZC_STAGE_PATH "/storage/add_identities.stage"
#endif

typedef enum { ZC_RECOVER, ZC_RESET, ZC_APPEND, ZC_REMOVE, ZC_ACTIVATE, ZC_READ } zc_operation_t;
typedef struct {
    uint64_t id, revision, deadline_us;
    uint32_t offset;
    uint16_t length;
    uint8_t file, operation;
    uint8_t bytes[ZC_CHUNK_BYTES];
} zc_request_t;
typedef struct {
    uint64_t id, revision;
    uint32_t offset, total;
    uint16_t length;
    bool eof;
    int error;
    const char *operation;
    uint8_t bytes[ZC_CHUNK_BYTES];
} zc_reply_t;
typedef struct {
    char paths[5][FT_PATH_BYTES];
    ft_port_t port;
    ft_work_t transaction;
    uint64_t next_id, ids[2], revision, work_ticket;
    uint32_t sizes[2];
    bool poisoned[2], recovered;
    uint8_t work_phase;
    int error;
    const char *operation;
} zc_store_t;

/* Owner-only state. Inputs contain copied bytes and fixed file selectors, no
 * caller-owned pointers or arbitrary paths. Every step closes every file.
 * The caller holds the shared filesystem lock for one step and releases it
 * before yielding. A pending ticket must be resumed before another catalog
 * command (journal append/settlement may run between steps).
 * Paths are parameters only for host qualification; the ESP owner uses the
 * five fixed catalog paths above. Catalog data is already encrypted. */
bool zc_store_init(zc_store_t *store, const char *active, const char *commit,
                   const char *backup, const char *temporary, const char *stage, ft_port_t port);
/* Called under the same owner/local lock as the ensuing step. Resetting an
 * existing optional producer reclaims bytes and must remain possible at the
 * write ceiling; creating a missing file still requires metadata admission. */
size_t zc_store_admission_bytes(const zc_store_t *store, const zc_request_t *request);
static inline bool zc_request_valid(const zc_request_t *request)
{
    return request && request->operation <= ZC_READ && request->file < 2 && request->deadline_us &&
        request->length <= ZC_CHUNK_BYTES && (request->operation != ZC_APPEND || request->length) &&
        ((request->operation != ZC_APPEND && request->operation != ZC_REMOVE && request->operation != ZC_ACTIVATE) || request->id);
}
/* true means more bounded work on the SAME ticket; result is final otherwise.
 * Expiry refuses a command before it starts. Once started, an admitted
 * activation/recovery finishes even when the caller abandons its reply. */
bool zc_store_step(zc_store_t *store, uint64_t ticket, uint64_t now_us,
                   const zc_request_t *request, zc_reply_t *reply, zj_result_t *result);
