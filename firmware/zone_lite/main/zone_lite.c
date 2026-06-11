#include <errno.h>
#include <ctype.h>
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
#include "esp_err.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_spiffs.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "mbedtls/sha256.h"
#include "nvs_flash.h"

#if __has_include("zone_lite_config.h")
#include "zone_lite_config.h"
#else
#include "zone_lite_config.example.h"
#endif

#include "led_status.h"

#ifndef ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED
#define ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED 0
#endif
#ifndef ZONE_LITE_ZKT_RECOVERY_FAILURES
#define ZONE_LITE_ZKT_RECOVERY_FAILURES 2
#endif
#ifndef ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS
#define ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS (30 * 60 * 1000)
#endif
#ifndef ZONE_LITE_ZKT_REBOOT_WAIT_MS
#define ZONE_LITE_ZKT_REBOOT_WAIT_MS 90000
#endif
#ifndef ZONE_LITE_ZKT_TELNET_PORT
#define ZONE_LITE_ZKT_TELNET_PORT 23
#endif
#ifndef ZONE_LITE_ZKT_TELNET_USERNAME
#define ZONE_LITE_ZKT_TELNET_USERNAME "root"
#endif
#ifndef ZONE_LITE_ZKT_TELNET_PASSWORD
#define ZONE_LITE_ZKT_TELNET_PASSWORD ""
#endif
#ifndef ZONE_LITE_ZKT_TELNET_EXPECT_BANNER
#define ZONE_LITE_ZKT_TELNET_EXPECT_BANNER "Linux"
#endif
#ifndef ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND
#define ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND "reboot"
#endif
#ifndef ZONE_LITE_SNTP_SERVER
#define ZONE_LITE_SNTP_SERVER "pool.ntp.org"
#endif
#ifndef ZONE_LITE_SNTP_SYNC_TIMEOUT_MS
#define ZONE_LITE_SNTP_SYNC_TIMEOUT_MS 15000
#endif
#ifndef ZONE_LITE_MIN_VALID_UNIX_TIME
#define ZONE_LITE_MIN_VALID_UNIX_TIME 1767225600
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
#define PENDING_BACKUP_PATH STORAGE_BASE "/pending.bak"
#define BLOCKED_PATH STORAGE_BASE "/blocked_identity.jsonl"
#define ACKED_PATH STORAGE_BASE "/acked_uids.txt"
#define MAX_USERS 512
#define SEEN_HASH_CAPACITY 262144
#define MAX_EVENT_JSON 1024
#ifndef ZONE_LITE_ORDS_BULK_CHUNK_SIZE
#define ZONE_LITE_ORDS_BULK_CHUNK_SIZE 100
#endif
#ifndef ZONE_LITE_ORDS_TIMEOUT_MS
#define ZONE_LITE_ORDS_TIMEOUT_MS 15000
#endif
#ifndef ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS 60000
#endif
#ifndef ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS
#define ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS (10 * 60 * 1000)
#endif
#ifndef ZONE_LITE_ORDS_RECONCILE_ENABLED
#define ZONE_LITE_ORDS_RECONCILE_ENABLED 1
#endif
#ifndef ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS
#define ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS 5000
#endif
#ifndef ZONE_LITE_ZKT_USER_REFRESH_RETRIES
#define ZONE_LITE_ZKT_USER_REFRESH_RETRIES 3
#endif
#ifndef ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS
#define ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS 2000
#endif
#define ZKT_IO_TIMEOUT_SEC 8
#define ZKT_KEEPALIVE_IDLE_SEC 60
#define ZKT_KEEPALIVE_INTERVAL_SEC 10
#define ZKT_KEEPALIVE_COUNT 3
#define ZKT_LIVE_REREGISTER_INTERVAL_MS (10 * 60 * 1000)
#define ZKT_DISCOVERY_RESTART_AFTER_FAILURES 5
#define ZKT_TELNET_BUFFER_SIZE 768
#define ZKT_TELNET_IO_TIMEOUT_MS 5000

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
static uint32_t g_last_authenticated_zkt_ip;
static uint32_t g_last_zkt_tcp_candidate_ip;
static bool g_sntp_started;
static bool g_time_synced;
static int64_t g_ords_next_attempt_ms;
static uint32_t g_ords_failure_backoff_ms = ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS;
static bool g_truth_reconcile_warning;

static bool oracle_send_reconcile(char **events, size_t count, int year, int month);

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

static void configure_zkt_socket(int sock)
{
    struct timeval io_timeout = {.tv_sec = ZKT_IO_TIMEOUT_SEC, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &io_timeout, sizeof(io_timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &io_timeout, sizeof(io_timeout));

    int keepalive = 1;
    setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive));
#ifdef TCP_KEEPIDLE
    int keepidle = ZKT_KEEPALIVE_IDLE_SEC;
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &keepidle, sizeof(keepidle));
#endif
#ifdef TCP_KEEPINTVL
    int keepintvl = ZKT_KEEPALIVE_INTERVAL_SEC;
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &keepintvl, sizeof(keepintvl));
#endif
#ifdef TCP_KEEPCNT
    int keepcnt = ZKT_KEEPALIVE_COUNT;
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &keepcnt, sizeof(keepcnt));
#endif
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

static bool zk_send_ack_only(int sock, uint16_t session_id)
{
    uint8_t tx[sizeof(zk_tcp_header_t) + sizeof(zk_header_t)];
    uint16_t reply_id = USHRT_MAX_ZK - 1;
    uint16_t next_reply = (uint16_t)(reply_id + 1);
    if (next_reply >= USHRT_MAX_ZK) {
        next_reply = (uint16_t)(next_reply - USHRT_MAX_ZK);
    }

    zk_header_t header = {
        .command = CMD_ACK_OK,
        .checksum = 0,
        .session_id = session_id,
        .reply_id = reply_id,
    };
    uint8_t *packet = tx + sizeof(zk_tcp_header_t);
    memcpy(packet, &header, sizeof(header));
    ((zk_header_t *)packet)->checksum = zk_checksum(packet, sizeof(zk_header_t));
    ((zk_header_t *)packet)->reply_id = next_reply;

    zk_tcp_header_t tcp_header = {
        .marker_1 = MACHINE_PREPARE_DATA_1,
        .marker_2 = MACHINE_PREPARE_DATA_2,
        .length = sizeof(zk_header_t),
    };
    memcpy(tx, &tcp_header, sizeof(tcp_header));
    return send_all(sock, tx, sizeof(tx));
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
    configure_zkt_socket(sock);
    *out_sock = sock;
    return true;
}

static void ip_to_text(uint32_t host_order_ip, char *out, size_t out_len)
{
    struct in_addr addr = {.s_addr = htonl(host_order_ip)};
    inet_ntoa_r(addr, out, out_len);
}

