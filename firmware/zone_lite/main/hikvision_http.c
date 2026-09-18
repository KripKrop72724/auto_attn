#include "hikvision_http.h"
#include "zone_config.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdio.h>

static int last_http_status;
int hik_http_last_status(void) { return last_http_status; }

typedef struct { bool digest; char content_type[300]; } headers_t;
static esp_err_t header_event(esp_http_client_event_t *e)
{
    headers_t *h = e->user_data;
    if (e->event_id == HTTP_EVENT_ON_HEADER && e->header_key && e->header_value) {
        if (!strcasecmp(e->header_key, "WWW-Authenticate")) {
            h->digest = !strncasecmp(e->header_value, "Digest ", 7);
        } else if (!strcasecmp(e->header_key, "Content-Type")) {
            if (strlen(e->header_value) >= sizeof(h->content_type)) return ESP_FAIL;
            strlcpy(h->content_type, e->header_value, sizeof(h->content_type));
        }
    }
    return ESP_OK;
}
static bool config_valid(void)
{
    const zone_config_t *c = zone_config_get();
    if (!c->hik_host[0] || !c->hik_port || !c->hik_username[0] || !c->hik_password[0] ||
        !c->hik_expected_serial[0] || (!c->hik_https && !c->hik_http_digest_allowed)) return false;
    /* Host is provisioned, never obtained from a redirect or event body. */
    return strspn(c->hik_host, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-") == strlen(c->hik_host);
}
static hik_result_t open_request(esp_http_client_method_t method, const char *path,
    const char *body, headers_t *headers, esp_http_client_handle_t *out)
{
    *out = NULL;
    last_http_status = 0;
    if (!config_valid() || !path || strncmp(path, "/ISAPI/", 7) || strlen(path) > 180) return HIK_CONFIGURATION;
    const zone_config_t *c = zone_config_get();
    char url[300];
    int n = snprintf(url, sizeof(url), "%s://%s:%u%s", c->hik_https ? "https" : "http", c->hik_host, c->hik_port, path);
    if (n < 0 || (size_t)n >= sizeof(url)) return HIK_CONFIGURATION;
    esp_http_client_config_t cfg = {
        .url = url, .method = method, .username = c->hik_username, .password = c->hik_password,
        .auth_type = HTTP_AUTH_TYPE_DIGEST, .timeout_ms = 10000,
        .disable_auto_redirect = true, .max_authorization_retries = 2,
        .event_handler = header_event, .user_data = headers,
        .cert_pem = c->hik_ca_pem[0] ? c->hik_ca_pem : NULL,
        .crt_bundle_attach = c->hik_ca_pem[0] ? NULL : esp_crt_bundle_attach,
        .buffer_size = 2048, .buffer_size_tx = 2048,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) return HIK_NETWORK;
    if (body) esp_http_client_set_header(client, "Content-Type", "application/json");
    hik_result_t result = HIK_NETWORK;
    for (unsigned attempt = 0; attempt < 3; attempt++) {
        memset(headers, 0, sizeof(*headers));
        size_t size = body ? strlen(body) : 0;
        if (size > 32768 || esp_http_client_open(client, (int)size) != ESP_OK) break;
        size_t sent = 0;
        while (sent < size) {
            int written = esp_http_client_write(client, body + sent, (int)(size - sent));
            if (written <= 0) break;
            sent += written;
        }
        if (sent != size || esp_http_client_fetch_headers(client) < 0) break;
        int status = esp_http_client_get_status_code(client);
        last_http_status = status;
        if (status == 200) { *out = client; return HIK_OK; }
        if (status != 401) { result = HIK_HTTP_STATUS; break; }
        result = HIK_AUTH;
        /* Explicit Digest only; never let IDF downgrade a challenge to Basic. */
        if (!headers->digest || attempt == 2 || esp_http_client_add_auth(client) != ESP_OK) break;
        /* fetch_headers may cache part/all of the 401 body. close() alone does
         * not clear that cache in IDF; drain it before the authenticated reply
         * so XML error text cannot be prepended to successful JSON. */
        size_t discarded = 0;
        int64_t deadline = esp_timer_get_time() + 15000000;
        bool drained = false;
        for (;;) {
            char discard[512];
            int n = esp_http_client_read(client, discard, sizeof(discard));
            if (n < 0 || esp_timer_get_time() > deadline) break;
            if (!n) { drained = esp_http_client_is_complete_data_received(client); break; }
            discarded += (size_t)n;
            if (discarded > 16384) break;
        }
        if (!drained) { result = HIK_NETWORK; break; }
        esp_http_client_close(client);
    }
    esp_http_client_cleanup(client);
    return result;
}
hik_result_t hik_http_request(esp_http_client_method_t method, const char *path,
    const char *body, char *response, size_t capacity, size_t *length)
{
    if (!response || capacity < 2 || !length) return HIK_CONFIGURATION;
    *length = 0;
    headers_t headers = {0};
    esp_http_client_handle_t client;
    hik_result_t result = open_request(method, path, body, &headers, &client);
    if (result != HIK_OK) return result;
    int64_t deadline = esp_timer_get_time() + 15000000;
    for (;;) {
        char chunk[1024];
        int n = esp_http_client_read(client, chunk, sizeof(chunk));
        if (n < 0 || esp_timer_get_time() > deadline) { result = HIK_NETWORK; break; }
        if (!n) {
            if (!esp_http_client_is_complete_data_received(client)) result = HIK_NETWORK;
            break;
        }
        if ((size_t)n >= capacity - *length) { result = HIK_OVERSIZED; break; }
        memcpy(response + *length, chunk, n);
        *length += n;
    }
    response[*length] = 0;
    esp_http_client_cleanup(client);
    return result;
}
hik_result_t hik_http_verify_identity(void)
{
    char *body = malloc(8192);
    if (!body) return HIK_NETWORK;
    size_t length;
    hik_result_t result = hik_http_request(HTTP_METHOD_GET, "/ISAPI/System/deviceInfo", NULL, body, 8192, &length);
    if (result == HIK_OK) {
        const char *start = strstr(body, "<serialNumber>");
        const char *end = start ? strstr(start + 14, "</serialNumber>") : NULL;
        const char *expected = zone_config_get()->hik_expected_serial;
        result = start && end && (size_t)(end - start - 14) == strlen(expected) &&
            !memcmp(start + 14, expected, strlen(expected)) ? HIK_OK : HIK_BINDING;
    }
    free(body);
    return result;
}
hik_result_t hik_http_stream(hik_message_fn callback, void *context)
{
    hik_result_t result = hik_http_verify_identity();
    if (result != HIK_OK) return result;
    headers_t headers = {0};
    esp_http_client_handle_t client;
    result = open_request(HTTP_METHOD_GET, "/ISAPI/Event/notification/alertStream", NULL, &headers, &client);
    if (result != HIK_OK) return result;
    char boundary[HIK_BOUNDARY_MAX + 1];
    hik_stream_t *parser = malloc(sizeof(*parser));
    if (!parser || !hik_stream_boundary(headers.content_type, boundary) ||
        !hik_stream_init(parser, boundary, callback, context)) result = HIK_PARSE;
    else for (;;) {
        char chunk[1024];
        int n = esp_http_client_read(client, chunk, sizeof(chunk));
        if (n <= 0) { result = HIK_NETWORK; break; }
        if (!hik_stream_feed(parser, chunk, n)) { result = HIK_CUSTODY; break; }
        if (parser->oversized) { result = HIK_OVERSIZED; break; }
    }
    free(parser);
    esp_http_client_cleanup(client);
    return result;
}
