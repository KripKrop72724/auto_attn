#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_spiffs.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "mbedtls/sha256.h"
#include "nvs_flash.h"

#if __has_include("zone_lite_config.h")
#include "zone_lite_config.h"
#else
#include "zone_lite_config.example.h"
#endif

#define CMD_OPTIONS_RRQ 11
#define CMD_USERTEMP_RRQ 9
#define CMD_ATTLOG_RRQ 13
#define CMD_GET_FREE_SIZES 50
#define CMD_STARTVERIFY 60
#define CMD_CANCELCAPTURE 62
#define CMD_GET_TIME 201
#define CMD_REG_EVENT 500
#define CMD_CONNECT 1000
#define CMD_EXIT 1001
#define CMD_AUTH 1102
#define CMD_FREE_DATA 1502
#define CMD_READ_WITH_BUFFER 1503
#define CMD_READ_BUFFER_CHUNK 1504

#define CMD_ACK_OK 2000
#define CMD_ACK_DATA 2002
#define CMD_PREPARE_DATA 1500
#define CMD_DATA 1501
#define CMD_ACK_UNAUTH 2005

#define FCT_USER 5
#define EF_ATTLOG 1

#define MACHINE_PREPARE_DATA_1 20560
#define MACHINE_PREPARE_DATA_2 32130
#define USHRT_MAX_ZK 65535

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT BIT1
#define WIFI_MAXIMUM_RETRY 1000000

#define STORAGE_BASE "/storage"
#define PENDING_PATH STORAGE_BASE "/pending.jsonl"
#define PENDING_TMP_PATH STORAGE_BASE "/pending.tmp"
#define BLOCKED_PATH STORAGE_BASE "/blocked_identity.jsonl"
#define ACKED_PATH STORAGE_BASE "/acked_uids.txt"
#define MAX_USERS 512
#define SEEN_HASH_CAPACITY 262144
#define MAX_EVENT_JSON 1024
#define ORDS_BULK_CHUNK_SIZE 5000
#define ORDS_TIMEOUT_MS 20000

static const char *TAG = "zone_lite";
static EventGroupHandle_t wifi_event_group;
static int wifi_retry_count;

typedef struct {
    uint16_t command;
    uint16_t checksum;
    uint16_t session_id;
    uint16_t reply_id;
} __attribute__((packed)) zk_header_t;

typedef struct {
    uint16_t marker_1;
    uint16_t marker_2;
    uint32_t length;
} __attribute__((packed)) zk_tcp_header_t;

typedef struct {
    uint16_t session_id;
    uint16_t reply_id;
} zk_context_t;

typedef struct {
    uint16_t code;
    uint16_t session_id;
    uint16_t reply_id;
    uint8_t *data;
    size_t data_len;
} zk_response_t;

typedef struct {
    char uid[16];
    char user_id[32];
    char name[64];
    char employee_name[64];
    char cnic[16];
    bool raw_punch;
} zkt_user_t;

typedef struct {
    zkt_user_t rows[MAX_USERS];
    size_t count;
} user_table_t;

typedef struct {
    char user_id[32];
    char employee_name[64];
    char cnic[16];
    char timestamp[24];
    char event_uid[65];
    uint8_t status;
    uint8_t punch;
    bool raw_punch;
} attendance_event_t;

static char g_device_serial[80] = "";
static uint64_t *g_seen_hashes;
static size_t g_seen_count;

static uint16_t read_le16(const uint8_t *data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint32_t read_le32(const uint8_t *data)
{
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) | ((uint32_t)data[2] << 16) |
           ((uint32_t)data[3] << 24);
}

static int32_t read_le32_signed(const uint8_t *data)
{
    return (int32_t)read_le32(data);
}

static void write_le16(uint8_t *data, uint16_t value)
{
    data[0] = value & 0xff;
    data[1] = (value >> 8) & 0xff;
}

static void write_le32(uint8_t *data, uint32_t value)
{
    data[0] = value & 0xff;
    data[1] = (value >> 8) & 0xff;
    data[2] = (value >> 16) & 0xff;
    data[3] = (value >> 24) & 0xff;
}

static uint16_t zk_checksum(const uint8_t *data, size_t len)
{
    int32_t checksum = 0;
    while (len > 1) {
        checksum += read_le16(data);
        data += 2;
        len -= 2;
        if (checksum > USHRT_MAX_ZK) {
            checksum -= USHRT_MAX_ZK;
        }
    }
    if (len > 0) {
        checksum += data[0];
    }
    while (checksum > USHRT_MAX_ZK) {
        checksum -= USHRT_MAX_ZK;
    }
    checksum = ~checksum;
    while (checksum < 0) {
        checksum += USHRT_MAX_ZK;
    }
    return (uint16_t)checksum;
}

static void make_commkey(uint32_t key, uint16_t session_id, uint8_t out[4])
{
    uint32_t reversed = 0;
    for (int i = 0; i < 32; i++) {
        reversed <<= 1;
        if ((key & (1U << i)) != 0) {
            reversed |= 1;
        }
    }
    uint32_t mixed = reversed + session_id;
    uint8_t k[4] = {
        (uint8_t)(mixed & 0xff),
        (uint8_t)((mixed >> 8) & 0xff),
        (uint8_t)((mixed >> 16) & 0xff),
        (uint8_t)((mixed >> 24) & 0xff),
    };
    k[0] ^= 'Z';
    k[1] ^= 'K';
    k[2] ^= 'S';
    k[3] ^= 'O';
    uint8_t swapped[4] = {k[2], k[3], k[0], k[1]};
    uint8_t ticks = 50;
    out[0] = swapped[0] ^ ticks;
    out[1] = swapped[1] ^ ticks;
    out[2] = ticks;
    out[3] = swapped[3] ^ ticks;
}

static bool recv_exact(int sock, uint8_t *buf, size_t len)
{
    size_t offset = 0;
    while (offset < len) {
        int got = recv(sock, buf + offset, len - offset, 0);
        if (got <= 0) {
            return false;
        }
        offset += (size_t)got;
    }
    return true;
}

static bool drain_bytes(int sock, size_t len)
{
    uint8_t scratch[512];
    while (len > 0) {
        size_t want = len > sizeof(scratch) ? sizeof(scratch) : len;
        int got = recv(sock, scratch, want, 0);
        if (got <= 0) {
            return false;
        }
        len -= (size_t)got;
    }
    return true;
}

static bool send_all(int sock, const uint8_t *buf, size_t len)
{
    size_t offset = 0;
    while (offset < len) {
        int sent = send(sock, buf + offset, len - offset, 0);
        if (sent <= 0) {
            return false;
        }
        offset += (size_t)sent;
    }
    return true;
}

