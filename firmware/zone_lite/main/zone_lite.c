#include <errno.h>
#include <ctype.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
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
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "mbedtls/md.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "zone_lite_config.example.h"

#include "led_status.h"
#include "add_connector.h"
#include "zone_config.h"

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
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED
#define ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED 1
#endif
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_HOUR
#define ZONE_LITE_DAILY_ZKT_REBOOT_HOUR 3
#endif
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_MINUTE
#define ZONE_LITE_DAILY_ZKT_REBOOT_MINUTE 0
#endif
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES
#define ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES 300
#endif
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_WINDOW_MINUTES
#define ZONE_LITE_DAILY_ZKT_REBOOT_WINDOW_MINUTES 30
#endif
#ifndef ZONE_LITE_DAILY_ZKT_REBOOT_RETRY_DELAY_MS
#define ZONE_LITE_DAILY_ZKT_REBOOT_RETRY_DELAY_MS (5 * 60 * 1000)
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
#ifndef ZONE_LITE_ZKT_EXPECTED_SERIAL
#define ZONE_LITE_ZKT_EXPECTED_SERIAL ""
#endif

#define CMD_OPTIONS_RRQ 11
#define CMD_USERTEMP_RRQ 9
#define CMD_USER_WRQ 8
#define CMD_DELETE_USER 18
#define CMD_ATTLOG_RRQ 13
#define CMD_GET_FREE_SIZES 50
#define CMD_STARTVERIFY 60
#define CMD_CANCELCAPTURE 62
#define CMD_GET_TIME 201
#define CMD_REG_EVENT 500
#define CMD_CONNECT 1000
#define CMD_EXIT 1001
#define CMD_RESTART 1004
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
#define BLOCKED_RECOVERY_TMP_PATH STORAGE_BASE "/blocked_recovery.tmp"
#define BLOCKED_RECOVERY_BACKUP_PATH STORAGE_BASE "/blocked_recovery.bak"
#define CORRUPT_ORDS_PATH STORAGE_BASE "/corrupt_ords.jsonl"
#define ACKED_PATH STORAGE_BASE "/acked_uids.txt"
#define PROCESSED_COMMANDS_PATH STORAGE_BASE "/processed_commands.txt"
#define CANCELLED_COMMANDS_PATH STORAGE_BASE "/add_cancelled.txt"
#define MAX_USERS 2048
#define SEEN_HASH_CAPACITY 262144
#define MAX_EVENT_JSON 1024
#define ADD_RECONCILE_BATCH_EVENTS 10
#define ADD_RECONCILE_COMMIT_BATCHES 32
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
#ifndef ZONE_LITE_ORDS_CA_CERT_PEM
#define ZONE_LITE_ORDS_CA_CERT_PEM NULL
#endif
#ifndef ZONE_LITE_ZKT_USER_REFRESH_RETRIES
#define ZONE_LITE_ZKT_USER_REFRESH_RETRIES 3
#endif
#ifndef ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS
#define ZONE_LITE_ZKT_USER_REFRESH_RETRY_DELAY_MS 2000
#endif
#ifndef ZONE_LITE_DISCOVERY_FULL_SCAN_INTERVAL_MS
#define ZONE_LITE_DISCOVERY_FULL_SCAN_INTERVAL_MS (15 * 60 * 1000)
#endif
#ifndef ZONE_LITE_RECOVERY_STABILITY_MS
#define ZONE_LITE_RECOVERY_STABILITY_MS (2 * 60 * 1000)
#endif
#ifndef ZONE_LITE_FLAP_WINDOW_MS
#define ZONE_LITE_FLAP_WINDOW_MS (15 * 60 * 1000)
#endif
#ifndef ZONE_LITE_FLAP_THRESHOLD
#define ZONE_LITE_FLAP_THRESHOLD 3
#endif
#ifndef ZONE_LITE_FLAP_QUIET_MS
#define ZONE_LITE_FLAP_QUIET_MS (5 * 60 * 1000)
#endif
#ifndef ZONE_LITE_ZKT_BACKOFF_MAX_MS
#define ZONE_LITE_ZKT_BACKOFF_MAX_MS (10 * 60 * 1000)
#endif
#ifndef ZONE_LITE_RESTART_SLOT_1_HOUR
#define ZONE_LITE_RESTART_SLOT_1_HOUR 2
#endif
#ifndef ZONE_LITE_RESTART_SLOT_2_HOUR
#define ZONE_LITE_RESTART_SLOT_2_HOUR 12
#endif
#ifndef ZONE_LITE_RESTART_SLOT_3_HOUR
#define ZONE_LITE_RESTART_SLOT_3_HOUR 22
#endif
#ifndef ZONE_LITE_FULL_TRUTH_RECONCILE_MS
#define ZONE_LITE_FULL_TRUTH_RECONCILE_MS (6 * 60 * 60 * 1000LL)
#endif

// Per-device NVS provisioning overrides compile-time development defaults.
#undef ZONE_LITE_WIFI_SSID
#define ZONE_LITE_WIFI_SSID (zone_config_get()->wifi_ssid)
#undef ZONE_LITE_WIFI_PASSWORD
#define ZONE_LITE_WIFI_PASSWORD (zone_config_get()->wifi_password)
#undef ZONE_LITE_ZKT_PORT
#define ZONE_LITE_ZKT_PORT (zone_config_get()->zkt_port)
#undef ZONE_LITE_ZKT_COMM_KEY
#define ZONE_LITE_ZKT_COMM_KEY (zone_config_get()->zkt_comm_key)
#undef ZONE_LITE_ZKT_PREFERRED_IP
#define ZONE_LITE_ZKT_PREFERRED_IP (zone_config_get()->zkt_preferred_ip)
#undef ZONE_LITE_ZKT_EXPECTED_SERIAL
#define ZONE_LITE_ZKT_EXPECTED_SERIAL (zone_config_get()->zkt_expected_serial)
#undef ZONE_LITE_ZONE_DEVICE_ID
#define ZONE_LITE_ZONE_DEVICE_ID (zone_config_get()->zone_device_id)
#undef ZONE_LITE_ZONE_ID
#define ZONE_LITE_ZONE_ID (zone_config_get()->zone_id)
#undef ZONE_LITE_ORDS_BASE_URL
#define ZONE_LITE_ORDS_BASE_URL (zone_config_get()->ords_base_url)
#undef ZONE_LITE_ORDS_USERNAME
#define ZONE_LITE_ORDS_USERNAME (zone_config_get()->ords_username)
#undef ZONE_LITE_ORDS_PASSWORD
#define ZONE_LITE_ORDS_PASSWORD (zone_config_get()->ords_password)
#undef ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED
#define ZONE_LITE_ZKT_RECOVERY_REBOOT_ENABLED (zone_config_get()->zkt_recovery_enabled)
#undef ZONE_LITE_ZKT_RECOVERY_FAILURES
#define ZONE_LITE_ZKT_RECOVERY_FAILURES (zone_config_get()->zkt_recovery_failures)
#undef ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS
#define ZONE_LITE_ZKT_RECOVERY_COOLDOWN_MS (zone_config_get()->zkt_recovery_cooldown_ms)
#undef ZONE_LITE_ZKT_REBOOT_WAIT_MS
#define ZONE_LITE_ZKT_REBOOT_WAIT_MS (zone_config_get()->zkt_reboot_wait_ms)
#undef ZONE_LITE_ZKT_TELNET_PORT
#define ZONE_LITE_ZKT_TELNET_PORT (zone_config_get()->zkt_telnet_port)
#undef ZONE_LITE_ZKT_TELNET_USERNAME
#define ZONE_LITE_ZKT_TELNET_USERNAME (zone_config_get()->zkt_telnet_username)
#undef ZONE_LITE_ZKT_TELNET_PASSWORD
#define ZONE_LITE_ZKT_TELNET_PASSWORD (zone_config_get()->zkt_telnet_password)
#undef ZONE_LITE_ZKT_TELNET_EXPECT_BANNER
#define ZONE_LITE_ZKT_TELNET_EXPECT_BANNER (zone_config_get()->zkt_telnet_banner)
#undef ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND
#define ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND (zone_config_get()->zkt_telnet_command)
// Large MB40 attendance buffers can remain in CMD_PREPARE_DATA while flash is
// being read for more than a minute. Connection attempts retain their own
// short deadline; authenticated command/data reads get this wider bound.
#define ZKT_IO_TIMEOUT_SEC 90
// Use the ZKT TCP protocol's native maximum. It is proven on this MB40 and
// halves the number of flash-backed requests needed for a multi-megabyte dump.
#define ZKT_BUFFER_CHUNK_BYTES 0xffc0
#define ZKT_KEEPALIVE_IDLE_SEC 60
#define ZKT_KEEPALIVE_INTERVAL_SEC 10
#define ZKT_KEEPALIVE_COUNT 3
#define ZKT_LIVE_REREGISTER_INTERVAL_MS (30 * 60 * 1000)
#define ZKT_DISCOVERY_RESTART_AFTER_FAILURES 5
#define ZKT_TELNET_BUFFER_SIZE 768
#define ZKT_TELNET_IO_TIMEOUT_MS 5000

static const char *TAG = "zone_lite";
static EventGroupHandle_t wifi_event_group;
static SemaphoreHandle_t g_storage_lock;
static SemaphoreHandle_t g_ords_http_lock;
static SemaphoreHandle_t g_ords_outbox_gate;
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
    uint8_t privilege;
    char password[9];
    uint32_t card;
    char group_id[8];
    uint8_t record_size;
    char terminal_identity_fingerprint[65];
    char terminal_state_fingerprint[65];
} zkt_user_t;

typedef struct {
    zkt_user_t rows[MAX_USERS];
    size_t count;
    uint8_t record_size;
    bool complete;
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
static int32_t g_last_synced_attendance_count = -1;
static int64_t g_last_full_truth_reconcile_epoch;
static int64_t g_last_full_truth_reconcile_ms;
static uint32_t g_last_zkt_tcp_candidate_ip;
static bool g_sntp_started;
static bool g_time_synced;
static int64_t g_ords_next_attempt_ms;
static uint32_t g_ords_failure_backoff_ms = ZONE_LITE_ORDS_FAILURE_BACKOFF_INITIAL_MS;
static bool g_truth_reconcile_warning;
static int g_daily_zkt_reboot_completed_day = -1;
static int64_t g_daily_zkt_reboot_last_attempt_ms;
static int64_t g_last_full_scan_ms;
static int64_t g_flap_window_started_ms;
static int64_t g_session_stable_since_ms;
static add_zkt_telemetry_t g_add_zkt;
static bool g_temp_admin_active;
static uint16_t g_temp_admin_uid;
static int64_t g_temp_admin_expires_epoch;

static bool oracle_send_reconcile(
    const attendance_event_t *events,
    size_t event_count,
    int year,
    int month,
    size_t *truth_count_out);

static int64_t uptime_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static int64_t epoch_now(void)
{
    time_t now = 0;
    time(&now);
    return (int64_t)now;
}

static void nvs_save_runtime_state(void)
{
    nvs_handle_t handle;
    if (nvs_open("zone_lite", NVS_READWRITE, &handle) != ESP_OK) {
        return;
    }
    (void)nvs_set_u32(handle, "zkt_ip", g_last_authenticated_zkt_ip);
    (void)nvs_set_i32(handle, "attn_count", g_last_synced_attendance_count);
    (void)nvs_set_i64(handle, "truth_epoch", g_last_full_truth_reconcile_epoch);
    (void)nvs_set_i32(handle, "restart_slot", g_daily_zkt_reboot_completed_day);
    (void)nvs_set_u8(handle, "lease_active", g_temp_admin_active ? 1 : 0);
    (void)nvs_set_u16(handle, "lease_uid", g_temp_admin_uid);
    (void)nvs_set_i64(handle, "lease_exp", g_temp_admin_expires_epoch);
    (void)nvs_commit(handle);
    nvs_close(handle);
}

static void nvs_load_runtime_state(void)
{
    nvs_handle_t handle;
    if (nvs_open("zone_lite", NVS_READONLY, &handle) != ESP_OK) {
        return;
    }
    uint8_t active = 0;
    int32_t restart_slot = -1;
    (void)nvs_get_u32(handle, "zkt_ip", &g_last_authenticated_zkt_ip);
    (void)nvs_get_i32(handle, "attn_count", &g_last_synced_attendance_count);
    (void)nvs_get_i64(handle, "truth_epoch", &g_last_full_truth_reconcile_epoch);
    if (nvs_get_i32(handle, "restart_slot", &restart_slot) == ESP_OK) {
        g_daily_zkt_reboot_completed_day = (int)restart_slot;
    }
    (void)nvs_get_u8(handle, "lease_active", &active);
    (void)nvs_get_u16(handle, "lease_uid", &g_temp_admin_uid);
    (void)nvs_get_i64(handle, "lease_exp", &g_temp_admin_expires_epoch);
    g_temp_admin_active = active != 0;
    nvs_close(handle);
}

static void zkt_publish_state(const char *state, const char *reason, bool online)
{
    strlcpy(g_add_zkt.connection_state, state, sizeof(g_add_zkt.connection_state));
    strlcpy(g_add_zkt.transition_reason, reason ? reason : "", sizeof(g_add_zkt.transition_reason));
    g_add_zkt.online = online;
    add_connector_set_zkt(&g_add_zkt);
    add_connector_set_activity(state);
}

static void zkt_count_transition(void)
{
    int64_t now_ms = uptime_ms();
    if (g_flap_window_started_ms == 0 || now_ms - g_flap_window_started_ms > ZONE_LITE_FLAP_WINDOW_MS) {
        g_flap_window_started_ms = now_ms;
        g_add_zkt.flap_count_15m = 0;
    }
    g_add_zkt.flap_count_15m++;
}

static uint32_t zkt_failure_backoff_ms(void)
{
    uint32_t shift = g_add_zkt.consecutive_failures > 6 ? 6 : g_add_zkt.consecutive_failures;
    uint32_t value = ZONE_LITE_DISCOVERY_RETRY_DELAY_MS << (shift > 0 ? shift - 1 : 0);
    if (value > ZONE_LITE_ZKT_BACKOFF_MAX_MS) value = ZONE_LITE_ZKT_BACKOFF_MAX_MS;
    value += esp_random() % 5000;
    if (g_add_zkt.flap_count_15m >= ZONE_LITE_FLAP_THRESHOLD && value < ZONE_LITE_FLAP_QUIET_MS) {
        value = ZONE_LITE_FLAP_QUIET_MS + (esp_random() % 30000);
    }
    return value;
}

static uint32_t zkt_mark_failure(const char *reason)
{
    bool was_online = g_add_zkt.online || strcmp(g_add_zkt.connection_state, "RECOVERING") == 0;
    if (was_online) zkt_count_transition();
    g_add_zkt.online = false;
    g_add_zkt.consecutive_failures++;
    g_add_zkt.consecutive_successes = 0;
    uint32_t backoff = zkt_failure_backoff_ms();
    int64_t epoch = epoch_now();
    g_add_zkt.backoff_until_epoch = epoch > 1700000000 ? epoch + (backoff / 1000) : 0;
    if (g_add_zkt.flap_count_15m >= ZONE_LITE_FLAP_THRESHOLD) {
        zkt_publish_state("FLAPPING", reason, false);
        led_status_set(LED_STATUS_ZKT_FLAPPING);
    } else if (g_add_zkt.consecutive_failures == 1) {
        zkt_publish_state("SUSPECT", reason, false);
    } else {
        zkt_publish_state("RETRY_WAIT", reason, false);
    }
    add_connector_log("WARN", "zkt", "ZKT_CONNECTION_LOST", reason);
    return backoff;
}

static void zkt_mark_authenticated(uint32_t ip, const char *reason)
{
    if (!g_add_zkt.online) zkt_count_transition();
    g_add_zkt.consecutive_successes++;
    g_add_zkt.consecutive_failures = 0;
    g_add_zkt.backoff_until_epoch = 0;
    g_session_stable_since_ms = uptime_ms();
    g_add_zkt.stability_since_epoch = epoch_now();
    bool authenticated_ip_changed = g_last_authenticated_zkt_ip != ip;
    g_last_authenticated_zkt_ip = ip;
    if (authenticated_ip_changed) {
        nvs_save_runtime_state();
    }
    zkt_publish_state("RECOVERING", reason, true);
}

static void zkt_mark_stable(void)
{
    if (strcmp(g_add_zkt.connection_state, "ONLINE") == 0) return;
    g_add_zkt.consecutive_successes = g_add_zkt.consecutive_successes < 3 ? 3 : g_add_zkt.consecutive_successes;
    zkt_publish_state("ONLINE", "stable authenticated session", true);
    add_connector_log("INFO", "zkt", "ZKT_STABLE", "ZKT session passed the recovery stability window");
}

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
        (void)zk_send_command(sock, ctx, CMD_FREE_DATA, NULL, 0, rx, 8192, &response);
        free(rx);
        return false;
    }

    uint32_t offset = 0;
    // The MB40 can intermittently spend more than a minute preparing a
    // flash-backed chunk; the authenticated socket deadline accounts for it.
    const uint32_t max_chunk = ZKT_BUFFER_CHUNK_BYTES;
    while (offset < size) {
        uint32_t want = size - offset > max_chunk ? max_chunk : size - offset;
        uint8_t chunk_payload[8];
        write_le32(chunk_payload, offset);
        write_le32(chunk_payload + 4, want);
        uint8_t *chunk_rx = malloc(want + 64);
        if (chunk_rx == NULL) {
            (void)zk_send_command(sock, ctx, CMD_FREE_DATA, NULL, 0, rx, 8192, &response);
            free(buffer);
            free(rx);
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
            // A prepared ZKT read is terminal-side state. Always release it
            // before abandoning a partial transfer so subsequent sessions do
            // not inherit an exhausted or wedged device buffer.
            (void)zk_send_command(sock, ctx, CMD_FREE_DATA, NULL, 0, rx, 8192, &response);
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

static bool zk_protocol_restart(int sock, zk_context_t *ctx)
{
    uint8_t rx[128];
    zk_response_t response = {0};
    bool ok = zk_send_command(sock, ctx, CMD_RESTART, NULL, 0, rx, sizeof(rx), &response) &&
              response.code == CMD_ACK_OK;
    ESP_LOGW(TAG, "ZKT protocol restart response=%u ok=%s", response.code, ok ? "true" : "false");
    return ok;
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
        "Attempting ZKT telnet OS reboot on %s:%d",
        ip_text,
        ZONE_LITE_ZKT_TELNET_PORT);

    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_TELNET_PORT, ZKT_TELNET_IO_TIMEOUT_MS, &sock)) {
        ESP_LOGW(TAG, "Could not connect to ZKT telnet reboot target %s:%d", ip_text, ZONE_LITE_ZKT_TELNET_PORT);
        return false;
    }

    char text[ZKT_TELNET_BUFFER_SIZE];
    bool ok = false;
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (!text_contains_ci(text, "login:")) {
        ESP_LOGW(TAG, "ZKT telnet reboot target %s did not show a login prompt", ip_text);
        goto done;
    }
    if (ZONE_LITE_ZKT_TELNET_EXPECT_BANNER[0] != '\0' &&
        !text_contains_ci(text, ZONE_LITE_ZKT_TELNET_EXPECT_BANNER)) {
        ESP_LOGW(TAG, "ZKT telnet reboot target %s did not match the expected banner", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_USERNAME)) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (!text_contains_ci(text, "password:")) {
        ESP_LOGW(TAG, "ZKT telnet reboot target %s did not ask for a password", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_PASSWORD)) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), 2000);
    if (text_contains_ci(text, "login incorrect")) {
        ESP_LOGW(TAG, "ZKT telnet reboot login failed for %s", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, "id")) {
        goto done;
    }
    telnet_read_text(sock, text, sizeof(text), ZKT_TELNET_IO_TIMEOUT_MS);
    if (text_contains_ci(text, "login incorrect") || !text_contains_ci(text, "uid=")) {
        ESP_LOGW(TAG, "ZKT telnet reboot could not confirm a shell on %s", ip_text);
        goto done;
    }

    if (!telnet_send_line(sock, "sync")) {
        goto done;
    }
    vTaskDelay(pdMS_TO_TICKS(300));
    if (!telnet_send_line(sock, ZONE_LITE_ZKT_TELNET_REBOOT_COMMAND)) {
        goto done;
    }
    ESP_LOGW(TAG, "ZKT telnet reboot command sent to %s", ip_text);
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
    // Never issue an OS-level recovery command to a merely open TCP candidate.
    // Only an IP that previously completed ZKT authentication is eligible.
    if (g_last_authenticated_zkt_ip != 0) return g_last_authenticated_zkt_ip;
    uint32_t preferred = configured_preferred_zkt_ip();
    return preferred != 0 && preferred == g_last_authenticated_zkt_ip ? preferred : 0;
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

