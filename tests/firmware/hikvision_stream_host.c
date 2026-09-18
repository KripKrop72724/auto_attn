#include "hikvision_stream.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned calls;
static bool accept(void *ctx, const char *body, size_t n)
{
    (void)ctx;
    assert(n == 7 && !memcmp(body, "{\"a\":1}", 7));
    calls++;
    return true;
}
static bool refuse(void *ctx, const char *body, size_t n)
{ (void)ctx; (void)body; (void)n; return false; }

int main(void)
{
    char boundary[HIK_BOUNDARY_MAX + 1];
    assert(hik_stream_boundary("multipart/mixed; boundary=\"probe\"", boundary));
    assert(!strcmp(boundary, "probe"));
    assert(!hik_stream_boundary("text/plain; boundary=probe", boundary));
    assert(!hik_stream_boundary("multipart/mixed; boundary=\"oops", boundary));
    const char *data = "--probe\r\nContent-Type: application/json\r\n\r\n{\"a\":1}\r\n"
        "--probe\r\nContent-Type: image/jpeg\r\n\r\nbinary\r\n--probeXxbytes\r\n"
        "--probe\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{\"a\":1}\r\n"
        "--probe\r\nContent-Disposition: form-data; name=\"event_log\"\r\n\r\n{\"a\":1}\r\n"
        "--probe\r\nContent-Disposition: form-data; name=\"event_log\"\r\nContent-Type: image/jpeg\r\n\r\nbinary\r\n--probe--\r\n";
    hik_stream_t *s = malloc(sizeof(*s));
    assert(s);
    for (size_t split = 1; split <= strlen(data); split++) {
        calls = 0;
        assert(hik_stream_init(s, "probe", accept, NULL));
        for (size_t p = 0; p < strlen(data); p += split) {
            size_t n = strlen(data) - p;
            if (n > split) n = split;
            assert(hik_stream_feed(s, data + p, n));
        }
        assert(calls == 3 && s->discarded == 2 && s->oversized == 0);
    }
    assert(hik_stream_init(s, "probe", refuse, NULL));
    assert(!hik_stream_feed(s, data, strlen(data)));
    assert(!hik_stream_feed(s, data, 1));
    assert(hik_stream_init(s, "probe", accept, NULL));
    const char *head = "--probe\r\nContent-Type: application/json\r\n\r\n";
    assert(hik_stream_feed(s, head, strlen(head)));
    char block[1024]; memset(block, 'x', sizeof(block));
    for (int i = 0; i < 256; i++) assert(hik_stream_feed(s, block, sizeof(block)));
    const char *tail = "\r\n--probe\r\nContent-Type: application/json\r\n\r\n{\"a\":1}\r\n--probe--";
    calls = 0;
    assert(hik_stream_feed(s, tail, strlen(tail)));
    assert(s->oversized == 1 && calls == 1);
    free(s);
    puts("Hikvision fragmented MIME, oversized body, binary skip and custody failure passed");
}