static bool zk_recv_data_stream(int sock, uint8_t *out, size_t out_len, size_t *actual_len)
{
    size_t written = 0;
    bool saw_ack = false;
    while (!saw_ack) {
        zk_tcp_header_t top;
        if (!recv_exact(sock, (uint8_t *)&top, sizeof(top))) {
            return false;
        }
        if (top.marker_1 != MACHINE_PREPARE_DATA_1 || top.marker_2 != MACHINE_PREPARE_DATA_2 ||
            top.length < sizeof(zk_header_t)) {
            return false;
        }
        uint8_t *packet = malloc(top.length);
        if (packet == NULL) {
            (void)drain_bytes(sock, top.length);
            return false;
        }
        if (!recv_exact(sock, packet, top.length)) {
            free(packet);
            return false;
        }
        zk_header_t *header = (zk_header_t *)packet;
        size_t data_len = top.length - sizeof(zk_header_t);
        uint8_t *data = packet + sizeof(zk_header_t);
        if (header->command == CMD_DATA) {
            size_t copy_len = data_len;
            if (copy_len > out_len - written) {
                copy_len = out_len - written;
            }
            if (copy_len > 0) {
                memcpy(out + written, data, copy_len);
                written += copy_len;
            }
        } else if (header->command == CMD_ACK_OK) {
            saw_ack = true;
            free(packet);
            break;
        } else {
            ESP_LOGW(TAG, "Unexpected ZKT stream command %u", header->command);
            free(packet);
            return false;
        }
        free(packet);
    }
    *actual_len = written;
    return written == out_len && saw_ack;
}

static bool zk_send_command(
    int sock,
    zk_context_t *ctx,
    uint16_t command,
    const uint8_t *payload,
    size_t payload_len,
    uint8_t *rx,
    size_t rx_cap,
    zk_response_t *response)
{
    uint8_t tx[sizeof(zk_tcp_header_t) + 512];
    size_t packet_len = sizeof(zk_header_t) + payload_len;
    if (packet_len > sizeof(tx) - sizeof(zk_tcp_header_t)) {
        ESP_LOGE(TAG, "ZKT command payload too large: %u", (unsigned)payload_len);
        return false;
    }

    uint16_t current_reply = ctx->reply_id;
    uint16_t next_reply = (uint16_t)(current_reply + 1);
    if (next_reply >= USHRT_MAX_ZK) {
        next_reply = (uint16_t)(next_reply - USHRT_MAX_ZK);
    }

    zk_header_t header = {
        .command = command,
        .checksum = 0,
        .session_id = ctx->session_id,
        .reply_id = current_reply,
    };
    uint8_t *packet = tx + sizeof(zk_tcp_header_t);
    memcpy(packet, &header, sizeof(header));
    if (payload_len > 0) {
        memcpy(packet + sizeof(header), payload, payload_len);
    }
    ((zk_header_t *)packet)->checksum = zk_checksum(packet, packet_len);
    ((zk_header_t *)packet)->reply_id = next_reply;

    zk_tcp_header_t tcp_header = {
        .marker_1 = MACHINE_PREPARE_DATA_1,
        .marker_2 = MACHINE_PREPARE_DATA_2,
        .length = (uint32_t)packet_len,
    };
    memcpy(tx, &tcp_header, sizeof(tcp_header));

    if (!send_all(sock, tx, sizeof(zk_tcp_header_t) + packet_len)) {
        return false;
    }

    zk_tcp_header_t reply_top;
    if (!recv_exact(sock, (uint8_t *)&reply_top, sizeof(reply_top))) {
        return false;
    }
    if (reply_top.marker_1 != MACHINE_PREPARE_DATA_1 ||
        reply_top.marker_2 != MACHINE_PREPARE_DATA_2 || reply_top.length < sizeof(zk_header_t)) {
        return false;
    }
    if (reply_top.length > rx_cap) {
        ESP_LOGW(TAG, "ZKT reply too large for buffer: %lu", (unsigned long)reply_top.length);
        (void)drain_bytes(sock, reply_top.length);
        return false;
    }
    if (!recv_exact(sock, rx, reply_top.length)) {
        return false;
    }

    zk_header_t *reply = (zk_header_t *)rx;
    ctx->reply_id = reply->reply_id;
    response->code = reply->command;
    response->session_id = reply->session_id;
    response->reply_id = reply->reply_id;
    response->data = rx + sizeof(zk_header_t);
    response->data_len = reply_top.length - sizeof(zk_header_t);
    return true;
}

static bool zk_status_ok(uint16_t code)
{
    return code == CMD_ACK_OK || code == CMD_ACK_DATA || code == CMD_PREPARE_DATA ||
           code == CMD_DATA;
}

static bool zk_connect_and_auth(int sock, zk_context_t *ctx)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    ctx->session_id = 0;
    ctx->reply_id = USHRT_MAX_ZK - 1;

    if (!zk_send_command(sock, ctx, CMD_CONNECT, NULL, 0, rx, sizeof(rx), &response)) {
        ESP_LOGW(TAG, "ZKT CMD_CONNECT did not return a valid TCP response");
        return false;
    }

    ctx->session_id = response.session_id;
    ESP_LOGI(
        TAG,
        "ZKT CMD_CONNECT response code=%u session=%u reply=%u data_len=%u",
        response.code,
        response.session_id,
        response.reply_id,
        (unsigned)response.data_len);
    if (response.code == CMD_ACK_UNAUTH) {
        uint8_t commkey[4];
        make_commkey(ZONE_LITE_ZKT_COMM_KEY, ctx->session_id, commkey);
        if (!zk_send_command(sock, ctx, CMD_AUTH, commkey, sizeof(commkey), rx, sizeof(rx), &response)) {
            ESP_LOGW(TAG, "ZKT CMD_AUTH did not return a valid TCP response");
            return false;
        }
        ESP_LOGI(
            TAG,
            "ZKT CMD_AUTH response code=%u session=%u reply=%u data_len=%u",
            response.code,
            response.session_id,
            response.reply_id,
            (unsigned)response.data_len);
    }

    if (!zk_status_ok(response.code)) {
        ESP_LOGW(TAG, "ZKT auth failed with response code %u", response.code);
        return false;
    }
    ctx->session_id = response.session_id;
    return true;
}

static bool zk_read_option(int sock, zk_context_t *ctx, const char *name, char *out, size_t out_len)
{
    uint8_t rx[1024];
    uint8_t request[80];
    zk_response_t response = {0};
    size_t name_len = strlen(name) + 1;
    if (name_len > sizeof(request) || out_len == 0) {
        return false;
    }
    memcpy(request, name, name_len);

    if (!zk_send_command(sock, ctx, CMD_OPTIONS_RRQ, request, name_len, rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code)) {
        return false;
    }

    const uint8_t *value = response.data;
    size_t value_len = response.data_len;
    for (size_t i = 0; i < response.data_len; i++) {
        if (response.data[i] == '=') {
            value = response.data + i + 1;
            value_len = response.data_len - i - 1;
            break;
        }
    }

    size_t copy_len = 0;
    while (copy_len < value_len && copy_len + 1 < out_len && value[copy_len] != '\0') {
        out[copy_len] = (char)value[copy_len];
        copy_len++;
    }
    out[copy_len] = '\0';
    return copy_len > 0;
}

static void decode_zk_time(uint32_t value, struct tm *out)
{
    memset(out, 0, sizeof(*out));
    out->tm_sec = value % 60;
    value /= 60;
    out->tm_min = value % 60;
    value /= 60;
    out->tm_hour = value % 24;
    value /= 24;
    out->tm_mday = value % 31 + 1;
    value /= 31;
    out->tm_mon = value % 12;
    value /= 12;
    out->tm_year = (int)value + 100;
}

