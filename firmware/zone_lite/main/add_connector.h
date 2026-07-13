#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    char command_id[48];
    char command_type[32];
    char uid[16];
    char name[64];
    char lease_id[48];
    int privilege;
    int duration_seconds;
    bool has_name;
    bool has_privilege;
} add_command_t;

typedef struct {
    bool online;
    char connection_state[24];
    char ip_address[16];
    char serial[80];
    char model[80];
    char platform[80];
    char device_time[32];
    int64_t device_time_sampled_epoch;
    char transition_reason[96];
    int32_t user_count;
    int32_t attendance_count;
    uint32_t consecutive_failures;
    uint32_t consecutive_successes;
    uint32_t flap_count_15m;
    uint32_t probe_latency_ms;
    uint32_t user_record_size;
    int64_t backoff_until_epoch;
    int64_t stability_since_epoch;
    int64_t last_reconcile_epoch;
    int64_t next_restart_epoch;
} add_zkt_telemetry_t;

void add_connector_init(void);
void add_connector_start(void);
bool add_connector_is_connected(void);
bool add_connector_consume_connected_edge(void);
uint32_t add_connector_outbox_depth(void);
void add_connector_set_activity(const char *activity);
void add_connector_set_zkt(const add_zkt_telemetry_t *telemetry);
bool add_connector_take_command(add_command_t *out);
bool add_connector_command_update(
    const char *command_id,
    const char *status,
    const char *error_code,
    const char *error_message,
    const char *result_json);
bool add_connector_send_payload(const char *type, const char *payload_json);
bool add_connector_enqueue_attendance(const char *payload_json);
bool add_connector_enqueue_attendance_bulk(const char *const *payloads, size_t count);
bool add_connector_log(
    const char *level,
    const char *subsystem,
    const char *code,
    const char *message);
