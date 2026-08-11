#include "setup_portal.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/ip4_addr.h"

#include "ota_manager.h"
#include "setup_password.h"
#include "setup_portal_assets.h"
#include "zone_config.h"

#define PORTAL_IP              "192.168.254.1"
#define PORTAL_NET             0xC0A8FE00UL
#define PORTAL_MASK            0xFFFFFF00UL
#define PORTAL_RECOVERY_MS     (2 * 60 * 1000)
#define PORTAL_MANUAL_IDLE_MS  (10 * 60 * 1000)
#define PORTAL_STABLE_CLOSE_MS (30 * 1000)
#define PORTAL_BUTTON_GPIO     GPIO_NUM_0
#define PORTAL_BUTTON_HOLD_MS  5000
#define PORTAL_STA_RETRY_MS    5000
#define PORTAL_ACTIVE_STA_RETRY_MS (60 * 1000)
#define PORTAL_HTTP_MAX_OPEN_SOCKETS 7
#define PORTAL_HTTP_MAX_URI_HANDLERS 16
#define PORTAL_PENDING_ROLLBACK_MS (15 * 60 * 1000)
#define VALIDATION_TIMEOUT_MS  30000
#define VALIDATION_OK_BIT      BIT0
#define VALIDATION_FAIL_BIT    BIT1

typedef enum {
    PORTAL_RESULT_IDLE,
    PORTAL_RESULT_TESTING,
    PORTAL_RESULT_SAVED,
    PORTAL_RESULT_FAILED,
} portal_result_t;

typedef struct {
    char ssid[33];
    char password[65];
} validation_request_t;

static const char *TAG = "wifi_setup";
static esp_netif_t *s_ap_netif;
static setup_portal_station_visibility_cb_t s_visibility_cb;
static httpd_handle_t s_httpd;
static EventGroupHandle_t s_validation_events;
static TaskHandle_t s_dns_task;
static volatile bool s_active;
static volatile bool s_recovery_mode;
static volatile bool s_station_owned;
static volatile bool s_validation_connecting;
static volatile bool s_dns_running;
static int64_t s_disconnected_since_ms;
static int64_t s_connected_since_ms;
static int64_t s_last_sta_retry_ms;
static int64_t s_last_activity_ms;
static bool s_pending_rollback_attempted;
static char s_csrf[33];
static portal_result_t s_result = PORTAL_RESULT_IDLE;
static char s_result_message[128] = "Ready to select a network.";
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static bool running_app_pending_verify(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    return running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY;
}

static void secure_zero(void *buffer, size_t length)
{
    volatile unsigned char *p = buffer;
    while (length--) *p++ = 0;
}

static bool constant_time_equal(const char *left, const char *right)
{
    size_t left_len = left ? strlen(left) : 0;
    size_t right_len = right ? strlen(right) : 0;
    size_t max_len = left_len > right_len ? left_len : right_len;
    size_t diff = left_len ^ right_len;
    for (size_t index = 0; index < max_len; ++index) {
        unsigned char a = index < left_len ? (unsigned char)left[index] : 0;
        unsigned char b = index < right_len ? (unsigned char)right[index] : 0;
        diff |= a ^ b;
    }
    return diff == 0;
}

static void wipe_json_password(cJSON *wifi_password)
{
    if (cJSON_IsString(wifi_password) && wifi_password->valuestring) {
        secure_zero(wifi_password->valuestring, strlen(wifi_password->valuestring));
    }
}

static void set_result(portal_result_t result, const char *message)
{
    portENTER_CRITICAL(&s_lock);
    s_result = result;
    strlcpy(s_result_message, message, sizeof(s_result_message));
    portEXIT_CRITICAL(&s_lock);
}

static void generate_csrf(void)
{
    uint8_t random[16];
    esp_fill_random(random, sizeof(random));
    for (size_t index = 0; index < sizeof(random); ++index) {
        snprintf(&s_csrf[index * 2], 3, "%02x", random[index]);
    }
    secure_zero(random, sizeof(random));
}

