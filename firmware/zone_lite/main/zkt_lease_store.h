#pragma once
#include "runtime_checkpoint.h"
#include "zkt_journal_store.h"
#define ZL_LEASE_VERSION 2U

/* Separate from runtime_v1: source-checkpoint damage must not discard a
 * previously committed revocation obligation. Encrypted NVS protects this
 * blob; its CRC detects torn/changed data, not malicious forgery. */
typedef struct {
    uint32_t version, generation;
    int64_t expires_epoch;
    uint16_t uid;
    uint8_t active;
    char terminal_serial[81], identity_fingerprint[65];
    uint32_t crc;
} zl_lease_record_t;

static inline bool zl_lease_valid(const zl_lease_record_t *record)
{
    if (!record || record->version != ZL_LEASE_VERSION || !record->generation ||
        record->active > 1 || !record->uid || !record->terminal_serial[0] ||
        !memchr(record->terminal_serial, 0, sizeof(record->terminal_serial)) ||
        record->identity_fingerprint[64] ||
        (record->active ? record->expires_epoch < 1767225600LL : record->expires_epoch != 0) ||
        record->crc != dq_crc32(record, offsetof(zl_lease_record_t, crc))) return false;
    for (unsigned i = 0; i < 64; ++i) {
        char c = record->identity_fingerprint[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}
bool zl_lease_matches(const zl_lease_record_t *record, const char *serial,
                     uint16_t uid, const char *identity_fingerprint);
void zl_lease_checksum(zl_lease_record_t *record);

/* Startup only, before storage-owner or terminal tasks exist. EMPTY means
 * absent, never proven inactive. Unreadable bytes are retained unchanged. */
zj_result_t zl_lease_load_boot(zl_lease_record_t *record, int *nvs_error);

/* Storage owner only, under the shared storage lock. A first record requires
 * a valid inactive runtime_v1 checkpoint. A legacy active lease requires
 * independent identity evidence; its current enrollment ID is insufficient.
 * No corrupt or disappeared record is repaired by overwriting it. */
zj_result_t zl_lease_commit(const zl_lease_record_t *proposed, uint64_t deadline_us,
                          zl_lease_record_t *confirmed, int *nvs_error);

/* Single terminal-session producer. Keep one accepted ticket across timeout.
 * confirmed can contain an earlier late commit even when false is returned.
 * Only true confirms the exact facts requested by this call. */
bool zl_lease_save(const zl_lease_record_t *proposed, zl_lease_record_t *confirmed);
