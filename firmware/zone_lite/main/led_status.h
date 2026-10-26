#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    LED_STATUS_BOOTING = 0,
    LED_STATUS_WIFI_CONNECTING,
    LED_STATUS_ZKT_DISCOVERING,
    LED_STATUS_ZKT_AUTHENTICATED,
    LED_STATUS_SYNCING,
    LED_STATUS_HEALTHY,
    LED_STATUS_BACKLOG,
    LED_STATUS_ORDS_FAILURE,
    LED_STATUS_ZKT_FAILURE,
    LED_STATUS_ZKT_FLAPPING,
    LED_STATUS_TRUTH_REPAIR,
    LED_STATUS_RECOVERY_REBOOT,
    LED_STATUS_LOCAL_FAILURE,
    LED_STATUS_FATAL,
    LED_STATUS_BLOCKED_IDENTITY,
    /* ZKT 2.7.5: the boot storage repair is writing; do not power the unit off. */
    LED_STATUS_STORAGE_REPAIR,
} led_status_t;

typedef enum {
    LED_EVENT_LIVE_PUNCH = 0,
} led_status_event_t;

void led_status_init(void);
void led_status_set(led_status_t status);
void led_status_fault_at(led_status_t status, const char *file, int line);
#define led_status_fault(status) led_status_fault_at((status), __FILE__, __LINE__)
/* ZKT 2.7.7: a fault with the errno its site captured (0 when it has none).
 * For LOCAL_FAILURE, ENOSPC counts as a store write refused for room; every
 * other errno is an ordinary incident. The confirm-first storage exception
 * accepts a latched LOCAL_FAILURE only when every incident of the boot was
 * a room refusal or a held read (below). */
void led_status_fault_errno_at(led_status_t status, int error, const char *file, int line);
#define led_status_fault_errno(status, error) led_status_fault_errno_at((status), (error), __FILE__, __LINE__)
/* The reclaim queues, in storage_exception.h's sx_queue_names order. */
typedef enum {
    LED_QUEUE_PENDING = 0,      /* /pending.jsonl */
    LED_QUEUE_BLOCKED,          /* /blocked_identity.jsonl */
    LED_QUEUE_CORRUPT_ORDS,     /* /corrupt_ords.jsonl */
    LED_QUEUE_ADD_CORRUPT,      /* /add_corrupt.jsonl */
    LED_QUEUE_ADD_CORRUPT_BAK,  /* /add_corrupt.bak */
    LED_QUEUE_COUNT,
} led_queue_t;
/* A read of a reclaim queue that started at byte `offset` and failed with
 * `error`: EIO counts as a read of a span the boot repair may hold on flash
 * (the exception checks the queue and the offset against the held spans);
 * so does ENOENT (ZKT 2.7.8: ESP-IDF's errno for a span whose index entry
 * points at a deleted page), but only when `from_read` says it came from a
 * read or seek of the open file, never from its open (a missing file). Any
 * other errno is an ordinary incident. */
void led_status_fault_read_at(led_status_t status, int error, bool from_read, led_queue_t queue, uint32_t offset,
                              const char *file, int line);
#define led_status_fault_read(status, error, from_read, queue, offset)                                                 \
    led_status_fault_read_at((status), (error), (from_read), (queue), (offset), __FILE__, __LINE__)
void led_status_clear_fault(led_status_t status);
/* LOCAL_FAILURE incidents raised this boot (wrapping) and whether the latch
 * is on. A latch that cannot be read is reported as on. */
uint32_t led_status_local_failures(bool *active);
/* Clear the LOCAL_FAILURE latch only if no incident was raised since `seen`.
 * The local-recovery supervisor calls this once storage has been verified
 * after the latest incident; nothing clears it on a timer. */
bool led_status_clear_local_failure(uint32_t seen);
typedef struct {
    bool active;
    uint32_t incidents, recoveries;
    char first_source[80], last_source[80];
} led_local_failure_t;
void led_status_local_failure_report(led_local_failure_t *report);
/* ZKT 2.7.7: this boot's LOCAL_FAILURE incidents by cause; `known` false
 * when the state lock was busy. */
typedef struct {
    bool known, fatal;
    uint32_t incidents, room, held_reads, held_queues; /* held_queues: bit per led_queue_t */
    uint32_t held_last[LED_QUEUE_COUNT];               /* the largest start offset of a held read, per queue */
} led_local_causes_t;
void led_status_local_causes(led_local_causes_t *causes);
void led_status_event(led_status_event_t event);
void led_status_set_backlog(bool has_backlog);
const char *led_status_current_name(void);
