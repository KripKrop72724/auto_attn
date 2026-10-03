#include "zkt_custody_wire.h"
#include "zkt_journal_crypto.h"
#include "cJSON.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void receipt_tests(zj_crypto_port_t crypto, const zj_custody_expected_t *expected)
{
    char text[1024];
    snprintf(text, sizeof(text), "{\"type\":\"zkt_observation_ack\",\"schema_version\":1,"
        "\"committed\":true,\"delivery_authority\":\"ADD\",\"oracle_completion\":\"NOT_ASSERTED\","
        "\"items\":[{\"index\":0,\"observation_id\":\"%s\",\"payload_digest\":\"%s\","
        "\"receipt_id\":\"11111111-2222-4333-8444-555555555555\",\"custody\":\"PRESERVED_UNRESOLVED\"}]}",
        expected->observation_id, expected->payload_digest);
    cJSON *ack = cJSON_Parse(text);
    assert(ack);
    uint8_t proof[32];
    assert(zj_custody_verify(ack, expected, crypto, proof));
    printf("proof=");
    for (unsigned i = 0; i < 32; ++i) printf("%02x", proof[i]);
    puts("");
    const char *fields[] = {"type", "schema_version", "committed", "delivery_authority", "oracle_completion"};
    for (unsigned i = 0; i < sizeof(fields) / sizeof(fields[0]); ++i) {
        cJSON *saved = cJSON_DetachItemFromObjectCaseSensitive(ack, fields[i]);
        assert(saved && !zj_custody_verify(ack, expected, crypto, proof));
        cJSON_AddStringToObject(ack, fields[i], "INVALID");
        assert(!zj_custody_verify(ack, expected, crypto, proof));
        cJSON_DeleteItemFromObjectCaseSensitive(ack, fields[i]);
        cJSON_AddItemToObject(ack, fields[i], saved);
        assert(zj_custody_verify(ack, expected, crypto, proof));
    }
    cJSON *items = cJSON_GetObjectItemCaseSensitive(ack, "items"), *row = cJSON_GetArrayItem(items, 0);
    const char *row_fields[] = {"index", "observation_id", "payload_digest", "receipt_id", "custody"};
    for (unsigned i = 0; i < sizeof(row_fields) / sizeof(row_fields[0]); ++i) {
        cJSON *saved = cJSON_DetachItemFromObjectCaseSensitive(row, row_fields[i]);
        assert(saved && !zj_custody_verify(ack, expected, crypto, proof));
        cJSON_AddItemToObject(row, row_fields[i], saved);
        cJSON_AddItemToObject(row, row_fields[i], cJSON_Duplicate(saved, true));
        assert(!zj_custody_verify(ack, expected, crypto, proof));
        cJSON_DeleteItemFromObjectCaseSensitive(row, row_fields[i]);
        assert(zj_custody_verify(ack, expected, crypto, proof));
    }
    cJSON_AddItemToArray(items, cJSON_Duplicate(row, true));
    assert(!zj_custody_verify(ack, expected, crypto, proof));
    cJSON_DeleteItemFromArray(items, 1);
    zj_custody_expected_t wrong = *expected;
    wrong.payload_digest[0] = wrong.payload_digest[0] == '0' ? '1' : '0';
    assert(!zj_custody_verify(ack, &wrong, crypto, proof));
    for (unsigned i = 0; i < 32; ++i) assert(proof[i] == 0);
    cJSON_Delete(ack);
}
int main(void)
{
    zj_crypto_key_t key = {{0}};
    zj_crypto_port_t crypto = zj_crypto_port(&key);
    zj_item_t item = {.kind = ZJ_OBSERVATION, .metadata = {.segment_id = 1,
        .terminal_serial = "TEST01", .decoder_profile = "zkt-g3-v1", .decoder_version = "1"},
        .observation = {.sequence = INT64_MAX, .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNSYNCED,
            .encoded_time = UINT32_MAX, .captured_at_seconds = 1700000000, .captured_uptime_ms = UINT64_MAX,
            .source_ordinal = UINT32_MAX, .identity_revision = 7}};
    for (unsigned i = 0; i < 16; ++i) item.metadata.capture_epoch[i] = (uint8_t)i;
    for (unsigned i = 0; i < ZJ_RAW_MAX; ++i) item.observation.raw[i] = (uint8_t)i;
    const unsigned lengths[] = {1, 2, 3, 512, 40, 512};
    for (unsigned example = 0; example < 6; ++example) {
        item.observation.raw_length = (uint16_t)lengths[example];
        if (example == 4) {
            item.observation.raw_format = ZJ_SOURCE_RECORD;
            item.observation.source_ordinal = 200000;
            for (unsigned i = 0; i < 16; ++i) item.observation.source_epoch[i] = (uint8_t)(i + 1);
        }
        if (example == 5) {
            item.kind = ZJ_PRESERVED_EXCEPTION;
            item.exception = ZJ_EXCEPTION_AUTH;
            item.exception_length = 512;
            memcpy(item.exception_bytes, item.observation.raw, 512);
            memcpy(item.custody_epoch, item.metadata.capture_epoch, 16);
            strcpy(item.custody_serial, item.metadata.terminal_serial);
            item.token.segment_id = INT64_MAX;
            item.token.offset = 65500;
            item.token.end = 65500 + 512;
        }
        char payload[ZJ_CUSTODY_PAYLOAD_MAX];
        zj_custody_expected_t expected;
        assert(zj_custody_encode(&item, crypto, payload, sizeof(payload), &expected));
        printf("wire=%s\n", payload);
        printf("expected=%s,%s\n", expected.observation_id, expected.payload_digest);
        /* ADD's websocket sender parses then reprints the payload. Neither
         * decimal identities nor canonical digest may change in that step. */
        cJSON *json = cJSON_Parse(payload);
        assert(json);
        char *round_trip = cJSON_PrintUnformatted(json);
        assert(round_trip && !strcmp(payload, round_trip));
        free(round_trip);
        cJSON_Delete(json);
        receipt_tests(crypto, &expected);
        size_t needed = strlen(payload) + 1;
        for (size_t capacity = 1; capacity < needed; ++capacity) {
            assert(!zj_custody_encode(&item, crypto, payload, capacity, &expected));
            assert(!payload[0] && !expected.observation_id[0] && !expected.payload_digest[0]);
        }
    }
    return 0;
}
