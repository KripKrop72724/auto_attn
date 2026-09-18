#include "hikvision_stream.h"
#include <ctype.h>
#include <string.h>
#include <strings.h>

enum { BOUNDARY, HEADERS, BODY, DONE, FAILED };
static void consume(hik_stream_t *s, size_t n)
{
    s->pending_length -= n;
    memmove(s->pending, s->pending + n, s->pending_length);
}
static void append(hik_stream_t *s, const char *p, size_t n)
{
    if (!s->capture) return;
    if (n > HIK_MESSAGE_MAX - s->body_length) {
        s->capture = false;
        s->body_length = 0;
        s->oversized++;
        return;
    }
    memcpy(s->body + s->body_length, p, n);
    s->body_length += n;
}
bool hik_stream_init(hik_stream_t *s, const char *boundary, hik_message_fn fn, void *ctx)
{
    if (!s || !boundary || !fn) return false;
    size_t n = strlen(boundary);
    if (!n || n > HIK_BOUNDARY_MAX) return false;
    for (size_t i = 0; i < n; i++)
        if ((unsigned char)boundary[i] < 32 || (unsigned char)boundary[i] > 126) return false;
    memset(s, 0, sizeof(*s));
    memcpy(s->marker, "\r\n--", 4);
    memcpy(s->marker + 4, boundary, n);
    s->marker_length = n + 4;
    s->message = fn;
    s->context = ctx;
    return true;
}
static bool drain(hik_stream_t *s)
{
    for (;;) {
        if (s->state == BOUNDARY) {
            size_t n = s->marker_length - 2;
            if (s->pending_length < n + 2) return true;
            if (memcmp(s->pending, s->marker + 2, n)) {
                consume(s, 1);
                continue;
            }
            if (!memcmp(s->pending + n, "--", 2)) { s->state = DONE; return true; }
            if (memcmp(s->pending + n, "\r\n", 2)) return false;
            consume(s, n + 2);
            s->state = HEADERS;
        } else if (s->state == HEADERS) {
            if (s->pending_length > HIK_HEADERS_MAX) return false;
            if (s->pending_length < 4 || memcmp(s->pending + s->pending_length - 4, "\r\n\r\n", 4)) return true;
            s->pending[s->pending_length] = 0;
            s->capture = false;
            bool has_type = false, event_log = false;
            char *line = s->pending;
            while (*line) {
                char *end = strstr(line, "\r\n");
                if (!end) break;
                *end = 0;
                if (!strncasecmp(line, "Content-Type:", 13)) {
                    has_type = true;
                    char *v = line + 13;
                    while (*v == ' ' || *v == '\t') v++;
                    const char *types[] = {"application/json", "application/xml", "text/xml"};
                    for (size_t i = 0; i < 3; i++) {
                        size_t n = strlen(types[i]);
                        if (!strncasecmp(v, types[i], n) && (!v[n] || v[n] == ';' || v[n] == ' ')) s->capture = true;
                    }
                }
                /* V3.3.5 HTTP notifications omit Content-Type for this exact
                 * metadata part. Do not classify arbitrary unnamed binary parts. */
                if (!strcasecmp(line, "Content-Disposition: form-data; name=\"event_log\"")) event_log = true;
                line = end + 2;
            }
            if (!has_type && event_log) s->capture = true;
            if (!s->capture) s->discarded++;
            s->pending_length = s->body_length = 0;
            s->state = BODY;
        } else if (s->state == BODY) {
            size_t n = s->marker_length;
            if (s->pending_length < n + 2) return true;
            if (!memcmp(s->pending, s->marker, n) &&
                (!memcmp(s->pending + n, "\r\n", 2) || !memcmp(s->pending + n, "--", 2))) {
                if (s->capture) {
                    s->body[s->body_length] = 0;
                    if (!s->message(s->context, s->body, s->body_length)) return false;
                    s->messages++;
                }
                consume(s, 2);
                s->state = BOUNDARY;
            } else {
                append(s, s->pending, 1);
                consume(s, 1);
            }
        } else return s->state == DONE;
    }
}
bool hik_stream_feed(hik_stream_t *s, const void *data, size_t length)
{
    if (!s || (!data && length) || s->state == FAILED) return false;
    const char *bytes = data;
    for (size_t i = 0; i < length; i++) {
        if (s->state == DONE) return true;
        if (s->pending_length >= sizeof(s->pending) - 1) { s->state = FAILED; return false; }
        s->pending[s->pending_length++] = bytes[i];
        if (!drain(s)) { s->state = FAILED; return false; }
    }
    return true;
}
bool hik_stream_boundary(const char *type, char output[HIK_BOUNDARY_MAX + 1])
{
    if (!type || !output || strncasecmp(type, "multipart/", 10)) return false;
    const char *p = strchr(type, ';');
    while (p) {
        p++;
        while (*p == ' ' || *p == '\t') p++;
        if (!strncasecmp(p, "boundary=", 9)) {
            p += 9;
            bool quoted = *p == '"';
            if (quoted) p++;
            size_t n = 0;
            while (*p && (quoted ? *p != '"' : *p != ';' && !isspace((unsigned char)*p))) {
                if (n >= HIK_BOUNDARY_MAX || (unsigned char)*p < 32 || (unsigned char)*p > 126) return false;
                output[n++] = *p++;
            }
            if (!n || (quoted && *p != '"')) return false;
            output[n] = 0;
            return true;
        }
        p = strchr(p, ';');
    }
    return false;
}