static void iso_from_zk_time(uint32_t value, char out[24])
{
    struct tm decoded;
    decode_zk_time(value, &decoded);
    decoded.tm_isdst = 0;
    time_t utc_epoch = mktime(&decoded) - (5 * 60 * 60);
    struct tm utc_decoded;
    gmtime_r(&utc_epoch, &utc_decoded);
    decoded = utc_decoded;
    int year = decoded.tm_year + 1900;
    int month = decoded.tm_mon + 1;
    int day = decoded.tm_mday;
    int hour = decoded.tm_hour;
    int minute = decoded.tm_min;
    int second = decoded.tm_sec;
    if (year < 2000 || year > 2099) {
        year = 2000;
    }
    if (month < 1 || month > 12) {
        month = 1;
    }
    if (day < 1 || day > 31) {
        day = 1;
    }
    if (hour < 0 || hour > 23) {
        hour = 0;
    }
    if (minute < 0 || minute > 59) {
        minute = 0;
    }
    if (second < 0 || second > 59) {
        second = 0;
    }
    snprintf(
        out,
        24,
        "%04d-%02d-%02dT%02d:%02d:%02dZ",
        year,
        month,
        day,
        hour,
        minute,
        second);
}

static bool zk_get_time_parts(int sock, zk_context_t *ctx, struct tm *out)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    if (!zk_send_command(sock, ctx, CMD_GET_TIME, NULL, 0, rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code) || response.data_len < 4) {
        return false;
    }
    decode_zk_time(read_le32(response.data), out);
    return true;
}

static bool zk_get_time(int sock, zk_context_t *ctx, char *out, size_t out_len)
{
    struct tm decoded;
    if (!zk_get_time_parts(sock, ctx, &decoded)) {
        return false;
    }
    uint32_t encoded = (uint32_t)(decoded.tm_year - 100);
    encoded = encoded * 12 + (uint32_t)decoded.tm_mon;
    encoded = encoded * 31 + (uint32_t)(decoded.tm_mday - 1);
    encoded = encoded * 24 + (uint32_t)decoded.tm_hour;
    encoded = encoded * 60 + (uint32_t)decoded.tm_min;
    encoded = encoded * 60 + (uint32_t)decoded.tm_sec;
    char iso[24];
    iso_from_zk_time(encoded, iso);
    snprintf(out, out_len, "%s", iso);
    return true;
}

static bool zk_get_counts(int sock, zk_context_t *ctx, int32_t *users, int32_t *records)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    if (!zk_send_command(sock, ctx, CMD_GET_FREE_SIZES, NULL, 0, rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code) || response.data_len < 36) {
        return false;
    }
    *users = response.data_len >= 20 ? read_le32_signed(response.data + (4 * 4)) : -1;
    *records = read_le32_signed(response.data + (8 * 4));
    return true;
}

static bool zk_read_buffer(int sock, zk_context_t *ctx, uint16_t command, uint32_t fct, uint8_t **out, size_t *out_len)
{
    uint8_t *rx = malloc(8192);
    if (rx == NULL) {
        return false;
    }
    uint8_t payload[11] = {0};
    payload[0] = 1;
    write_le16(payload + 1, command);
    write_le32(payload + 3, fct);
    write_le32(payload + 7, 0);

    zk_response_t response = {0};
    if (!zk_send_command(sock, ctx, CMD_READ_WITH_BUFFER, payload, sizeof(payload), rx, 8192, &response) ||
        !zk_status_ok(response.code)) {
        ESP_LOGW(
            TAG,
            "ZKT read_with_buffer command=%u fct=%lu failed code=%u data_len=%u",
            command,
            (unsigned long)fct,
            response.code,
            (unsigned)response.data_len);
        free(rx);
        return false;
    }

    ESP_LOGI(
        TAG,
        "ZKT read_with_buffer command=%u fct=%lu response code=%u data_len=%u",
        command,
        (unsigned long)fct,
        response.code,
        (unsigned)response.data_len);

    if (response.code == CMD_DATA) {
        *out = malloc(response.data_len);
        if (*out == NULL) {
            free(rx);
            return false;
        }
        memcpy(*out, response.data, response.data_len);
        *out_len = response.data_len;
        free(rx);
        return true;
    }

    if (response.data_len < 5) {
        ESP_LOGW(
            TAG,
            "ZKT read_with_buffer command=%u returned short prepare payload len=%u",
            command,
            (unsigned)response.data_len);
        free(rx);
        return false;
    }
    uint32_t size = read_le32(response.data + 1);
    if (size == 0 || size > 5 * 1024 * 1024) {
        ESP_LOGW(TAG, "Rejecting unexpected ZKT buffer size %lu", (unsigned long)size);
        free(rx);
        return false;
    }

    uint8_t *buffer = malloc(size);
    if (buffer == NULL) {
        ESP_LOGE(TAG, "Could not allocate %lu byte ZKT buffer", (unsigned long)size);
        free(rx);
        return false;
    }

    uint32_t offset = 0;
    const uint32_t max_chunk = 0xffc0;
    while (offset < size) {
        uint32_t want = size - offset > max_chunk ? max_chunk : size - offset;
        uint8_t chunk_payload[8];
        write_le32(chunk_payload, offset);
        write_le32(chunk_payload + 4, want);
        uint8_t *chunk_rx = malloc(want + 64);
        if (chunk_rx == NULL) {
            free(buffer);
            return false;
        }
        zk_response_t chunk_response = {0};
        bool ok = zk_send_command(
            sock,
            ctx,
            CMD_READ_BUFFER_CHUNK,
            chunk_payload,
            sizeof(chunk_payload),
            chunk_rx,
            want + 64,
            &chunk_response);
        if (ok && chunk_response.code == CMD_PREPARE_DATA) {
            size_t actual = 0;
            ok = zk_recv_data_stream(sock, chunk_rx, want, &actual);
            chunk_response.data = chunk_rx;
            chunk_response.data_len = actual;
            chunk_response.code = ok ? CMD_DATA : chunk_response.code;
        }
        if (!ok || chunk_response.code != CMD_DATA || chunk_response.data_len < want) {
            ESP_LOGW(
                TAG,
                "Failed reading ZKT chunk at %lu code=%u len=%u want=%lu",
                (unsigned long)offset,
                chunk_response.code,
                (unsigned)chunk_response.data_len,
                (unsigned long)want);
            free(chunk_rx);
            free(buffer);
            free(rx);
            return false;
        }
        memcpy(buffer + offset, chunk_response.data, want);
        free(chunk_rx);
        offset += want;
        ESP_LOGI(TAG, "Read ZKT buffered data %lu/%lu", (unsigned long)offset, (unsigned long)size);
    }

    (void)zk_send_command(sock, ctx, CMD_FREE_DATA, NULL, 0, rx, 8192, &response);
    free(rx);
    *out = buffer;
    *out_len = size;
    return true;
}

static void zk_disconnect(int sock, zk_context_t *ctx)
{
    uint8_t rx[64];
    zk_response_t response = {0};
    (void)zk_send_command(sock, ctx, CMD_EXIT, NULL, 0, rx, sizeof(rx), &response);
}