static bool text_contains_ci(const char *haystack, const char *needle)
{
    if (needle == NULL || needle[0] == '\0') {
        return true;
    }
    if (haystack == NULL) {
        return false;
    }
    size_t needle_len = strlen(needle);
    for (const char *p = haystack; *p != '\0'; p++) {
        size_t i = 0;
        while (i < needle_len && p[i] != '\0' &&
               tolower((unsigned char)p[i]) == tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == needle_len) {
            return true;
        }
    }
    return false;
}

static size_t telnet_read_text(int sock, char *out, size_t out_len, int timeout_ms)
{
    if (out_len == 0) {
        return 0;
    }
    size_t written = 0;
    out[0] = '\0';
    int64_t deadline_ms = (esp_timer_get_time() / 1000) + timeout_ms;
    while (written + 1 < out_len) {
        int64_t remaining_ms = deadline_ms - (esp_timer_get_time() / 1000);
        if (remaining_ms <= 0) {
            break;
        }
        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(sock, &read_fds);
        struct timeval tv = {
            .tv_sec = (int)(remaining_ms / 1000),
            .tv_usec = (int)((remaining_ms % 1000) * 1000),
        };
        int rc = select(sock + 1, &read_fds, NULL, NULL, &tv);
        if (rc <= 0) {
            break;
        }
        uint8_t rx[128];
        int got = recv(sock, rx, sizeof(rx), 0);
        if (got <= 0) {
            break;
        }
        for (int i = 0; i < got && written + 1 < out_len; i++) {
            if (rx[i] == 255) {
                i += 2;
                continue;
            }
            if (rx[i] != '\0') {
                out[written++] = (char)rx[i];
            }
        }
        out[written] = '\0';
    }
    return written;
}

static bool telnet_send_line(int sock, const char *value)
{
    char line[160];
    int len = snprintf(line, sizeof(line), "%s\r\n", value);
    if (len <= 0 || len >= (int)sizeof(line)) {
        return false;
    }
    return send_all(sock, (const uint8_t *)line, (size_t)len);
}

static bool zkt_telnet_reboot(uint32_t host_order_ip)
{
    char ip_text[16];
    ip_to_text(host_order_ip, ip_text, sizeof(ip_text));
    ESP_LOGW(
        TAG,
        "Attempting ZKT telnet OS recovery reboot on %s:%d",
        ip_text,
        ZONE_LITE_ZKT_TELNET_PORT);

    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_TELNET_PORT, ZKT_TELNET_IO_TIMEOUT_MS, &sock)) {
        ESP_LOGW(TAG, "Could not connect to ZKT telnet recovery target %s:%d", ip_text, ZONE_LITE_ZKT_TELNET_PORT);
        return false;
    }

    char text[ZKT_TELNET_BUFFER_SIZE];
    bool ok = false;
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (!text_contains_ci(text, "login:")) {
        ESP_LOGW(TAG, "ZKT telnet recovery target %s did not show a login prompt", ip_text);
        goto done;
    }
    if (ZONE_LITE_ZKT_TELNET_EXPECT_BANNER[0] != '\0' &&
        !text_contains_ci(text, ZONE_LITE_ZKT_TELNET_EXPECT_BANNER)) {
        ESP_LOGW(TAG, "ZKT telnet recovery target %s did not match the expected banner", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_USERNAME)) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (!text_contains_ci(text, "password:")) {
        ESP_LOGW(TAG, "ZKT telnet recovery target %s did not ask for a password", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_PASSWORD)) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), 2000);
    if (text_contains_ci(text, "login incorrect")) {
        ESP_LOGW(TAG, "ZKT telnet recovery login failed for %s", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, "id")) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (text_contains_ci(text, "login incorrect") || !text_contains_ci(text, "uid=")) {
        ESP_LOGW(TAG, "ZKT telnet recovery could not confirm a shell on %s", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, "sync")) {
        goto done;
    }
    vTaskDelay(pdMS_TO_TICKS(300));
    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND)) {
        goto done;
    }
    ESP_LOGW(TAG, "ZKT telnet recovery reboot command sent to %s", ip_text);
    ok = true;

done:
    close(sock);
    return ok;
}

static uint32_t configured_preferred_zkt_ip(void)
{
    struct in_addr preferred_addr;
    if (inet_aton(ZONE_LITE_ZKT_PREFERRED_IP, &preferred_addr) == 0) {
        return 0;
    }
    return ntohl(preferred_addr.s_addr);
}

static uint32_t zkt_recovery_target_ip(void)
{
    if (g_last_authenticated_zkt_ip != 0 && g_last_zkt_tcp_candidate_ip != 0) {
        return g_last_zkt_tcp_candidate_ip;
    }
    uint32_t preferred = configured_preferred_zkt_ip();
    if (preferred != 0) {
        return preferred;
    }
    return g_last_authenticated_zkt_ip;
}

static bool maybe_reboot_zkt_for_recovery(uint32_t discovery_failures, int64_t *last_reboot_ms)
{
    if (!ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED ||
        discovery_failures < ZONE_LITE_ZKT_RECOVERY_FAILURES) {
        return false;
    }

    int64_t now_ms = esp_timer_get_time() / 1000;
    if (*last_reboot_ms > 0 && now_ms - *last_reboot_ms < ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS) {
        ESP_LOGW(TAG, "Skipping ZKT telnet recovery because reboot cooldown is active");
        return false;
    }

    uint32_t target_ip = zkt_recovery_target_ip();
    if (target_ip == 0) {
        ESP_LOGW(TAG, "Skipping ZKT telnet recovery because no recovery target is known");
        return false;
    }

    // When the ZKT application service is stuck, port 4370 can accept TCP but
    // never answer protocol commands. Recovery must use the OS telnet service.
    led_status_set(LED_STATUS_RECOVERY_REBOOT);
    if (!zkt_telnet_reboot(target_ip)) {
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        return false;
    }

    *last_reboot_ms = esp_timer_get_time() / 1000;
    ESP_LOGW(TAG, "Waiting %d ms for ZKT device to reboot", ZONE_LITE_ZKT_REBOOT_WAIT_MS);
    vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_ZKT_REBOOT_WAIT_MS));
    led_status_set(LED_STATUS_ZKT_DISCOVERING);
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

