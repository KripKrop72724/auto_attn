#pragma once
#include "esp_http_client.h"
#include "hikvision_stream.h"

typedef enum {
    HIK_OK, HIK_CONFIGURATION, HIK_NETWORK, HIK_AUTH, HIK_HTTP_STATUS,
    HIK_OVERSIZED, HIK_BINDING, HIK_PARSE, HIK_CUSTODY
} hik_result_t;
/* Per-socket-operation timeout of every terminal request. 3.1.7: an AcsEvent
 * search on a time-sorted terminal (Mirpur-02, DS-K1T342MFWX-E1 V4.38.0) can
 * take just over 10 s, so event searches alone get HIK_HTTP_SEARCH_TIMEOUT_MS
 * through hik_http_request_timeout(). Identity, clock, users, commands,
 * discovery and the stream keep HIK_HTTP_TIMEOUT_MS. */
#define HIK_HTTP_TIMEOUT_MS 10000
#define HIK_HTTP_SEARCH_TIMEOUT_MS 25000
#define HIK_HTTP_MIN_TIMEOUT_MS 1000
/* A worker owns a request until completion. Stream task uses its own client.
 * Writes are never retried on timeout: the command worker must verify state. */
hik_result_t hik_http_request(esp_http_client_method_t method, const char *path,
    const char *body, char *response, size_t capacity, size_t *length);
/* Same, with an explicit timeout in [HIK_HTTP_MIN_TIMEOUT_MS, HIK_HTTP_SEARCH_TIMEOUT_MS]. */
hik_result_t hik_http_request_timeout(esp_http_client_method_t method, const char *path,
    const char *body, int timeout_ms, char *response, size_t capacity, size_t *length);
/* Called after every completed (or failed) buffered request, on the calling
 * task, so workers can count a slow but answering terminal as progress. */
void hik_http_set_progress_hook(void (*hook)(void));
hik_result_t hik_http_verify_identity(void);
/* Discovery can authenticate a candidate without changing the active host. */
hik_result_t hik_http_verify_identity_at(const char *host, int timeout_ms);
hik_result_t hik_http_stream(hik_message_fn callback, void *context);
/* State reports a serial-verified, valid MIME connection and every close. */
hik_result_t hik_http_stream_with_state(hik_message_fn callback, void *context,
    void (*state)(bool open));

/* Read only while holding the serialized terminal-request lock. */
int hik_http_last_status(void);