static bool tcp_connect_with_timeout(uint32_t host_order_ip, uint16_t port, int timeout_ms, int *out_sock)
{
    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (sock < 0) {
        return false;
    }
    int flags = fcntl(sock, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(sock, F_SETFL, flags | O_NONBLOCK);
    }
    struct sockaddr_in dest = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
        .sin_addr.s_addr = htonl(host_order_ip),
    };
    int rc = connect(sock, (struct sockaddr *)&dest, sizeof(dest));
    if (rc < 0 && errno != EINPROGRESS) {
        close(sock);
        return false;
    }
    fd_set write_fds;
    FD_ZERO(&write_fds);
    FD_SET(sock, &write_fds);
    struct timeval tv = {.tv_sec = timeout_ms / 1000, .tv_usec = (timeout_ms % 1000) * 1000};
    rc = select(sock + 1, NULL, &write_fds, NULL, &tv);
    if (rc <= 0) {
        close(sock);
        return false;
    }
    int so_error = 0;
    socklen_t len = sizeof(so_error);
    if (getsockopt(sock, SOL_SOCKET, SO_ERROR, &so_error, &len) < 0 || so_error != 0) {
        close(sock);
        return false;
    }
    if (flags >= 0) {
        fcntl(sock, F_SETFL, flags);
    }
    struct timeval io_timeout = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &io_timeout, sizeof(io_timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &io_timeout, sizeof(io_timeout));
    *out_sock = sock;
    return true;
}

static void trim_spaces(char *value)
{
    char *start = value;
    while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') {
        start++;
    }
    if (start != value) {
        memmove(value, start, strlen(start) + 1);
    }
    size_t len = strlen(value);
    while (len > 0 && (value[len - 1] == ' ' || value[len - 1] == '\t' ||
                       value[len - 1] == '\r' || value[len - 1] == '\n')) {
        value[--len] = '\0';
    }
}

static void parse_machine_identity(zkt_user_t *user)
{
    snprintf(user->employee_name, sizeof(user->employee_name), "%s", user->name);
    user->cnic[0] = '\0';
    user->raw_punch = false;
    trim_spaces(user->employee_name);
    size_t len = strlen(user->employee_name);
    if (len < 14) {
        return;
    }
    char *last_dash = strrchr(user->employee_name, '-');
    if (last_dash == NULL || strlen(last_dash + 1) != 13) {
        return;
    }
    for (int i = 0; i < 13; i++) {
        if (last_dash[1 + i] < '0' || last_dash[1 + i] > '9') {
            return;
        }
    }
    snprintf(user->cnic, sizeof(user->cnic), "%s", last_dash + 1);
    *last_dash = '\0';
    size_t name_len = strlen(user->employee_name);
    if (name_len >= 2 && strcmp(user->employee_name + name_len - 2, "-S") == 0) {
        user->raw_punch = true;
        user->employee_name[name_len - 2] = '\0';
    }
    trim_spaces(user->employee_name);
}

static void copy_zk_string(char *out, size_t out_len, const uint8_t *data, size_t len)
{
    size_t n = 0;
    while (n + 1 < out_len && n < len && data[n] != 0) {
        out[n] = (char)data[n];
        n++;
    }
    out[n] = '\0';
    trim_spaces(out);
}

static bool zk_load_users(int sock, zk_context_t *ctx, user_table_t *users, int32_t user_count)
{
    memset(users, 0, sizeof(*users));
    if (user_count <= 0) {
        return true;
    }
    ESP_LOGI(TAG, "Requesting ZKT user table (%ld users)", (long)user_count);
    uint8_t *data = NULL;
    size_t len = 0;
    if (!zk_read_buffer(sock, ctx, CMD_USERTEMP_RRQ, FCT_USER, &data, &len) || len < 4) {
        ESP_LOGW(TAG, "Could not read ZKT users");
        free(data);
        return false;
    }
    uint32_t total_size = read_le32(data);
    uint32_t packet_size = user_count > 0 ? total_size / (uint32_t)user_count : 0;
    const uint8_t *p = data + 4;
    size_t remain = len - 4;
    ESP_LOGI(TAG, "Reading %ld ZKT users packet_size=%lu", (long)user_count, (unsigned long)packet_size);
    while (users->count < MAX_USERS) {
        if (packet_size == 28 && remain >= 28) {
            zkt_user_t *u = &users->rows[users->count++];
            snprintf(u->uid, sizeof(u->uid), "%u", read_le16(p));
            copy_zk_string(u->name, sizeof(u->name), p + 8, 8);
            snprintf(u->user_id, sizeof(u->user_id), "%lu", (unsigned long)read_le32(p + 24));
            parse_machine_identity(u);
            p += 28;
            remain -= 28;
        } else if (remain >= 72) {
            zkt_user_t *u = &users->rows[users->count++];
            snprintf(u->uid, sizeof(u->uid), "%u", read_le16(p));
            copy_zk_string(u->name, sizeof(u->name), p + 11, 24);
            copy_zk_string(u->user_id, sizeof(u->user_id), p + 48, 24);
            parse_machine_identity(u);
            p += 72;
            remain -= 72;
        } else {
            break;
        }
    }
    free(data);
    ESP_LOGI(TAG, "Loaded %u users", (unsigned)users->count);
    return true;
}

static const zkt_user_t *find_user_by_user_id(const user_table_t *users, const char *user_id)
{
    for (size_t i = 0; i < users->count; i++) {
        if (strcmp(users->rows[i].user_id, user_id) == 0 || strcmp(users->rows[i].uid, user_id) == 0) {
            return &users->rows[i];
        }
    }
    return NULL;
}

static const zkt_user_t *find_user_by_uid(const user_table_t *users, uint16_t uid)
{
    char text[16];
    snprintf(text, sizeof(text), "%u", uid);
    for (size_t i = 0; i < users->count; i++) {
        if (strcmp(users->rows[i].uid, text) == 0) {
            return &users->rows[i];
        }
    }
    return NULL;
}

static void sha256_hex(const char *input, char out[65])
{
    unsigned char digest[32];
    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    mbedtls_sha256_starts(&ctx, 0);
    mbedtls_sha256_update(&ctx, (const unsigned char *)input, strlen(input));
    mbedtls_sha256_finish(&ctx, digest);
    mbedtls_sha256_free(&ctx);
    for (int i = 0; i < 32; i++) {
        snprintf(out + (i * 2), 3, "%02x", digest[i]);
    }
    out[64] = '\0';
}

static void build_event_uid(attendance_event_t *event)
{
    char material[256];
    snprintf(
        material,
        sizeof(material),
        "{\"device_event_time\":\"%s\",\"device_serial\":\"%s\",\"punch\":\"%u\",\"user_id\":\"%s\"}",
        event->timestamp,
        g_device_serial,
        event->punch,
        event->user_id);
    sha256_hex(material, event->event_uid);
}

static uint64_t seen_hash_uid(const char *uid)
{
    uint64_t hash = 1469598103934665603ULL;
    while (*uid != '\0') {
        hash ^= (uint8_t)*uid++;
        hash *= 1099511628211ULL;
    }
    return hash == 0 ? 1 : hash;
}

static bool seen_contains(const char *uid)
{
    if (g_seen_hashes == NULL) {
        return false;
    }
    uint64_t hash = seen_hash_uid(uid);
    size_t slot = hash % SEEN_HASH_CAPACITY;
    for (size_t i = 0; i < SEEN_HASH_CAPACITY; i++) {
        uint64_t current = g_seen_hashes[slot];
        if (current == 0) {
            return false;
        }
        if (current == hash) {
            return true;
        }
        slot = (slot + 1) % SEEN_HASH_CAPACITY;
    }
    return true;
}

