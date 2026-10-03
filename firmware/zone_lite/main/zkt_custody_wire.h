#pragma once
#include "zkt_journal_store.h"
struct cJSON;

#define ZJ_CUSTODY_PAYLOAD_MAX 3072U
typedef struct { char observation_id[65], payload_digest[65]; } zj_custody_expected_t;

/* One immutable item per request. Output key order is canonical JSON and all
 * 64-bit wire quantities use decimal strings. Encoding must remain stable
 * across reboot/bridge replay; a mutable roster or current clock is not used. */
bool zj_custody_encode(const zj_item_t *item, zj_crypto_port_t crypto,
                        char *payload, size_t capacity, zj_custody_expected_t *expected);
/* The socket owner must first correlate the envelope's message_id with its
 * current request. This verifies committed, exact per-item custody; generic
 * transport ACKs and Oracle responses cannot authorize retirement. */
bool zj_custody_verify(const struct cJSON *ack, const zj_custody_expected_t *expected,
                        zj_crypto_port_t crypto, uint8_t receipt_digest[32]);