static bool request_from_ap(httpd_req_t *request)
{
    int fd = httpd_req_to_sockfd(request);
    struct sockaddr_in local = {0};
    struct sockaddr_in peer = {0};
    socklen_t length = sizeof(local);
    if (getsockname(fd, (struct sockaddr *)&local, &length) != 0) return false;
    length = sizeof(peer);
    if (getpeername(fd, (struct sockaddr *)&peer, &length) != 0) return false;
    uint32_t local_ip = ntohl(local.sin_addr.s_addr);
    uint32_t peer_ip = ntohl(peer.sin_addr.s_addr);
    return local_ip == 0xC0A8FE01UL && (peer_ip & PORTAL_MASK) == PORTAL_NET;
}

static esp_err_t begin_response(httpd_req_t *request, const char *content_type)
{
    if (!s_active || !request_from_ap(request)) {
        return httpd_resp_send_err(request, HTTPD_404_NOT_FOUND, "Not found");
    }
    httpd_resp_set_type(request, content_type);
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    httpd_resp_set_hdr(request, "X-Content-Type-Options", "nosniff");
    httpd_resp_set_hdr(request, "X-Frame-Options", "DENY");
    httpd_resp_set_hdr(request, "Referrer-Policy", "no-referrer");
    httpd_resp_set_hdr(request, "Permissions-Policy", "camera=(), microphone=(), geolocation=()");
    // Captive-network probes and browsers often retain idle HTTP/1.1
    // connections. The portal is stateless, so close every response and keep
    // admission capacity available for the actual setup page and API calls.
    httpd_resp_set_hdr(request, "Connection", "close");
    return ESP_OK;
}

static esp_err_t send_json(httpd_req_t *request, const char *status, cJSON *payload)
{
    char *body = cJSON_PrintUnformatted(payload);
    cJSON_Delete(payload);
    if (!body) return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    httpd_resp_set_status(request, status);
    esp_err_t result = httpd_resp_sendstr(request, body);
    cJSON_free(body);
    return result;
}