static bool seen_add(const char *uid)
{
    if (g_seen_hashes == NULL) {
        return false;
    }
    if (g_seen_count + 1 >= (SEEN_HASH_CAPACITY * 7 / 10)) {
        ESP_LOGW(TAG, "Seen UID cache near capacity; increase SEEN_HASH_CAPACITY");
        return false;
    }
    uint64_t hash = seen_hash_uid(uid);
    size_t slot = hash % SEEN_HASH_CAPACITY;
    for (size_t i = 0; i < SEEN_HASH_CAPACITY; i++) {
        uint64_t current = g_seen_hashes[slot];
        if (current == hash) {
            return true;
        }
        if (current == 0) {
            g_seen_hashes[slot] = hash;
            g_seen_count++;
            return true;
        }
        slot = (slot + 1) % SEEN_HASH_CAPACITY;
    }
    ESP_LOGW(TAG, "Seen UID cache full");
    return false;
}

static void append_line(const char *path, const char *line)
{
    FILE *f = fopen(path, "a");
    if (f == NULL) {
        ESP_LOGE(TAG, "Could not open %s for append", path);
        return;
    }
    fputs(line, f);
    fputc('\n', f);
    fclose(f);
}

static void append_line_to_open_file(FILE *f, const char *path, const char *line)
{
    if (f != NULL) {
        fputs(line, f);
        fputc('\n', f);
        return;
    }
    append_line(path, line);
}

static bool extract_event_uid(const char *line, char uid[65])
{
    const char *key = strstr(line, "\"event_uid\"");
    if (key == NULL) {
        return false;
    }
    const char *colon = strchr(key, ':');
    if (colon == NULL) {
        return false;
    }
    const char *start = strchr(colon, '"');
    if (start == NULL) {
        return false;
    }
    start++;
    const char *end = strchr(start, '"');
    if (end == NULL || end - start != 64) {
        return false;
    }
    memcpy(uid, start, 64);
    uid[64] = '\0';
    return true;
}

static void load_seen_from_file(const char *path)
{
    FILE *f = fopen(path, "r");
    if (f == NULL) {
        return;
    }
    char line[MAX_EVENT_JSON];
    size_t loaded = 0;
    while (fgets(line, sizeof(line), f) != NULL) {
        char uid[65];
        line[strcspn(line, "\r\n")] = '\0';
        if (extract_event_uid(line, uid)) {
            seen_add(uid);
            loaded++;
        } else if (strlen(line) == 64) {
            seen_add(line);
            loaded++;
        }
        if (loaded > 0 && (loaded % 250) == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    fclose(f);
}

static void storage_init(void)
{
    g_seen_hashes = heap_caps_calloc(SEEN_HASH_CAPACITY, sizeof(uint64_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (g_seen_hashes == NULL) {
        g_seen_hashes = calloc(SEEN_HASH_CAPACITY, sizeof(uint64_t));
    }
    if (g_seen_hashes == NULL) {
        ESP_LOGE(TAG, "Could not allocate event UID cache");
    }
    esp_vfs_spiffs_conf_t conf = {
        .base_path = STORAGE_BASE,
        .partition_label = NULL,
        .max_files = 8,
        .format_if_mount_failed = true,
    };
    ESP_ERROR_CHECK(esp_vfs_spiffs_register(&conf));
    load_seen_from_file(PENDING_PATH);
    load_seen_from_file(BLOCKED_PATH);
    load_seen_from_file(ACKED_PATH);
    ESP_LOGI(TAG, "Storage ready; loaded %u known event UIDs", (unsigned)g_seen_count);
}

static char *event_to_json(const attendance_event_t *event, const char *capturetype)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "event_uid", event->event_uid);
    cJSON_AddStringToObject(root, "zone_id", ZONE_LITE_ZONE_ID);
    cJSON_AddStringToObject(root, "device_id", ZONE_LITE_ZONE_DEVICE_ID);
    cJSON_AddStringToObject(root, "device_serial", g_device_serial[0] ? g_device_serial : "unknown");
    cJSON_AddStringToObject(root, "user_id", event->user_id);
    cJSON_AddStringToObject(root, "employee_name", event->employee_name);
    cJSON_AddStringToObject(root, "cnic", event->cnic);
    cJSON_AddStringToObject(root, "timestamp", event->timestamp);
    cJSON_AddStringToObject(root, "clockdiff", "0.0");
    cJSON_AddStringToObject(root, "capturetype", capturetype);
    cJSON_AddStringToObject(root, "trust_status", "TRUSTED_LIVE");
    cJSON_AddStringToObject(root, "raw_punch", event->raw_punch ? "T" : "F");
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}

typedef enum {
    ENQUEUE_DUPLICATE = 0,
    ENQUEUE_PENDING,
    ENQUEUE_BLOCKED,
} enqueue_result_t;

static enqueue_result_t enqueue_event_to_files(
    const attendance_event_t *event,
    const char *capturetype,
    FILE *pending_file,
    FILE *blocked_file)
{
    if (seen_contains(event->event_uid)) {
        return ENQUEUE_DUPLICATE;
    }
    char *json = event_to_json(event, capturetype);
    if (json == NULL) {
        return ENQUEUE_DUPLICATE;
    }
    enqueue_result_t result = ENQUEUE_PENDING;
    if (event->cnic[0] == '\0') {
        append_line_to_open_file(blocked_file, BLOCKED_PATH, json);
        result = ENQUEUE_BLOCKED;
        if (strcmp(capturetype, "LIVE") == 0) {
            ESP_LOGW(TAG, "Blocked LIVE identity user_id=%s event_uid=%s", event->user_id, event->event_uid);
        }
    } else {
        append_line_to_open_file(pending_file, PENDING_PATH, json);
        if (strcmp(capturetype, "LIVE") == 0) {
            ESP_LOGI(TAG, "Queued LIVE event_uid=%s user_id=%s raw=%s", event->event_uid, event->user_id, event->raw_punch ? "T" : "F");
        }
    }
    seen_add(event->event_uid);
    free(json);
    return result;
}

static enqueue_result_t enqueue_event(const attendance_event_t *event, const char *capturetype)
{
    return enqueue_event_to_files(event, capturetype, NULL, NULL);
}

static bool build_attendance_event(
    attendance_event_t *out,
    const user_table_t *users,
    const char *user_id,
    uint16_t uid,
    uint32_t timestamp,
    uint8_t status,
    uint8_t punch)
{
    memset(out, 0, sizeof(*out));
    const zkt_user_t *user = find_user_by_user_id(users, user_id);
    if (user == NULL && uid != 0) {
        user = find_user_by_uid(users, uid);
    }
    if (user != NULL) {
        snprintf(out->user_id, sizeof(out->user_id), "%s", user->user_id);
        snprintf(out->employee_name, sizeof(out->employee_name), "%s", user->employee_name);
        snprintf(out->cnic, sizeof(out->cnic), "%s", user->cnic);
        out->raw_punch = user->raw_punch;
    } else {
        snprintf(out->user_id, sizeof(out->user_id), "%s", user_id);
        out->employee_name[0] = '\0';
        out->cnic[0] = '\0';
        out->raw_punch = false;
    }
    out->status = status;
    out->punch = punch;
    iso_from_zk_time(timestamp, out->timestamp);
    build_event_uid(out);
    return out->user_id[0] != '\0';
}

static bool zk_timestamp_in_month(uint32_t timestamp, int year, int month)
{
    struct tm decoded;
    decode_zk_time(timestamp, &decoded);
    return decoded.tm_year + 1900 == year && decoded.tm_mon + 1 == month;
}

static size_t reconcile_attendance_dump(
    int sock,
    zk_context_t *ctx,
    const user_table_t *users,
    int32_t records,
    const char *capturetype,
    int filter_year,
    int filter_month)
{
    uint8_t *data = NULL;
    size_t len = 0;
    if (records <= 0) {
        return 0;
    }
    int32_t refreshed_users = 0;
    int32_t refreshed_records = 0;
    if (zk_get_counts(sock, ctx, &refreshed_users, &refreshed_records) && refreshed_records > 0) {
        records = refreshed_records;
        ESP_LOGI(
            TAG,
            "Refreshed ZKT counts in sync session users=%ld records=%ld",
            (long)refreshed_users,
            (long)refreshed_records);
    }
    ESP_LOGI(TAG, "Requesting attendance dump records=%ld capturetype=%s", (long)records, capturetype);
    if (!zk_read_buffer(sock, ctx, CMD_ATTLOG_RRQ, 0, &data, &len) || len < 4) {
        ESP_LOGW(TAG, "Could not read attendance dump");
        free(data);
        return 0;
    }
    uint32_t total_size = read_le32(data);
    uint32_t record_size = records > 0 ? total_size / (uint32_t)records : 0;
    const uint8_t *p = data + 4;
    size_t remain = len - 4;
    size_t processed = 0;
    size_t added = 0;
    size_t pending = 0;
    size_t blocked = 0;
    size_t duplicates = 0;
    size_t filtered = 0;
    size_t skipped = 0;
    ESP_LOGI(
        TAG,
        "Reconciling %ld attendance records packet_size=%lu month_filter=%04d-%02d",
        (long)records,
        (unsigned long)record_size,
        filter_year,
        filter_month);
    FILE *pending_file = fopen(PENDING_PATH, "a");
    if (pending_file == NULL) {
        ESP_LOGW(TAG, "Could not keep %s open for reconcile appends", PENDING_PATH);
    }
    FILE *blocked_file = fopen(BLOCKED_PATH, "a");
    if (blocked_file == NULL) {
        ESP_LOGW(TAG, "Could not keep %s open for reconcile appends", BLOCKED_PATH);
    }
    while (remain >= record_size && record_size > 0) {
        char user_id[32] = "";
        uint16_t uid = 0;
        uint32_t timestamp = 0;
        uint8_t status = 0;
        uint8_t punch = 0;
        if (record_size == 8 && remain >= 8) {
            uid = read_le16(p);
            status = p[2];
            timestamp = read_le32(p + 3);
            punch = p[7];
            const zkt_user_t *user = find_user_by_uid(users, uid);
            snprintf(user_id, sizeof(user_id), "%s", user ? user->user_id : "");
        } else if (record_size == 16 && remain >= 16) {
            snprintf(user_id, sizeof(user_id), "%lu", (unsigned long)read_le32(p));
            timestamp = read_le32(p + 4);
            status = p[8];
            punch = p[9];
        } else if (remain >= 40) {
            uid = read_le16(p);
            copy_zk_string(user_id, sizeof(user_id), p + 2, 24);
            status = p[26];
            timestamp = read_le32(p + 27);
            punch = p[31];
        } else {
            break;
        }
        if (filter_year > 0 && filter_month > 0 && !zk_timestamp_in_month(timestamp, filter_year, filter_month)) {
            filtered++;
            p += record_size;
            remain -= record_size;
            processed++;
            if ((processed % 50) == 0) {
                vTaskDelay(pdMS_TO_TICKS(1));
            }
            continue;
        }
        attendance_event_t event;
        if (build_attendance_event(&event, users, user_id, uid, timestamp, status, punch)) {
            enqueue_result_t result = enqueue_event_to_files(&event, capturetype, pending_file, blocked_file);
            if (result == ENQUEUE_PENDING) {
                added++;
                pending++;
            } else if (result == ENQUEUE_BLOCKED) {
                added++;
                blocked++;
            } else {
                duplicates++;
            }
        } else {
            skipped++;
        }
        p += record_size;
        remain -= record_size;
        processed++;
        if ((processed % 50) == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    if (pending_file != NULL) {
        fclose(pending_file);
    }
    if (blocked_file != NULL) {
        fclose(blocked_file);
    }
    free(data);
    ESP_LOGI(
        TAG,
        "Reconcile %s processed=%u new=%u pending=%u blocked=%u duplicates=%u filtered=%u skipped=%u",
        capturetype,
        (unsigned)processed,
        (unsigned)added,
        (unsigned)pending,
        (unsigned)blocked,
        (unsigned)duplicates,
        (unsigned)filtered,
        (unsigned)skipped);
    return added;
}

static int http_post_json(const char *url, const char *json, char **response_body)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = ORDS_TIMEOUT_MS,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return -1;
    }
    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "X-API-Username", ZONE_LITE_ORDS_USERNAME);
    esp_http_client_set_header(client, "X-API-Password", ZONE_LITE_ORDS_PASSWORD);
    esp_http_client_set_post_field(client, json, strlen(json));
    esp_err_t err = esp_http_client_perform(client);
    int status = err == ESP_OK ? esp_http_client_get_status_code(client) : -1;
    if (response_body != NULL) {
        int len = esp_http_client_get_content_length(client);
        if (len > 0 && len < 8192) {
            *response_body = calloc(1, len + 1);
            if (*response_body != NULL) {
                (void)esp_http_client_read_response(client, *response_body, len);
            }
        }
    }
    esp_http_client_cleanup(client);
    return status;
}

static bool oracle_success_body(const char *body)
{
    if (body == NULL || body[0] == '\0') {
        return true;
    }
    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return true;
    }
    cJSON *success = cJSON_GetObjectItemCaseSensitive(root, "success");
    bool ok = !cJSON_IsBool(success) || cJSON_IsTrue(success);
    cJSON_Delete(root);
    return ok;
}

