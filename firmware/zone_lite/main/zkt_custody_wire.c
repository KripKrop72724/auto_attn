#include "zkt_custody_wire.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

typedef struct { char *text; size_t capacity, used; bool ok; } output_t;
static void append(output_t *out, const char *format, ...)
{
    if (!out->ok) return;
    va_list args;
    va_start(args, format);
    int size = vsnprintf(out->text + out->used, out->capacity - out->used, format, args);
    va_end(args);
    if (size < 0 || (size_t)size >= out->capacity - out->used) out->ok = false;
    else out->used += (size_t)size;
}
static void hex(const uint8_t *bytes, size_t length, char *out)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < length; ++i) {
        out[i * 2] = digits[bytes[i] >> 4];
        out[i * 2 + 1] = digits[bytes[i] & 15];
    }
    out[length * 2] = 0;
}
static bool hashed(zj_crypto_port_t crypto, const void *bytes, size_t length, char out[65])
{
    uint8_t value[32];
    if (!crypto.digest || !crypto.digest(crypto.context, bytes, length, value)) return false;
    hex(value, sizeof(value), out);
    return true;
}
static void base64(const uint8_t *bytes, size_t length, char out[685])
{
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t position = 0;
    for (size_t i = 0; i < length; i += 3) {
        uint32_t value = (uint32_t)bytes[i] << 16;
        if (i + 1 < length) value |= (uint32_t)bytes[i + 1] << 8;
        if (i + 2 < length) value |= bytes[i + 2];
        out[position++] = alphabet[(value >> 18) & 63];
        out[position++] = alphabet[(value >> 12) & 63];
        out[position++] = i + 1 < length ? alphabet[(value >> 6) & 63] : '=';
        out[position++] = i + 2 < length ? alphabet[value & 63] : '=';
    }
    out[position] = 0;
}
static bool metadata_valid(const zj_metadata_t *metadata)
{
    uint8_t checked[ZJ_META_BYTES];
    return zj_metadata_encode(metadata, checked);
}
static bool observation(output_t *out, const zj_item_t *item, zj_crypto_port_t crypto,
                         zj_custody_expected_t *expected)
{
    const zj_observation_t *value = &item->observation;
    const zj_metadata_t *meta = &item->metadata;
    if (!zj_observation_valid(value) || !metadata_valid(meta)) return false;
    char epoch[33], raw_digest[65], raw[685], identity[256];
    hex(meta->capture_epoch, 16, epoch);
    base64(value->raw, value->raw_length, raw);
    if (!hashed(crypto, value->raw, value->raw_length, raw_digest)) return false;
    int count = snprintf(identity, sizeof(identity), "[\"zkt-observation-v1\",\"%s\",\"%s\",%llu]",
        meta->terminal_serial, epoch, (unsigned long long)value->sequence);
    if (count < 0 || (size_t)count >= sizeof(identity) ||
        !hashed(crypto, identity, (size_t)count, expected->observation_id)) return false;
    char timestamp[32] = "null";
    if (value->captured_at_seconds <= INT64_C(253402300799)) {
        time_t seconds = (time_t)value->captured_at_seconds;
        struct tm utc;
        if ((int64_t)seconds != value->captured_at_seconds || !gmtime_r(&seconds, &utc) ||
            !strftime(timestamp, sizeof(timestamp), "\"%Y-%m-%dT%H:%M:%SZ\"", &utc)) return false;
    }
    static const char *formats[] = {NULL, "LIVE_FRAME", "SOURCE_RECORD", "UNKNOWN"};
    static const char *qualities[] = {NULL, "VERIFIED", "UNSYNCED", "INVALID", "UNKNOWN"};
    append(out, "{\"capture_epoch\":\"%s\",\"capture_sequence\":\"%llu\",\"captured_at\":%s,"
                "\"captured_at_seconds\":\"%lld\",\"captured_uptime_ms\":\"%llu\","
                "\"decoder_profile\":\"%s\",\"decoder_version\":\"%s\",\"encoded_time\":%lu,"
                "\"identity_snapshot\":\"%lu\",\"observation_id\":\"%s\",",
        epoch, (unsigned long long)value->sequence, timestamp, (long long)value->captured_at_seconds,
        (unsigned long long)value->captured_uptime_ms, meta->decoder_profile, meta->decoder_version,
        (unsigned long)value->encoded_time, (unsigned long)value->identity_revision, expected->observation_id);
    if (value->source_ordinal != UINT32_MAX) {
        char compact[33];
        hex(value->source_epoch, 16, compact);
        append(out, "\"occurrence\":{\"ordinal\":%lu,\"source_epoch\":\"%.8s-%.4s-%.4s-%.4s-%.12s\"},",
            (unsigned long)value->source_ordinal, compact, compact + 8, compact + 12, compact + 16, compact + 20);
    }
    append(out, "\"raw_b64\":\"%s\",\"raw_digest\":\"%s\",\"raw_format\":\"%s\","
                "\"terminal_serial\":\"%s\",\"time_quality\":\"%s\"}",
        raw, raw_digest, formats[value->raw_format], meta->terminal_serial, qualities[value->time_quality]);
    return out->ok;
}
static bool exception(output_t *out, const zj_item_t *item, zj_crypto_port_t crypto,
                       zj_custody_expected_t *expected)
{
    if (!item->exception_length || item->exception_length > ZJ_EXCEPTION_MAX ||
        item->token.end <= item->token.offset || item->token.end - item->token.offset != item->exception_length ||
        !item->token.segment_id || item->token.segment_id > ZJ_SEQUENCE_MAX ||
        item->exception < ZJ_EXCEPTION_METADATA || item->exception > ZJ_EXCEPTION_TAIL) return false;
    zj_metadata_t binding = {.segment_id = 1, .decoder_profile = "opaque", .decoder_version = "1"};
    memcpy(binding.terminal_serial, item->custody_serial, sizeof(binding.terminal_serial));
    memcpy(binding.capture_epoch, item->custody_epoch, sizeof(binding.capture_epoch));
    if (!metadata_valid(&binding)) return false;
    char epoch[33], raw_digest[65], raw[685], identity[384];
    hex(item->custody_epoch, 16, epoch);
    base64(item->exception_bytes, item->exception_length, raw);
    if (!hashed(crypto, item->exception_bytes, item->exception_length, raw_digest)) return false;
    int count = snprintf(identity, sizeof(identity),
        "[\"zkt-journal-exception-v1\",\"%s\",\"%s\",%llu,%lu,%lu,\"%s\"]",
        item->custody_serial, epoch, (unsigned long long)item->token.segment_id,
        (unsigned long)item->token.offset, (unsigned long)item->token.end, raw_digest);
    if (count < 0 || (size_t)count >= sizeof(identity) ||
        !hashed(crypto, identity, (size_t)count, expected->observation_id)) return false;
    static const char *kinds[] = {NULL, "METADATA", "FRAME", "AUTH", "TAIL"};
    append(out, "{\"capture_epoch\":\"%s\",\"end_offset\":%lu,\"exception_kind\":\"%s\","
                "\"item_type\":\"JOURNAL_EXCEPTION\",\"observation_id\":\"%s\","
                "\"raw_b64\":\"%s\",\"raw_digest\":\"%s\",\"segment_id\":\"%llu\","
                "\"start_offset\":%lu,\"terminal_serial\":\"%s\"}",
        epoch, (unsigned long)item->token.end, kinds[item->exception], expected->observation_id,
        raw, raw_digest, (unsigned long long)item->token.segment_id,
        (unsigned long)item->token.offset, item->custody_serial);
    return out->ok;
}
bool zj_custody_encode(const zj_item_t *item, zj_crypto_port_t crypto, char *payload,
                        size_t capacity, zj_custody_expected_t *expected)
{
    if (payload && capacity) payload[0] = 0;
    if (expected) memset(expected, 0, sizeof(*expected));
    if (!item || !payload || !capacity || !expected) return false;
    output_t out = {payload, capacity, 0, true};
    append(&out, "{\"observations\":[");
    size_t start = out.used;
    bool ok = item->kind == ZJ_OBSERVATION ? observation(&out, item, crypto, expected) :
        item->kind == ZJ_PRESERVED_EXCEPTION ? exception(&out, item, crypto, expected) : false;
    if (ok) ok = hashed(crypto, payload + start, out.used - start, expected->payload_digest);
    append(&out, "],\"schema_version\":1}");
    if (!ok || !out.ok) {
        memset(expected, 0, sizeof(*expected));
        payload[0] = 0;
        return false;
    }
    return true;
}
