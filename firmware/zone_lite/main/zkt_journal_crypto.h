#pragma once
#include "zkt_journal_codec.h"

/* The master is a dedicated 32-byte random journal key in encrypted NVS. It
 * must not rotate with a transport token and must never be recreated while
 * retained journal segments exist. Callers own and erase this key's lifetime. */
typedef struct { uint8_t master[32]; } zj_crypto_key_t;
zj_crypto_port_t zj_crypto_port(zj_crypto_key_t *key);