static uint32_t choose_zk_record_size(
    uint32_t total_size,
    uint32_t reported_count,
    const uint32_t *preferred_sizes,
    size_t preferred_count)
{
    if (reported_count > 0 && total_size % reported_count == 0) {
        uint32_t candidate = total_size / reported_count;
        for (size_t i = 0; i < preferred_count; i++) {
            if (candidate == preferred_sizes[i]) {
                return candidate;
            }
        }
    }
    for (size_t i = 0; i < preferred_count; i++) {
        if (preferred_sizes[i] > 0 && total_size % preferred_sizes[i] == 0) {
            return preferred_sizes[i];
        }
    }
    return 0;
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
    static const uint32_t user_record_sizes[] = {72, 28};
    uint32_t packet_size = choose_zk_record_size(
        total_size,
        (uint32_t)user_count,
        user_record_sizes,
        sizeof(user_record_sizes) / sizeof(user_record_sizes[0]));
    if (packet_size == 0) {
        ESP_LOGW(TAG, "Unsupported ZKT user table size total=%lu users=%ld", (unsigned long)total_size, (long)user_count);
        free(data);
        return false;
    }
    uint32_t parsed_users = total_size / packet_size;
    if (parsed_users != (uint32_t)user_count) {
        ESP_LOGW(
            TAG,
            "ZKT user count changed during read reported=%ld parsed=%lu packet_size=%lu",
            (long)user_count,
            (unsigned long)parsed_users,
            (unsigned long)packet_size);
        user_count = (int32_t)parsed_users;
    }
    const uint8_t *p = data + 4;
    size_t remain = len - 4;
    ESP_LOGI(TAG, "Reading %ld ZKT users packet_size=%lu", (long)user_count, (unsigned long)packet_size);
    while (users->count < MAX_USERS && remain >= packet_size) {
        if (packet_size == 28 && remain >= 28) {
            zkt_user_t *u = &users->rows[users->count++];
            snprintf(u->uid, sizeof(u->uid), "%u", read_le16(p));
            copy_zk_string(u->name, sizeof(u->name), p + 8, 8);
            snprintf(u->user_id, sizeof(u->user_id), "%lu", (unsigned long)read_le32(p + 24));
            parse_machine_identity(u);
            p += 28;
            remain -= 28;
        } else if (packet_size == 72 && remain >= 72) {
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

static bool zk_refresh_users_preserving_current(int sock, zk_context_t *ctx, user_table_t *users, int32_t user_count)
{
    user_table_t *updated = calloc(1, sizeof(user_table_t));
    if (updated == NULL) {
        ESP_LOGW(TAG, "Could not allocate temporary ZKT user table for refresh");
        return false;
    }
    bool ok = zk_load_users(sock, ctx, updated, user_count);
    if (ok) {
        memcpy(users, updated, sizeof(*users));
    }
    free(updated);
    return ok;
}

static bool zk_refresh_users_after_count_change(
    int sock,
    zk_context_t *ctx,
    user_table_t *users,
    int32_t old_count,
    int32_t new_count)
{
    ESP_LOGI(TAG, "ZKT user count changed %ld -> %ld; refreshing user cache", (long)old_count, (long)new_count);
    led_status_set(LED_STATUS_SYNCING);
    for (int attempt = 1; attempt <= ZONE_LITE_ZKT_USER_REFRESH_RETRIES; attempt++) {
        if (zk_refresh_users_preserving_current(sock, ctx, users, new_count)) {
            return true;
        }
        ESP_LOGW(
            TAG,
            "ZKT user refresh attempt %d/%d failed after count change",
            attempt,
            ZONE_LITE_ZKT_USER_REFRESH_RETRIES);
        if (attempt < ZONE_LITE_ZKT_USER_REFRESH_RETRIES) {
            vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS));
        }
    }
    return false;
}

static const zkt_user_t *find_user_by_user_id(const user_table_t *users, const char *user_id)
{
    for (size_t i = 0; i < users->count; i++) {
        if (strcmp(users->rows[i].user_id, user_id) == 0) {
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

static bool file_has_nonempty_line(const char *path)
{
    FILE *f = fopen(path, "r");
    if (f == NULL) {
        return false;
    }
    char line[MAX_EVENT_JSON];
    bool has_line = false;
    while (fgets(line, sizeof(line), f) != NULL) {
        if (line[strspn(line, "\r\n")] != '\0') {
            has_line = true;
            break;
        }
    }
    fclose(f);
    return has_line;
}

static void restore_pending_backup_if_needed(void)
{
    struct stat pending_stat;
    struct stat backup_stat;
    bool pending_exists = stat(PENDING_PATH, &pending_stat) == 0;
    bool backup_exists = stat(PENDING_BACKUP_PATH, &backup_stat) == 0;
    if (!backup_exists) {
        return;
    }
    if (!pending_exists) {
        if (rename(PENDING_BACKUP_PATH, PENDING_PATH) == 0) {
            ESP_LOGW(TAG, "Restored pending outbox from backup after interrupted rewrite");
        } else {
            ESP_LOGE(TAG, "Could not restore pending outbox backup errno=%d", errno);
        }
        return;
    }
    if (remove(PENDING_BACKUP_PATH) != 0 && errno != ENOENT) {
        ESP_LOGW(TAG, "Could not remove stale pending outbox backup errno=%d", errno);
    }
}

static void storage_init(void)
{
    g_seen_hashes = heap_caps_calloc(SEEN_HASH_CAPACITY, sizeof(uint64_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (g_seen_hashes == NULL) {
        g_seen_hashes = calloc(SEEN_HASH_CAPACITY, sizeof(uint64_t));
    }
    if (g_seen_hashes == NULL) {
        ESP_LOGE(TAG, "Could not allocate event UID cache");
        led_status_fault(LED_STATUS_FATAL);
    }
    esp_vfs_spiffs_conf_t conf = {
        .base_path = STORAGE_BASE,
        .partition_label = NULL,
        .max_files = 8,
        .format_if_mount_failed = true,
    };
    esp_err_t spiffs_ret = esp_vfs_spiffs_register(&conf);
    if (spiffs_ret != ESP_OK) {
        ESP_LOGE(TAG, "Could not mount SPIFFS storage: %s", esp_err_to_name(spiffs_ret));
        led_status_fault(LED_STATUS_FATAL);
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP_ERROR_CHECK(spiffs_ret);
    }
    restore_pending_backup_if_needed();
    load_seen_from_file(PENDING_PATH);
    load_seen_from_file(BLOCKED_PATH);
    load_seen_from_file(ACKED_PATH);
    led_status_set_backlog(file_has_nonempty_line(PENDING_PATH));
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
        led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        result = ENQUEUE_BLOCKED;
        if (strcmp(capturetype, "LIVE") == 0) {
            ESP_LOGW(TAG, "Blocked LIVE identity user_id=%s event_uid=%s", event->user_id, event->event_uid);
        }
    } else {
        append_line_to_open_file(pending_file, PENDING_PATH, json);
        led_status_set_backlog(true);
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
    const zkt_user_t *user = NULL;
    if (user_id != NULL && user_id[0] != '\0') {
        user = find_user_by_user_id(users, user_id);
    } else if (uid != 0) {
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
    static const uint32_t attendance_record_sizes[] = {40, 16, 8};
    uint32_t record_size = choose_zk_record_size(
        total_size,
        (uint32_t)records,
        attendance_record_sizes,
        sizeof(attendance_record_sizes) / sizeof(attendance_record_sizes[0]));
    if (record_size == 0) {
        ESP_LOGW(TAG, "Unsupported ZKT attendance table size total=%lu records=%ld", (unsigned long)total_size, (long)records);
        free(data);
        return 0;
    }
    uint32_t parsed_records = total_size / record_size;
    if (parsed_records != (uint32_t)records) {
        ESP_LOGW(
            TAG,
            "ZKT attendance count changed during read reported=%ld parsed=%lu packet_size=%lu",
            (long)records,
            (unsigned long)parsed_records,
            (unsigned long)record_size);
        records = (int32_t)parsed_records;
    }
    const uint8_t *p = data + 4;
    size_t remain = len - 4;
    size_t processed = 0;
    size_t added = 0;
    size_t pending = 0;
    size_t blocked = 0;
    size_t duplicates = 0;
    size_t filtered = 0;
    size_t skipped = 0;
    bool truth_enabled = ZONE_LITE_ORDS_RECONCILE_ENABLED && filter_year > 0 && filter_month > 0;
    bool truth_overflow = false;
    bool truth_build_failed = false;
    char **truth_events = NULL;
    size_t truth_count = 0;
    size_t truth_capacity = truth_enabled ? ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS : 0;
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
    if (truth_enabled) {
        truth_events = heap_caps_calloc(truth_capacity, sizeof(char *), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (truth_events == NULL) {
            truth_events = calloc(truth_capacity, sizeof(char *));
        }
        if (truth_events == NULL) {
            ESP_LOGE(TAG, "Could not allocate ORDS truth reconcile event list capacity=%u", (unsigned)truth_capacity);
            led_status_fault(LED_STATUS_FATAL);
            truth_enabled = false;
        }
    }
    while (remain >= record_size) {
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
        } else if (record_size == 40 && remain >= 40) {
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
            if (truth_enabled && event.cnic[0] != '\0') {
                if (truth_count < truth_capacity) {
                    char *truth_json = event_to_json(&event, "MANUAL_REPROCESS");
                    if (truth_json != NULL) {
                        truth_events[truth_count++] = truth_json;
                    } else {
                        truth_build_failed = true;
                    }
                } else {
                    truth_overflow = true;
                }
            }
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
    if (truth_enabled) {
        if (truth_overflow) {
            ESP_LOGE(TAG, "Skipping ORDS truth reconcile because current-month truth exceeded capacity=%u", (unsigned)truth_capacity);
            led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        } else if (truth_build_failed) {
            ESP_LOGE(TAG, "Skipping ORDS truth reconcile because one or more truth events could not be serialized");
            led_status_fault(LED_STATUS_FATAL);
        } else if (blocked > 0 || skipped > 0) {
            ESP_LOGW(
                TAG,
                "Skipping ORDS truth reconcile because current-month dump has blocked=%u skipped=%u identity gaps",
                (unsigned)blocked,
                (unsigned)skipped);
            led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        } else {
            (void)oracle_send_reconcile(truth_events, truth_count, filter_year, filter_month);
        }
    }
    if (truth_events != NULL) {
        for (size_t i = 0; i < truth_count; i++) {
            free(truth_events[i]);
        }
        free(truth_events);
    }
    ESP_LOGI(
        TAG,
        "Reconcile %s processed=%u new=%u pending=%u blocked=%u duplicates=%u filtered=%u skipped=%u truth=%u",
        capturetype,
        (unsigned)processed,
        (unsigned)added,
        (unsigned)pending,
        (unsigned)blocked,
        (unsigned)duplicates,
        (unsigned)filtered,
        (unsigned)skipped,
        (unsigned)truth_count);
    return added;
}

static bool system_time_is_valid(void)
{
    time_t now = 0;
    time(&now);
    return now >= ZONE_LITE_MIN_VALID_UNIX_TIME;
}

static void log_system_time(const char *message)
{
    time_t now = 0;
    struct tm utc = {0};
    char timestamp[32];
    time(&now);
    gmtime_r(&now, &utc);
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
    ESP_LOGI(TAG, "%s: %s", message, timestamp);
}

static bool ensure_system_time_synced(void)
{
    if (g_time_synced || system_time_is_valid()) {
        g_time_synced = true;
        return true;
    }

    if (!g_sntp_started) {
        esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(ZONE_LITE_SNTP_SERVER);
        esp_err_t err = esp_netif_sntp_init(&config);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "Could not start SNTP time sync: %s", esp_err_to_name(err));
            return false;
        }
        g_sntp_started = true;
        ESP_LOGI(TAG, "SNTP time sync started using %s", ZONE_LITE_SNTP_SERVER);
    }

    esp_err_t err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(ZONE_LITE_SNTP_SYNC_TIMEOUT_MS));
    if (err != ESP_OK && err != ESP_ERR_NOT_FINISHED) {
        ESP_LOGW(TAG, "SNTP time sync not ready yet: %s", esp_err_to_name(err));
        return false;
    }

    g_time_synced = system_time_is_valid();
    if (!g_time_synced) {
        ESP_LOGW(TAG, "System time is still invalid after SNTP sync");
        log_system_time("Current system UTC time");
    } else {
        log_system_time("System UTC time synchronized");
    }
    return g_time_synced;
}

static bool ords_send_allowed(void)
{
    int64_t now_ms = esp_timer_get_time() / 1000;
    return g_ords_next_attempt_ms == 0 || now_ms >= g_ords_next_attempt_ms;
}

static void ords_mark_success(void)
{
    g_ords_next_attempt_ms = 0;
    g_ords_failure_backoff_ms = ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS;
}

static void ords_mark_failure(void)
{
    int64_t now_ms = esp_timer_get_time() / 1000;
    uint32_t backoff_ms = g_ords_failure_backoff_ms;
    if (backoff_ms == 0) {
        backoff_ms = ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS;
    }
    g_ords_next_attempt_ms = now_ms + backoff_ms;
    if (backoff_ms < ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS / 2) {
        g_ords_failure_backoff_ms = backoff_ms * 2;
    } else {
        g_ords_failure_backoff_ms = ZONE_LITE_ORDS_FAILURE_BACKOFF_MAX_MS;
    }
    ESP_LOGW(TAG, "ORDS send failed; retrying in %lu ms", (unsigned long)backoff_ms);
}

static int http_post_json(const char *url, const char *json, char **response_body)
{
    if (!ensure_system_time_synced()) {
        return -1;
    }
    log_system_time("HTTPS system UTC time");
    ESP_LOGI(
        TAG,
        "HTTPS payload=%u heap_internal=%u heap_psram=%u",
        (unsigned)strlen(json),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = ZONE_LITE_ORDS_TIMEOUT_MS,
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
    if (ok) {
        ords_mark_success();
    } else {
        ords_mark_failure();
        led_status_fault(LED_STATUS_ORDS_FAILURE);
    }
    free(body);
    return ok;
}

static char *build_bulk_payload(char **events, size_t count, const char *batch_uid)
{
    const char *prefix = "{\"batch_uid\":\"";
    const char *middle = "\",\"events\":[";
    const char *suffix = "]}";
    size_t payload_len = strlen(prefix) + strlen(batch_uid) + strlen(middle) + strlen(suffix);
    size_t included = 0;

    for (size_t i = 0; i < count; i++) {
        if (events[i] == NULL || events[i][0] == '\0') {
            continue;
        }
        payload_len += strlen(events[i]);
        if (included > 0) {
            payload_len++;
        }
        included++;
    }
    if (included == 0) {
        return NULL;
    }

    char *payload = heap_caps_malloc(payload_len + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (payload == NULL) {
        payload = malloc(payload_len + 1);
    }
    if (payload == NULL) {
        return NULL;
    }

    char *write_at = payload;
    size_t len = strlen(prefix);
    memcpy(write_at, prefix, len);
    write_at += len;
    len = strlen(batch_uid);
    memcpy(write_at, batch_uid, len);
    write_at += len;
    len = strlen(middle);
    memcpy(write_at, middle, len);
    write_at += len;

    included = 0;
    for (size_t i = 0; i < count; i++) {
        if (events[i] == NULL || events[i][0] == '\0') {
            continue;
        }
        if (included > 0) {
            *write_at++ = ',';
        }
        len = strlen(events[i]);
        memcpy(write_at, events[i], len);
        write_at += len;
        included++;
    }

    len = strlen(suffix);
    memcpy(write_at, suffix, len);
    write_at += len;
    *write_at = '\0';
    return payload;
}

static bool oracle_send_bulk(char **events, size_t count)
{
    if (count == 0) {
        return true;
    }
    char batch_uid[64];
    snprintf(batch_uid, sizeof(batch_uid), "ZONE-ORDS-%lld", (long long)(esp_timer_get_time() / 1000));
    char *payload = build_bulk_payload(events, count, batch_uid);
    if (payload == NULL) {
        return false;
    }
    char url[256];
    snprintf(url, sizeof(url), "%s/raw-captures/bulk", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, payload, &body);
    bool ok = status == 409 || ((status == 200 || status == 201) && oracle_success_body(body));
    ESP_LOGI(TAG, "ORDS bulk count=%u status=%d ok=%s", (unsigned)count, status, ok ? "true" : "false");
    if (ok) {
        ords_mark_success();
    } else {
        ords_mark_failure();
        led_status_fault(LED_STATUS_ORDS_FAILURE);
    }
    free(body);
    free(payload);
    return ok;
}

static int days_in_month(int year, int month)
{
    static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (month < 1 || month > 12) {
        return 0;
    }
    if (month == 2 && ((year % 4 == 0 && year % 100 != 0) || (year % 400 == 0))) {
        return 29;
    }
    return days[month - 1];
}

static char *json_escape_alloc(const char *value)
{
    if (value == NULL) {
        value = "";
    }
    size_t len = strlen(value);
    char *escaped = malloc((len * 2) + 1);
    if (escaped == NULL) {
        return NULL;
    }
    char *out = escaped;
    for (size_t i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)value[i];
        if (ch == '"' || ch == '\\') {
            *out++ = '\\';
            *out++ = (char)ch;
        } else if (ch >= 0x20) {
            *out++ = (char)ch;
        }
    }
    *out = '\0';
    return escaped;
}

static char *build_reconcile_payload(char **events, size_t count, int year, int month)
{
    int last_day = days_in_month(year, month);
    if (last_day == 0) {
        return NULL;
    }
    char *zone_id = json_escape_alloc(ZONE_LITE_ZONE_ID);
    char *device_id = json_escape_alloc(ZONE_LITE_ZONE_DEVICE_ID);
    char *device_serial = json_escape_alloc(g_device_serial[0] ? g_device_serial : "unknown");
    if (zone_id == NULL || device_id == NULL || device_serial == NULL) {
        free(zone_id);
        free(device_id);
        free(device_serial);
        return NULL;
    }

    char header[512];
    int header_len = snprintf(
        header,
        sizeof(header),
        "{\"api_version\":1,\"zone_id\":\"%s\",\"device_id\":\"%s\",\"device_serial\":\"%s\","
        "\"window_start\":\"%04d-%02d-01\",\"window_end\":\"%04d-%02d-%02d\","
        "\"mode\":\"authoritative_replace\",\"events\":[",
        zone_id,
        device_id,
        device_serial,
        year,
        month,
        year,
        month,
        last_day);
    free(zone_id);
    free(device_id);
    free(device_serial);
    if (header_len <= 0 || (size_t)header_len >= sizeof(header)) {
        return NULL;
    }

    const char *suffix = "]}";
    size_t payload_len = (size_t)header_len + strlen(suffix);
    size_t included = 0;
    for (size_t i = 0; i < count; i++) {
        if (events[i] == NULL || events[i][0] == '\0') {
            continue;
        }
        payload_len += strlen(events[i]);
        if (included > 0) {
            payload_len++;
        }
        included++;
    }

    char *payload = heap_caps_malloc(payload_len + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (payload == NULL) {
        payload = malloc(payload_len + 1);
    }
    if (payload == NULL) {
        return NULL;
    }

    char *write_at = payload;
    memcpy(write_at, header, (size_t)header_len);
    write_at += header_len;
    included = 0;
    for (size_t i = 0; i < count; i++) {
        if (events[i] == NULL || events[i][0] == '\0') {
            continue;
        }
        if (included > 0) {
            *write_at++ = ',';
        }
        size_t len = strlen(events[i]);
        memcpy(write_at, events[i], len);
        write_at += len;
        included++;
    }
    size_t suffix_len = strlen(suffix);
    memcpy(write_at, suffix, suffix_len);
    write_at += suffix_len;
    *write_at = '\0';
    return payload;
}

static bool oracle_reconcile_body_ok(const char *body, int *deleted, int *corrected, int *invalid)
{
    *deleted = 0;
    *corrected = 0;
    *invalid = 0;
    if (body == NULL || body[0] == '\0') {
        return true;
    }
    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return true;
    }
    cJSON *success = cJSON_GetObjectItemCaseSensitive(root, "success");
    bool ok = !cJSON_IsBool(success) || cJSON_IsTrue(success);
    cJSON *deleted_item = cJSON_GetObjectItemCaseSensitive(root, "deleted_count");
    cJSON *corrected_item = cJSON_GetObjectItemCaseSensitive(root, "corrected_count");
    cJSON *invalid_item = cJSON_GetObjectItemCaseSensitive(root, "invalid_count");
    if (cJSON_IsNumber(deleted_item)) {
        *deleted = deleted_item->valueint;
    }
    if (cJSON_IsNumber(corrected_item)) {
        *corrected = corrected_item->valueint;
    }
    if (cJSON_IsNumber(invalid_item)) {
        *invalid = invalid_item->valueint;
    }
    cJSON_Delete(root);
    return ok;
}

static bool oracle_send_reconcile(char **events, size_t count, int year, int month)
{
    if (!ZONE_LITE_ORDS_RECONCILE_ENABLED) {
        return true;
    }
    if (!ords_send_allowed()) {
        ESP_LOGI(TAG, "Skipping ORDS truth reconcile until current ORDS backoff expires");
        return false;
    }
    if (count > ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS) {
        ESP_LOGE(TAG, "Truth reconcile has %u events, above safety limit %u", (unsigned)count, (unsigned)ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS);
        led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        return false;
    }

    char *payload = build_reconcile_payload(events, count, year, month);
    if (payload == NULL) {
        ESP_LOGE(TAG, "Could not build ORDS truth reconcile payload count=%u", (unsigned)count);
        led_status_fault(LED_STATUS_FATAL);
        return false;
    }

    char url[288];
    snprintf(url, sizeof(url), "%s/raw-captures/reconcile", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, payload, &body);
    int deleted = 0;
    int corrected = 0;
    int invalid = 0;
    bool body_ok = oracle_reconcile_body_ok(body, &deleted, &corrected, &invalid);
    bool ok = (status == 200 || status == 201) && body_ok;

    if (status == 404 || status == 405) {
        ESP_LOGW(TAG, "ORDS truth reconcile endpoint is not deployed yet status=%d; legacy outbox remains active", status);
        free(body);
        free(payload);
        return false;
    }

    ESP_LOGI(
        TAG,
        "ORDS truth reconcile count=%u month=%04d-%02d status=%d ok=%s deleted=%d corrected=%d invalid=%d",
        (unsigned)count,
        year,
        month,
        status,
        ok ? "true" : "false",
        deleted,
        corrected,
        invalid);

    if (ok) {
        ords_mark_success();
        if (deleted > 0 || corrected > 0 || invalid > 0) {
            g_truth_reconcile_warning = true;
            ESP_LOGW(TAG, "Oracle raw table was repaired from ZKT truth deleted=%d corrected=%d invalid=%d", deleted, corrected, invalid);
            led_status_fault(invalid > 0 ? LED_STATUS_BLOCKED_IDENTITY : LED_STATUS_TRUTH_REPAIR);
        } else if (g_truth_reconcile_warning) {
            g_truth_reconcile_warning = false;
            ESP_LOGI(TAG, "ORDS truth reconcile is clean after previous repair warning");
        }
    } else if (status == 400) {
        ESP_LOGW(TAG, "ORDS rejected truth reconcile payload status=400 body=%s", body ? body : "");
        led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
    } else {
        ESP_LOGW(TAG, "ORDS truth reconcile failed status=%d body=%s", status, body ? body : "");
        ords_mark_failure();
        led_status_fault(LED_STATUS_ORDS_FAILURE);
    }

    free(body);
    free(payload);
    return ok;
}

static void append_acked_uid_from_json_to_file(const char *event_json, FILE *acked_file)
{
    cJSON *root = cJSON_Parse(event_json);
    if (root == NULL) {
        return;
    }
    cJSON *uid = cJSON_GetObjectItemCaseSensitive(root, "event_uid");
    if (cJSON_IsString(uid)) {
        if (acked_file != NULL) {
            append_line_to_open_file(acked_file, ACKED_PATH, uid->valuestring);
        } else {
            append_line(ACKED_PATH, uid->valuestring);
        }
        seen_add(uid->valuestring);
    }
    cJSON_Delete(root);
}

static bool replace_pending_with_backup(void)
{
    bool had_backup = false;
    if (remove(PENDING_BACKUP_PATH) != 0 && errno != ENOENT) {
        ESP_LOGW(TAG, "Could not remove stale pending backup errno=%d", errno);
    }
    if (rename(PENDING_PATH, PENDING_BACKUP_PATH) == 0) {
        had_backup = true;
    } else if (errno != ENOENT) {
        ESP_LOGW(TAG, "Could not preserve pending outbox before rewrite errno=%d", errno);
        return false;
    }

    if (rename(PENDING_TMP_PATH, PENDING_PATH) == 0) {
        if (had_backup && remove(PENDING_BACKUP_PATH) != 0 && errno != ENOENT) {
            ESP_LOGW(TAG, "Could not remove pending backup after rewrite errno=%d", errno);
        }
        return true;
    }

    int rewrite_errno = errno;
    ESP_LOGW(TAG, "Could not rewrite pending outbox errno=%d", rewrite_errno);
    if (had_backup && rename(PENDING_BACKUP_PATH, PENDING_PATH) != 0) {
        ESP_LOGE(TAG, "Could not restore pending outbox backup errno=%d", errno);
    }
    return false;
}

static void oracle_drain_pending(bool live_first)
{
    if (!file_has_nonempty_line(PENDING_PATH)) {
        led_status_set_backlog(false);
        return;
    }
    led_status_set_backlog(true);
    if (!ords_send_allowed()) {
        return;
    }
    led_status_set(LED_STATUS_SYNCING);

    FILE *in = fopen(PENDING_PATH, "r");
    if (in == NULL) {
        led_status_set_backlog(false);
        return;
    }
    FILE *out = fopen(PENDING_TMP_PATH, "w");
    if (out == NULL) {
        fclose(in);
        led_status_fault(LED_STATUS_FATAL);
        return;
    }
    FILE *acked_file = fopen(ACKED_PATH, "a");
    if (acked_file == NULL) {
        ESP_LOGW(TAG, "Could not keep %s open for ack appends", ACKED_PATH);
    }

    char **bulk = calloc(ZONE_LITE_ORDS_BULK_CHUNK_SIZE, sizeof(char *));
    if (bulk == NULL) {
        fclose(in);
        fclose(out);
        if (acked_file != NULL) {
            fclose(acked_file);
        }
        led_status_fault(LED_STATUS_FATAL);
        return;
    }
    size_t bulk_count = 0;
    char line[MAX_EVENT_JSON];
    bool failed = false;
    bool made_progress = false;

    while (fgets(line, sizeof(line), in) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0') {
            continue;
        }
        if (live_first && bulk_count == 0) {
            if (oracle_send_live(line)) {
                append_acked_uid_from_json_to_file(line, acked_file);
                made_progress = true;
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
        if (bulk_count == ZONE_LITE_ORDS_BULK_CHUNK_SIZE) {
            if (oracle_send_bulk(bulk, bulk_count)) {
                for (size_t i = 0; i < bulk_count; i++) {
                    append_acked_uid_from_json_to_file(bulk[i], acked_file);
                    free(bulk[i]);
                    bulk[i] = NULL;
                }
                made_progress = true;
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
                append_acked_uid_from_json_to_file(bulk[i], acked_file);
            }
            made_progress = true;
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
    if (failed && !made_progress) {
        fclose(in);
        fclose(out);
        if (acked_file != NULL) {
            fclose(acked_file);
        }
        (void)remove(PENDING_TMP_PATH);
        led_status_set_backlog(true);
        free(bulk);
        return;
    }
    if (failed) {
        while (fgets(line, sizeof(line), in) != NULL) {
            fputs(line, out);
        }
    }
    fclose(in);
    fclose(out);
    if (acked_file != NULL) {
        fclose(acked_file);
    }
    if (!replace_pending_with_backup()) {
        (void)remove(PENDING_TMP_PATH);
        led_status_fault(LED_STATUS_FATAL);
    }
    bool has_backlog = failed || file_has_nonempty_line(PENDING_PATH);
    led_status_set_backlog(has_backlog);
    if (!has_backlog) {
        led_status_set(LED_STATUS_HEALTHY);
    }
    free(bulk);
}

static bool probe_zkt_device(uint32_t host_order_ip, uint32_t *selected_ip)
{
    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_PORT, ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS, &sock)) {
        return false;
    }
    g_last_zkt_tcp_candidate_ip = host_order_ip;

    char ip_text[16];
    ip_to_text(host_order_ip, ip_text, sizeof(ip_text));

    bool ok = false;
    zk_context_t ctx = {0};
    if (!zk_connect_and_auth(sock, &ctx)) {
        ESP_LOGW(TAG, "%s:%d answered TCP but failed ZKT auth", ip_text, ZONE_LITE_ZKT_PORT);
        led_status_fault(LED_STATUS_ZKT_FAILURE);
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
    g_last_authenticated_zkt_ip = host_order_ip;
    led_status_set(LED_STATUS_ZKT_AUTHENTICATED);
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
    led_status_set(LED_STATUS_ZKT_DISCOVERING);
    g_last_zkt_tcp_candidate_ip = 0;
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
    uint32_t preferred = configured_preferred_zkt_ip();
    if (preferred != 0 && preferred != own_ip && preferred != ntohl(ip_info.gw.addr)) {
        ESP_LOGI(TAG, "Trying preferred ZKT IP %s:%d", ZONE_LITE_ZKT_PREFERRED_IP, ZONE_LITE_ZKT_PORT);
        if (probe_zkt_device(preferred, selected_ip)) {
            return true;
        }
    }
    ESP_LOGI(TAG, "Scanning %lu hosts for ZKT TCP port %d", (unsigned long)host_count, ZONE_LITE_ZKT_PORT);
    for (uint32_t candidate = network + 1; candidate < broadcast; candidate++) {
        if (candidate == own_ip || candidate == ntohl(ip_info.gw.addr) || candidate == preferred) {
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
            enqueue_result_t result = enqueue_event(&event, "LIVE");
            if (result == ENQUEUE_PENDING) {
                led_status_event(LED_EVENT_LIVE_PUNCH);
            }
            any = true;
        }
    }
    return any;
}

static bool zk_register_attlog_events(int sock, zk_context_t *ctx, bool enable)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    uint8_t flags[4];
    write_le32(flags, enable ? EF_ATTLOG : 0);
    if (!zk_send_command(sock, ctx, CMD_REG_EVENT, flags, sizeof(flags), rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code)) {
        ESP_LOGW(TAG, "Could not %s ZKT live attendance events", enable ? "register" : "unregister");
        if (enable) {
            led_status_fault(LED_STATUS_ZKT_FAILURE);
        }
        return false;
    }
    ESP_LOGI(TAG, "ZKT live attendance events %s", enable ? "registered" : "unregistered");
    if (enable) {
        led_status_set(LED_STATUS_HEALTHY);
    }
    return true;
}

static void gateway_run(uint32_t host_order_ip)
{
    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_PORT, 3000, &sock)) {
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        return;
    }
    zk_context_t ctx = {0};
    if (!zk_connect_and_auth(sock, &ctx)) {
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        close(sock);
        return;
    }
    led_status_set(LED_STATUS_ZKT_AUTHENTICATED);
    (void)zk_read_option(sock, &ctx, "~SerialNumber", g_device_serial, sizeof(g_device_serial));
    int32_t user_count = 0;
    int32_t records = 0;
    struct tm device_now;
    if (!zk_get_counts(sock, &ctx, &user_count, &records)) {
        ESP_LOGW(TAG, "Could not read initial ZKT counts; reconnecting");
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        zk_disconnect(sock, &ctx);
        close(sock);
        return;
    }
    user_table_t *users = calloc(1, sizeof(user_table_t));
    if (users == NULL) {
        ESP_LOGE(TAG, "Could not allocate ZKT user table");
        zk_disconnect(sock, &ctx);
        close(sock);
        return;
    }
    if (!zk_load_users(sock, &ctx, users, user_count)) {
        ESP_LOGW(TAG, "Could not load ZKT users; reconnecting before sync");
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        zk_disconnect(sock, &ctx);
        close(sock);
        free(users);
        return;
    }
    led_status_set(LED_STATUS_SYNCING);
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
        led_status_fault(LED_STATUS_ZKT_FAILURE);
    }
    uint8_t rx[1024];
    zk_response_t response = {0};
    (void)zk_send_command(sock, &ctx, CMD_CANCELCAPTURE, NULL, 0, rx, sizeof(rx), &response);
    (void)zk_send_command(sock, &ctx, CMD_STARTVERIFY, NULL, 0, rx, sizeof(rx), &response);
    if (!zk_register_attlog_events(sock, &ctx, true)) {
        zk_disconnect(sock, &ctx);
        close(sock);
        free(users);
        return;
    }
    oracle_drain_pending(false);

    int64_t last_reconcile = esp_timer_get_time() / 1000;
    int64_t last_live_register = last_reconcile;
    while (true) {
        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(sock, &read_fds);
        struct timeval tv = {.tv_sec = 5, .tv_usec = 0};
        int rc = select(sock + 1, &read_fds, NULL, NULL, &tv);
        if (rc > 0 && FD_ISSET(sock, &read_fds)) {
            zk_tcp_header_t top;
            if (!recv_exact(sock, (uint8_t *)&top, sizeof(top)) ||
                top.marker_1 != MACHINE_PREPARE_DATA_1 ||
                top.marker_2 != MACHINE_PREPARE_DATA_2 ||
                top.length < sizeof(zk_header_t) || top.length > 2048) {
                ESP_LOGW(TAG, "ZKT live socket returned an invalid packet; reconnecting");
                led_status_fault(LED_STATUS_ZKT_FAILURE);
                break;
            }
            uint8_t *packet = malloc(top.length);
            if (packet == NULL || !recv_exact(sock, packet, top.length)) {
                free(packet);
                ESP_LOGW(TAG, "Could not read complete ZKT live packet; reconnecting");
                led_status_fault(LED_STATUS_ZKT_FAILURE);
                break;
            }
            zk_header_t *header = (zk_header_t *)packet;
            bool keep_session = true;
            if (header->command == CMD_REG_EVENT && top.length > sizeof(zk_header_t)) {
                process_live_packet(packet + sizeof(zk_header_t), top.length - sizeof(zk_header_t), users);
                if (!zk_send_ack_only(sock, ctx.session_id)) {
                    ESP_LOGW(TAG, "Could not ACK ZKT live event; reconnecting");
                    led_status_fault(LED_STATUS_ZKT_FAILURE);
                    keep_session = false;
                }
                oracle_drain_pending(true);
                if (!file_has_nonempty_line(PENDING_PATH)) {
                    led_status_set(LED_STATUS_HEALTHY);
                }
            }
            free(packet);
            if (!keep_session) {
                break;
            }
        } else if (rc < 0) {
            ESP_LOGW(TAG, "ZKT live socket select failed; reconnecting");
            led_status_fault(LED_STATUS_ZKT_FAILURE);
            break;
        }
        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms - last_reconcile >= ZONE_LITE_RECONCILE_INTERVAL_MS) {
            last_reconcile = now_ms;
            int32_t refreshed_users = 0;
            int32_t refreshed_records = 0;
            if (!zk_get_counts(sock, &ctx, &refreshed_users, &refreshed_records)) {
                ESP_LOGW(TAG, "ZKT health check failed during reconcile; reconnecting");
                led_status_fault(LED_STATUS_ZKT_FAILURE);
                break;
            }
            if (refreshed_users != user_count) {
                if (!zk_refresh_users_after_count_change(sock, &ctx, users, user_count, refreshed_users)) {
                    ESP_LOGW(TAG, "Could not refresh ZKT users after count change; reconnecting");
                    led_status_fault(LED_STATUS_ZKT_FAILURE);
                    break;
                }
                user_count = refreshed_users;
            }
            if (zk_get_time_parts(sock, &ctx, &device_now)) {
                led_status_set(LED_STATUS_SYNCING);
                reconcile_attendance_dump(
                    sock,
                    &ctx,
                    users,
                    refreshed_records,
                    "LIVE_POLL",
                    device_now.tm_year + 1900,
                    device_now.tm_mon + 1);
            } else {
                ESP_LOGW(TAG, "ZKT time read failed during reconcile; reconnecting");
                led_status_fault(LED_STATUS_ZKT_FAILURE);
                break;
            }
            oracle_drain_pending(false);
            if (!file_has_nonempty_line(PENDING_PATH)) {
                led_status_set(LED_STATUS_HEALTHY);
            }
            now_ms = esp_timer_get_time() / 1000;
            if (now_ms - last_live_register >= ZKT_LIVE_REREGISTER_INTERVAL_MS) {
                if (!zk_register_attlog_events(sock, &ctx, true)) {
                    break;
                }
                last_live_register = now_ms;
            }
        }
    }
    (void)zk_register_attlog_events(sock, &ctx, false);
    zk_disconnect(sock, &ctx);
    close(sock);
    free(users);
}

static void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_data;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        led_status_set(LED_STATUS_WIFI_CONNECTING);
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_event_group, WIFI_CONNECTED_BIT);
        led_status_set(LED_STATUS_WIFI_CONNECTING);
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
        led_status_set(LED_STATUS_ZKT_DISCOVERING);
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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_LOGI(TAG, "Wi-Fi power save disabled for long-lived ZKT sockets");
    led_status_set(LED_STATUS_WIFI_CONNECTING);
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
    led_status_fault(LED_STATUS_FATAL);
    return false;
}

static void gateway_task(void *arg)
{
    (void)arg;
    uint32_t discovery_failures = 0;
    int64_t last_zkt_reboot_ms = 0;
    while (true) {
        if ((xEventGroupGetBits(wifi_event_group) & WIFI_CONNECTED_BIT) == 0) {
            ESP_LOGW(TAG, "Waiting for Wi-Fi before ZKT discovery");
            (void)wait_for_wifi();
        }
        uint32_t selected_ip = 0;
        if (discover_zkt(&selected_ip)) {
            discovery_failures = 0;
            gateway_run(selected_ip);
            ESP_LOGW(TAG, "ZKT session ended; rediscovering");
        } else {
            discovery_failures++;
            led_status_fault(LED_STATUS_ZKT_FAILURE);
            ESP_LOGW(
                TAG,
                "No authenticated ZKT device found on port %d (failure %lu/%d)",
                ZONE_LITE_ZKT_PORT,
                (unsigned long)discovery_failures,
                ZKT_DISCOVERY_RESTART_AFTER_FAILURES);
            if (maybe_reboot_zkt_for_recovery(discovery_failures, &last_zkt_reboot_ms)) {
                discovery_failures = 0;
            }
            if (discovery_failures >= ZKT_DISCOVERY_RESTART_AFTER_FAILURES) {
                ESP_LOGE(TAG, "Restarting ESP32 after repeated ZKT discovery failures");
                led_status_fault(LED_STATUS_FATAL);
                vTaskDelay(pdMS_TO_TICKS(1000));
                esp_restart();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_DISCOVERY_RETRY_DELAY_MS));
    }
}

void app_main(void)
{
    setenv("TZ", "UTC0", 1);
    tzset();
    led_status_init();
    led_status_set(LED_STATUS_BOOTING);
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
    if (xTaskCreate(gateway_task, "zone_gateway", 16384, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not start Zone Lite gateway task");
        led_status_fault(LED_STATUS_FATAL);
    }
}