static esp_err_t root_handler(httpd_req_t *request)
{
    if (begin_response(request, "text/html; charset=utf-8") != ESP_OK) return ESP_OK;
    s_last_activity_ms = now_ms();
    httpd_resp_set_hdr(request, "Content-Security-Policy",
                       "default-src 'none'; img-src data:; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'");
    httpd_resp_send_chunk(request, setup_portal_html_prefix, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(request, setup_portal_logo_base64, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(request, setup_portal_html_middle, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(request, s_csrf, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(request, setup_portal_html_suffix, HTTPD_RESP_USE_STRLEN);
    return httpd_resp_send_chunk(request, NULL, 0);
}

static esp_err_t css_handler(httpd_req_t *request)
{
    if (begin_response(request, "text/css; charset=utf-8") != ESP_OK) return ESP_OK;
    return httpd_resp_sendstr(request, setup_portal_css);
}

static esp_err_t js_handler(httpd_req_t *request)
{
    if (begin_response(request, "application/javascript; charset=utf-8") != ESP_OK) return ESP_OK;
    return httpd_resp_sendstr(request, setup_portal_js);
}

static bool compatible_auth(wifi_auth_mode_t mode)
{
    return mode == WIFI_AUTH_WPA_WPA2_PSK || mode == WIFI_AUTH_WPA2_PSK ||
           mode == WIFI_AUTH_WPA2_WPA3_PSK || mode == WIFI_AUTH_WPA3_PSK;
}

static const char *auth_label(wifi_auth_mode_t mode)
{
    if (mode == WIFI_AUTH_WPA3_PSK) return "WPA3";
    if (mode == WIFI_AUTH_WPA2_WPA3_PSK) return "WPA2/WPA3";
    if (mode == WIFI_AUTH_WPA_WPA2_PSK) return "WPA/WPA2";
    return "WPA2";
}

static int compare_access_points(const void *left, const void *right)
{
    const wifi_ap_record_t *a = left;
    const wifi_ap_record_t *b = right;
    return b->rssi - a->rssi;
}

static esp_err_t networks_handler(httpd_req_t *request)
{
    if (begin_response(request, "application/json") != ESP_OK) return ESP_OK;
    s_last_activity_ms = now_ms();
    if (s_station_owned || ota_manager_busy()) {
        cJSON *payload = cJSON_CreateObject();
        cJSON_AddStringToObject(payload, "message", "The device is busy. Try again shortly.");
        return send_json(request, "409 Conflict", payload);
    }

    // A bounded background recovery probe may still be associating when the
    // user asks for a scan. Cancel only that disconnected probe and restart
    // its one-minute clock; an already healthy station link is left intact.
    if (s_disconnected_since_ms) {
        s_last_sta_retry_ms = now_ms();
        (void)esp_wifi_disconnect();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    wifi_scan_config_t scan = {
        .show_hidden = true,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time.active = {.min = 0, .max = 120},
        .home_chan_dwell_time = 30,
        .coex_background_scan = true,
    };
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    uint16_t count = 0;
    if (err == ESP_OK) err = esp_wifi_scan_get_ap_num(&count);
    if (count > 40) count = 40;
    wifi_ap_record_t *records = count ? calloc(count, sizeof(*records)) : NULL;
    if (err == ESP_OK && count && !records) err = ESP_ERR_NO_MEM;
    if (err == ESP_OK && count) err = esp_wifi_scan_get_ap_records(&count, records);
    if (err != ESP_OK) {
        free(records);
        cJSON *payload = cJSON_CreateObject();
        cJSON_AddStringToObject(payload, "message", "Network scan could not complete.");
        return send_json(request, "503 Service Unavailable", payload);
    }
    qsort(records, count, sizeof(*records), compare_access_points);

    cJSON *payload = cJSON_CreateObject();
    cJSON *networks = cJSON_AddArrayToObject(payload, "networks");
    for (uint16_t index = 0; index < count; ++index) {
        if (!records[index].ssid[0] || !compatible_auth(records[index].authmode)) continue;
        bool duplicate = false;
        cJSON *existing = NULL;
        cJSON_ArrayForEach(existing, networks) {
            cJSON *ssid = cJSON_GetObjectItem(existing, "ssid");
            if (cJSON_IsString(ssid) && strcmp(ssid->valuestring, (char *)records[index].ssid) == 0) {
                duplicate = true;
                break;
            }
        }
        if (duplicate) continue;
        cJSON *network = cJSON_CreateObject();
        cJSON_AddStringToObject(network, "ssid", (char *)records[index].ssid);
        cJSON_AddNumberToObject(network, "rssi", records[index].rssi);
        cJSON_AddStringToObject(network, "security", auth_label(records[index].authmode));
        cJSON_AddItemToArray(networks, network);
        if (cJSON_GetArraySize(networks) >= 20) break;
    }
    secure_zero(records, count * sizeof(*records));
    free(records);
    return send_json(request, "200 OK", payload);
}

static esp_err_t status_handler(httpd_req_t *request)
{
    if (begin_response(request, "application/json") != ESP_OK) return ESP_OK;
    portal_result_t result;
    char message[sizeof(s_result_message)];
    portENTER_CRITICAL(&s_lock);
    result = s_result;
    strlcpy(message, s_result_message, sizeof(message));
    portEXIT_CRITICAL(&s_lock);
    static const char *states[] = {"idle", "testing", "saved", "failed"};
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "state", states[result]);
    cJSON_AddStringToObject(payload, "message", message);
    return send_json(request, "200 OK", payload);
}

static void restore_station(wifi_config_t *configuration)
{
    s_validation_connecting = false;
    (void)esp_wifi_set_config(WIFI_IF_STA, configuration);
    s_station_owned = false;
    (void)esp_wifi_connect();
}

static void validation_task(void *argument)
{
    validation_request_t *candidate = argument;
    wifi_config_t old_configuration = {0};
    wifi_config_t candidate_configuration = {0};
    esp_err_t err = esp_wifi_get_config(WIFI_IF_STA, &old_configuration);
    if (err != ESP_OK) {
        set_result(PORTAL_RESULT_FAILED, "Could not read the existing network. Nothing was changed.");
        goto done;
    }

    strlcpy((char *)candidate_configuration.sta.ssid, candidate->ssid, sizeof(candidate_configuration.sta.ssid));
    strlcpy((char *)candidate_configuration.sta.password, candidate->password, sizeof(candidate_configuration.sta.password));
    candidate_configuration.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    candidate_configuration.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    s_station_owned = true;
    s_validation_connecting = false;
    if (s_visibility_cb) s_visibility_cb(false);
    (void)esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(250));
    xEventGroupClearBits(s_validation_events, VALIDATION_OK_BIT | VALIDATION_FAIL_BIT);
    err = esp_wifi_set_config(WIFI_IF_STA, &candidate_configuration);
    if (err == ESP_OK) {
        s_validation_connecting = true;
        err = esp_wifi_connect();
    }
    EventBits_t result = 0;
    if (err == ESP_OK) {
        result = xEventGroupWaitBits(s_validation_events, VALIDATION_OK_BIT | VALIDATION_FAIL_BIT,
                                     pdTRUE, pdFALSE, pdMS_TO_TICKS(VALIDATION_TIMEOUT_MS));
    }
    s_validation_connecting = false;
    if ((result & VALIDATION_OK_BIT) == 0) {
        restore_station(&old_configuration);
        set_result(PORTAL_RESULT_FAILED, "The network test failed. The previous network was restored.");
        goto done;
    }

    err = zone_config_save_wifi(candidate->ssid, candidate->password);
    if (err != ESP_OK) {
        restore_station(&old_configuration);
        set_result(PORTAL_RESULT_FAILED, "Secure storage failed. The previous network was restored.");
        goto done;
    }

    s_station_owned = false;
    s_connected_since_ms = now_ms();
    if (s_visibility_cb) s_visibility_cb(true);
    set_result(PORTAL_RESULT_SAVED, "Network saved securely. The attendance device is reconnecting.");
    ESP_LOGI(TAG, "Validated and atomically saved a new Wi-Fi network");

    esp_ota_img_states_t ota_state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    bool pending_verification = running &&
        esp_ota_get_state_partition(running, &ota_state) == ESP_OK &&
        ota_state == ESP_OTA_IMG_PENDING_VERIFY;
    if (pending_verification) {
        ESP_LOGI(TAG, "Deferring setup reboot until OTA first-boot health confirmation completes");
    } else {
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }

done:
    secure_zero(&old_configuration, sizeof(old_configuration));
    secure_zero(&candidate_configuration, sizeof(candidate_configuration));
    secure_zero(candidate, sizeof(*candidate));
    free(candidate);
    vTaskDelete(NULL);
}

static esp_err_t network_post_handler(httpd_req_t *request)
{
    if (begin_response(request, "application/json") != ESP_OK) return ESP_OK;
    s_last_activity_ms = now_ms();
    cJSON *payload = cJSON_CreateObject();
    if (s_station_owned || ota_manager_busy()) {
        cJSON_AddStringToObject(payload, "message", "The device is busy. Try again shortly.");
        return send_json(request, "409 Conflict", payload);
    }
    if (request->content_len <= 0 || request->content_len > 512) {
        cJSON_AddStringToObject(payload, "message", "Invalid request.");
        return send_json(request, "400 Bad Request", payload);
    }
    char body[513] = {0};
    int received = 0;
    while (received < request->content_len) {
        int chunk = httpd_req_recv(request, body + received, request->content_len - received);
        if (chunk <= 0) {
            secure_zero(body, sizeof(body));
            cJSON_AddStringToObject(payload, "message", "Incomplete request.");
            return send_json(request, "400 Bad Request", payload);
        }
        received += chunk;
    }
    cJSON *input = cJSON_ParseWithLength(body, received);
    secure_zero(body, sizeof(body));
    cJSON *csrf = input ? cJSON_GetObjectItemCaseSensitive(input, "csrf") : NULL;
    cJSON *ssid = input ? cJSON_GetObjectItemCaseSensitive(input, "ssid") : NULL;
    cJSON *password = input ? cJSON_GetObjectItemCaseSensitive(input, "password") : NULL;
    bool valid_fields = cJSON_IsString(csrf) && cJSON_IsString(ssid) &&
                        cJSON_IsString(password);
    // WPA2 admission to the one-client setup AP is the authentication
    // boundary. The unpredictable per-boot token prevents cross-origin
    // submission, and begin_response() restricts every request to the AP
    // interface and subnet.
    bool authorized = valid_fields && constant_time_equal(csrf->valuestring, s_csrf);
    if (!authorized) {
        wipe_json_password(password);
        cJSON_Delete(input);
        cJSON_AddStringToObject(payload, "message", "The setup session is invalid. Reload the page and try again.");
        return send_json(request, "403 Forbidden", payload);
    }
    size_t ssid_length = strlen(ssid->valuestring);
    size_t password_length = strlen(password->valuestring);
    if (ssid_length == 0 || ssid_length > 32 || password_length < 8 || password_length > 63) {
        wipe_json_password(password);
        cJSON_Delete(input);
        cJSON_AddStringToObject(payload, "message", "Use a valid SSID and an 8–63 character WPA2/WPA3 password.");
        return send_json(request, "400 Bad Request", payload);
    }
    validation_request_t *candidate = calloc(1, sizeof(*candidate));
    if (!candidate) {
        wipe_json_password(password);
        cJSON_Delete(input);
        cJSON_AddStringToObject(payload, "message", "The device is temporarily busy.");
        return send_json(request, "503 Service Unavailable", payload);
    }
    strlcpy(candidate->ssid, ssid->valuestring, sizeof(candidate->ssid));
    strlcpy(candidate->password, password->valuestring, sizeof(candidate->password));
    wipe_json_password(password);
    cJSON_Delete(input);
    set_result(PORTAL_RESULT_TESTING, "Testing the new network for up to 30 seconds…");
    if (xTaskCreate(validation_task, "wifi_validate", 6144, candidate, 3, NULL) != pdPASS) {
        secure_zero(candidate, sizeof(*candidate));
        free(candidate);
        set_result(PORTAL_RESULT_FAILED, "The network test could not start. Nothing was changed.");
        cJSON_AddStringToObject(payload, "message", "The device is temporarily busy.");
        return send_json(request, "503 Service Unavailable", payload);
    }
    cJSON_AddStringToObject(payload, "message", "Network test started.");
    return send_json(request, "202 Accepted", payload);
}

static esp_err_t redirect_handler(httpd_req_t *request)
{
    if (begin_response(request, "text/plain") != ESP_OK) return ESP_OK;
    httpd_resp_set_status(request, "302 Found");
    httpd_resp_set_hdr(request, "Location", "http://" PORTAL_IP "/");
    return httpd_resp_sendstr(request, "Open the SLIC Attendance setup page");
}

static void dns_server_task(void *argument)
{
    (void)argument;
    int socket_fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (socket_fd < 0) goto finished;
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 500000};
    setsockopt(socket_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(53)};
    inet_pton(AF_INET, PORTAL_IP, &address.sin_addr);
    if (bind(socket_fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(socket_fd);
        goto finished;
    }
    uint8_t query[512];
    while (s_dns_running) {
        struct sockaddr_in source = {0};
        socklen_t source_length = sizeof(source);
        int length = recvfrom(socket_fd, query, sizeof(query), 0,
                              (struct sockaddr *)&source, &source_length);
        if (length < 12) continue;
        uint32_t source_ip = ntohl(source.sin_addr.s_addr);
        if ((source_ip & PORTAL_MASK) != PORTAL_NET) continue;
        size_t question_end = 12;
        while (question_end < (size_t)length && query[question_end] != 0) {
            uint8_t label_length = query[question_end];
            if (label_length > 63 || question_end + 1 + label_length >= (size_t)length) break;
            question_end += 1 + label_length;
        }
        if (question_end + 5 > (size_t)length) continue;
        question_end += 5;
        if (question_end + 16 > sizeof(query)) continue;
        query[2] = 0x81; query[3] = 0x80;
        query[6] = 0; query[7] = 1;
        query[8] = query[9] = query[10] = query[11] = 0;
        uint8_t answer[] = {0xC0,0x0C,0x00,0x01,0x00,0x01,0x00,0x00,0x00,0x1E,0x00,0x04,192,168,254,1};
        memcpy(query + question_end, answer, sizeof(answer));
        sendto(socket_fd, query, question_end + sizeof(answer), 0,
               (struct sockaddr *)&source, source_length);
    }
    secure_zero(query, sizeof(query));
    close(socket_fd);
finished:
    s_dns_task = NULL;
    vTaskDelete(NULL);
}

static esp_err_t start_http_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = PORTAL_HTTP_MAX_URI_HANDLERS;
    config.max_open_sockets = PORTAL_HTTP_MAX_OPEN_SOCKETS;
    config.lru_purge_enable = true;
    config.recv_wait_timeout = 3;
    config.send_wait_timeout = 3;
    config.uri_match_fn = httpd_uri_match_wildcard;
    esp_err_t err = httpd_start(&s_httpd, &config);
    if (err != ESP_OK) return err;
    const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = root_handler},
        {.uri = "/app.css", .method = HTTP_GET, .handler = css_handler},
        {.uri = "/app.js", .method = HTTP_GET, .handler = js_handler},
        {.uri = "/api/networks", .method = HTTP_GET, .handler = networks_handler},
        {.uri = "/api/status", .method = HTTP_GET, .handler = status_handler},
        {.uri = "/api/network", .method = HTTP_POST, .handler = network_post_handler},
        {.uri = "/hotspot-detect.html", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/library/test/success.html", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/generate_204", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/gen_204", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/connecttest.txt", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/ncsi.txt", .method = HTTP_GET, .handler = redirect_handler},
        {.uri = "/*", .method = HTTP_GET, .handler = redirect_handler},
    };
    for (size_t index = 0; index < sizeof(routes) / sizeof(routes[0]); ++index) {
        err = httpd_register_uri_handler(s_httpd, &routes[index]);
        if (err != ESP_OK) {
            httpd_stop(s_httpd);
            s_httpd = NULL;
            return err;
        }
    }
    return ESP_OK;
}

static esp_err_t portal_start(bool recovery)
{
    if (s_active) {
        if (recovery) s_recovery_mode = true;
        return ESP_OK;
    }
    if (!ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION) {
        ESP_LOGE(TAG, "Wi-Fi setup portal is disabled because the protected build secret was not supplied");
        return ESP_ERR_INVALID_STATE;
    }
    if (ota_manager_busy()) return ESP_ERR_INVALID_STATE;
    wifi_config_t ap = {0};
    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP), TAG, "read AP MAC");
    snprintf((char *)ap.ap.ssid, sizeof(ap.ap.ssid), "SLIC ATTENDANCE-%02X%02X", mac[4], mac[5]);
    ap.ap.ssid_len = strlen((char *)ap.ap.ssid);
    strlcpy((char *)ap.ap.password, ZONE_LITE_SETUP_PASSWORD, sizeof(ap.ap.password));
    ap.ap.channel = 1;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.max_connection = 1;
    ap.ap.pmf_cfg.capable = true;
    ap.ap.pmf_cfg.required = false;
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_APSTA), TAG, "enable APSTA");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &ap), TAG, "configure setup AP");
    secure_zero(&ap, sizeof(ap));
    esp_err_t dhcp = esp_netif_dhcps_start(s_ap_netif);
    if (dhcp != ESP_OK) {
        (void)esp_wifi_set_mode(WIFI_MODE_STA);
        return dhcp;
    }
    generate_csrf();
    set_result(PORTAL_RESULT_IDLE, "Ready to select a network.");
    s_recovery_mode = recovery;
    s_last_activity_ms = now_ms();
    // A disconnected station scan uses the same radio as the SoftAP.  Reset
    // the retry clock when the portal opens so clients get a full, quiet
    // discovery window before the first bounded recovery probe.
    s_last_sta_retry_ms = s_last_activity_ms;
    s_active = true;
    // Stop any in-flight association inherited from the normal retry loop.
    // Disconnect events are now owned by the active portal, so this cannot
    // restart the rapid retry storm and the first user scan is deterministic.
    (void)esp_wifi_disconnect();
    esp_err_t err = start_http_server();
    if (err != ESP_OK) {
        s_active = false;
        (void)esp_netif_dhcps_stop(s_ap_netif);
        (void)esp_wifi_set_mode(WIFI_MODE_STA);
        return err;
    }
    s_dns_running = true;
    if (xTaskCreate(dns_server_task, "setup_dns", 3072, NULL, 1, &s_dns_task) != pdPASS) {
        s_dns_running = false;
        httpd_stop(s_httpd);
        s_httpd = NULL;
        s_active = false;
        (void)esp_netif_dhcps_stop(s_ap_netif);
        (void)esp_wifi_set_mode(WIFI_MODE_STA);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGW(TAG, "Wi-Fi setup access point enabled (%s mode)", recovery ? "recovery" : "manual");
    return ESP_OK;
}

