#include "zkt_journal_crypto.h"
#include <string.h>
#include "mbedtls/gcm.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"

static bool key_for(const zj_crypto_key_t *root, const uint8_t metadata[ZJ_META_BYTES],
                    uint8_t key[32])
{
    zj_metadata_t parsed;
    if (!root || !zj_metadata_decode(metadata, &parsed)) return false;
    static const unsigned char domain[] = "ZKT-ATTENDANCE-JOURNAL-AES256GCM-V1";
    uint8_t info[sizeof(domain) - 1 + sizeof(parsed.terminal_serial)];
    memcpy(info, domain, sizeof(domain) - 1);
    memcpy(info + sizeof(domain) - 1, parsed.terminal_serial, sizeof(parsed.terminal_serial));
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    return md && mbedtls_hkdf(md, parsed.capture_epoch, sizeof(parsed.capture_epoch),
                              root->master, sizeof(root->master), info, sizeof(info), key, 32) == 0;
}

static bool seal(void *context, const uint8_t metadata[ZJ_META_BYTES],
                 const uint8_t nonce[12], const uint8_t *aad, size_t aad_length,
                 const uint8_t *plain, size_t length, uint8_t *cipher, uint8_t tag[ZJ_TAG_BYTES])
{
    uint8_t key[32] = {0};
    bool derived = key_for(context, metadata, key);
    mbedtls_gcm_context gcm;
    mbedtls_gcm_init(&gcm);
    int result = -1;
    if (derived) result = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, key, 256);
    if (!result) result = mbedtls_gcm_crypt_and_tag(&gcm, MBEDTLS_GCM_ENCRYPT, length,
        nonce, 12, aad, aad_length, plain, cipher, ZJ_TAG_BYTES, tag);
    mbedtls_gcm_free(&gcm);
    mbedtls_platform_zeroize(key, sizeof(key));
    return result == 0;
}

static bool open_record(void *context, const uint8_t metadata[ZJ_META_BYTES],
                        const uint8_t nonce[12], const uint8_t *aad, size_t aad_length,
                        const uint8_t *cipher, size_t length,
                        const uint8_t tag[ZJ_TAG_BYTES], uint8_t *plain)
{
    uint8_t key[32] = {0};
    bool derived = key_for(context, metadata, key);
    mbedtls_gcm_context gcm;
    mbedtls_gcm_init(&gcm);
    int result = -1;
    if (derived) result = mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, key, 256);
    if (!result) result = mbedtls_gcm_auth_decrypt(&gcm, length, nonce, 12,
        aad, aad_length, tag, ZJ_TAG_BYTES, cipher, plain);
    mbedtls_gcm_free(&gcm);
    mbedtls_platform_zeroize(key, sizeof(key));
    if (result) mbedtls_platform_zeroize(plain, length);
    return result == 0;
}

static bool digest(void *context, const uint8_t *data, size_t length, uint8_t out[32])
{
    (void)context;
    return data && out && mbedtls_sha256(data, length, out, 0) == 0;
}

zj_crypto_port_t zj_crypto_port(zj_crypto_key_t *key)
{
    return (zj_crypto_port_t){.seal = seal, .open = open_record, .digest = digest, .context = key};
}
