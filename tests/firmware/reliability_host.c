#include "reliability.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>

int main(void)
{
    FILE *append = rel_open_append("legacy-append"); assert(append);
    assert(fputs("settled\n", append) >= 0); assert(fclose(append) == 0);
    append = rel_open_append("legacy-append"); assert(append);
    assert(fputs("interrupted", append) >= 0); assert(fclose(append) == 0);
    assert(rel_open_append("legacy-append") == NULL);
    append = fopen("legacy-append", "rb"); assert(append);
    char preserved[64] = {0};
    assert(fread(preserved, 1, sizeof(preserved), append) == 19);
    assert(strcmp(preserved, "settled\ninterrupted") == 0);
    assert(fclose(append) == 0);
    assert(remove("legacy-append") == 0);
    const char *valid[] = {"{}", "[]", "null", "-12.30e+4", "{\"a\":[true,false,null,\"x\\n\",{}]}", " \"\\u0041\" "};
    const char *invalid[] = {"", "{", "[1,]", "{\"a\":}", "01", "+1", "1.", "1e", "true false", "\"x\n\"", "\"\\x\"", "[}"};
    for (size_t i = 0; i < sizeof(valid)/sizeof(*valid); i++) assert(rel_json_syntax_valid(valid[i], strlen(valid[i])));
    for (size_t i = 0; i < sizeof(invalid)/sizeof(*invalid); i++) assert(!rel_json_syntax_valid(invalid[i], strlen(invalid[i])));
    char deep[200]; memset(deep, '[', 99); memset(deep + 99, ']', 99); deep[198] = 0;
    assert(!rel_json_syntax_valid(deep, 198));

    size_t shape;
    assert(!rel_live_frame_size(13, 0, &shape));
    assert(!rel_live_frame_size(36, 0, &shape));
    assert(rel_live_frame_size(36, 12, &shape) && shape == 12);
    assert(rel_live_frame_size(36, 36, &shape) && shape == 36);
    assert(!rel_live_frame_size(64, 0, &shape));
    assert(rel_live_frame_size(64, 32, &shape) && shape == 32);
    assert(!rel_live_frame_size(65, 32, &shape));
    for (size_t length = 0; length <= 4096; length++) {
        unsigned char *bytes = calloc(length ? length : 1, 1);
        rel_live_record_t record;
        (void)rel_parse_live_record(bytes, length, &record);
        free(bytes);
    }
    const size_t shapes[] = {12, 32, 36, 52};
    for (size_t i = 0; i < 4; i++) {
        unsigned char bytes[52] = {0};
        size_t b = shapes[i] == 12 ? 4 : 24;
        bytes[0] = shapes[i] == 12 ? 7 : '7';
        bytes[b+2] = 26; bytes[b+3] = 9; bytes[b+4] = 16;
        bytes[b+5] = 10; bytes[b+6] = 30; bytes[b+7] = 15;
        rel_live_record_t record;
        assert(rel_parse_live_record(bytes, shapes[i], &record));
        assert(!strcmp(record.user_id, "7"));
        bytes[b+3] = 0;
        assert(!rel_parse_live_record(bytes, shapes[i], &record));
    }

    FILE *queue = tmpfile(); assert(queue);
    assert(fputs("A\nB\nC\n", queue) >= 0); assert(fflush(queue) == 0);
    /* The count is advisory: even a falsely zero count cannot authorize
       retiring the file when only A has been acknowledged. */
    assert(!rel_settled_eof(queue, 2));
    assert(!rel_settled_eof(queue, 7));
    assert(rel_settled_eof(queue, 6));
    rewind(queue);
    uint32_t count = 99;
    assert(rel_count_rows(queue, &count) == REL_SCAN_OK && count == 3);
    assert(fseek(queue, 0, SEEK_END) == 0); assert(fputs("partial", queue) >= 0);
    rewind(queue); count = 99;
    assert(rel_count_rows(queue, &count) == REL_SCAN_ERROR && count == 99);
    assert(rel_count_rows(NULL, &count) == REL_SCAN_ERROR);
    fclose(queue);
    queue = tmpfile(); assert(queue);
    assert(rel_count_rows(queue, &count) == REL_SCAN_EMPTY && count == 0);
    fclose(queue);

    const char *fp = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    assert(rel_identity_matches("OLD", "OLD", fp, fp));
    assert(!rel_identity_matches("OLD", "NEW", fp, fp));
    assert(!rel_identity_matches("OLD", "OLD", NULL, fp));

    /* Seeded malformed-input exercise runs the exact firmware parser. */
    unsigned seed = 17;
    for (unsigned round = 0; round < 100000; round++) {
        unsigned char data[128];
        for (size_t j = 0; j < sizeof(data); j++) {
            seed = seed * 1664525u + 1013904223u;
            data[j] = (unsigned char)(seed >> 24);
        }
        size_t n = seed % sizeof(data);
        rel_live_record_t record;
        (void)rel_parse_live_record(data, n, &record);
        (void)rel_json_syntax_valid((const char *)data, n);
    }
    puts("reliability host regression tests passed");
    return 0;
}