static void portal_stop(void)
{
    if (!s_active || s_station_owned) return;
    s_active = false;
    s_dns_running = false;
    if (s_httpd) {
        httpd_stop(s_httpd);
        s_httpd = NULL;
    }
    (void)esp_netif_dhcps_stop(s_ap_netif);
    (void)esp_wifi_set_mode(WIFI_MODE_STA);
    if (s_disconnected_since_ms) {
        s_last_sta_retry_ms = now_ms();
        (void)esp_wifi_connect();
    }
    secure_zero(s_csrf, sizeof(s_csrf));
    ESP_LOGI(TAG, "Wi-Fi setup access point disabled; station-only operation restored");
}

static void controller_task(void *argument)
{
    (void)argument;
    int64_t pressed_since = 0;
    bool button_latched = false;
    while (true) {
        int64_t current = now_ms();
        bool pressed = gpio_get_level(PORTAL_BUTTON_GPIO) == 0;
        if (pressed && pressed_since == 0) pressed_since = current;
        if (!pressed) {
            pressed_since = 0;
            button_latched = false;
        } else if (!button_latched && current - pressed_since >= PORTAL_BUTTON_HOLD_MS) {
            button_latched = true;
            (void)portal_start(false);
        }
        int64_t retry_interval = s_active
            ? PORTAL_ACTIVE_STA_RETRY_MS
            : PORTAL_STA_RETRY_MS;
        if (!s_station_owned && s_disconnected_since_ms &&
            current - s_last_sta_retry_ms >= retry_interval) {
            s_last_sta_retry_ms = current;
            esp_err_t retry = esp_wifi_connect();
            if (retry != ESP_OK) {
                ESP_LOGW(TAG, "Bounded station retry failed: %s", esp_err_to_name(retry));
            }
        }
        if (!s_station_owned && !s_pending_rollback_attempted &&
            s_disconnected_since_ms &&
            current - s_disconnected_since_ms >= PORTAL_PENDING_ROLLBACK_MS &&
            running_app_pending_verify()) {
            s_pending_rollback_attempted = true;
            ESP_LOGE(TAG, "Pending OTA image could not restore Wi-Fi; rolling back safely");
            esp_err_t rollback = esp_ota_mark_app_invalid_rollback_and_reboot();
            ESP_LOGE(TAG, "Pending OTA rollback failed: %s", esp_err_to_name(rollback));
        }
        if (!s_active && s_disconnected_since_ms &&
            current - s_disconnected_since_ms >= PORTAL_RECOVERY_MS) {
            (void)portal_start(true);
        }
        if (s_active && !s_station_owned) {
            bool recovered = s_recovery_mode && s_connected_since_ms &&
                             current - s_connected_since_ms >= PORTAL_STABLE_CLOSE_MS;
            bool idle = !s_recovery_mode && current - s_last_activity_ms >= PORTAL_MANUAL_IDLE_MS;
            if (recovered || idle) portal_stop();
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

esp_err_t setup_portal_prepare(setup_portal_station_visibility_cb_t visibility_cb)
{
    s_visibility_cb = visibility_cb;
    s_validation_events = xEventGroupCreate();
    if (!s_validation_events) return ESP_ERR_NO_MEM;
    s_ap_netif = esp_netif_create_default_wifi_ap();
    if (!s_ap_netif) return ESP_ERR_NO_MEM;
    esp_netif_ip_info_t ip = {0};
    ip4addr_aton(PORTAL_IP, (ip4_addr_t *)&ip.ip);
    ip4addr_aton(PORTAL_IP, (ip4_addr_t *)&ip.gw);
    ip4addr_aton("255.255.255.0", (ip4_addr_t *)&ip.netmask);
    ESP_RETURN_ON_ERROR(esp_netif_dhcps_stop(s_ap_netif), TAG, "stop AP DHCP");
    ESP_RETURN_ON_ERROR(esp_netif_set_ip_info(s_ap_netif, &ip), TAG, "set AP address");
    static const char captive_uri[] = "http://" PORTAL_IP "/";
    ESP_RETURN_ON_ERROR(esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET,
                                               ESP_NETIF_CAPTIVEPORTAL_URI,
                                               (void *)captive_uri, strlen(captive_uri)),
                        TAG, "set captive portal DHCP option");
    return ESP_OK;
}

esp_err_t setup_portal_start_controller(void)
{
    gpio_config_t button = {
        .pin_bit_mask = 1ULL << PORTAL_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&button), TAG, "configure BOOT button");
    return xTaskCreate(controller_task, "setup_control", 4096, NULL, 1, NULL) == pdPASS
               ? ESP_OK : ESP_ERR_NO_MEM;
}

bool setup_portal_handle_sta_disconnected(void)
{
    s_connected_since_ms = 0;
    if (!s_disconnected_since_ms) {
        s_disconnected_since_ms = now_ms();
        s_last_sta_retry_ms = s_disconnected_since_ms;
    }
    if (s_station_owned) {
        if (s_validation_connecting) xEventGroupSetBits(s_validation_events, VALIDATION_FAIL_BIT);
        return true;
    }
    // Suppress the main event handler's immediate reconnect loop while the
    // SoftAP is visible.  The controller performs one bounded station probe
    // per minute instead, preventing scans from starving AP beacons.
    if (s_active) return true;
    return false;
}

bool setup_portal_handle_sta_got_ip(void)
{
    if (s_station_owned) {
        if (s_validation_connecting) xEventGroupSetBits(s_validation_events, VALIDATION_OK_BIT);
        return true;
    }
    s_disconnected_since_ms = 0;
    s_last_sta_retry_ms = 0;
    s_pending_rollback_attempted = false;
    if (!s_connected_since_ms) s_connected_since_ms = now_ms();
    return false;
}

bool setup_portal_active(void)
{
    return s_active;
}