static bool append_fingerprint_field(
    uint8_t *material,
    size_t capacity,
    size_t *length,
    uint8_t tag,
    const uint8_t *value,
    size_t value_length)
{
    if (!material || !length || !value || value_length > 255 ||
        *length + 2 + value_length > capacity) {
        return false;
    }
    material[(*length)++] = tag;
    material[(*length)++] = (uint8_t)value_length;
    memcpy(material + *length, value, value_length);
    *length += value_length;
    return true;
}

static void bytes_to_hex(const uint8_t *value, size_t length, char *out, size_t out_size)
{
    if (!value || !out || out_size < (length * 2) + 1) return;
    for (size_t index = 0; index < length; index++) {
        snprintf(out + index * 2, 3, "%02x", value[index]);
    }
    out[length * 2] = '\0';
}

static bool keyed_terminal_fingerprint(
    const char *domain,
    const uint8_t *material,
    size_t material_length,
    char out[65])
{
    const zone_config_t *runtime = zone_config_get();
    if (!runtime || !runtime->bootstrap_secret[0] || !domain || !material || !out) {
        if (out) out[0] = '\0';
        return false;
    }
    uint8_t input[192];
    size_t domain_length = strlen(domain);
    if (domain_length + 1 + material_length > sizeof(input)) {
        out[0] = '\0';
        return false;
    }
    memcpy(input, domain, domain_length);
    input[domain_length] = 0;
    memcpy(input + domain_length + 1, material, material_length);
    uint8_t digest[32];
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    int result = mbedtls_md_hmac(
        md,
        (const unsigned char *)runtime->bootstrap_secret,
        strlen(runtime->bootstrap_secret),
        input,
        domain_length + 1 + material_length,
        digest);
    if (result != 0) {
        out[0] = '\0';
        return false;
    }
    bytes_to_hex(digest, sizeof(digest), out, 65);
    return true;
}

