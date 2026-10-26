#pragma once
/* ZKT 2.7.7: bridge-era journal leftovers and the OTA guard.
 *
 * The 2.6.18-2.6.21 bridges (ZONE-SLICTOWER-3FL ran all four) opened the
 * journal with legacy delivery authority: they left an encrypted NVS
 * namespace "zkt_journal" (its "root" and the bridge's "reader_v1" proof) and
 * possibly empty /zktj<16 hex>.j segments. Under legacy authority the journal
 * can never reserve a sequence (zj_state_reserve needs ADD authority), so the
 * root's limit stays 1 and no segment can hold a journal record: every punch
 * those images captured went through the legacy queues. Before 2.7.7 that
 * leftover made the OTA guard refuse every update
 * (JOURNAL_LEGACY_EVIDENCE_PRESENT) with no way out over the air.
 *
 * zle_decide accepts exactly that leftover and nothing else: the running
 * image's journal runtime disabled with no owner started; the namespace, if
 * present, holding only "root" and "reader_v1"; the root, if present, a
 * well-formed legacy-authority root whose limit is 1; and every zktj* file a
 * segment name the storage repair classifies SRP_CLASS_EVIDENCE (so neither
 * the repair nor OTA ever removes it), within one segment's size and fully
 * readable. ADD authority, a malformed root, any other key (a retirement
 * checkpoint, a source boundary), files without a root, an unreadable or
 * oversized segment, or an unknown zktj* name still refuse. Nothing is
 * deleted or rewritten: the evidence stays on the device.
 *
 * No ESP-IDF dependency; tests/firmware/zkt_legacy_evidence_host.c. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZLE_ROOT_BYTES 160U
#define ZLE_FILES_MAX 16U
/* One journal segment (ZJ_SEGMENT_BYTES) and its metadata (ZJ_META_BYTES). */
#define ZLE_FILE_BYTES_MAX (64U * 1024U + 240U)

typedef enum {
    ZLE_ROOT_ABSENT = 0,
    ZLE_ROOT_LEGACY,     /* well formed, authority byte 0, limit 1 */
    ZLE_ROOT_ADD,        /* well formed, authority transferred to ADD */
    ZLE_ROOT_INVALID,    /* wrong size, magic, CRC or layout */
    ZLE_ROOT_UNREADABLE, /* NVS read error */
} zle_root_t;

typedef enum { ZLE_NONE = 0, ZLE_ACCEPTED, ZLE_REFUSED } zle_state_t;

typedef struct {
    /* The running image. */
    bool runtime_disabled, owner_started;
    /* NVS: the namespace, its root, and its keys (listed: the walk ended). */
    bool namespace_present, namespace_listed, reader_proof;
    zle_root_t root;
    uint32_t keys, unknown_keys;
    /* The store: listed (the directory walk ended without error), then every
     * name starting with "zktj". */
    bool store_listed;
    uint32_t files, bytes, not_segment, unreadable, oversized;
} zle_inventory_t;

typedef struct {
    zle_state_t state;
    const char *error;  /* the OTA guard's refusal, NULL when not refused */
    const char *reason; /* why, for the heartbeat: always set */
} zle_verdict_t;

/* A root blob as nvs_get_blob returned it (length is what NVS reported). */
zle_root_t zle_root_classify(const uint8_t *bytes, size_t length);
/* One NVS key of the namespace. */
void zle_note_key(zle_inventory_t *inventory, const char *key);
/* One directory entry whose name starts with "zktj" (name as readdir returns
 * it, no leading '/'); size from stat, read_bytes what a full read returned
 * (read_ok false on any read error). */
void zle_note_file(zle_inventory_t *inventory, const char *name, uint64_t size, bool read_ok, uint64_t read_bytes);
zle_verdict_t zle_decide(const zle_inventory_t *inventory);
const char *zle_root_name(zle_root_t root);
const char *zle_state_name(zle_state_t state);
