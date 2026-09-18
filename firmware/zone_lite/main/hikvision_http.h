#pragma once
#include "esp_http_client.h"
#include "hikvision_stream.h"

typedef enum {
    HIK_OK, HIK_CONFIGURATION, HIK_NETWORK, HIK_AUTH, HIK_HTTP_STATUS,
    HIK_OVERSIZED, HIK_BINDING, HIK_PARSE, HIK_CUSTODY
} hik_result_t;
/* A worker owns a request until completion. Stream task uses its own client.
 * Writes are never retried on timeout: the command worker must verify state. */
hik_result_t hik_http_request(esp_http_client_method_t method, const char *path,
    const char *body, char *response, size_t capacity, size_t *length);
hik_result_t hik_http_verify_identity(void);
hik_result_t hik_http_stream(hik_message_fn callback, void *context);

/* Read only while holding the serialized terminal-request lock. */
int hik_http_last_status(void);