static void set_terminal_user_fingerprints(
    zkt_user_t *user,
    const uint8_t *record,
    uint8_t record_size)
{
    if (!user || !record) return;
    uint8_t identity[96];
    size_t identity_length = 0;
    const uint8_t *user_id = record_size == 72 ? record + 48 : record + 24;
    size_t user_id_length = record_size == 72
        ? strnlen((const char *)user_id, 24)
        : 4;
    const uint8_t *card = record_size == 72 ? record + 35 : record + 16;
    if (!append_fingerprint_field(
            identity,
            sizeof(identity),
            &identity_length,
            1,
            &record_size,
            1) ||
        !append_fingerprint_field(
            identity,
            sizeof(identity),
            &identity_length,
            2,
            record,
            2) ||
        !append_fingerprint_field(
            identity,
            sizeof(identity),
            &identity_length,
            3,
            user_id,
            user_id_length) ||
        !append_fingerprint_field(
            identity,
            sizeof(identity),
            &identity_length,
            4,
            card,
            4)) {
        return;
    }
    (void)keyed_terminal_fingerprint(
        "ZONE-LITE-ZKT-USER-IDENTITY-V1",
        identity,
        identity_length,
        user->terminal_identity_fingerprint);

    uint8_t state[160];
    memcpy(state, identity, identity_length);
    size_t state_length = identity_length;
    const uint8_t *name = record_size == 72 ? record + 11 : record + 8;
    size_t name_limit = record_size == 72 ? 24 : 8;
    size_t name_length = strnlen((const char *)name, name_limit);
    if (!append_fingerprint_field(
            state,
            sizeof(state),
            &state_length,
            5,
            record + 2,
            1) ||
        !append_fingerprint_field(
            state,
            sizeof(state),
            &state_length,
            6,
            name,
            name_length)) {
        user->terminal_state_fingerprint[0] = '\0';
        return;
    }
    (void)keyed_terminal_fingerprint(
        "ZONE-LITE-ZKT-USER-STATE-V1",
        state,
        state_length,
        user->terminal_state_fingerprint);
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
    users->complete = true;
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
    users->complete = parsed_users <= MAX_USERS;
    if (!users->complete) {
        ESP_LOGE(
            TAG,
            "ZKT user snapshot truncated parsed=%lu capacity=%u; all writes disabled",
            (unsigned long)parsed_users,
            (unsigned)MAX_USERS);
        add_connector_log(
            "CRITICAL",
            "users",
            "USER_SNAPSHOT_TRUNCATED",
            "The complete ZKT user table did not fit safely; user writes are disabled.");
    }
    users->record_size = (uint8_t)packet_size;
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
            u->privilege = p[2];
            copy_zk_string(u->password, sizeof(u->password), p + 3, 5);
            copy_zk_string(u->name, sizeof(u->name), p + 8, 8);
            u->card = read_le32(p + 16);
            snprintf(u->group_id, sizeof(u->group_id), "%u", p[21]);
            snprintf(u->user_id, sizeof(u->user_id), "%lu", (unsigned long)read_le32(p + 24));
            u->record_size = 28;
            set_terminal_user_fingerprints(u, p, 28);
            parse_machine_identity(u);
            p += 28;
            remain -= 28;
        } else if (packet_size == 72 && remain >= 72) {
            zkt_user_t *u = &users->rows[users->count++];
            snprintf(u->uid, sizeof(u->uid), "%u", read_le16(p));
            u->privilege = p[2];
            copy_zk_string(u->password, sizeof(u->password), p + 3, 8);
            copy_zk_string(u->name, sizeof(u->name), p + 11, 24);
            u->card = read_le32(p + 35);
            copy_zk_string(u->group_id, sizeof(u->group_id), p + 40, 7);
            copy_zk_string(u->user_id, sizeof(u->user_id), p + 48, 24);
            u->record_size = 72;
            set_terminal_user_fingerprints(u, p, 72);
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
    user_table_t *updated = heap_caps_calloc(1, sizeof(user_table_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (updated == NULL) updated = calloc(1, sizeof(user_table_t));
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

static zkt_user_t *find_mutable_user_by_uid(user_table_t *users, const char *uid)
{
    for (size_t i = 0; i < users->count; i++) {
        if (strcmp(users->rows[i].uid, uid) == 0) return &users->rows[i];
    }
    return NULL;
}

static bool zk_write_user(
    int sock,
    zk_context_t *ctx,
    zkt_user_t *user,
    const char *new_name,
    int new_privilege)
{
    if (!user || (new_privilege != 0 && new_privilege != 14)) return false;
    const char *name = new_name && new_name[0] ? new_name : user->name;
    size_t name_limit = user->record_size == 28 ? 8 : 24;
    if (strlen(name) > name_limit) {
        ESP_LOGW(TAG, "Refusing user write: name is too wide for %u-byte record", user->record_size);
        return false;
    }
    uint8_t packet[72] = {0};
    uint16_t uid = (uint16_t)strtoul(user->uid, NULL, 10);
    write_le16(packet, uid);
    packet[2] = (uint8_t)new_privilege;
    if (user->record_size == 28) {
        memcpy(packet + 3, user->password, strnlen(user->password, 5));
        memcpy(packet + 8, name, strlen(name));
        write_le32(packet + 16, user->card);
        packet[21] = (uint8_t)strtoul(user->group_id, NULL, 10);
        write_le32(packet + 24, (uint32_t)strtoul(user->user_id, NULL, 10));
    } else if (user->record_size == 72) {
        memcpy(packet + 3, user->password, strnlen(user->password, 8));
        memcpy(packet + 11, name, strlen(name));
        write_le32(packet + 35, user->card);
        memcpy(packet + 40, user->group_id, strnlen(user->group_id, 7));
        memcpy(packet + 48, user->user_id, strnlen(user->user_id, 24));
    } else {
        return false;
    }
    uint8_t rx[1024];
    zk_response_t response = {0};
    if (!zk_send_command(
            sock,
            ctx,
            CMD_USER_WRQ,
            packet,
            user->record_size,
            rx,
            sizeof(rx),
            &response) || response.code != CMD_ACK_OK) {
        ESP_LOGW(TAG, "ZKT user write failed uid=%s response=%u", user->uid, response.code);
        return false;
    }
    user->privilege = (uint8_t)new_privilege;
    strlcpy(user->name, name, sizeof(user->name));
    parse_machine_identity(user);
    return true;
}

static bool zk_delete_user(int sock, zk_context_t *ctx, uint16_t uid)
{
    uint8_t packet[2];
    write_le16(packet, uid);
    uint8_t rx[1024];
    zk_response_t response = {0};
    bool ok = zk_send_command(
                  sock,
                  ctx,
                  CMD_DELETE_USER,
                  packet,
                  sizeof(packet),
                  rx,
                  sizeof(rx),
                  &response) &&
              response.code == CMD_ACK_OK;
    if (!ok) {
        ESP_LOGW(TAG, "ZKT user delete failed uid=%u response=%u", uid, response.code);
    }
    return ok;
}

static bool user_matches_command(const zkt_user_t *user, const add_command_t *command)
{
    if (!user || !command) return false;
    if (command->uid[0] && strcmp(user->uid, command->uid) != 0) return false;
    if (command->has_expected_terminal_identity_fingerprint) {
        if (strcmp(
                user->terminal_identity_fingerprint,
                command->expected_terminal_identity_fingerprint) != 0) {
            return false;
        }
    } else if (command->user_id[0] && strcmp(user->user_id, command->user_id) != 0) {
        return false;
    }
    if (command->has_name && strcmp(user->name, command->name) != 0) return false;
    if (command->has_privilege && user->privilege != command->privilege) return false;
    return true;
}

static bool user_matches_expected_state(const zkt_user_t *user, const add_command_t *command)
{
    if (!user || !command) return false;
    if (command->uid[0] && strcmp(user->uid, command->uid) != 0) return false;
    if (command->has_expected_terminal_state_fingerprint) {
        return strcmp(
                   user->terminal_state_fingerprint,
                   command->expected_terminal_state_fingerprint) == 0;
    }
    if (command->has_expected_terminal_identity_fingerprint) {
        if (strcmp(
                user->terminal_identity_fingerprint,
                command->expected_terminal_identity_fingerprint) != 0) {
            return false;
        }
    } else if (command->user_id[0] && strcmp(user->user_id, command->user_id) != 0) {
        return false;
    }
    if (command->has_expected_name && strcmp(user->name, command->expected_name) != 0) {
        return false;
    }
    if (command->has_expected_privilege &&
        user->privilege != command->expected_privilege) {
        return false;
    }
    return true;
}

static bool command_error_is_retryable(const char *error_code)
{
    if (!error_code) return false;
    return strcmp(error_code, "ZKT_USER_READ_FAILED") == 0 ||
           strcmp(error_code, "ZKT_USER_CREATE_FAILED") == 0 ||
           strcmp(error_code, "ZKT_USER_DELETE_VERIFY_FAILED") == 0 ||
           strcmp(error_code, "ZKT_USER_WRITE_FAILED") == 0 ||
           strcmp(error_code, "ZKT_USER_REREAD_FAILED") == 0 ||
           strcmp(error_code, "ZKT_USER_POSTCONDITION_FAILED") == 0 ||
           strcmp(error_code, "ZKT_RESTART_FAILED") == 0 ||
           strcmp(error_code, "ADD_SNAPSHOT_SEND_FAILED") == 0 ||
           strcmp(error_code, "TRUSTED_TIME_UNAVAILABLE") == 0 ||
           strcmp(error_code, "IDENTITY_TOMBSTONE_PERSIST_FAILED") == 0;
}

static void iso_system_now(char out[32])
{
    time_t now = 0;
    time(&now);
    struct tm value = {0};
    gmtime_r(&now, &value);
    strftime(out, 32, "%Y-%m-%dT%H:%M:%SZ", &value);
}

static size_t valid_utf8_sequence_length(const unsigned char *value, size_t remaining)
{
    if (remaining == 0) return 0;
    unsigned char first = value[0];
    if (first <= 0x7f) return 1;
    if (first >= 0xc2 && first <= 0xdf && remaining >= 2 &&
        value[1] >= 0x80 && value[1] <= 0xbf) {
        return 2;
    }
    if (remaining >= 3 && value[2] >= 0x80 && value[2] <= 0xbf) {
        if (first == 0xe0 && value[1] >= 0xa0 && value[1] <= 0xbf) return 3;
        if (first >= 0xe1 && first <= 0xec && value[1] >= 0x80 && value[1] <= 0xbf) return 3;
        if (first == 0xed && value[1] >= 0x80 && value[1] <= 0x9f) return 3;
        if (first >= 0xee && first <= 0xef && value[1] >= 0x80 && value[1] <= 0xbf) return 3;
    }
    if (remaining >= 4 && value[2] >= 0x80 && value[2] <= 0xbf &&
        value[3] >= 0x80 && value[3] <= 0xbf) {
        if (first == 0xf0 && value[1] >= 0x90 && value[1] <= 0xbf) return 4;
        if (first >= 0xf1 && first <= 0xf3 && value[1] >= 0x80 && value[1] <= 0xbf) return 4;
        if (first == 0xf4 && value[1] >= 0x80 && value[1] <= 0x8f) return 4;
    }
    return 0;
}

static bool json_add_utf8_string(cJSON *object, const char *key, const char *value)
{
    if (!value) value = "";
    size_t input_len = strlen(value);
    char *safe = malloc(input_len + 1);
    if (!safe) {
        cJSON_AddStringToObject(object, key, "");
        return true;
    }
    const unsigned char *input = (const unsigned char *)value;
    size_t read_at = 0;
    size_t write_at = 0;
    bool changed = false;
    while (read_at < input_len) {
        if (input[read_at] < 0x20 || input[read_at] == 0x7f) {
            safe[write_at++] = '?';
            read_at++;
            changed = true;
            continue;
        }
        size_t sequence = valid_utf8_sequence_length(input + read_at, input_len - read_at);
        if (sequence == 0) {
            safe[write_at++] = '?';
            read_at++;
            changed = true;
            continue;
        }
        memcpy(safe + write_at, input + read_at, sequence);
        write_at += sequence;
        read_at += sequence;
    }
    safe[write_at] = '\0';
    cJSON_AddStringToObject(object, key, safe);
    free(safe);
    return changed;
}

static bool add_send_user_snapshot(const user_table_t *users)
{
    cJSON *payload = cJSON_CreateObject();
    if (!payload) return false;
    char snapshot_id[80];
    snprintf(
        snapshot_id,
        sizeof(snapshot_id),
        "snapshot-%08lx-%08lx",
        (unsigned long)epoch_now(),
        (unsigned long)esp_random());
    char observed[32];
    iso_system_now(observed);
    cJSON_AddStringToObject(payload, "snapshot_id", snapshot_id);
    cJSON_AddBoolToObject(payload, "complete", users->complete);
    cJSON_AddStringToObject(payload, "observed_at", observed);
    cJSON *rows = cJSON_AddArrayToObject(payload, "users");
    size_t sanitized_fields = 0;
    for (size_t i = 0; i < users->count; i++) {
        const zkt_user_t *user = &users->rows[i];
        cJSON *row = cJSON_CreateObject();
        sanitized_fields += json_add_utf8_string(row, "uid", user->uid) ? 1 : 0;
        sanitized_fields += json_add_utf8_string(row, "user_id", user->user_id) ? 1 : 0;
        sanitized_fields += json_add_utf8_string(row, "name", user->name) ? 1 : 0;
        if (user->terminal_identity_fingerprint[0]) {
            cJSON_AddStringToObject(
                row,
                "terminal_identity_fingerprint",
                user->terminal_identity_fingerprint);
        }
        if (user->terminal_state_fingerprint[0]) {
            cJSON_AddStringToObject(
                row,
                "terminal_state_fingerprint",
                user->terminal_state_fingerprint);
        }
        cJSON_AddNumberToObject(row, "privilege", user->privilege);
        cJSON_AddNumberToObject(row, "card", user->card);
        cJSON_AddItemToArray(rows, row);
    }
    char *json = cJSON_PrintUnformatted(payload);
    cJSON_Delete(payload);
    bool ok = json && add_connector_send_payload("user_snapshot", json);
    ESP_LOGI(
        TAG,
        "ADD user snapshot users=%u bytes=%u sanitized_fields=%u sent=%s",
        (unsigned)users->count,
        json ? (unsigned)strlen(json) : 0,
        (unsigned)sanitized_fields,
        ok ? "true" : "false");
    free(json);
    return ok;
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

static bool append_line(const char *path, const char *line)
{
    FILE *f = fopen(path, "a");
    if (f == NULL) {
        ESP_LOGE(TAG, "Could not open %s for append", path);
        return false;
    }
    bool ok = fputs(line, f) >= 0 && fputc('\n', f) != EOF && fflush(f) == 0;
    if (ok && fsync(fileno(f)) != 0) ok = false;
    if (fclose(f) != 0) ok = false;
    if (!ok) ESP_LOGE(TAG, "Durable append failed for %s errno=%d", path, errno);
    return ok;
}

static bool append_line_to_open_file(FILE *f, const char *path, const char *line)
{
    if (f != NULL) {
        return fputs(line, f) >= 0 && fputc('\n', f) != EOF;
    }
    return append_line(path, line);
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

static bool json_event_has_valid_identity_and_no_block_reason(const char *event_json)
{
    cJSON *root = cJSON_Parse(event_json);
    if (!root || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return false;
    }
    cJSON *cnic = cJSON_GetObjectItemCaseSensitive(root, "cnic");
    cJSON *user_id = cJSON_GetObjectItemCaseSensitive(root, "user_id");
    cJSON *blocked_reason = cJSON_GetObjectItemCaseSensitive(root, "blocked_reason");
    bool valid = cJSON_IsString(cnic) && strlen(cnic->valuestring) == 13 &&
                 cJSON_IsString(user_id) && user_id->valuestring[0] != '\0' &&
                 blocked_reason == NULL;
    for (size_t i = 0; valid && i < 13; i++) {
        valid = isdigit((unsigned char)cnic->valuestring[i]) != 0;
    }
    cJSON_Delete(root);
    return valid;
}

static void recover_valid_unclassified_blocked_events(void)
{
    FILE *in = fopen(BLOCKED_PATH, "r");
    if (!in) return;
    FILE *kept = fopen(BLOCKED_RECOVERY_TMP_PATH, "w");
    FILE *pending = fopen(PENDING_PATH, "a");
    if (!kept || !pending) {
        if (kept) fclose(kept);
        if (pending) fclose(pending);
        fclose(in);
        (void)remove(BLOCKED_RECOVERY_TMP_PATH);
        ESP_LOGE(TAG, "Could not open attendance outboxes for blocked-event recovery");
        return;
    }

    bool ok = true;
    size_t recovered = 0;
    char line[MAX_EVENT_JSON];
    while (fgets(line, sizeof(line), in) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0') continue;
        FILE *destination = json_event_has_valid_identity_and_no_block_reason(line) ? pending : kept;
        if (fprintf(destination, "%s\n", line) < 0) {
            ok = false;
            break;
        }
        if (destination == pending) recovered++;
    }
    if (ferror(in)) ok = false;
    if (fflush(kept) != 0 || fsync(fileno(kept)) != 0) ok = false;
    if (fflush(pending) != 0 || fsync(fileno(pending)) != 0) ok = false;
    fclose(in);
    fclose(kept);
    fclose(pending);

    if (!ok) {
        (void)remove(BLOCKED_RECOVERY_TMP_PATH);
        ESP_LOGE(TAG, "Blocked-event recovery was interrupted; original rows remain preserved");
        return;
    }
    (void)remove(BLOCKED_RECOVERY_BACKUP_PATH);
    if (rename(BLOCKED_PATH, BLOCKED_RECOVERY_BACKUP_PATH) != 0 ||
        rename(BLOCKED_RECOVERY_TMP_PATH, BLOCKED_PATH) != 0) {
        (void)rename(BLOCKED_RECOVERY_BACKUP_PATH, BLOCKED_PATH);
        (void)remove(BLOCKED_RECOVERY_TMP_PATH);
        ESP_LOGE(TAG, "Could not commit blocked-event recovery; backup remains preserved");
        return;
    }
    (void)remove(BLOCKED_RECOVERY_BACKUP_PATH);
    if (recovered > 0) {
        ESP_LOGW(TAG, "Recovered %u valid event(s) from the legacy blocked outbox", (unsigned)recovered);
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
        .max_files = 16,
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
    recover_valid_unclassified_blocked_events();
    load_seen_from_file(PENDING_PATH);
    load_seen_from_file(BLOCKED_PATH);
    load_seen_from_file(ACKED_PATH);
    led_status_set_backlog(file_has_nonempty_line(PENDING_PATH));
    ESP_LOGI(TAG, "Storage ready; loaded %u known event UIDs", (unsigned)g_seen_count);
}

static const char *oracle_capture_type(const char *capturetype)
{
    if (capturetype == NULL) return "MANUAL_REPROCESS";
    if (strcmp(capturetype, "LIVE") == 0 || strcmp(capturetype, "LIVE_POLL") == 0 ||
        strcmp(capturetype, "DUMP_RECONNECT") == 0 || strcmp(capturetype, "DUMP_STARTUP") == 0 ||
        strcmp(capturetype, "MANUAL_REPROCESS") == 0) {
        return capturetype;
    }
    if (strcmp(capturetype, "RECONCILE_15M") == 0) {
        return "DUMP_RECONNECT";
    }
    return "MANUAL_REPROCESS";
}

static const char *oracle_trust_status(const char *capturetype)
{
    const char *normalized = oracle_capture_type(capturetype);
    return strcmp(normalized, "LIVE") == 0 || strcmp(normalized, "LIVE_POLL") == 0
        ? "TRUSTED_LIVE"
        : "BACKFILL_ACCEPTED_CLOCK_OK";
}

static char *event_to_json(const attendance_event_t *event, const char *capturetype)
{
    const char *normalized_capturetype = oracle_capture_type(capturetype);
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "event_uid", event->event_uid);
    cJSON_AddStringToObject(root, "zone_id", ZONE_LITE_ZONE_ID);
    cJSON_AddStringToObject(root, "device_id", ZONE_LITE_ZONE_DEVICE_ID);
    json_add_utf8_string(root, "device_serial", g_device_serial[0] ? g_device_serial : "unknown");
    json_add_utf8_string(root, "user_id", event->user_id);
    json_add_utf8_string(root, "employee_name", event->employee_name);
    cJSON_AddStringToObject(root, "cnic", event->cnic);
    cJSON_AddStringToObject(root, "timestamp", event->timestamp);
    cJSON_AddStringToObject(root, "clockdiff", "0.0");
    cJSON_AddStringToObject(root, "capturetype", normalized_capturetype);
    cJSON_AddStringToObject(root, "trust_status", oracle_trust_status(normalized_capturetype));
    cJSON_AddStringToObject(root, "raw_punch", event->raw_punch ? "T" : "F");
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}

typedef enum {
    ENQUEUE_DUPLICATE = 0,
    ENQUEUE_PENDING,
    ENQUEUE_BLOCKED,
    ENQUEUE_STORAGE_ERROR,
} enqueue_result_t;

static cJSON *add_attendance_json_row(const attendance_event_t *event, const char *capturetype)
{
    cJSON *row = cJSON_CreateObject();
    if (!row) return NULL;
    cJSON_AddStringToObject(row, "event_uid", event->event_uid);
    json_add_utf8_string(row, "user_id", event->user_id);
    char raw_name[128];
    if (event->cnic[0]) {
        snprintf(raw_name, sizeof(raw_name), "%s%s-%s", event->employee_name, event->raw_punch ? "-S" : "", event->cnic);
        json_add_utf8_string(row, "raw_name", raw_name);
    } else if (event->employee_name[0]) {
        json_add_utf8_string(row, "raw_name", event->employee_name);
    }
    cJSON_AddStringToObject(row, "device_event_time", event->timestamp);
    char captured[32];
    iso_system_now(captured);
    cJSON_AddStringToObject(row, "captured_at", captured);
    cJSON_AddStringToObject(row, "source", capturetype);
    cJSON_AddNumberToObject(row, "status", event->status);
    cJSON_AddNumberToObject(row, "punch", event->punch);
    cJSON_AddBoolToObject(row, "raw_punch", event->raw_punch);
    cJSON_AddStringToObject(row, "clock_quality", "OK");
    cJSON_AddItemToObject(row, "raw_event", cJSON_CreateObject());
    return row;
}

static char *add_serialize_attendance_events(cJSON *events, const char *batch_id)
{
    if (!events) return NULL;
    cJSON *payload = cJSON_CreateObject();
    if (!payload) {
        cJSON_Delete(events);
        return NULL;
    }
    cJSON_AddStringToObject(payload, "batch_id", batch_id);
    cJSON_AddItemToObject(payload, "events", events);
    char *json = cJSON_PrintUnformatted(payload);
    cJSON_Delete(payload);
    if (!json) return NULL;

    // Reconcile can retain hundreds of batches until the ZKT dump and the
    // primary attendance-file transaction are complete.  Keep that bounded
    // backlog in PSRAM so live capture retains ample internal heap.
    size_t json_size = strlen(json) + 1;
    char *psram_json = heap_caps_malloc(json_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (psram_json) {
        memcpy(psram_json, json, json_size);
        free(json);
        return psram_json;
    }
    return json;
}

static bool add_enqueue_attendance_events(cJSON *events, const char *batch_id)
{
    char *json = add_serialize_attendance_events(events, batch_id);
    bool ok = json && add_connector_enqueue_attendance(json);
    free(json);
    return ok;
}

static bool add_send_attendance_event(const attendance_event_t *event, const char *capturetype)
{
    cJSON *events = cJSON_CreateArray();
    cJSON *row = add_attendance_json_row(event, capturetype);
    if (!events || !row) {
        cJSON_Delete(events);
        cJSON_Delete(row);
        return false;
    }
    cJSON_AddItemToArray(events, row);
    return add_enqueue_attendance_events(events, event->event_uid);
}

static void free_reconcile_payloads(char **payloads, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        free(payloads[i]);
        payloads[i] = NULL;
    }
}

static bool flush_add_reconcile_payloads(char **payloads, size_t *count)
{
    if (*count == 0) {
        return true;
    }
    size_t queued = *count;
    bool ok = add_connector_enqueue_attendance_bulk((const char *const *)payloads, queued);
    free_reconcile_payloads(payloads, queued);
    *count = 0;
    return ok;
}

static bool add_enqueue_reconcile_events(
    const attendance_event_t *events,
    size_t event_count,
    const char *capturetype)
{
    char *payloads[ADD_RECONCILE_COMMIT_BATCHES] = {0};
    size_t payload_count = 0;
    size_t total_batches = 0;
    size_t batch_count = 0;
    char batch_id[80] = {0};
    cJSON *batch_events = NULL;

    for (size_t i = 0; i < event_count; i++) {
        if (batch_events == NULL) {
            batch_events = cJSON_CreateArray();
            snprintf(batch_id, sizeof(batch_id), "truth-%s", events[i].event_uid);
        }
        cJSON *row = add_attendance_json_row(&events[i], capturetype);
        if (batch_events == NULL || row == NULL) {
            cJSON_Delete(row);
            cJSON_Delete(batch_events);
            free_reconcile_payloads(payloads, payload_count);
            ESP_LOGE(TAG, "Could not serialize a bounded ADD reconcile batch");
            return false;
        }
        cJSON_AddItemToArray(batch_events, row);
        batch_count++;
        if (batch_count < ADD_RECONCILE_BATCH_EVENTS && i + 1 < event_count) {
            continue;
        }

        char *batch_json = add_serialize_attendance_events(batch_events, batch_id);
        batch_events = NULL;
        batch_count = 0;
        batch_id[0] = '\0';
        if (batch_json == NULL) {
            free_reconcile_payloads(payloads, payload_count);
            ESP_LOGE(TAG, "Could not serialize a bounded ADD reconcile payload");
            return false;
        }
        payloads[payload_count++] = batch_json;
        total_batches++;
        if (payload_count == ADD_RECONCILE_COMMIT_BATCHES &&
            !flush_add_reconcile_payloads(payloads, &payload_count)) {
            ESP_LOGE(TAG, "Could not durably append an ADD reconcile payload chunk");
            return false;
        }
        if ((total_batches % ADD_RECONCILE_COMMIT_BATCHES) == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }

    if (!flush_add_reconcile_payloads(payloads, &payload_count)) {
        ESP_LOGE(TAG, "Could not durably append the final ADD reconcile payload chunk");
        return false;
    }
    ESP_LOGI(
        TAG,
        "ADD reconcile enqueue complete events=%u batches=%u commit_chunk=%u",
        (unsigned)event_count,
        (unsigned)total_batches,
        (unsigned)ADD_RECONCILE_COMMIT_BATCHES);
    return true;
}

static enqueue_result_t enqueue_event_to_files(
    const attendance_event_t *event,
    const char *capturetype,
    FILE *pending_file,
    FILE *blocked_file,
    bool publish_add)
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
        if (!append_line_to_open_file(blocked_file, BLOCKED_PATH, json)) {
            free(json);
            led_status_fault(LED_STATUS_FATAL);
            return ENQUEUE_STORAGE_ERROR;
        }
        led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        result = ENQUEUE_BLOCKED;
        if (strcmp(capturetype, "LIVE") == 0) {
            ESP_LOGW(TAG, "Blocked LIVE identity user_id=%s event_uid=%s", event->user_id, event->event_uid);
        }
    } else {
        if (!append_line_to_open_file(pending_file, PENDING_PATH, json)) {
            free(json);
            led_status_fault(LED_STATUS_FATAL);
            return ENQUEUE_STORAGE_ERROR;
        }
        led_status_set_backlog(true);
        if (strcmp(capturetype, "LIVE") == 0) {
            ESP_LOGI(TAG, "Queued LIVE event_uid=%s user_id=%s raw=%s", event->event_uid, event->user_id, event->raw_punch ? "T" : "F");
        }
    }
    if (!seen_add(event->event_uid)) {
        ESP_LOGW(TAG, "Event persisted but volatile dedup cache could not record %s", event->event_uid);
    }
    if (publish_add && !add_send_attendance_event(event, capturetype)) {
        ESP_LOGE(TAG, "Attendance persisted for ORDS but could not be added to the independent ADD outbox");
        led_status_fault(LED_STATUS_FATAL);
    }
    free(json);
    return result;
}

static enqueue_result_t enqueue_event(const attendance_event_t *event, const char *capturetype)
{
    if (!g_storage_lock || xSemaphoreTake(g_storage_lock, pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGE(TAG, "Could not lock durable attendance outbox");
        return ENQUEUE_STORAGE_ERROR;
    }
    enqueue_result_t result = enqueue_event_to_files(event, capturetype, NULL, NULL, true);
    xSemaphoreGive(g_storage_lock);
    return result;
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
        (void)add_connector_lookup_identity(
            out->user_id,
            out->employee_name,
            sizeof(out->employee_name),
            out->cnic,
            sizeof(out->cnic),
            &out->raw_punch);
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

static bool reconcile_attendance_dump(
    int sock,
    zk_context_t *ctx,
    const user_table_t *users,
    int32_t records,
    const char *capturetype,
    int filter_year,
    int filter_month,
    size_t *added_out)
{
    if (added_out) *added_out = 0;
    uint8_t *data = NULL;
    size_t len = 0;
    if (records <= 0) {
        return true;
    }
    // Do not download a multi-megabyte ZKT dump while the background ORDS
    // worker owns the durable outbox for a network rewrite. Waiting here keeps
    // the expensive device read outside that contention window.
    if (g_ords_outbox_gate == NULL ||
        xSemaphoreTake(
            g_ords_outbox_gate,
            pdMS_TO_TICKS(ZONE_LITE_ORDS_TIMEOUT_MS * 5)) != pdTRUE) {
        ESP_LOGW(TAG, "Timed out waiting for ORDS outbox before full reconcile");
        return false;
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
        xSemaphoreGive(g_ords_outbox_gate);
        return false;
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
        xSemaphoreGive(g_ords_outbox_gate);
        return false;
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
    bool reconcile_overflow = false;
    size_t reconcile_event_count = 0;
    size_t truth_count = 0;
    size_t reconcile_capacity = ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS;
    attendance_event_t *reconcile_events = heap_caps_calloc(
        reconcile_capacity,
        sizeof(attendance_event_t),
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (reconcile_events == NULL) {
        reconcile_events = calloc(reconcile_capacity, sizeof(attendance_event_t));
    }
    if (reconcile_events == NULL) {
        ESP_LOGE(
            TAG,
            "Could not allocate bounded reconcile event store capacity=%u",
            (unsigned)reconcile_capacity);
        free(data);
        xSemaphoreGive(g_ords_outbox_gate);
        led_status_fault(LED_STATUS_TRUTH_REPAIR);
        return false;
    }
    ESP_LOGI(
        TAG,
        "Reconciling %ld attendance records packet_size=%lu month_filter=%04d-%02d",
        (long)records,
        (unsigned long)record_size,
        filter_year,
        filter_month);
    if (!g_storage_lock || xSemaphoreTake(g_storage_lock, pdMS_TO_TICKS(5000)) != pdTRUE) {
        ESP_LOGW(TAG, "Skipping reconcile because attendance storage is busy");
        free(reconcile_events);
        free(data);
        xSemaphoreGive(g_ords_outbox_gate);
        return false;
    }
    FILE *pending_file = fopen(PENDING_PATH, "a");
    if (pending_file == NULL) {
        ESP_LOGW(TAG, "Could not keep %s open for reconcile appends", PENDING_PATH);
    }
    FILE *blocked_file = fopen(BLOCKED_PATH, "a");
    if (blocked_file == NULL) {
        ESP_LOGW(TAG, "Could not keep %s open for reconcile appends", BLOCKED_PATH);
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
            if (reconcile_event_count < reconcile_capacity) {
                reconcile_events[reconcile_event_count++] = event;
                if (event.cnic[0] != '\0') {
                    truth_count++;
                }
            } else {
                reconcile_overflow = true;
            }
            enqueue_result_t result = enqueue_event_to_files(
                &event, capturetype, pending_file, blocked_file, false);
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
        (void)fflush(pending_file);
        (void)fsync(fileno(pending_file));
        fclose(pending_file);
    }
    if (blocked_file != NULL) {
        (void)fflush(blocked_file);
        (void)fsync(fileno(blocked_file));
        fclose(blocked_file);
    }
    xSemaphoreGive(g_storage_lock);
    free(data);
    xSemaphoreGive(g_ords_outbox_gate);
    ESP_LOGI(
        TAG,
        "Released ZKT dump before downstream reconcile serialization events=%u truth=%u",
        (unsigned)reconcile_event_count,
        (unsigned)truth_count);

    bool truth_delivery_ok = true;
    bool add_delivery_ok = true;
    if (reconcile_overflow) {
        ESP_LOGE(
            TAG,
            "Current-month reconcile exceeded bounded event capacity=%u",
            (unsigned)reconcile_capacity);
        truth_delivery_ok = false;
        add_delivery_ok = false;
    } else {
        if (truth_enabled) {
            truth_delivery_ok = oracle_send_reconcile(
                reconcile_events,
                reconcile_event_count,
                filter_year,
                filter_month,
                &truth_count);
        }
        // ADD receives the same compact truth stream independently of ORDS
        // dedup state. Payloads are serialized and committed in bounded chunks
        // only after the multi-megabyte ZKT dump has been released.
        add_delivery_ok = add_enqueue_reconcile_events(
            reconcile_events,
            reconcile_event_count,
            capturetype);
    }
    free(reconcile_events);

    if (!add_delivery_ok) {
        ESP_LOGW(
            TAG,
            "ADD truth batching could not persist the complete cycle; live punches remain prioritized and the six-hour truth cycle will repair history");
        add_connector_log(
            "ERROR",
            "reconcile",
            "ADD_TRUTH_QUEUE_SATURATED",
            "Dashboard truth cycle could not be fully persisted; live punches remain prioritized and history will be repaired by the next truth cycle");
    }
    bool reconcile_complete = !reconcile_overflow && truth_delivery_ok && add_delivery_ok;
    if (!reconcile_complete) {
        led_status_fault(LED_STATUS_TRUTH_REPAIR);
    }
    ESP_LOGI(
        TAG,
        "Reconcile %s processed=%u new=%u pending=%u blocked=%u duplicates=%u filtered=%u skipped=%u truth=%u complete=%s",
        capturetype,
        (unsigned)processed,
        (unsigned)added,
        (unsigned)pending,
        (unsigned)blocked,
        (unsigned)duplicates,
        (unsigned)filtered,
        (unsigned)skipped,
        (unsigned)truth_count,
        reconcile_complete ? "true" : "false");
    if (added_out) *added_out = added;
    return reconcile_complete;
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

static bool daily_zkt_reboot_local_time(struct tm *local_time, int *local_day_key)
{
    if (!ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED) {
        return false;
    }
    if (!ensure_system_time_synced()) {
        return false;
    }

    time_t now = 0;
    time(&now);
    now += (time_t)ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES * 60;
    gmtime_r(&now, local_time);
    if (local_day_key != NULL) {
        *local_day_key = ((local_time->tm_year + 1900) * 1000) + local_time->tm_yday;
    }
    return true;
}

static int64_t daily_zkt_reboot_next_epoch(void)
{
    if (!ZONE_LITE_DAILY_ZKT_REBOOT_ENABLED || !system_time_is_valid()) {
        return 0;
    }

    const int hours[] = {
        ZONE_LITE_RESTART_SLOT_1_HOUR,
        ZONE_LITE_RESTART_SLOT_2_HOUR,
        ZONE_LITE_RESTART_SLOT_3_HOUR,
    };
    time_t utc_now = 0;
    time(&utc_now);
    int64_t offset_seconds = (int64_t)ZONE_LITE_DAILY_ZKT_REBOOT_UTC_OFFSET_MINUTES * 60;
    int64_t local_now = (int64_t)utc_now + offset_seconds;
    int64_t local_day_start = local_now - (local_now % (24 * 60 * 60));
    time_t local_clock = (time_t)local_now;
    struct tm local_time = {0};
    gmtime_r(&local_clock, &local_time);
    int local_day_key = ((local_time.tm_year + 1900) * 1000) + local_time.tm_yday;
    int64_t window_seconds = (int64_t)ZONE_LITE_DAILY_ZKT_REBOOT_WINDOW_MINUTES * 60;
    int64_t next_local = 0;

    for (size_t i = 0; i < sizeof(hours) / sizeof(hours[0]); i++) {
        if (hours[i] < 0 || hours[i] >= 24) continue;
        int64_t scheduled_local = local_day_start + ((int64_t)hours[i] * 60 * 60);
        int slot_key = local_day_key * 10 + (int)i;
        if (window_seconds > 0 && scheduled_local <= local_now &&
            local_now < scheduled_local + window_seconds &&
            slot_key != g_daily_zkt_reboot_completed_day) {
            // This slot is currently due (or awaiting its bounded retry).
            return (int64_t)utc_now;
        }
        if (scheduled_local > local_now && (next_local == 0 || scheduled_local < next_local)) {
            next_local = scheduled_local;
        }
    }

    if (next_local == 0) {
        int earliest_hour = 24;
        for (size_t i = 0; i < sizeof(hours) / sizeof(hours[0]); i++) {
            if (hours[i] >= 0 && hours[i] < earliest_hour) earliest_hour = hours[i];
        }
        if (earliest_hour >= 24) return 0;
        next_local = local_day_start + (24 * 60 * 60) + ((int64_t)earliest_hour * 60 * 60);
    }
    return next_local - offset_seconds;
}

static int daily_zkt_reboot_slot_in_window(const struct tm *local_time)
{
    const int hours[] = {
        ZONE_LITE_RESTART_SLOT_1_HOUR,
        ZONE_LITE_RESTART_SLOT_2_HOUR,
        ZONE_LITE_RESTART_SLOT_3_HOUR,
    };
    int local_minute = (local_time->tm_hour * 60) + local_time->tm_min;
    int window_minutes = ZONE_LITE_DAILY_ZKT_REBOOT_WINDOW_MINUTES;
    if (window_minutes <= 0) return -1;
    for (size_t i = 0; i < sizeof(hours) / sizeof(hours[0]); i++) {
        int scheduled_minute = hours[i] * 60;
        if (scheduled_minute < 0 || scheduled_minute >= 24 * 60) continue;
        int elapsed = local_minute - scheduled_minute;
        if (elapsed >= 0 && elapsed < window_minutes) return (int)i;
    }
    return -1;
}

static bool daily_zkt_reboot_should_attempt(int *local_day_key)
{
    struct tm local_time = {0};
    int day_key = -1;
    if (!daily_zkt_reboot_local_time(&local_time, &day_key)) {
        return false;
    }
    int slot = daily_zkt_reboot_slot_in_window(&local_time);
    if (slot < 0 || g_temp_admin_active) {
        return false;
    }
    int slot_key = day_key * 10 + slot;
    if (slot_key == g_daily_zkt_reboot_completed_day) return false;

    int64_t now_ms = esp_timer_get_time() / 1000;
    int64_t retry_delay_ms = ZONE_LITE_DAILY_ZKT_REBOOT_RETRY_DELAY_MS;
    if (retry_delay_ms <= 0) {
        retry_delay_ms = 5 * 60 * 1000;
    }
    if (g_daily_zkt_reboot_last_attempt_ms > 0 &&
        now_ms - g_daily_zkt_reboot_last_attempt_ms < retry_delay_ms) {
        return false;
    }

    g_daily_zkt_reboot_last_attempt_ms = now_ms;
    if (local_day_key != NULL) {
        *local_day_key = slot_key;
    }
    ESP_LOGW(
        TAG,
        "Daily ZKT maintenance reboot due at local %02d:%02d day=%d",
        local_time.tm_hour,
        local_time.tm_min,
        slot_key);
    return true;
}

static void daily_zkt_reboot_mark_complete(int local_day_key)
{
    g_daily_zkt_reboot_completed_day = local_day_key;
    nvs_save_runtime_state();
    ESP_LOGW(TAG, "Scheduled ZKT maintenance reboot completed for slot=%d", local_day_key);
}

static uint32_t daily_zkt_reboot_target_ip(void)
{
    uint32_t preferred = configured_preferred_zkt_ip();
    if (preferred != 0) {
        return preferred;
    }
    return g_last_authenticated_zkt_ip;
}

static bool daily_zkt_reboot_try_target(uint32_t target_ip, int local_day_key)
{
    if (target_ip == 0) {
        ESP_LOGW(TAG, "Skipping daily ZKT maintenance reboot because no target IP is known");
        return false;
    }

    char ip_text[16];
    ip_to_text(target_ip, ip_text, sizeof(ip_text));
    ESP_LOGW(TAG, "Starting daily ZKT maintenance reboot for %s", ip_text);
    led_status_set(LED_STATUS_RECOVERY_REBOOT);
    int sock = -1;
    zk_context_t ctx = {0};
    bool restarted = tcp_connect_with_timeout(target_ip, ZONE_LITE_ZKT_PORT, 3000, &sock) &&
                     zk_connect_and_auth(sock, &ctx) && zk_protocol_restart(sock, &ctx);
    if (sock >= 0) close(sock);
    if (!restarted && target_ip == g_last_authenticated_zkt_ip) {
        ESP_LOGW(TAG, "Authenticated protocol restart failed; attempting configured recovery channel");
        restarted = zkt_telnet_reboot(target_ip);
    }
    if (!restarted) {
        led_status_fault(LED_STATUS_ZKT_FAILURE);
        ESP_LOGW(TAG, "Scheduled ZKT maintenance reboot failed for %s", ip_text);
        return false;
    }

    daily_zkt_reboot_mark_complete(local_day_key);
    g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
    add_connector_set_zkt(&g_add_zkt);
    ESP_LOGW(TAG, "Waiting %d ms after daily ZKT maintenance reboot", ZONE_LITE_ZKT_REBOOT_WAIT_MS);
    vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_ZKT_REBOOT_WAIT_MS));
    led_status_set(LED_STATUS_ZKT_DISCOVERING);
    return true;
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

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
} http_response_buffer_t;

static esp_err_t http_capture_event(esp_http_client_event_t *event)
{
    http_response_buffer_t *buffer = event ? event->user_data : NULL;
    if (!buffer || event->event_id != HTTP_EVENT_ON_DATA || event->data_len <= 0) return ESP_OK;
    size_t available = buffer->capacity > buffer->length ? buffer->capacity - buffer->length - 1 : 0;
    size_t copy = (size_t)event->data_len < available ? (size_t)event->data_len : available;
    if (copy > 0) {
        memcpy(buffer->data + buffer->length, event->data, copy);
        buffer->length += copy;
        buffer->data[buffer->length] = '\0';
    }
    return ESP_OK;
}

static int http_post_json_with_tls_source(
    const char *url,
    const char *json,
    char **response_body,
    const char *ca_cert_pem,
    const char *tls_source)
{
    http_response_buffer_t captured = {
        .data = response_body ? calloc(1, 8192) : NULL,
        .capacity = response_body ? 8192 : 0,
    };
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = ZONE_LITE_ORDS_TIMEOUT_MS,
        .event_handler = http_capture_event,
        .user_data = &captured,
    };
    if (ca_cert_pem != NULL && ca_cert_pem[0] != '\0') {
        cfg.cert_pem = ca_cert_pem;
    } else {
        cfg.crt_bundle_attach = esp_crt_bundle_attach;
    }

    ESP_LOGI(TAG, "HTTPS trust source: %s", tls_source);
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
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "HTTPS POST failed using %s: %s", tls_source, esp_err_to_name(err));
    }
    if (response_body != NULL) {
        if (err == ESP_OK && captured.data != NULL) {
            *response_body = captured.data;
            captured.data = NULL;
        } else {
            *response_body = NULL;
        }
    }
    free(captured.data);
    esp_http_client_cleanup(client);
    return status;
}

static int http_post_json_unlocked(const char *url, const char *json, char **response_body)
{
    if (response_body != NULL) {
        *response_body = NULL;
    }
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

    const char *ords_ca_cert_pem = ZONE_LITE_ORDS_CA_CERT_PEM;
    if (ords_ca_cert_pem != NULL && ords_ca_cert_pem[0] != '\0') {
        int status = http_post_json_with_tls_source(url, json, response_body, ords_ca_cert_pem, "configured ORDS CA");
        if (status >= 0) {
            return status;
        }
        ESP_LOGW(TAG, "Configured ORDS CA failed; retrying with ESP-IDF certificate bundle");
    }

    return http_post_json_with_tls_source(url, json, response_body, NULL, "ESP-IDF certificate bundle");
}

static int http_post_json(const char *url, const char *json, char **response_body)
{
    if (response_body != NULL) {
        *response_body = NULL;
    }
    if (g_ords_http_lock == NULL ||
        xSemaphoreTake(
            g_ords_http_lock,
            pdMS_TO_TICKS(ZONE_LITE_ORDS_TIMEOUT_MS + 10000)) != pdTRUE) {
        ESP_LOGW(TAG, "Timed out waiting for exclusive ORDS HTTPS transport");
        return -1;
    }
    int status = http_post_json_unlocked(url, json, response_body);
    xSemaphoreGive(g_ords_http_lock);
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

static bool oracle_duplicate_body(const char *body)
{
    if (!body || !body[0]) return false;
    cJSON *root = cJSON_Parse(body);
    if (!root) return false;
    cJSON *duplicate = cJSON_GetObjectItemCaseSensitive(root, "duplicate");
    cJSON *duplicate_count = cJSON_GetObjectItemCaseSensitive(root, "duplicate_existing_count");
    cJSON *status = cJSON_GetObjectItemCaseSensitive(root, "status");
    bool ok = cJSON_IsTrue(duplicate) ||
              (cJSON_IsNumber(duplicate_count) && duplicate_count->valuedouble >= 1) ||
              (cJSON_IsString(status) && strcasecmp(status->valuestring, "duplicate") == 0);
    cJSON_Delete(root);
    return ok;
}

static char *oracle_normalize_event_json(const char *event_json)
{
    cJSON *root = cJSON_Parse(event_json);
    if (!root || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return NULL;
    }
    cJSON *capturetype = cJSON_GetObjectItemCaseSensitive(root, "capturetype");
    const char *source = cJSON_IsString(capturetype) ? capturetype->valuestring : NULL;
    char normalized[32];
    strlcpy(normalized, oracle_capture_type(source), sizeof(normalized));
    cJSON_DeleteItemFromObjectCaseSensitive(root, "capturetype");
    cJSON_AddStringToObject(root, "capturetype", normalized);
    cJSON_DeleteItemFromObjectCaseSensitive(root, "trust_status");
    cJSON_AddStringToObject(root, "trust_status", oracle_trust_status(normalized));
    char *result = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return result;
}

static char *oracle_mark_permanent_rejection(const char *event_json)
{
    cJSON *root = cJSON_Parse(event_json);
    if (!root || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return NULL;
    }
    cJSON_DeleteItemFromObjectCaseSensitive(root, "blocked_reason");
    cJSON_AddStringToObject(root, "blocked_reason", "ORDS_PERMANENT_REJECTION");
    char *result = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return result;
}

static void oracle_log_rejection_details(const char *operation, int status, const char *body)
{
    int received = -1;
    int invalid = -1;
    int conflicts = -1;
    const char *detail = "";
    cJSON *root = body ? cJSON_Parse(body) : NULL;
    if (root) {
        cJSON *received_item = cJSON_GetObjectItemCaseSensitive(root, "received_count");
        cJSON *invalid_item = cJSON_GetObjectItemCaseSensitive(root, "invalid_count");
        cJSON *conflicts_item = cJSON_GetObjectItemCaseSensitive(root, "conflicts");
        cJSON *message_item = cJSON_GetObjectItemCaseSensitive(root, "message");
        cJSON *error_item = cJSON_GetObjectItemCaseSensitive(root, "error");
        if (cJSON_IsNumber(received_item)) received = received_item->valueint;
        if (cJSON_IsNumber(invalid_item)) invalid = invalid_item->valueint;
        if (cJSON_IsArray(conflicts_item)) conflicts = cJSON_GetArraySize(conflicts_item);
        if (cJSON_IsString(message_item)) detail = message_item->valuestring;
        else if (cJSON_IsString(error_item)) detail = error_item->valuestring;
    }
    ESP_LOGW(
        TAG,
        "ORDS %s rejection status=%d received=%d invalid=%d conflicts=%d detail=%.*s",
        operation,
        status,
        received,
        invalid,
        conflicts,
        160,
        detail);
    cJSON_Delete(root);
}

typedef enum {
    ORACLE_DELIVERY_RETRYABLE = 0,
    ORACLE_DELIVERY_ACKED,
    ORACLE_DELIVERY_PERMANENT_REJECTION,
    ORACLE_DELIVERY_CORRUPT_LOCAL_ROW,
} oracle_delivery_result_t;

static oracle_delivery_result_t oracle_send_live(const char *event_json)
{
    char *normalized_event = oracle_normalize_event_json(event_json);
    if (!normalized_event) {
        ESP_LOGE(TAG, "Could not normalize persisted ORDS event JSON");
        led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
        return ORACLE_DELIVERY_CORRUPT_LOCAL_ROW;
    }
    char url[576];
    snprintf(url, sizeof(url), "%s/raw-captures", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, normalized_event, &body);
    free(normalized_event);
    // The ORDS single-event endpoint uses HTTP 409 as its idempotent
    // duplicate acknowledgement.  Its 409 response is not guaranteed to use
    // the bulk endpoint's duplicate_existing_count response shape, so a
    // successfully completed 409 request must be removed from the outbox.
    bool ok = status == 409 ||
              ((status == 200 || status == 201) && oracle_success_body(body));
    ESP_LOGI(TAG, "ORDS live status=%d ok=%s", status, ok ? "true" : "false");
    if (ok) {
        ords_mark_success();
        free(body);
        return ORACLE_DELIVERY_ACKED;
    } else {
        oracle_log_rejection_details("live", status, body);
        if (status == 400 || status == 422) {
            led_status_fault(LED_STATUS_BLOCKED_IDENTITY);
            free(body);
            return ORACLE_DELIVERY_PERMANENT_REJECTION;
        }
        ords_mark_failure();
        led_status_fault(LED_STATUS_ORDS_FAILURE);
    }
    free(body);
    return ORACLE_DELIVERY_RETRYABLE;
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
    char **normalized_events = calloc(count, sizeof(char *));
    if (!normalized_events) {
        led_status_fault(LED_STATUS_FATAL);
        return false;
    }
    for (size_t i = 0; i < count; i++) {
        normalized_events[i] = oracle_normalize_event_json(events[i]);
        if (!normalized_events[i]) {
            for (size_t j = 0; j < count; j++) free(normalized_events[j]);
            free(normalized_events);
            ESP_LOGE(TAG, "Could not normalize ORDS bulk event index=%u", (unsigned)i);
            led_status_fault(LED_STATUS_FATAL);
            return false;
        }
    }
    char batch_uid[64];
    snprintf(
        batch_uid,
        sizeof(batch_uid),
        "Z-%08lx-%08lx-%08lx",
        (unsigned long)epoch_now(),
        (unsigned long)(esp_timer_get_time() / 1000),
        (unsigned long)esp_random());
    char *payload = build_bulk_payload(normalized_events, count, batch_uid);
    for (size_t i = 0; i < count; i++) free(normalized_events[i]);
    free(normalized_events);
    if (payload == NULL) {
        return false;
    }
    char url[576];
    snprintf(url, sizeof(url), "%s/raw-captures/bulk", ZONE_LITE_ORDS_BASE_URL);
    char *body = NULL;
    int status = http_post_json(url, payload, &body);
    bool ok = (status == 409 && oracle_duplicate_body(body)) ||
              ((status == 200 || status == 201) && oracle_success_body(body));
    ESP_LOGI(TAG, "ORDS bulk count=%u status=%d ok=%s", (unsigned)count, status, ok ? "true" : "false");
    if (ok) {
        ords_mark_success();
    } else {
        oracle_log_rejection_details("bulk", status, body);
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

static char *build_reconcile_payload(
    const attendance_event_t *events,
    size_t event_count,
    int year,
    int month,
    size_t *included_out)
{
    if (included_out) *included_out = 0;
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
    // Size the authoritative payload with one transient JSON row at a time.
    // Keeping thousands of row strings alongside the ZKT dump caused the
    // original production memory exhaustion.
    for (size_t i = 0; i < event_count; i++) {
        if (events[i].cnic[0] == '\0') {
            continue;
        }
        char *event_json = event_to_json(&events[i], "MANUAL_REPROCESS");
        if (event_json == NULL) {
            return NULL;
        }
        size_t event_len = strlen(event_json);
        free(event_json);
        size_t separator = included > 0 ? 1 : 0;
        if (event_len > SIZE_MAX - payload_len - separator) {
            return NULL;
        }
        payload_len += event_len + separator;
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
    size_t written = 0;
    for (size_t i = 0; i < event_count; i++) {
        if (events[i].cnic[0] == '\0') {
            continue;
        }
        char *event_json = event_to_json(&events[i], "MANUAL_REPROCESS");
        if (event_json == NULL) {
            free(payload);
            return NULL;
        }
        if (written > 0) {
            *write_at++ = ',';
        }
        size_t len = strlen(event_json);
        memcpy(write_at, event_json, len);
        write_at += len;
        free(event_json);
        written++;
        if ((written % 100) == 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    size_t suffix_len = strlen(suffix);
    memcpy(write_at, suffix, suffix_len);
    write_at += suffix_len;
    *write_at = '\0';
    if (written != included) {
        free(payload);
        return NULL;
    }
    if (included_out) *included_out = included;
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

static bool oracle_send_reconcile(
    const attendance_event_t *events,
    size_t event_count,
    int year,
    int month,
    size_t *truth_count_out)
{
    if (truth_count_out) *truth_count_out = 0;
    if (!ZONE_LITE_ORDS_RECONCILE_ENABLED) {
        return true;
    }
    if (!ords_send_allowed()) {
        ESP_LOGI(TAG, "Skipping ORDS truth reconcile until current ORDS backoff expires");
        return false;
    }
    if (event_count > ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS) {
        ESP_LOGE(TAG, "Truth reconcile has %u events, above safety limit %u", (unsigned)event_count, (unsigned)ZONE_LITE_ORDS_RECONCILE_MAX_EVENTS);
        led_status_fault(LED_STATUS_TRUTH_REPAIR);
        return false;
    }

    size_t truth_count = 0;
    char *payload = build_reconcile_payload(events, event_count, year, month, &truth_count);
    if (payload == NULL) {
        ESP_LOGE(TAG, "Could not build bounded ORDS truth reconcile payload events=%u", (unsigned)event_count);
        led_status_fault(LED_STATUS_TRUTH_REPAIR);
        return false;
    }
    if (truth_count_out) *truth_count_out = truth_count;

    char url[576];
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
        (unsigned)truth_count,
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

static void oracle_drain_pending_locked(bool live_first)
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
            oracle_delivery_result_t delivery = oracle_send_live(line);
            if (delivery == ORACLE_DELIVERY_ACKED) {
                append_acked_uid_from_json_to_file(line, acked_file);
                made_progress = true;
                live_first = false;
                continue;
            }
            if (delivery == ORACLE_DELIVERY_PERMANENT_REJECTION) {
                char *blocked_json = oracle_mark_permanent_rejection(line);
                if (blocked_json && append_line(BLOCKED_PATH, blocked_json)) {
                    char event_uid[65] = "unknown";
                    (void)extract_event_uid(line, event_uid);
                    ESP_LOGE(
                        TAG,
                        "Preserved permanently rejected ORDS event in blocked outbox uid=%s",
                        event_uid);
                    (void)add_connector_log(
                        "ERROR",
                        "ords",
                        "ORDS_EVENT_QUARANTINED",
                        "Oracle permanently rejected a queued attendance event; it remains preserved for review while later events continue.");
                    free(blocked_json);
                    made_progress = true;
                    continue;
                }
                free(blocked_json);
                ESP_LOGE(TAG, "Could not preserve permanently rejected ORDS event");
            }
            if (delivery == ORACLE_DELIVERY_CORRUPT_LOCAL_ROW) {
                if (append_line(CORRUPT_ORDS_PATH, line)) {
                    ESP_LOGE(
                        TAG,
                        "Preserved malformed local ORDS row for forensic recovery and continued draining");
                    (void)add_connector_log(
                        "ERROR",
                        "ords",
                        "ORDS_LOCAL_ROW_QUARANTINED",
                        "A malformed legacy ORDS outbox row was preserved separately so newer attendance can continue.");
                    made_progress = true;
                    continue;
                }
                ESP_LOGE(TAG, "Could not preserve malformed local ORDS row");
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

static void oracle_drain_pending(bool live_first)
{
    if (!g_ords_outbox_gate ||
        xSemaphoreTake(g_ords_outbox_gate, pdMS_TO_TICKS(100)) != pdTRUE) return;
    if (!g_storage_lock || xSemaphoreTake(g_storage_lock, pdMS_TO_TICKS(5000)) != pdTRUE) {
        xSemaphoreGive(g_ords_outbox_gate);
        return;
    }
    oracle_drain_pending_locked(live_first);
    xSemaphoreGive(g_storage_lock);
    xSemaphoreGive(g_ords_outbox_gate);
}

static void ords_uploader_task(void *arg)
{
    (void)arg;
    while (true) {
        if ((xEventGroupGetBits(wifi_event_group) & WIFI_CONNECTED_BIT) != 0) {
            oracle_drain_pending(true);
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

static bool probe_zkt_device(uint32_t host_order_ip, uint32_t *selected_ip)
{
    int64_t probe_started_ms = uptime_ms();
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
    if (ZONE_LITE_ZKT_EXPECTED_SERIAL[0] != '\0' &&
        strcmp(g_device_serial, ZONE_LITE_ZKT_EXPECTED_SERIAL) != 0) {
        ESP_LOGE(TAG, "Rejecting authenticated ZKT %s because serial %s does not match assignment", ip_text, g_device_serial);
        add_connector_log("CRITICAL", "zkt", "ZKT_SERIAL_MISMATCH", "Authenticated terminal serial does not match the connector assignment");
        goto done;
    }
    strlcpy(g_add_zkt.serial, g_device_serial, sizeof(g_add_zkt.serial));
    strlcpy(g_add_zkt.model, device_name, sizeof(g_add_zkt.model));
    strlcpy(g_add_zkt.platform, platform, sizeof(g_add_zkt.platform));
    strlcpy(g_add_zkt.device_time, device_time, sizeof(g_add_zkt.device_time));
    if (device_time[0]) g_add_zkt.device_time_sampled_epoch = epoch_now();
    strlcpy(g_add_zkt.ip_address, ip_text, sizeof(g_add_zkt.ip_address));
    g_add_zkt.user_count = users;
    g_add_zkt.attendance_count = records;
    g_add_zkt.probe_latency_ms = (uint32_t)(uptime_ms() - probe_started_ms);
    g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
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
    zkt_mark_authenticated(host_order_ip, "authenticated discovery probe");
    led_status_set(LED_STATUS_ZKT_AUTHENTICATED);
    ok = true;

done:
    if (ctx.session_id != 0) {
        zk_disconnect(sock, &ctx);
    }
    close(sock);
    return ok;
}

static bool discover_zkt(uint32_t *selected_ip, uint32_t skip_ip)
{
    led_status_set(LED_STATUS_ZKT_DISCOVERING);
    zkt_publish_state("DISCOVERING", "searching for authenticated ZKT terminal", false);
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
    if (preferred != 0 && preferred != skip_ip && preferred != own_ip && preferred != ntohl(ip_info.gw.addr)) {
        ESP_LOGI(TAG, "Trying preferred ZKT IP %s:%d", ZONE_LITE_ZKT_PREFERRED_IP, ZONE_LITE_ZKT_PORT);
        if (probe_zkt_device(preferred, selected_ip)) {
            return true;
        }
    }
    if (g_last_authenticated_zkt_ip != 0 && g_last_authenticated_zkt_ip != skip_ip &&
        g_last_authenticated_zkt_ip != preferred &&
        g_last_authenticated_zkt_ip != own_ip && g_last_authenticated_zkt_ip != ntohl(ip_info.gw.addr)) {
        char last_ip[16];
        ip_to_text(g_last_authenticated_zkt_ip, last_ip, sizeof(last_ip));
        ESP_LOGI(TAG, "Trying last authenticated ZKT IP %s:%d", last_ip, ZONE_LITE_ZKT_PORT);
        if (probe_zkt_device(g_last_authenticated_zkt_ip, selected_ip)) return true;
    }
    int64_t now_ms = uptime_ms();
    if (g_last_full_scan_ms > 0 && now_ms - g_last_full_scan_ms < ZONE_LITE_DISCOVERY_FULL_SCAN_INTERVAL_MS) {
        ESP_LOGI(TAG, "Skipping full subnet scan during protective discovery interval");
        return false;
    }
    g_last_full_scan_ms = now_ms;
    ESP_LOGI(TAG, "Scanning %lu hosts for ZKT TCP port %d", (unsigned long)host_count, ZONE_LITE_ZKT_PORT);
    for (uint32_t candidate = network + 1; candidate < broadcast; candidate++) {
        if (candidate == own_ip || candidate == ntohl(ip_info.gw.addr) ||
            candidate == preferred || candidate == skip_ip) {
            continue;
        }
        if (probe_zkt_device(candidate, selected_ip)) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return false;
}

static size_t process_live_packet(const uint8_t *data, size_t len, const user_table_t *users)
{
    size_t observed = 0;
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
            observed++;
        }
    }
    return observed;
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

static bool command_was_processed(const char *command_id)
{
    FILE *file = fopen(PROCESSED_COMMANDS_PATH, "r");
    if (!file) return false;
    char line[96];
    bool found = false;
    while (fgets(line, sizeof(line), file)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strcmp(line, command_id) == 0) { found = true; break; }
    }
    fclose(file);
    return found;
}

static bool command_was_cancelled(const char *command_id)
{
    FILE *file = fopen(CANCELLED_COMMANDS_PATH, "r");
    if (!file) return false;
    char line[96];
    bool found = false;
    while (fgets(line, sizeof(line), file)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strcmp(line, command_id) == 0) {
            found = true;
            break;
        }
    }
    fclose(file);
    return found;
}

static void mark_command_processed(const char *command_id)
{
    if (!command_was_processed(command_id)) (void)append_line(PROCESSED_COMMANDS_PATH, command_id);
}

static void temp_admin_clear(void)
{
    g_temp_admin_active = false;
    g_temp_admin_uid = 0;
    g_temp_admin_expires_epoch = 0;
    nvs_save_runtime_state();
}

static bool temp_admin_revoke_if_due(int sock, zk_context_t *ctx, user_table_t *users)
{
    if (!g_temp_admin_active) return true;
    int64_t now = epoch_now();
    if (now >= ZONE_LITE_MIN_VALID_UNIX_TIME && now < g_temp_admin_expires_epoch) return true;
    char uid[16];
    snprintf(uid, sizeof(uid), "%u", g_temp_admin_uid);
    zkt_user_t *user = find_mutable_user_by_uid(users, uid);
    if (!user) {
        add_connector_log("CRITICAL", "enrollment", "LEASE_USER_MISSING", "Temporary administrator user is missing from the terminal snapshot");
        return false;
    }
    if (user->privilege == 0) {
        temp_admin_clear();
        add_connector_log("INFO", "enrollment", "LEASE_REVOKED_LOCAL", "Temporary administrator privilege was revoked by the ESP watchdog");
        return true;
    }
    if (zk_write_user(sock, ctx, user, NULL, 0)) {
        int32_t verified_users = 0;
        int32_t verified_records = 0;
        if (zk_get_counts(sock, ctx, &verified_users, &verified_records) &&
            zk_refresh_users_preserving_current(sock, ctx, users, verified_users)) {
            zkt_user_t *verified = find_mutable_user_by_uid(users, uid);
            if (verified && verified->privilege == 0) {
                g_add_zkt.user_count = verified_users;
                g_add_zkt.attendance_count = verified_records;
                add_connector_set_zkt(&g_add_zkt);
                temp_admin_clear();
                add_connector_log("INFO", "enrollment", "LEASE_REVOKED_LOCAL", "Temporary administrator privilege was revoked and verified by reread");
                return true;
            }
        }
    }
    add_connector_log("CRITICAL", "enrollment", "LEASE_REVOKE_FAILED", "Temporary administrator privilege could not be revoked; retrying");
    led_status_fault(LED_STATUS_FATAL);
    return false;
}

static bool process_add_commands(
    int sock,
    zk_context_t *ctx,
    user_table_t *users,
    int32_t *user_count)
{
    add_command_t command;
    while (add_connector_take_command(&command)) {
        if (command_was_cancelled(command.command_id)) {
            (void)add_connector_command_update(
                command.command_id,
                "CANCELLED",
                "COMMAND_CANCELLED",
                "The command was cancelled before terminal execution.",
                "{}");
            (void)add_connector_command_complete(command.command_id);
            continue;
        }
        int64_t command_now = epoch_now();
        if (command.expires_epoch > 0 && command_now >= ZONE_LITE_MIN_VALID_UNIX_TIME &&
            command_now >= command.expires_epoch) {
            (void)add_connector_command_update(
                command.command_id,
                "EXPIRED",
                "COMMAND_EXPIRED",
                "The command expired before terminal execution; no mutation was attempted.",
                "{}");
            (void)add_connector_command_complete(command.command_id);
            continue;
        }
        if (command_was_processed(command.command_id)) {
            char duplicate_result[192] = "{\"duplicate\":true}";
            if (strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0 &&
                g_temp_admin_active && g_temp_admin_uid == (uint16_t)strtoul(command.uid, NULL, 10)) {
                snprintf(
                    duplicate_result,
                    sizeof(duplicate_result),
                    "{\"duplicate\":true,\"verified_privilege\":14,\"expires_epoch\":%lld}",
                    (long long)g_temp_admin_expires_epoch);
            }
            (void)add_connector_command_update(command.command_id, "SUCCEEDED", NULL, NULL, duplicate_result);
            (void)add_connector_command_complete(command.command_id);
            continue;
        }
        (void)add_connector_command_update(command.command_id, "RUNNING", NULL, NULL, "{}");
        bool ok = false;
        const char *error_code = "COMMAND_UNSUPPORTED";
        const char *error_message = "The requested command is not supported by this firmware.";
        char result[512] = "{}";
        if (strcmp(command.command_type, "REFRESH_USERS") == 0) {
            int32_t records = 0;
            if (zk_get_counts(sock, ctx, user_count, &records) &&
                zk_refresh_users_preserving_current(sock, ctx, users, *user_count)) {
                g_add_zkt.user_count = *user_count;
                g_add_zkt.attendance_count = records;
                add_connector_set_zkt(&g_add_zkt);
                ok = add_send_user_snapshot(users);
                if (!ok) {
                    error_code = "ADD_SNAPSHOT_SEND_FAILED";
                    error_message = "User snapshot was read but could not be delivered to ADD.";
                }
            } else {
                error_code = "ZKT_USER_READ_FAILED";
                error_message = "The terminal user table could not be read and verified.";
            }
        } else if (strcmp(command.command_type, "CREATE_USER") == 0 ||
                   strcmp(command.command_type, "UPDATE_USER") == 0 ||
                   strcmp(command.command_type, "DELETE_USER") == 0 ||
                   strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0 ||
                   strcmp(command.command_type, "REVOKE_TEMP_ADMIN") == 0) {
            int32_t before_users = 0;
            int32_t before_records = 0;
            if (command.expected_serial[0] &&
                strcmp(command.expected_serial, g_device_serial) != 0) {
                error_code = "ZKT_SERIAL_PRECONDITION_FAILED";
                error_message = "The authenticated terminal serial no longer matches the command target.";
            } else if (!zk_get_counts(sock, ctx, &before_users, &before_records) ||
                !zk_refresh_users_preserving_current(sock, ctx, users, before_users)) {
                error_code = "ZKT_USER_READ_FAILED";
                error_message = "A fresh terminal read could not be completed before mutation.";
            } else if (!users->complete) {
                error_code = "USER_SNAPSHOT_TRUNCATED";
                error_message = "Writes are disabled because the full user table does not fit safely.";
            } else if (users->record_size != 72) {
                error_code = "LEGACY_USER_RECORD_READ_ONLY";
                error_message = "This legacy 8-byte-name record is intentionally read-only.";
            } else if (strcmp(command.command_type, "CREATE_USER") == 0) {
                zkt_user_t *uid_match = find_mutable_user_by_uid(users, command.uid);
                const zkt_user_t *id_match = find_user_by_user_id(users, command.user_id);
                if ((uid_match || id_match) && uid_match == id_match &&
                    user_matches_command(uid_match, &command)) {
                    ok = true;
                } else if (uid_match || id_match) {
                    error_code = "USER_IDENTIFIER_CONFLICT";
                    error_message = "The allocated UID or user ID is already held by another user.";
                } else {
                    zkt_user_t candidate = {0};
                    strlcpy(candidate.uid, command.uid, sizeof(candidate.uid));
                    strlcpy(candidate.user_id, command.user_id, sizeof(candidate.user_id));
                    strlcpy(candidate.name, command.name, sizeof(candidate.name));
                    strlcpy(candidate.group_id, "1", sizeof(candidate.group_id));
                    candidate.record_size = 72;
                    candidate.privilege = 0;
                    ok = zk_write_user(sock, ctx, &candidate, command.name, 0);
                }
                int32_t after_users = 0;
                int32_t after_records = 0;
                if (ok && (!zk_get_counts(sock, ctx, &after_users, &after_records) ||
                           !zk_refresh_users_preserving_current(sock, ctx, users, after_users))) {
                    ok = false;
                }
                zkt_user_t *verified = find_mutable_user_by_uid(users, command.uid);
                if (ok && !user_matches_command(verified, &command)) ok = false;
                if (!ok && strcmp(error_code, "COMMAND_UNSUPPORTED") == 0) {
                    error_code = "ZKT_USER_CREATE_FAILED";
                    error_message = "The created user was not present with the requested values after reread.";
                }
                if (ok) {
                    *user_count = after_users;
                    snprintf(
                        result,
                        sizeof(result),
                        "{\"verified_uid\":\"%s\",\"verified_user_id\":\"%s\",\"user_count\":%ld,"
                        "\"verified_terminal_identity_fingerprint\":\"%s\","
                        "\"verified_terminal_state_fingerprint\":\"%s\"}",
                        command.uid,
                        command.user_id,
                        (long)after_users,
                        verified->terminal_identity_fingerprint,
                        verified->terminal_state_fingerprint);
                }
            } else if (strcmp(command.command_type, "DELETE_USER") == 0) {
                zkt_user_t *user = find_mutable_user_by_uid(users, command.uid);
                if (!command.has_tombstone) {
                    error_code = "IDENTITY_TOMBSTONE_REQUIRED";
                    error_message = "Deletion was refused because no durable identity tombstone was supplied.";
                } else if (user && !user_matches_expected_state(user, &command)) {
                    error_code = "USER_PRECONDITION_FAILED";
                    error_message = "The fresh terminal user no longer matches the expected identity and version state.";
                } else if (!add_connector_persist_command_tombstone(&command)) {
                    error_code = "IDENTITY_TOMBSTONE_PERSIST_FAILED";
                    error_message = "The encrypted ESP identity tombstone could not be committed before deletion.";
                } else {
                    if (user) ok = zk_delete_user(sock, ctx, (uint16_t)strtoul(user->uid, NULL, 10));
                    else ok = true;
                    int32_t after_users = 0;
                    int32_t after_records = 0;
                    if (ok && (!zk_get_counts(sock, ctx, &after_users, &after_records) ||
                               !zk_refresh_users_preserving_current(sock, ctx, users, after_users))) {
                        ok = false;
                    }
                    bool absent = find_mutable_user_by_uid(users, command.uid) == NULL;
                    if (ok && (!absent || before_records != after_records)) ok = false;
                    if (!ok) {
                        error_code = "ZKT_USER_DELETE_VERIFY_FAILED";
                        error_message = "User absence or unchanged attendance count could not be verified.";
                    } else {
                        *user_count = after_users;
                        snprintf(
                            result,
                            sizeof(result),
                            "{\"user_absent\":true,\"attendance_count_before\":%ld,\"attendance_count_after\":%ld,\"user_count\":%ld}",
                            (long)before_records,
                            (long)after_records,
                            (long)after_users);
                    }
                }
            } else {
                zkt_user_t *user = find_mutable_user_by_uid(users, command.uid);
                if (!user) {
                    error_code = "USER_NOT_FOUND";
                    error_message = "The requested UID is not present on this terminal.";
                } else if (
                    strcmp(command.command_type, "UPDATE_USER") == 0 &&
                    user_matches_command(user, &command)) {
                    ok = true;
                    snprintf(
                        result,
                        sizeof(result),
                        "{\"duplicate\":true,\"verified_privilege\":%d,"
                        "\"verified_terminal_identity_fingerprint\":\"%s\","
                        "\"verified_terminal_state_fingerprint\":\"%s\"}",
                        user->privilege,
                        user->terminal_identity_fingerprint,
                        user->terminal_state_fingerprint);
                } else if (
                    strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0 &&
                    user->privilege == 14) {
                    int64_t deadline = command.lease_expires_epoch > 0
                        ? command.lease_expires_epoch
                        : epoch_now() + 600;
                    if (deadline <= epoch_now()) {
                        error_code = "COMMAND_EXPIRED";
                        error_message = "The enrollment lease deadline passed before recovery completed.";
                    } else {
                        g_temp_admin_active = true;
                        g_temp_admin_uid = (uint16_t)strtoul(user->uid, NULL, 10);
                        g_temp_admin_expires_epoch = deadline;
                        nvs_save_runtime_state();
                        ok = true;
                        snprintf(
                            result,
                            sizeof(result),
                            "{\"duplicate\":true,\"verified_privilege\":14,\"expires_epoch\":%lld,"
                            "\"verified_terminal_identity_fingerprint\":\"%s\","
                            "\"verified_terminal_state_fingerprint\":\"%s\"}",
                            (long long)deadline,
                            user->terminal_identity_fingerprint,
                            user->terminal_state_fingerprint);
                    }
                } else if (
                    strcmp(command.command_type, "REVOKE_TEMP_ADMIN") == 0 &&
                    user->privilege == 0) {
                    temp_admin_clear();
                    ok = true;
                    snprintf(
                        result,
                        sizeof(result),
                        "{\"duplicate\":true,\"verified_privilege\":0,"
                        "\"verified_terminal_identity_fingerprint\":\"%s\","
                        "\"verified_terminal_state_fingerprint\":\"%s\"}",
                        user->terminal_identity_fingerprint,
                        user->terminal_state_fingerprint);
                } else if (
                    strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0 &&
                    command.lease_expires_epoch > 0 &&
                    command.lease_expires_epoch <= epoch_now()) {
                    error_code = "COMMAND_EXPIRED";
                    error_message = "The enrollment lease deadline passed before elevation started.";
                } else if (!user_matches_expected_state(user, &command)) {
                    error_code = "USER_PRECONDITION_FAILED";
                    error_message = "The fresh terminal user no longer matches the command precondition.";
                } else {
                int privilege = user->privilege;
                const char *name = NULL;
                if (strcmp(command.command_type, "UPDATE_USER") == 0) {
                    if (command.has_privilege) privilege = command.privilege;
                    if (command.has_name) name = command.name;
                } else if (strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0) {
                    privilege = 14;
                } else {
                    privilege = 0;
                }
                ok = zk_write_user(sock, ctx, user, name, privilege);
                if (!ok) {
                    error_code = "ZKT_USER_WRITE_FAILED";
                    error_message = "The terminal did not acknowledge and verify the user write.";
                } else if (strcmp(command.command_type, "GRANT_TEMP_ADMIN") == 0) {
                    if (!ensure_system_time_synced()) {
                        (void)zk_write_user(sock, ctx, user, NULL, 0);
                        ok = false;
                        error_code = "TRUSTED_TIME_UNAVAILABLE";
                        error_message = "The elevation was rolled back because trusted time is unavailable.";
                    } else {
                        int32_t after_users = 0;
                        int32_t after_records = 0;
                        if (!zk_get_counts(sock, ctx, &after_users, &after_records) ||
                            !zk_refresh_users_preserving_current(sock, ctx, users, after_users)) {
                            ok = false;
                            error_code = "ZKT_USER_REREAD_FAILED";
                            error_message = "Administrator elevation could not be verified by reread.";
                        } else {
                            zkt_user_t *verified = find_mutable_user_by_uid(users, command.uid);
                            if (!verified || !user_matches_command(verified, &command) ||
                                verified->privilege != 14) {
                                ok = false;
                                error_code = "ZKT_USER_POSTCONDITION_FAILED";
                                error_message = "Administrator elevation did not persist after reread.";
                            } else {
                                g_temp_admin_active = true;
                                g_temp_admin_uid = (uint16_t)strtoul(verified->uid, NULL, 10);
                                g_temp_admin_expires_epoch = command.lease_expires_epoch > 0
                                    ? command.lease_expires_epoch
                                    : epoch_now() + 600;
                                if (g_temp_admin_expires_epoch <= epoch_now()) {
                                    g_temp_admin_active = true;
                                    g_temp_admin_uid = (uint16_t)strtoul(verified->uid, NULL, 10);
                                    nvs_save_runtime_state();
                                    (void)temp_admin_revoke_if_due(sock, ctx, users);
                                    ok = false;
                                    error_code = "COMMAND_EXPIRED";
                                    error_message = "The enrollment lease deadline passed before elevation verification.";
                                }
                                nvs_save_runtime_state();
                                if (ok) {
                                    snprintf(
                                        result,
                                        sizeof(result),
                                        "{\"verified_privilege\":14,\"expires_epoch\":%lld,"
                                        "\"verified_terminal_identity_fingerprint\":\"%s\","
                                        "\"verified_terminal_state_fingerprint\":\"%s\"}",
                                        (long long)g_temp_admin_expires_epoch,
                                        verified->terminal_identity_fingerprint,
                                        verified->terminal_state_fingerprint);
                                }
                            }
                        }
                    }
                } else if (strcmp(command.command_type, "REVOKE_TEMP_ADMIN") == 0) {
                    int32_t after_users = 0;
                    int32_t after_records = 0;
                    if (!zk_get_counts(sock, ctx, &after_users, &after_records) ||
                        !zk_refresh_users_preserving_current(sock, ctx, users, after_users)) {
                        ok = false;
                        error_code = "ZKT_USER_REREAD_FAILED";
                        error_message = "Administrator revocation could not be verified by reread.";
                    } else {
                        zkt_user_t *verified = find_mutable_user_by_uid(users, command.uid);
                        if (!verified || !user_matches_command(verified, &command) ||
                            verified->privilege != 0) {
                            ok = false;
                            error_code = "ZKT_USER_POSTCONDITION_FAILED";
                            error_message = "Administrator revocation did not persist after reread.";
                        } else {
                            temp_admin_clear();
                            snprintf(
                                result,
                                sizeof(result),
                                "{\"verified_privilege\":0,"
                                "\"verified_terminal_identity_fingerprint\":\"%s\","
                                "\"verified_terminal_state_fingerprint\":\"%s\"}",
                                verified->terminal_identity_fingerprint,
                                verified->terminal_state_fingerprint);
                        }
                    }
                } else {
                    int32_t after_users = 0;
                    int32_t after_records = 0;
                    if (!zk_get_counts(sock, ctx, &after_users, &after_records) ||
                        !zk_refresh_users_preserving_current(sock, ctx, users, after_users)) {
                        ok = false;
                        error_code = "ZKT_USER_REREAD_FAILED";
                        error_message = "The terminal write ACK could not be verified by reread.";
                    } else {
                        zkt_user_t *verified = find_mutable_user_by_uid(users, command.uid);
                        if (!verified || !user_matches_command(verified, &command) ||
                            verified->privilege != privilege ||
                            (name && strcmp(verified->name, name) != 0)) {
                            ok = false;
                            error_code = "ZKT_USER_POSTCONDITION_FAILED";
                            error_message = "The terminal reread did not match the requested user values.";
                        } else {
                            snprintf(
                                result,
                                sizeof(result),
                                "{\"verified_privilege\":%d,"
                                "\"verified_terminal_identity_fingerprint\":\"%s\","
                                "\"verified_terminal_state_fingerprint\":\"%s\"}",
                                privilege,
                                verified->terminal_identity_fingerprint,
                                verified->terminal_state_fingerprint);
                        }
                    }
                }
                if (ok) (void)add_send_user_snapshot(users);
                }
            }
        } else if (strcmp(command.command_type, "RESTART_ZKT") == 0) {
            if (g_temp_admin_active) {
                error_code = "ACTIVE_ADMIN_LEASE";
                error_message = "Restart is blocked until the temporary administrator is revoked.";
            } else {
                ok = zk_protocol_restart(sock, ctx);
                if (!ok) {
                    error_code = "ZKT_RESTART_FAILED";
                    error_message = "The authenticated protocol restart was not acknowledged.";
                }
            }
        }
        if (ok) {
            if (strcmp(command.command_type, "CREATE_USER") == 0 ||
                strcmp(command.command_type, "DELETE_USER") == 0) {
                (void)add_send_user_snapshot(users);
            }
            mark_command_processed(command.command_id);
            (void)add_connector_command_update(command.command_id, "SUCCEEDED", NULL, NULL, result);
            (void)add_connector_command_complete(command.command_id);
            if (strcmp(command.command_type, "RESTART_ZKT") == 0) return true;
        } else {
            int64_t retry_now = epoch_now();
            bool not_expired = command.expires_epoch <= 0 ||
                retry_now < ZONE_LITE_MIN_VALID_UNIX_TIME || retry_now < command.expires_epoch;
            if (not_expired && command_error_is_retryable(error_code)) {
                (void)add_connector_command_update(
                    command.command_id,
                    "RETRYING",
                    error_code,
                    error_message,
                    "{}");
                add_connector_command_retry(command.command_id);
            } else {
                (void)add_connector_command_update(
                    command.command_id,
                    strcmp(error_code, "COMMAND_EXPIRED") == 0 ? "EXPIRED" : "FAILED",
                    error_code,
                    error_message,
                    "{}");
                (void)add_connector_command_complete(command.command_id);
            }
        }
    }
    return false;
}

static int64_t gateway_run(uint32_t host_order_ip)
{
    int64_t session_started_ms = uptime_ms();
    int sock = -1;
    if (!tcp_connect_with_timeout(host_order_ip, ZONE_LITE_ZKT_PORT, 3000, &sock)) return 0;
    zk_context_t ctx = {0};
    if (!zk_connect_and_auth(sock, &ctx)) { close(sock); return 0; }

    char serial[80] = {0};
    char device_name[80] = {0};
    char platform[80] = {0};
    char device_time[32] = {0};
    char ip_text[16] = {0};
    if (!zk_read_option(sock, &ctx, "~SerialNumber", serial, sizeof(serial))) {
        add_connector_log("ERROR", "zkt", "ZKT_SERIAL_UNREADABLE", "Authenticated live session did not return a terminal serial");
        zk_disconnect(sock, &ctx); close(sock); return uptime_ms() - session_started_ms;
    }
    strlcpy(g_device_serial, serial, sizeof(g_device_serial));
    if (ZONE_LITE_ZKT_EXPECTED_SERIAL[0] != '\0' &&
        strcmp(g_device_serial, ZONE_LITE_ZKT_EXPECTED_SERIAL) != 0) {
        add_connector_log("CRITICAL", "zkt", "ZKT_SERIAL_CHANGED", "Live session serial changed after discovery; session rejected");
        zk_disconnect(sock, &ctx); close(sock); return uptime_ms() - session_started_ms;
    }
    if (g_add_zkt.model[0]) {
        strlcpy(device_name, g_add_zkt.model, sizeof(device_name));
    } else {
        (void)zk_read_option(sock, &ctx, "~DeviceName", device_name, sizeof(device_name));
    }
    if (g_add_zkt.platform[0]) {
        strlcpy(platform, g_add_zkt.platform, sizeof(platform));
    } else {
        (void)zk_read_option(sock, &ctx, "~Platform", platform, sizeof(platform));
    }
    (void)zk_get_time(sock, &ctx, device_time, sizeof(device_time));
    ip_to_text(host_order_ip, ip_text, sizeof(ip_text));
    strlcpy(g_add_zkt.serial, g_device_serial, sizeof(g_add_zkt.serial));
    strlcpy(g_add_zkt.model, device_name, sizeof(g_add_zkt.model));
    strlcpy(g_add_zkt.platform, platform, sizeof(g_add_zkt.platform));
    strlcpy(g_add_zkt.ip_address, ip_text, sizeof(g_add_zkt.ip_address));
    if (device_time[0]) {
        strlcpy(g_add_zkt.device_time, device_time, sizeof(g_add_zkt.device_time));
        g_add_zkt.device_time_sampled_epoch = epoch_now();
    }
    int32_t user_count = 0;
    int32_t records = 0;
    struct tm device_now = {0};
    if (!zk_get_counts(sock, &ctx, &user_count, &records)) {
        zk_disconnect(sock, &ctx); close(sock); return uptime_ms() - session_started_ms;
    }
    g_add_zkt.user_count = user_count;
    g_add_zkt.attendance_count = records;
    g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
    zkt_mark_authenticated(host_order_ip, "live session authenticated and identified");
    led_status_set(LED_STATUS_ZKT_AUTHENTICATED);
    user_table_t *users = heap_caps_calloc(1, sizeof(user_table_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!users) users = calloc(1, sizeof(user_table_t));
    if (!users) {
        ESP_LOGE(TAG, "Could not allocate ZKT user table");
        zk_disconnect(sock, &ctx); close(sock); return uptime_ms() - session_started_ms;
    }
    if (!zk_load_users(sock, &ctx, users, user_count)) {
        zk_disconnect(sock, &ctx); close(sock); free(users); return uptime_ms() - session_started_ms;
    }
    g_add_zkt.user_record_size = users->record_size;
    add_connector_set_zkt(&g_add_zkt);
    (void)add_connector_consume_connected_edge();
    (void)add_send_user_snapshot(users);

    uint8_t rx[1024];
    zk_response_t response = {0};
    (void)zk_send_command(sock, &ctx, CMD_CANCELCAPTURE, NULL, 0, rx, sizeof(rx), &response);
    (void)zk_send_command(sock, &ctx, CMD_STARTVERIFY, NULL, 0, rx, sizeof(rx), &response);
    if (!zk_register_attlog_events(sock, &ctx, true)) {
        zk_disconnect(sock, &ctx); close(sock); free(users); return uptime_ms() - session_started_ms;
    }

    int64_t now_ms = uptime_ms();
    int64_t last_reconcile = now_ms - ZONE_LITE_RECONCILE_INTERVAL_MS + ZONE_LITE_RECOVERY_STABILITY_MS;
    int64_t last_live_register = now_ms;
    int64_t last_user_integrity = now_ms;
    int64_t last_time_sample = 0;
    size_t live_events_since_sync = 0;
    bool restarted = false;
    while (true) {
        fd_set read_fds;
        FD_ZERO(&read_fds);
        FD_SET(sock, &read_fds);
        struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
        int rc = select(sock + 1, &read_fds, NULL, NULL, &tv);
        if (rc > 0 && FD_ISSET(sock, &read_fds)) {
            zk_tcp_header_t top;
            if (!recv_exact(sock, (uint8_t *)&top, sizeof(top)) ||
                top.marker_1 != MACHINE_PREPARE_DATA_1 || top.marker_2 != MACHINE_PREPARE_DATA_2 ||
                top.length < sizeof(zk_header_t) || top.length > 4096) {
                ESP_LOGW(TAG, "ZKT live socket returned an invalid packet");
                break;
            }
            uint8_t *packet = malloc(top.length);
            if (!packet || !recv_exact(sock, packet, top.length)) {
                free(packet); ESP_LOGW(TAG, "Could not read complete ZKT live packet"); break;
            }
            zk_header_t *header = (zk_header_t *)packet;
            if (header->command == CMD_REG_EVENT && top.length > sizeof(zk_header_t)) {
                live_events_since_sync += process_live_packet(
                    packet + sizeof(zk_header_t),
                    top.length - sizeof(zk_header_t),
                    users);
                if (!zk_send_ack_only(sock, ctx.session_id)) { free(packet); break; }
            }
            free(packet);
        } else if (rc < 0) {
            ESP_LOGW(TAG, "ZKT live socket select failed errno=%d", errno);
            break;
        }

        now_ms = uptime_ms();
        if (now_ms - g_session_stable_since_ms >= ZONE_LITE_RECOVERY_STABILITY_MS) {
            zkt_mark_stable();
            if (!file_has_nonempty_line(PENDING_PATH)) led_status_set(LED_STATUS_HEALTHY);
        }
        if (process_add_commands(sock, &ctx, users, &user_count)) {
            restarted = true;
            break;
        }
        if (add_connector_consume_connected_edge()) {
            add_connector_log("INFO", "add", "ADD_RECONNECTED", "ADD channel recovered; publishing a fresh full user snapshot");
            (void)add_send_user_snapshot(users);
        }
        (void)temp_admin_revoke_if_due(sock, &ctx, users);

        if (now_ms - last_time_sample >= 60000) {
            last_time_sample = now_ms;
            g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
            char device_time[32] = {0};
            if (zk_get_time(sock, &ctx, device_time, sizeof(device_time))) {
                strlcpy(g_add_zkt.device_time, device_time, sizeof(g_add_zkt.device_time));
                g_add_zkt.device_time_sampled_epoch = epoch_now();
                g_add_zkt.consecutive_successes++;
            }
            add_connector_set_zkt(&g_add_zkt);
        }

        if (now_ms - last_reconcile >= ZONE_LITE_RECONCILE_INTERVAL_MS &&
            now_ms - g_session_stable_since_ms >= ZONE_LITE_RECOVERY_STABILITY_MS) {
            last_reconcile = now_ms;
            int32_t refreshed_users = 0;
            int32_t refreshed_records = 0;
            add_connector_set_activity("RECONCILING");
            if (!zk_get_counts(sock, &ctx, &refreshed_users, &refreshed_records)) break;
            bool integrity_due = now_ms - last_user_integrity >= (6 * 60 * 60 * 1000LL);
            if (refreshed_users != user_count || integrity_due) {
                if (!zk_refresh_users_preserving_current(sock, &ctx, users, refreshed_users)) break;
                user_count = refreshed_users;
                last_user_integrity = now_ms;
                (void)add_send_user_snapshot(users);
            }
            int64_t record_delta = g_last_synced_attendance_count >= 0
                ? (int64_t)refreshed_records - g_last_synced_attendance_count
                : -1;
            bool counter_mismatch = record_delta < 0 || (uint64_t)record_delta != live_events_since_sync;
            int64_t current_epoch = epoch_now();
            bool epoch_valid = current_epoch > 1700000000;
            bool truth_due = g_last_synced_attendance_count < 0 ||
                (g_last_full_truth_reconcile_epoch == 0 && g_last_full_truth_reconcile_ms == 0);
            if (!truth_due && epoch_valid && g_last_full_truth_reconcile_epoch > 0) {
                int64_t elapsed_seconds = current_epoch - g_last_full_truth_reconcile_epoch;
                truth_due = elapsed_seconds < 0 ||
                    elapsed_seconds >= ZONE_LITE_FULL_TRUTH_RECONCILE_MS / 1000;
            } else if (!truth_due && g_last_full_truth_reconcile_epoch == 0 &&
                       g_last_full_truth_reconcile_ms > 0) {
                truth_due = now_ms - g_last_full_truth_reconcile_ms >=
                    ZONE_LITE_FULL_TRUTH_RECONCILE_MS;
            }
            bool reconcile_succeeded = true;
            if (counter_mismatch || truth_due) {
                char reason[192];
                snprintf(
                    reason,
                    sizeof(reason),
                    "Full reconcile: device_delta=%lld live_observed=%u periodic_truth=%s",
                    (long long)record_delta,
                    (unsigned)live_events_since_sync,
                    truth_due ? "true" : "false");
                ESP_LOGI(TAG, "%s", reason);
                add_connector_log("INFO", "reconcile", "FULL_RECONCILE", reason);
                if (!zk_get_time_parts(sock, &ctx, &device_now)) break;
                led_status_set(LED_STATUS_SYNCING);
                size_t added = 0;
                const char *reconcile_capturetype = g_last_synced_attendance_count < 0
                    ? "DUMP_STARTUP"
                    : "DUMP_RECONNECT";
                if (reconcile_attendance_dump(
                        sock,
                        &ctx,
                        users,
                        refreshed_records,
                        reconcile_capturetype,
                        device_now.tm_year + 1900,
                        device_now.tm_mon + 1,
                        &added)) {
                    g_last_synced_attendance_count = refreshed_records;
                    live_events_since_sync = 0;
                    g_last_full_truth_reconcile_ms = now_ms;
                    if (epoch_valid) {
                        g_last_full_truth_reconcile_epoch = current_epoch;
                    }
                    nvs_save_runtime_state();
                } else {
                    reconcile_succeeded = false;
                    add_connector_log(
                        "ERROR",
                        "reconcile",
                        "FULL_RECONCILE_FAILED",
                        "Attendance truth read failed; live capture remains active and the next 15-minute cycle will retry");
                }
            } else {
                char summary[160];
                snprintf(
                    summary,
                    sizeof(summary),
                    "Light reconcile passed: device_delta=%lld matched %u live events; heavy dump skipped",
                    (long long)record_delta,
                    (unsigned)live_events_since_sync);
                ESP_LOGI(TAG, "%s", summary);
                add_connector_log("INFO", "reconcile", "LIGHT_RECONCILE_OK", summary);
                g_last_synced_attendance_count = refreshed_records;
                live_events_since_sync = 0;
                nvs_save_runtime_state();
            }
            g_add_zkt.user_count = refreshed_users;
            g_add_zkt.attendance_count = refreshed_records;
            if (reconcile_succeeded) g_add_zkt.last_reconcile_epoch = epoch_now();
            add_connector_set_zkt(&g_add_zkt);
            add_connector_set_activity("LIVE_CAPTURE");
            if (!file_has_nonempty_line(PENDING_PATH)) led_status_set(LED_STATUS_HEALTHY);
        }

        if (now_ms - last_live_register >= ZKT_LIVE_REREGISTER_INTERVAL_MS) {
            if (!zk_register_attlog_events(sock, &ctx, true)) break;
            last_live_register = now_ms;
        }

        int restart_slot = -1;
        if (daily_zkt_reboot_should_attempt(&restart_slot)) {
            add_connector_set_activity("SCHEDULED_RESTART");
            if (zk_protocol_restart(sock, &ctx)) {
                daily_zkt_reboot_mark_complete(restart_slot);
                g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
                add_connector_set_zkt(&g_add_zkt);
                add_connector_log("INFO", "zkt", "SCHEDULED_RESTART", "ZKT accepted its scheduled protocol restart");
                restarted = true;
                break;
            }
        }
    }
    if (!restarted) {
        (void)zk_register_attlog_events(sock, &ctx, false);
        zk_disconnect(sock, &ctx);
    }
    close(sock);
    free(users);
    int64_t duration = uptime_ms() - session_started_ms;
    if (restarted) {
        zkt_publish_state("RESTARTING", "ZKT protocol restart accepted", false);
        vTaskDelay(pdMS_TO_TICKS(ZONE_LITE_ZKT_REBOOT_WAIT_MS));
    }
    return duration;
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
    int64_t offline_started_ms = 0;
    while (true) {
        if ((xEventGroupGetBits(wifi_event_group) & WIFI_CONNECTED_BIT) == 0) {
            ESP_LOGW(TAG, "Waiting for Wi-Fi before ZKT discovery");
            (void)wait_for_wifi();
        }
        g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
        add_connector_set_zkt(&g_add_zkt);
        int daily_reboot_day = -1;
        if (daily_zkt_reboot_should_attempt(&daily_reboot_day)) {
            if (daily_zkt_reboot_try_target(daily_zkt_reboot_target_ip(), daily_reboot_day)) {
                discovery_failures = 0;
                continue;
            }
        }
        uint32_t selected_ip = 0;
        uint32_t directly_tried_ip = 0;
        if (g_last_authenticated_zkt_ip != 0) {
            directly_tried_ip = g_last_authenticated_zkt_ip;
            char direct_ip[16];
            ip_to_text(directly_tried_ip, direct_ip, sizeof(direct_ip));
            ESP_LOGI(TAG, "Opening one live session to last authenticated ZKT %s:%d", direct_ip, ZONE_LITE_ZKT_PORT);
            zkt_publish_state("CONNECTING", "opening live session to last authenticated terminal", false);
            int64_t session_duration = gateway_run(directly_tried_ip);
            if (session_duration > 0) {
                discovery_failures = 0;
                offline_started_ms = 0;
                if (strcmp(g_add_zkt.connection_state, "RESTARTING") == 0) {
                    continue;
                }
                uint32_t backoff = zkt_mark_failure(
                    session_duration < ZONE_LITE_RECOVERY_STABILITY_MS
                        ? "ZKT session ended before the stability window"
                        : "established ZKT session disconnected");
                ESP_LOGW(
                    TAG,
                    "ZKT session ended after %lld ms; retrying in %lu ms",
                    (long long)session_duration,
                    (unsigned long)backoff);
                vTaskDelay(pdMS_TO_TICKS(backoff));
                continue;
            }
        }
        if (discover_zkt(&selected_ip, directly_tried_ip)) {
            discovery_failures = 0;
            offline_started_ms = 0;
            int64_t session_duration = gateway_run(selected_ip);
            if (strcmp(g_add_zkt.connection_state, "RESTARTING") == 0) {
                discovery_failures = 0;
                continue;
            }
            uint32_t backoff = zkt_mark_failure(
                session_duration < ZONE_LITE_RECOVERY_STABILITY_MS
                    ? "ZKT session ended before the stability window"
                    : "established ZKT session disconnected");
            ESP_LOGW(TAG, "ZKT session ended after %lld ms; retrying in %lu ms", (long long)session_duration, (unsigned long)backoff);
            vTaskDelay(pdMS_TO_TICKS(backoff));
            continue;
        } else {
            discovery_failures++;
            if (offline_started_ms == 0) offline_started_ms = uptime_ms();
            uint32_t backoff = zkt_mark_failure("authenticated discovery did not find the assigned ZKT");
            ESP_LOGW(
                TAG,
                "No authenticated ZKT device found on port %d (failure %lu, backoff=%lu ms)",
                ZONE_LITE_ZKT_PORT,
                (unsigned long)discovery_failures,
                (unsigned long)backoff);
            bool continuously_offline = uptime_ms() - offline_started_ms >= (10 * 60 * 1000);
            bool flapping = strcmp(g_add_zkt.connection_state, "FLAPPING") == 0;
            if (continuously_offline && !flapping &&
                maybe_reboot_zkt_for_recovery(discovery_failures, &last_zkt_reboot_ms)) {
                discovery_failures = 0;
                offline_started_ms = 0;
            }
            vTaskDelay(pdMS_TO_TICKS(backoff));
            continue;
        }
    }
}

void app_main(void)
{
    setenv("TZ", "UTC0", 1);
    tzset();
    led_status_init();
    led_status_set(LED_STATUS_BOOTING);
    esp_err_t ret = nvs_flash_init();
    if (ret != ESP_OK) {
        ESP_LOGE(
            TAG,
            "Encrypted NVS initialization failed (%s); preserving provisioning data and staying inert",
            esp_err_to_name(ret));
        led_status_fault(LED_STATUS_FATAL);
        return;
    }
    ESP_ERROR_CHECK(zone_config_init());
    if (!zone_config_get()->provisioned) {
        ESP_LOGE(TAG, "Encrypted per-device provisioning is required; network startup is blocked");
        led_status_fault(LED_STATUS_FATAL);
        return;
    }
    nvs_load_runtime_state();
    g_storage_lock = xSemaphoreCreateMutex();
    g_ords_http_lock = xSemaphoreCreateMutex();
    g_ords_outbox_gate = xSemaphoreCreateMutex();
    if (!g_storage_lock || !g_ords_http_lock || !g_ords_outbox_gate) {
        ESP_LOGE(TAG, "Could not create durable storage or ORDS coordination locks");
        led_status_fault(LED_STATUS_FATAL);
        return;
    }
    add_connector_init();
    zkt_publish_state("BOOTING", "ESP32 firmware boot", false);
    ESP_LOGI(TAG, "Zone Lite starting zone=%s device_id=%s", ZONE_LITE_ZONE_ID, ZONE_LITE_ZONE_DEVICE_ID);
    storage_init();
    wifi_init_sta();
    if (!wait_for_wifi()) {
        return;
    }
    (void)ensure_system_time_synced();
    g_add_zkt.next_restart_epoch = daily_zkt_reboot_next_epoch();
    add_connector_set_zkt(&g_add_zkt);
    add_connector_start();
    if (xTaskCreate(ords_uploader_task, "ords_uploader", 16384, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not start ORDS outbox uploader task");
        led_status_fault(LED_STATUS_FATAL);
    }
    if (xTaskCreate(gateway_task, "zone_gateway", 24576, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not start Zone Lite gateway task");
        led_status_fault(LED_STATUS_FATAL);
    }
}
