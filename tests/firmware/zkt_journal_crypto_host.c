#include "zkt_journal_crypto.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    zj_metadata_t m = {.segment_id = 9, .terminal_serial = "TEST-TERMINAL",
        .decoder_profile = "G3-v1", .decoder_version = "1"};
    zj_crypto_key_t key;
    for (unsigned i = 0; i < 32; ++i) key.master[i] = (uint8_t)i;
    for (unsigned i = 0; i < 16; ++i) m.capture_epoch[i] = (uint8_t)i;
    uint8_t metadata[ZJ_META_BYTES];
    assert(zj_metadata_encode(&m, metadata));
    zj_observation_t o = {.sequence = 7, .raw_format = ZJ_LIVE_FRAME,
        .time_quality = ZJ_TIME_UNSYNCED, .encoded_time = 0,
        .captured_at_seconds = 1700000000, .captured_uptime_ms = 123456789,
        .source_ordinal = UINT32_MAX, .identity_revision = 5, .raw_length = 40};
    for (unsigned i = 0; i < 40; ++i) o.raw[i] = (uint8_t)i;
    uint8_t record[ZJ_RECORD_MAX];
    size_t length;
    zj_crypto_port_t crypto = zj_crypto_port(&key);
    const uint8_t expected_sha256[32] = {
        0xba,0x78,0x16,0xbf,0x8f,0x01,0xcf,0xea,0x41,0x41,0x40,0xde,0x5d,0xae,0x22,0x23,
        0xb0,0x03,0x61,0xa3,0x96,0x17,0x7a,0x9c,0xb4,0x10,0xff,0x61,0xf2,0x00,0x15,0xad
    };
    uint8_t hash[32];
    assert(crypto.digest(crypto.context, (const uint8_t *)"abc", 3, hash));
    assert(!memcmp(hash, expected_sha256, sizeof(hash)));
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_OK);
    printf("vector=");
    for (size_t i = 0; i < length; ++i) printf("%02x", record[i]);
    puts("");
    zj_observation_t restored;
    assert(zj_record_decode(metadata, record, length, crypto, &restored) == ZJ_CODEC_OK);
    assert(restored.sequence == o.sequence && restored.raw_length == o.raw_length);
    assert(!memcmp(restored.raw, o.raw, o.raw_length));
    /* Tampering at every byte of either authenticated region fails closed. */
    for (size_t i = 0; i < length; ++i) {
        record[i] ^= 1;
        assert(zj_record_decode(metadata, record, length, crypto, &restored) != ZJ_CODEC_OK);
        assert(!restored.raw_length);
        record[i] ^= 1;
    }
    for (size_t i = 0; i < sizeof(metadata); ++i) {
        metadata[i] ^= 1;
        assert(zj_record_decode(metadata, record, length, crypto, &restored) != ZJ_CODEC_OK);
        metadata[i] ^= 1;
    }
    key.master[0] ^= 1;
    assert(zj_record_decode(metadata, record, length, crypto, &restored) == ZJ_CODEC_AUTH);
    key.master[0] ^= 1;
    /* Same sequence under a different epoch or terminal uses a different key. */
    uint8_t second[ZJ_RECORD_MAX];
    size_t second_length;
    m.capture_epoch[0] ^= 1;
    assert(zj_metadata_encode(&m, metadata));
    assert(zj_record_encode(metadata, &o, crypto, second, sizeof(second), &second_length) == ZJ_CODEC_OK);
    assert(second_length == length && memcmp(second + ZJ_HEADER_BYTES, record + ZJ_HEADER_BYTES,
                                           o.raw_length + ZJ_FACT_BYTES));
    m.capture_epoch[0] ^= 1;
    m.terminal_serial[0] = 'X';
    assert(zj_metadata_encode(&m, metadata));
    assert(zj_record_encode(metadata, &o, crypto, second, sizeof(second), &second_length) == ZJ_CODEC_OK);
    assert(second_length == length && memcmp(second + ZJ_HEADER_BYTES, record + ZJ_HEADER_BYTES,
                                           o.raw_length + ZJ_FACT_BYTES));
    puts("Journal AES-GCM authentication and key separation passed");
    return 0;
}
