"""Portable journal bounds and durable sequence allocation, with faulted ports."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_journal_codec_and_nonce_reservations(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "journal.c"
    unit.write_text(r'''
#include "zkt_journal_codec.h"
#include <assert.h>
#include <string.h>

static uint64_t durable_limit = 1;
static bool reservation_fails, uncertain;
static bool reserve(void *context, uint64_t limit)
{
    assert(context == &durable_limit && limit > durable_limit);
    if (!reservation_fails || uncertain) durable_limit = limit;
    return !reservation_fails;
}
/* This port tests encoding and callback failure handling only. Production
 * AES-GCM is exercised separately against independent HKDF/AESGCM vectors. */
static bool crypto_fails;
static bool seal(void *context, const uint8_t *metadata, const uint8_t *nonce,
                 const uint8_t *aad, size_t aad_length, const uint8_t *plain,
                 size_t length, uint8_t *cipher, uint8_t *tag)
{
    assert(context == &crypto_fails && !memcmp(nonce, "ZJ01", 4));
    assert(aad_length == ZJ_META_BYTES + ZJ_HEADER_BYTES);
    assert(!memcmp(aad, metadata, ZJ_META_BYTES));
    assert(length >= ZJ_FACT_BYTES && length <= ZJ_FACT_BYTES + ZJ_RAW_MAX);
    memcpy(cipher, plain, length);
    memset(tag, 0x5a, ZJ_TAG_BYTES);
    return !crypto_fails;
}
static bool open_record(void *context, const uint8_t *metadata, const uint8_t *nonce,
                        const uint8_t *aad, size_t aad_length, const uint8_t *cipher,
                        size_t length, const uint8_t *tag, uint8_t *plain)
{
    uint8_t ignored[ZJ_TAG_BYTES];
    (void)tag;
    return seal(context, metadata, nonce, aad, aad_length, cipher, length, plain, ignored);
}
int main(void)
{
    zj_sequence_t sequence;
    uint64_t id, prior = 0;
    assert(!zj_sequence_init(&sequence, 0, reserve, &durable_limit));
    assert(zj_sequence_init(&sequence, durable_limit, reserve, &durable_limit));
    for (unsigned i = 0; i < 600; ++i) {
        assert(zj_sequence_next(&sequence, &id) && id > prior);
        assert(id < durable_limit);
        prior = id;
        if (i % 17 == 0) assert(zj_sequence_init(&sequence, durable_limit, reserve, &durable_limit));
    }
    for (unsigned persisted = 0; persisted < 2; ++persisted) {
        assert(zj_sequence_init(&sequence, durable_limit, reserve, &durable_limit));
        reservation_fails = true;
        uncertain = persisted;
        assert(!zj_sequence_next(&sequence, &id) && !id && !sequence.ready);
        reservation_fails = false;
        assert(!zj_sequence_next(&sequence, &id));
        assert(zj_sequence_init(&sequence, durable_limit, reserve, &durable_limit));
        assert(zj_sequence_next(&sequence, &id) && id > prior);
        prior = id;
    }
    durable_limit = ZJ_SEQUENCE_MAX;
    assert(zj_sequence_init(&sequence, durable_limit, reserve, &durable_limit));
    assert(zj_sequence_next(&sequence, &id) && id == ZJ_SEQUENCE_MAX);
    assert(!zj_sequence_next(&sequence, &id));
    zj_metadata_t m = {.segment_id = 1, .capture_epoch = {1},
        .terminal_serial = "TEST-TERMINAL", .decoder_profile = "G3-v1", .decoder_version = "1"};
    uint8_t metadata[ZJ_META_BYTES], canonical[ZJ_META_BYTES];
    assert(zj_metadata_encode(&m, metadata));
    memcpy(canonical, metadata, sizeof(metadata));
    zj_metadata_t decoded;
    assert(zj_metadata_decode(metadata, &decoded));
    assert(decoded.segment_id == m.segment_id && !strcmp(decoded.terminal_serial, m.terminal_serial));
    metadata[239] = 1;
    assert(!zj_metadata_decode(metadata, &decoded));
    memcpy(metadata, canonical, sizeof(metadata));
    memset(m.terminal_serial, 'x', sizeof(m.terminal_serial));
    assert(!zj_metadata_encode(&m, metadata));
    zj_observation_t o = {.sequence = 7, .raw_format = ZJ_LIVE_FRAME,
        .time_quality = ZJ_TIME_UNKNOWN, .encoded_time = 0,
        .captured_at_seconds = 1700000000, .captured_uptime_ms = UINT64_MAX,
        .source_ordinal = UINT32_MAX, .raw_length = ZJ_RAW_MAX};
    for (unsigned i = 0; i < ZJ_RAW_MAX; ++i) o.raw[i] = (uint8_t)i;
    zj_crypto_port_t crypto = {.seal = seal, .open = open_record, .context = &crypto_fails};
    uint8_t record[ZJ_RECORD_MAX];
    size_t length;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_OK);
    assert(length == sizeof(record));
    zj_observation_t restored;
    assert(zj_record_decode(metadata, record, length, crypto, &restored) == ZJ_CODEC_OK);
    assert(restored.encoded_time == 0 && restored.captured_uptime_ms == UINT64_MAX);
    assert(restored.raw_length == ZJ_RAW_MAX && !memcmp(restored.raw, o.raw, ZJ_RAW_MAX));
    assert(zj_record_encode(metadata, &o, crypto, record, 1, &length) == ZJ_CODEC_SMALL);
    assert(length == ZJ_RECORD_MAX);
    crypto_fails = true;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_CRYPTO);
    assert(!length);
    for (size_t i = 0; i < sizeof(record); ++i) assert(!record[i]);
    crypto_fails = false;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_OK);
    crypto_fails = true;
    assert(zj_record_decode(metadata, record, length, crypto, &restored) == ZJ_CODEC_AUTH);
    for (size_t i = 0; i < sizeof(restored); ++i) assert(!((uint8_t *)&restored)[i]);
    crypto_fails = false;
    for (size_t cut = 0; cut < length; ++cut)
        assert(zj_record_decode(metadata, record, cut, crypto, &restored) == ZJ_CODEC_INVALID);
    o.source_ordinal = 0;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_INVALID);
    o.source_epoch[0] = 1;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_INVALID);
    o.raw_format = ZJ_SOURCE_RECORD;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_OK);
    o.raw_length = ZJ_RAW_MAX + 1;
    assert(zj_record_encode(metadata, &o, crypto, record, sizeof(record), &length) == ZJ_CODEC_INVALID);
    return 0;
}
''')
    executable = tmp_path / "journal"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(unit), str(main / "zkt_journal_codec.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