static bool oracle_send_live(const char *event_json)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/raw-captures", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, event_json, &body);
    bool ok = status == 409 || ((status == 200 || status == 201) && oracle_success_body(body));
    ESP_LOGI(TAG, "ORDS live status=%d ok=%s", status, ok ? "true" : "false");
    free(body);
    return ok;
}

static bool oracle_send_bulk(char **events, size_t count)
{
    if (count == 0) {
        return true;
    }
    cJSON *root = cJSON_CreateObject();
    char batch_uid[64];
    snprintf(batch_uid, sizeof(batch_uid), "ZONE-ORDS-%lld", (long long)(esp_timer_get_time() / 1000));
    cJSON_AddStringToObject(root, "batch_uid", batch_uid);
    cJSON *array = cJSON_AddArrayToObject(root, "events");
    for (size_t i = 0; i < count; i++) {
        cJSON *event = cJSON_Parse(events[i]);
        if (event != NULL) {
            cJSON_AddItemToArray(array, event);
        }
    }
    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        return false;
    }
    char url[256];
    snprintf(url, sizeof(url), "%s/raw-captures/bulk", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, payload, &body);
    bool ok = status == 409 || ((status == 200 || status == 201) && oracle_success_body(body));
    ESP_LOGI(TAG, "ORDS bulk count=%u status=%d ok=%s", (unsigned)count, status, ok ? "true" : "false");
    free(body);
    free(payload);
    return ok;
}

static void append_acked_uid_from_json(const char *event_json)
{
    cJSON *root = cJSON_Parse(event_json);
    if (root == NULL) {
        return;
    }
    cJSON *uid = cJSON_GetObjectItemCaseSensitive(root, "event_uid");
    if (cJSON_IsString(uid)) {
        append_line(ACKED_PATH, uid->valuestring);
        seen_add(uid->valuestring);
    }
    cJSON_Delete(root);
}

