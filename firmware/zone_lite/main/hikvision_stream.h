#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Transport-independent MIME decoder. The caller owns all storage and must
 * durably accept a callback before returning true. No image is accumulated. */
#define HIK_BOUNDARY_MAX 200
#define HIK_HEADERS_MAX 2048
#define HIK_MESSAGE_MAX 8192
typedef bool (*hik_message_fn)(void *, const char *, size_t);
typedef struct {
    char marker[HIK_BOUNDARY_MAX + 5];
    size_t marker_length;
    unsigned state;
    char pending[HIK_HEADERS_MAX + HIK_BOUNDARY_MAX + 8];
    size_t pending_length;
    char body[HIK_MESSAGE_MAX + 1];
    size_t body_length;
    bool capture;
    uint32_t messages, discarded, oversized;
    hik_message_fn message;
    void *context;
} hik_stream_t;
bool hik_stream_init(hik_stream_t *, const char *boundary, hik_message_fn, void *);
bool hik_stream_feed(hik_stream_t *, const void *, size_t);
bool hik_stream_boundary(const char *content_type, char output[HIK_BOUNDARY_MAX + 1]);
