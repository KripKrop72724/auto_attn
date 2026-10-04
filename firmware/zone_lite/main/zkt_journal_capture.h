#pragma once
#include "zkt_storage_mailbox.h"
#include "zkt_capture_latency.h"

#define ZJ_PACKET_MAX 65536U
#define ZJ_FRAGMENT_HEADER 60U
#define ZJ_FRAGMENT_DATA (ZJ_RAW_MAX - ZJ_FRAGMENT_HEADER)

typedef struct {
    uint32_t (*now_ms)(void *context);
    void (*wait_ms)(void *context, uint32_t milliseconds);
    bool (*random)(void *context, uint8_t *out, size_t length);
    bool (*submit)(void *context, const zj_request_t *request, uint64_t *ticket);
    bool (*poll)(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete);
    bool (*abandon)(void *context, uint64_t ticket);
    void *context;
    zj_crypto_port_t crypto;
} zj_capture_port_t;
typedef struct {
    int64_t wall_seconds;
    uint64_t uptime_ms;
    uint32_t identity_revision;
    zj_time_quality_t time_quality;
} zj_capture_facts_t;
typedef struct {
    uint64_t pending_ticket, packets, fragments, failures, timeouts, first_sequence, last_sequence;
    uint32_t started_ms, progress_ms, sampled_ms;
    size_t committed_bytes;
    zj_result_t last_result;
    bool running;
    /* Successful complete operations only. Failures/timeouts above remain
     * separate evidence, including a packet with only some fragments stored. */
    zj_capture_latency_t packet_commit_latency, fragment_commit_latency;
} zj_capture_health_t;
typedef struct {
    zj_capture_port_t port;
    zj_capture_health_t health;
    zj_request_t request;
    zj_reply_t reply;
    bool initialized;
} zj_capture_t;

bool zj_capture_init(zj_capture_t *capture, zj_capture_port_t port);
/* Called by the one terminal-session owner before a protocol ACK. packet is
 * the complete ZKT header and payload, after transport/session checks, before
 * semantic decoding. The caller keeps it alive for this bounded call only.
 * False never authorizes ACK. A partial/uncertain capture stays recoverable;
 * accepted storage work is not cancelled when its reply times out. */
bool zj_capture_packet(zj_capture_t *capture, const uint8_t *packet, size_t length,
                        const zj_capture_facts_t *facts);