static void oracle_drain_pending(bool live_first)
{
    FILE *in = fopen(PENDING_PATH, "r");
    if (in == NULL) {
        return;
    }
    FILE *out = fopen(PENDING_TMP_PATH, "w");
    if (out == NULL) {
        fclose(in);
        return;
    }

    char **bulk = calloc(ORDS_BULK_CHUNK_SIZE, sizeof(char *));
    if (bulk == NULL) {
        fclose(in);
        fclose(out);
        return;
    }
    size_t bulk_count = 0;
    char line[MAX_EVENT_JSON];
    bool failed = false;

    while (fgets(line, sizeof(line), in) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0') {
            continue;
        }
        if (live_first && bulk_count == 0) {
            if (oracle_send_live(line)) {
                append_acked_uid_from_json(line);
                live_first = false;
                continue;
            }
            failed = true;
            fprintf(out, "%s\n", line);
            live_first = false;
            continue;
        }
        bulk[bulk_count] = strdup(line);
        if (bulk[bulk_count] == NULL) {
            failed = true;
            fprintf(out, "%s\n", line);
            continue;
        }
        bulk_count++;
        if (bulk_count == ORDS_BULK_CHUNK_SIZE) {
            if (oracle_send_bulk(bulk, bulk_count)) {
                for (size_t i = 0; i < bulk_count; i++) {
                    append_acked_uid_from_json(bulk[i]);
                    free(bulk[i]);
                    bulk[i] = NULL;
                }
            } else {
                failed = true;
                for (size_t i = 0; i < bulk_count; i++) {
                    fprintf(out, "%s\n", bulk[i]);
                    free(bulk[i]);
                    bulk[i] = NULL;
                }
            }
            bulk_count = 0;
        }
        if (failed) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    if (!failed && bulk_count > 0) {
        if (oracle_send_bulk(bulk, bulk_count)) {
            for (size_t i = 0; i < bulk_count; i++) {
                append_acked_uid_from_json(bulk[i]);
            }
        } else {
            failed = true;
            for (size_t i = 0; i < bulk_count; i++) {
                fprintf(out, "%s\n", bulk[i]);
            }
        }
    }
    for (size_t i = 0; i < bulk_count; i++) {
        free(bulk[i]);
    }
    if (failed) {
        while (fgets(line, sizeof(line), in) != NULL) {
            fputs(line, out);
        }
    }
    fclose(in);
    fclose(out);
    if (remove(PENDING_PATH) != 0 && errno != ENOENT) {
        ESP_LOGW(TAG, "Could not remove old pending outbox before rewrite errno=%d", errno);
    }
    if (rename(PENDING_TMP_PATH, PENDING_PATH) != 0) {
        ESP_LOGW(TAG, "Could not rewrite pending outbox errno=%d", errno);
    }
    free(bulk);
}

static bool probe_zkt_device(uint32_t host_order_ip, uint32_t *selected_ip)
{
    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_PORT, ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS, &sock)) {
        return false;
    }

    char ip_text[16];
    struct in_addr addr = {.s_addr = htonl(host_order_ip)};
    inet_ntoa_r(addr, ip_text, sizeof(ip_text));

    bool ok = false;
    zk_context_t ctx = {0};
    if (!zk_connect_and_auth(sock, &ctx)) {
        ESP_LOGW(TAG, "%s:%d answered TCP but failed ZKT auth", ip_text, ZONE_LITE_ZKT_PORT);
        goto done;
    }
    char serial[80] = {0};
    char device_name[80] = {0};
    char platform[80] = {0};
    char device_time[32] = {0};
    int32_t users = -1;
    int32_t records = -1;
    (void)zk_read_option(sock, &ctx, "~SerialNumber", serial, sizeof(serial));
    (void)zk_read_option(sock, &ctx, "~DeviceName", device_name, sizeof(device_name));
    (void)zk_read_option(sock, &ctx, "~Platform", platform, sizeof(platform));
    (void)zk_get_time(sock, &ctx, device_time, sizeof(device_time));
    (void)zk_get_counts(sock, &ctx, &users, &records);
    snprintf(g_device_serial, sizeof(g_device_serial), "%s", serial[0] ? serial : "unknown");
    ESP_LOGI(
        TAG,
        "Selected ZKT device %s:%d serial=%s name=%s platform=%s time=%s users=%ld records=%ld device_id=%s",
        ip_text,
        ZONE_LITE_ZKT_PORT,
        g_device_serial,
        device_name[0] ? device_name : "unknown",
        platform[0] ? platform : "unknown",
        device_time[0] ? device_time : "unknown",
        (long)users,
        (long)records,
        ZONE_LITE_ZONE_DEVICE_ID);
    *selected_ip = host_order_ip;
    ok = true;

done:
    if (ok) {
        zk_disconnect(sock, &ctx);
    }
    close(sock);
    return ok;
}

