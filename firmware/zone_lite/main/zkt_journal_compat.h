#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZJ_READER_PROOF_BYTES 192U
#ifndef ZJ_BRIDGE_VERSION
/* Historical builds retain their original proof contract. The new matrix
 * writer has no implicit fallback reader; bridge images define their own. */
#if defined(ZONE_LITE_QUALIFIED_READER_MATRIX) && ZONE_LITE_QUALIFIED_READER_MATRIX
#define ZJ_BRIDGE_VERSION "UNQUALIFIED"
#else
#define ZJ_BRIDGE_VERSION "2.6.17"
#endif
#endif
#define ZJ_WRITER_VERSION "2.7.0"
#define ZJ_READER_MASK 0x3fU /* Includes persisted ADD authority and bridge capture. */
#include "zkt_reader_policy.h"

/* This is local reader capability, not model, HIL or production qualification.
 * The storage owner supplies the verified key epoch and reader state. The ESP
 * adapter obtains image/partition/security facts from the platform, never a
 * network message. CRC detects accidental damage; encrypted NVS and secure
 * boot supply the device trust boundary. No key material belongs in this blob. */
typedef struct {
    uint8_t image_digest[32], terminal_digest[32], capture_epoch[16], layout_digest[32];
    uint32_t slot_address, slot_size;
} zj_reader_identity_t;

typedef struct {
    bool verified;
    char version[20];
    uint8_t image_digest[32];
    uint32_t slot_address, slot_size;
    uint64_t proof_generation;
} zj_reader_selection_t;

typedef struct {
    const char *application, *version;
    bool secure_boot, encrypted_nvs, ota_slot, image_validated;
    bool reader_ready, delivery_ready, persistence_verified, recovery_pending;
} zj_reader_environment_t;

typedef struct {
    /* 1: exact bytes loaded, 0: absent, -1: unavailable or wrong size.
     * write must commit before returning; the guard also reads back exactly. */
    int (*read)(void *context, uint8_t bytes[ZJ_READER_PROOF_BYTES]);
    bool (*write)(void *context, const uint8_t bytes[ZJ_READER_PROOF_BYTES]);
    void *context;
} zj_reader_proof_port_t;

typedef enum {
    ZJ_COMPAT_OK, ZJ_COMPAT_INVALID, ZJ_COMPAT_VERSION, ZJ_COMPAT_SECURITY,
    ZJ_COMPAT_NOT_READY, ZJ_COMPAT_MISSING, ZJ_COMPAT_IO, ZJ_COMPAT_CORRUPT,
    ZJ_COMPAT_BINDING, ZJ_COMPAT_ROLLBACK, ZJ_COMPAT_UNCERTAIN,
    ZJ_COMPAT_EXHAUSTED, ZJ_COMPAT_UPDATE_TARGET, ZJ_COMPAT_PROTECTED_SLOT,
    ZJ_COMPAT_SELECTION_EXPIRED, ZJ_COMPAT_SELECTION_UNCERTAIN,
    ZJ_COMPAT_ANTI_ROLLBACK, ZJ_COMPAT_CAPTURE_DISABLED
} zj_compat_result_t;

/* Only a validated bridge can create/renew proof. Existing corrupt or rebound
 * evidence is never overwritten. An unchanged proof incurs no NVS write. */
zj_compat_result_t zj_reader_attest(zj_reader_proof_port_t port,
    const zj_reader_environment_t *bridge, const zj_reader_identity_t *identity);

/* A pending writer may preserve data only with an exact validated bridge in
 * the other OTA slot. This reads proof without repairing or replacing it. */
zj_compat_result_t zj_reader_check_writer(zj_reader_proof_port_t port,
    const zj_reader_environment_t *writer, const zj_reader_identity_t *current,
    const zj_reader_environment_t *rollback, const zj_reader_identity_t *previous);

/* Selection retry only: the adapter must prove the bridge is the selected
 * boot partition and is NEW (not INVALID/ABORTED/PENDING_VERIFY). Its prior
 * validated-image attestation must still exactly match. This cannot grant
 * writer permission or create/renew the attestation. */
zj_compat_result_t zj_reader_check_selected(zj_reader_proof_port_t port,
    const zj_reader_environment_t *writer, const zj_reader_identity_t *current,
    const zj_reader_environment_t *rollback, const zj_reader_identity_t *previous);

/* The release's only journal-preserving install edge is a validated, attested
 * bridge -> exact writer, into the other slot. Never overwrite the certified
 * bridge from the writer. This check is read-only and runs before OTA erase. */
zj_compat_result_t zj_reader_check_update(zj_reader_proof_port_t port,
    const zj_reader_environment_t *bridge, const zj_reader_identity_t *current,
    uint32_t target_address, uint32_t target_size, const char *target_version);

bool zj_reader_proof_decode(const uint8_t bytes[ZJ_READER_PROOF_BYTES],
    zj_reader_identity_t *identity, uint64_t *generation);
const char *zj_compat_error(zj_compat_result_t result);
