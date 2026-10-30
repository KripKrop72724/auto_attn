#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* ZKTeco "secure pull" on TCP 4370 (SenseFace, SpeedFace and MB-VL Linux
 * firmware, e.g. SenseFace M2F-LR ZAM70_TFT Ver 6.60): CMD_CONNECT answers
 * 6001 instead of 2000/2005, an RSA handshake agrees an AES-256-CBC key, and
 * every later frame both ways is magic 5050837C + AES(AB BB len zk crc pad).
 * Pure C over mbedTLS; the socket glue lives in zone_lite.c. */

#define ZKT_SECURE_CMD_CHALLENGE 6001
#define ZKT_SECURE_CMD_DMC_EXCHANGE 10063
#define ZKT_SECURE_CMD_KEY_EXCHANGE 10064
#define ZKT_SECURE_CMD_CONFIRM_SESSION 10065
#define ZKT_SECURE_CMD_AUTH_EXT 1106
#define ZKT_SECURE_ACK_AUTH_LOCK 2032
/* Little-endian 16-bit markers of the encrypted TCP header 50 50 83 7C. */
#define ZKT_SECURE_TCP_MARKER_1 0x5050
#define ZKT_SECURE_TCP_MARKER_2 0x7C83
#define ZKT_SECURE_TCP_HEADER_BYTES 8
#define ZKT_SECURE_KEY_BYTES 32
#define ZKT_SECURE_AUTH_EXT_BYTES 36
/* AB BB + inner length + CRC, before padding to the AES block. */
#define ZKT_SECURE_INNER_OVERHEAD 10
#define ZKT_SECURE_SEAL_SLACK (ZKT_SECURE_INNER_OVERHEAD + 15)
/* Base64 handshake payloads either way (a 2048-bit key needs ~600). */
#define ZKT_SECURE_MESSAGE_MAX 1024
#define ZKT_SECURE_PEM_MAX 640

typedef int (*zkt_secure_rng_t)(void *context, unsigned char *output, size_t length);

typedef enum {
    ZKT_SECURE_OK = 0,
    ZKT_SECURE_ERR_ARGUMENT,
    ZKT_SECURE_ERR_MEMORY,
    ZKT_SECURE_ERR_KEYGEN,
    ZKT_SECURE_ERR_ENCODING,
    ZKT_SECURE_ERR_PEER_KEY,
    ZKT_SECURE_ERR_RSA,
    ZKT_SECURE_ERR_AES,
    ZKT_SECURE_ERR_DIGEST,
    ZKT_SECURE_ERR_FRAME,
} zkt_secure_status_t;

const char *zkt_secure_status_name(zkt_secure_status_t status);

/* Building blocks, exposed for the host tests. */
uint32_t zkt_secure_crc32(const uint8_t *data, size_t length);
size_t zkt_secure_keystream_length(void);
bool zkt_secure_keystream_apply(uint8_t *data, size_t length);
bool zkt_secure_dmc_build(uint8_t type, const uint8_t *body, size_t body_length,
    uint8_t *output, size_t capacity, size_t *written);
bool zkt_secure_dmc_parse(const uint8_t *plain, size_t length, uint8_t *type,
    const uint8_t **body, size_t *body_length);
void zkt_secure_derive_aes_key(uint32_t client_secret, uint32_t server_secret,
    uint8_t key[ZKT_SECURE_KEY_BYTES]);
/* CMD_AUTH's comm key in the byte order secure firmware expects. */
void zkt_secure_commkey_native(uint32_t comm_key, uint16_t session_id, uint8_t tick,
    uint8_t output[4]);
/* CMD_AUTH_EXT: HMAC-SHA256 of the comm key's decimal text, then B3 F6 00 01. */
zkt_secure_status_t zkt_secure_auth_ext_payload(uint32_t comm_key,
    uint8_t output[ZKT_SECURE_AUTH_EXT_BYTES]);

/* The ESP's RSA identity: generated once, reused by every secure session.
 * Decrypting with it updates RSA blinding state, so callers serialize use. */
typedef struct {
    void *rsa;
    char pem[ZKT_SECURE_PEM_MAX];
    size_t pem_length;
} zkt_secure_identity_t;

zkt_secure_status_t zkt_secure_identity_generate(zkt_secure_identity_t *identity,
    unsigned bits, zkt_secure_rng_t rng, void *rng_context);
void zkt_secure_identity_free(zkt_secure_identity_t *identity);

/* Handshake payloads are base64 text without a terminating NUL. */
zkt_secure_status_t zkt_secure_dmc_exchange_payload(const zkt_secure_identity_t *identity,
    uint8_t *output, size_t capacity, size_t *written);
/* Reads the device's public key from its 10063 reply and encrypts the
 * client secret to it for 10064. */
zkt_secure_status_t zkt_secure_key_exchange_payload(const uint8_t *device_reply,
    size_t reply_length, uint32_t client_secret, zkt_secure_rng_t rng, void *rng_context,
    uint8_t *output, size_t capacity, size_t *written);
zkt_secure_status_t zkt_secure_server_secret(const zkt_secure_identity_t *identity,
    const uint8_t *device_reply, size_t reply_length, zkt_secure_rng_t rng,
    void *rng_context, uint32_t *server_secret);

/* Frames after 10065. A sealed frame is the 8-byte TCP header followed by
 * zkt_secure_sealed_length(zk_length) ciphertext bytes. */
size_t zkt_secure_sealed_length(size_t zk_length);
zkt_secure_status_t zkt_secure_seal(const uint8_t key[ZKT_SECURE_KEY_BYTES],
    const uint8_t *zk, size_t zk_length, uint8_t *frame, size_t capacity,
    size_t *frame_length);
/* Decrypts in place; the ZK header and payload are then at
 * ciphertext + *zk_offset for *zk_length bytes. */
zkt_secure_status_t zkt_secure_open(const uint8_t key[ZKT_SECURE_KEY_BYTES],
    uint8_t *ciphertext, size_t length, size_t *zk_offset, size_t *zk_length);
