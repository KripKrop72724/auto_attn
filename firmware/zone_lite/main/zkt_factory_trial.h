#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZF_PROOF_BYTES 256U
#define ZF_FACTORY_ADDRESS 0x20000U
#define ZF_FACTORY_SIZE 0x280000U
typedef enum { ZF_VERIFIED = 1, ZF_REVOKED = 2, ZF_FALLBACK_INTENT = 3 } zf_state_t;
typedef struct {
    const char *connector_id, *terminal_serial, *application_sha256;
    uint8_t mac[6];
    uint32_t onboarding_generation;
} zf_target_t;
typedef struct {
    zf_state_t state;
    uint32_t target, signed_image_bytes;
    char deployment_id[48];
    uint8_t reader_digest[32], factory_digest[32], signed_digest[32], layout_digest[32];
} zf_proof_t;
typedef struct {
    int (*read)(void *, uint8_t out[ZF_PROOF_BYTES]);
    bool (*write)(void *, const uint8_t bytes[ZF_PROOF_BYTES]);
    void *context;
} zf_proof_port_t;

const zf_target_t *zf_target(unsigned index);
int zf_target_match(const char *connector, const uint8_t mac[6], const char *serial);
bool zf_digest_parse(const char *hex, uint8_t out[32]);
bool zf_proof_encode(const zf_proof_t *, uint8_t out[ZF_PROOF_BYTES]);
bool zf_proof_decode(const uint8_t bytes[ZF_PROOF_BYTES], zf_proof_t *);
/* Exact readback is required. An uncertain write cannot be retried as an
 * absence, revoke a newer checkpoint, or clear the old deployment's intent. */
bool zf_proof_create(zf_proof_port_t, const zf_proof_t *);
bool zf_proof_transition(zf_proof_port_t, const zf_proof_t *, zf_state_t, zf_proof_t *);
bool zf_fallback_metadata(uint32_t ota_count, uint32_t running_slot,
                          const uint32_t sequence[2], const uint32_t state[2],
                          const bool crc_valid[2]);