static bool discover_zkt(uint32_t *selected_ip)
{
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip_info;
    if (netif == NULL || esp_netif_get_ip_info(netif, &ip_info) != ESP_OK || ip_info.ip.addr == 0) {
        return false;
    }
    uint32_t own_ip = ntohl(ip_info.ip.addr);
    uint32_t netmask = ntohl(ip_info.netmask.addr);
    uint32_t network = own_ip & netmask;
    uint32_t broadcast = network | ~netmask;
    uint32_t host_count = broadcast > network ? broadcast - network - 1 : 0;
    if (host_count == 0 || host_count > 254) {
        network = own_ip & 0xffffff00U;
        broadcast = network | 0xffU;
        host_count = 254;
    }
    struct in_addr preferred_addr;
    if (inet_aton(ZONE_LITE_ZKT_PREFERRED_IP, &preferred_addr) != 0) {
        uint32_t preferred = ntohl(preferred_addr.s_addr);
        if (preferred != 0 && preferred != own_ip && preferred != ntohl(ip_info.gw.addr)) {
            ESP_LOGI(TAG, "Trying preferred ZKT IP %s:%d", ZONE_LITE_ZKT_PREFERRED_IP, ZONE_LITE_ZKT_PORT);
            if (probe_zkt_device(preferred, selected_ip)) {
                return true;
            }
        }
    }
    ESP_LOGI(TAG, "Scanning %lu hosts for ZKT TCP port %d", (unsigned long)host_count, ZONE_LITE_ZKT_PORT);
    for (uint32_t candidate = network + 1; candidate < broadcast; candidate++) {
        if (candidate == own_ip || candidate == ntohl(ip_info.gw.addr)) {
            continue;
        }
        if (probe_zkt_device(candidate, selected_ip)) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return false;
}

static bool process_live_packet(const uint8_t *data, size_t len, const user_table_t *users)
{
    bool any = false;
    while (len >= 12) {
        char user_id[32] = "";
        uint8_t status = 0;
        uint8_t punch = 0;
        uint32_t timestamp = 0;
        if (len == 12) {
            snprintf(user_id, sizeof(user_id), "%lu", (unsigned long)read_le32(data));
            status = data[4];
            punch = data[5];
            timestamp = (uint32_t)data[6] * 12 * 31 * 24 * 60 * 60;
            timestamp += ((uint32_t)data[7] - 1) * 31 * 24 * 60 * 60;
            timestamp += ((uint32_t)data[8] - 1) * 24 * 60 * 60;
            timestamp += (uint32_t)data[9] * 60 * 60 + (uint32_t)data[10] * 60 + data[11];
            data += 12;
            len -= 12;
        } else {
            size_t packet_len = len >= 52 ? 52 : (len >= 36 ? 36 : 32);
            copy_zk_string(user_id, sizeof(user_id), data, 24);
            status = data[24];
            punch = data[25];
            timestamp = (uint32_t)data[26] * 12 * 31 * 24 * 60 * 60;
            timestamp += ((uint32_t)data[27] - 1) * 31 * 24 * 60 * 60;
            timestamp += ((uint32_t)data[28] - 1) * 24 * 60 * 60;
            timestamp += (uint32_t)data[29] * 60 * 60 + (uint32_t)data[30] * 60 + data[31];
            data += packet_len;
            len -= packet_len;
        }
        attendance_event_t event;
        if (build_attendance_event(&event, users, user_id, 0, timestamp, status, punch)) {
            enqueue_event(&event, "LIVE");
            any = true;
        }
    }
    return any;
}

static void gateway_run(uint32_t host_order_ip)
{
    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_PORT, 3000, &sock)) {
        return;
    }
    zk_context_t ctx = {0};
    if (!zk_connect_and_auth(sock, &ctx)) {
        close(sock);
        return;
    }
    (void)zk_read_option(sock, &ctx, "~SerialNumber", g_device_serial, sizeof(g_device_serial));
    int32_t user_count = 0;
    int32_t records = 0;
    struct tm device_now;
    (void)zk_get_counts(sock, &ctx, &user_count, &records);
    user_table_t *users = calloc(1, sizeof(user_table_t));
    if (users == NULL) {
        ESP_LOGE(TAG, "Could not allocate ZKT user table");
        zk_disconnect(sock, &ctx);
        close(sock);
        return;
    }
    (void)zk_load_users(sock, &ctx, users, user_count);
    if (zk_get_time_parts(sock, &ctx, &device_now)) {
        reconcile_attendance_dump(
            sock,
            &ctx,
            users,
            records,
            "DUMP_STARTUP",
            device_now.tm_year + 1900,
            device_now.tm_mon + 1);
    } else {
        ESP_LOGW(TAG, "Skipping startup dump because ZKT device time could not be read");
    }
    oracle_drain_pending(false);

    uint8_t rx[1024];
    zk_response_t response = {0};
    (void)zk_send_command(sock, &ctx, CMD_CANCELCAPTURE, NULL, 0, rx, sizeof(rx), &response);
    (void)zk_send_command(sock, &ctx, CMD_STARTVERIFY, NULL, 0, rx, sizeof(rx), &response);
    uint8_t flags[4];
    write_le32(flags, EF_ATTLOG);
    (void)zk_send_command(sock, &ctx, CMD_REG_EVENT, flags, sizeof(flags), rx, sizeof(rx), &response);

    int64_t last_reconcile = esp_timer_get_time() / 1000;
    while (true) {
        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(sock, &read_fds);
        struct timeval tv = {.tv_sec = 5, .tv_usec = 0};
        int rc = select(sock + 1, &read_fds, NULL, NULL, &tv);
        if (rc > 0 && FD_ISSET(sock, &read_fds)) {
            zk_tcp_header_t top;
            if (!recv_exact(sock, (uint8_t *)&top, sizeof(top)) || top.length < sizeof(zk_header_t) || top.length > 2048) {
                break;
            }
            uint8_t *packet = malloc(top.length);
            if (packet == NULL || !recv_exact(sock, packet, top.length)) {
                free(packet);
                break;
            }
            zk_header_t *header = (zk_header_t *)packet;
            if (header->command == CMD_REG_EVENT && top.length > sizeof(zk_header_t)) {
                process_live_packet(packet + sizeof(zk_header_t), top.length - sizeof(zk_header_t), users);
                uint8_t ack_rx[64];
                zk_response_t ack_resp = {0};
                (void)zk_send_command(sock, &ctx, CMD_ACK_OK, NULL, 0, ack_rx, sizeof(ack_rx), &ack_resp);
                oracle_drain_pending(true);
            }
            free(packet);
        } else if (rc < 0) {
            break;
        }
        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - last_reconcile >= ZONE_LITE_RECONCILE_INTERVAL_MS) {
            last_reconcile = now_ms;
            int32_t refreshed_users = 0;
            int32_t refreshed_records = 0;
            (void)zk_get_counts(sock, &ctx, &refreshed_users, &refreshed_records);
            if (refreshed_users != user_count) {
                user_count = refreshed_users;
                (void)zk_load_users(sock, &ctx, users, user_count);
            }
            if (zk_get_time_parts(sock, &ctx, &device_now)) {
                reconcile_attendance_dump(
                    sock,
                    &ctx,
                    users,
                    refreshed_records,
                    "LIVE_POLL",
                    device_now.tm_year + 1900,
                    device_now.tm_mon + 1);
            } else {
                ESP_LOGW(TAG, "Skipping reconcile dump because ZKT device time could not be read");
            }
            oracle_drain_pending(false);
        }
    }
    uint8_t zero[4] = {0};
    (void)zk_send_command(sock, &ctx, CMD_REG_EVENT, zero, sizeof(zero), rx, sizeof(rx), &response);
    zk_disconnect(sock, &ctx);
    close(sock);
    free(users);
}

static void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_data;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (wifi_retry_count < WIFI_MAXIMUM_RETRY) {
            esp_wifi_connect();
            wifi_retry_count++;
            ESP_LOGW(TAG, "Retrying Wi-Fi connection (%d)", wifi_retry_count);
        } else {
            xEventGroupSetBits(wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Wi-Fi connected with IP " IPSTR, IP2STR(&event->ip_info.ip));
        wifi_retry_count = 0;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, &instance_got_ip));
    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, ZONE_LITE_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, ZONE_LITE_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "Connecting to Wi-Fi SSID %s", ZONE_LITE_WIFI_SSID);
}

static bool wait_for_wifi(void)
{
    EventBits_t bits = xEventGroupWaitBits(
        wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE,
        pdFALSE,
        portMAX_DELAY);
    if ((bits & WIFI_CONNECTED_BIT) != 0) {
        return true;
    }
    ESP_LOGE(TAG, "Could not connect to Wi-Fi SSID %s", ZONE_LITE_WIFI_SSID);
    return false;
}

static void gateway_task(void *arg)
{
    (void)arg;
    while (true) {
        uint32_t selected_ip = 0;
        if (discover_zkt(&selected_ip)) {
            gateway_run(selected_ip);
            ESP_LOGW(TAG, "ZKT session ended; rediscovering");
        } else {
            ESP_LOGW(TAG, "No authenticated ZKT device found on port %d", ZONE_LITE_ZKT_PORT);
        }
        vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_DISCOVERY_RETRY_DELAY_MS));
    }
}

void app_main(void)
{
    setenv("TZ", "UTC0", 1);
    tzset();
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "Zone Lite starting zone=%s device_id=%s", ZONE_LITE_ZONE_ID, ZONE_LITE_ZONE_DEVICE_ID);
    storage_init();
    wifi_init_sta();
    if (!wait_for_wifi()) {
        return;
    }
    xTaskCreate(gateway_task, "zone_gateway", 16384, NULL, 5, NULL);
}
