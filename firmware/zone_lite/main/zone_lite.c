#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "nvs_flash.h"

#if __has_include("zone_lite_config.h")
#include "zone_lite_config.h"
#else
#include "zone_lite_config.example.h"
#endif

#define CMD_OPTIONS_RRQ 11
#define CMD_GET_FREE_SIZES 50
#define CMD_GET_TIME 201
#define CMD_CONNECT 1000
#define CMD_EXIT 1001
#define CMD_AUTH 1102

#define CMD_ACK_OK 2000
#define CMD_ACK_DATA 2002
#define CMD_ACK_UNAUTH 2005

#define MACHINE_PREPARE_DATA_1 20560
#define MACHINE_PREPARE_DATA_2 32130
#define USHRT_MAX_ZK 65535

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT BIT1
#define WIFI_MAXIMUM_RETRY 10

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

static uint16_t read_le16(const uint8_t *data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint32_t read_le32(const uint8_t *data)
{
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) | ((uint32_t)data[2] << 16) |
           ((uint32_t)data[3] << 24);
}

static uint16_t zk_checksum(const uint8_t *data, size_t len)
{
    uint32_t checksum = 0;
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
    return (uint16_t)(~checksum & 0xffff);
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
    uint8_t scratch[256];
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
    uint8_t tx[512];
    size_t packet_len = sizeof(zk_header_t) + payload_len;
    if (packet_len > sizeof(tx)) {
        ESP_LOGE(TAG, "ZKT command payload too large: %u", (unsigned)payload_len);
        return false;
    }

    uint16_t next_reply = (uint16_t)(ctx->reply_id + 1);
    if (next_reply >= USHRT_MAX_ZK) {
        next_reply = (uint16_t)(next_reply - USHRT_MAX_ZK);
    }

    zk_header_t header = {
        .command = command,
        .checksum = 0,
        .session_id = ctx->session_id,
        .reply_id = next_reply,
    };
    memcpy(tx, &header, sizeof(header));
    if (payload_len > 0) {
        memcpy(tx + sizeof(header), payload, payload_len);
    }
    ((zk_header_t *)tx)->checksum = zk_checksum(tx, packet_len);

    zk_tcp_header_t tcp_header = {
        .marker_1 = MACHINE_PREPARE_DATA_1,
        .marker_2 = MACHINE_PREPARE_DATA_2,
        .length = (uint32_t)packet_len,
    };

    if (send(sock, &tcp_header, sizeof(tcp_header), 0) != sizeof(tcp_header)) {
        return false;
    }
    if (send(sock, tx, packet_len, 0) != (ssize_t)packet_len) {
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
        ESP_LOGW(TAG, "ZKT reply too large for probe buffer: %lu", (unsigned long)reply_top.length);
        return drain_bytes(sock, reply_top.length) && false;
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
    return code == CMD_ACK_OK || code == CMD_ACK_DATA;
}

static bool zk_connect_and_auth(int sock, zk_context_t *ctx)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    ctx->session_id = 0;
    ctx->reply_id = USHRT_MAX_ZK - 1;

    if (!zk_send_command(sock, ctx, CMD_CONNECT, NULL, 0, rx, sizeof(rx), &response)) {
        return false;
    }

    ctx->session_id = response.session_id;
    if (response.code == CMD_ACK_UNAUTH) {
        uint8_t commkey[4];
        make_commkey(ZONE_LITE_ZKT_COMM_KEY, ctx->session_id, commkey);
        if (!zk_send_command(sock, ctx, CMD_AUTH, commkey, sizeof(commkey), rx, sizeof(rx), &response)) {
            return false;
        }
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

    if (!zk_send_command(
            sock, ctx, CMD_OPTIONS_RRQ, request, name_len, rx, sizeof(rx), &response) ||
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

static bool zk_get_time(int sock, zk_context_t *ctx, char *out, size_t out_len)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    if (!zk_send_command(sock, ctx, CMD_GET_TIME, NULL, 0, rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code) || response.data_len < 4) {
        return false;
    }

    uint32_t t = read_le32(response.data);
    uint32_t second = t % 60;
    t /= 60;
    uint32_t minute = t % 60;
    t /= 60;
    uint32_t hour = t % 24;
    t /= 24;
    uint32_t day = t % 31 + 1;
    t /= 31;
    uint32_t month = t % 12 + 1;
    t /= 12;
    uint32_t year = t + 2000;

    snprintf(
        out,
        out_len,
        "%04lu-%02lu-%02lu %02lu:%02lu:%02lu",
        (unsigned long)year,
        (unsigned long)month,
        (unsigned long)day,
        (unsigned long)hour,
        (unsigned long)minute,
        (unsigned long)second);
    return true;
}

static int32_t read_le32_signed(const uint8_t *data)
{
    return (int32_t)read_le32(data);
}

static bool zk_get_record_count(int sock, zk_context_t *ctx, int32_t *records)
{
    uint8_t rx[1024];
    zk_response_t response = {0};
    if (!zk_send_command(sock, ctx, CMD_GET_FREE_SIZES, NULL, 0, rx, sizeof(rx), &response) ||
        !zk_status_ok(response.code) || response.data_len < 36) {
        return false;
    }

    *records = read_le32_signed(response.data + (8 * 4));
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
    struct timeval tv = {
        .tv_sec = timeout_ms / 1000,
        .tv_usec = (timeout_ms % 1000) * 1000,
    };

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

    struct timeval io_timeout = {.tv_sec = 3, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &io_timeout, sizeof(io_timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &io_timeout, sizeof(io_timeout));

    *out_sock = sock;
    return true;
}

static bool probe_zkt_device(uint32_t host_order_ip)
{
    int sock = -1;
    if (!tcp_connect_with_timeout(
            host_order_ip,
            ZONE_LITE_ZKT_PORT,
            ZONE_LITE_DISCOVERY_CONNECT_TIMEOUT_MS,
            &sock)) {
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
    int32_t records = -1;
    (void)zk_read_option(sock, &ctx, "~SerialNumber", serial, sizeof(serial));
    (void)zk_read_option(sock, &ctx, "~DeviceName", device_name, sizeof(device_name));
    (void)zk_read_option(sock, &ctx, "~Platform", platform, sizeof(platform));
    (void)zk_get_time(sock, &ctx, device_time, sizeof(device_time));
    (void)zk_get_record_count(sock, &ctx, &records);

    ESP_LOGI(
        TAG,
        "Selected ZKT device %s:%d serial=%s name=%s platform=%s time=%s records=%ld device_id=%s",
        ip_text,
        ZONE_LITE_ZKT_PORT,
        serial[0] ? serial : "unknown",
        device_name[0] ? device_name : "unknown",
        platform[0] ? platform : "unknown",
        device_time[0] ? device_time : "unknown",
        (long)records,
        ZONE_LITE_ZONE_DEVICE_ID);
    ok = true;

done:
    if (ok) {
        zk_disconnect(sock, &ctx);
    }
    close(sock);
    return ok;
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
            ESP_LOGW(TAG, "Retrying Wi-Fi connection (%d/%d)", wifi_retry_count, WIFI_MAXIMUM_RETRY);
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
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, ZONE_LITE_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy(
        (char *)wifi_config.sta.password,
        ZONE_LITE_WIFI_PASSWORD,
        sizeof(wifi_config.sta.password));
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

static void discover_task(void *arg)
{
    (void)arg;
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    while (true) {
        esp_netif_ip_info_t ip_info;
        if (netif == NULL || esp_netif_get_ip_info(netif, &ip_info) != ESP_OK ||
            ip_info.ip.addr == 0) {
            ESP_LOGW(TAG, "No station IP yet; waiting before discovery");
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        uint32_t own_ip = ntohl(ip_info.ip.addr);
        uint32_t netmask = ntohl(ip_info.netmask.addr);
        uint32_t network = own_ip & netmask;
        uint32_t broadcast = network | ~netmask;
        uint32_t host_count = broadcast > network ? broadcast - network - 1 : 0;

        // Keep the DHCP scan bounded. Branch routers are normally /24; very broad
        // masks would make boot discovery unpleasantly slow.
        if (host_count == 0 || host_count > 254) {
            network = own_ip & 0xffffff00U;
            broadcast = network | 0xffU;
            host_count = 254;
        }

        ESP_LOGI(
            TAG,
            "Scanning %lu hosts for ZKT TCP port %d",
            (unsigned long)host_count,
            ZONE_LITE_ZKT_PORT);

        bool found = false;
        for (uint32_t candidate = network + 1; candidate < broadcast; candidate++) {
            if (candidate == own_ip || candidate == ntohl(ip_info.gw.addr)) {
                continue;
            }
            if (probe_zkt_device(candidate)) {
                found = true;
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
        }

        if (!found) {
            ESP_LOGW(
                TAG,
                "No authenticated ZKT device found on port %d; retrying",
                ZONE_LITE_ZKT_PORT);
        }
        vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_DISCOVERY_RETRY_DELAY_MS));
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_LOGI(TAG, "Zone Lite starting for device id %s", ZONE_LITE_ZONE_DEVICE_ID);
    wifi_init_sta();
    if (!wait_for_wifi()) {
        return;
    }

    xTaskCreate(discover_task, "zkt_discovery", 8192, NULL, 5, NULL);
}
