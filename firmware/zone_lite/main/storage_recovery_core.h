#pragma once
/* One-shot custody transfer of retained legacy blocked-identity rows to ADD.
 *
 * Every readable row of every source generation must receive a matching
 * durable ADD receipt before anything is removed. Removal only happens after
 * that, and the retired event UIDs are then appended in the legacy acked-UID
 * format so the 2.5.2 rollback image does not queue the same punches again.
 *
 * A source region the filesystem cannot read is located with unbuffered
 * probes, reported to ADD with its exact offset and length, and skipped. A
 * scan pass measures every such region before anything is sent; more than
 * SR_UNREADABLE_LIMIT unreadable bytes refuses the run with nothing changed.
 * The engine has no ESP-IDF dependency so the host tests execute it. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SR_RECORD_MAX_BYTES 8192U /* ADD queue_evidence raw-byte limit */
#define SR_UID_BYTES 32U
#define SR_PROGRESS_INTERVAL 500U
#define SR_PROBE_BYTES 32U /* Resolution of an unreadable region */
/* Owner-approved (9 October 2026) maximum of unreadable bytes discarded by
 * one run; such bytes are unreadable by every image, including 2.5.2. */
#define SR_UNREADABLE_LIMIT (16U * 1024U)

typedef enum { SR_SEND_ACKED = 0, SR_SEND_RETRY, SR_SEND_STOP } sr_send_t;

typedef struct {
    void *context;
    /* Return SR_SEND_ACKED only after ADD durably acknowledged these exact
     * bytes. SR_SEND_RETRY repeats the same record; SR_SEND_STOP abandons the
     * run before any file is changed. */
    sr_send_t (*send)(void *context, const char *generation, const char *record_id,
                      const char *bytes, size_t length, const char *terminal_serial,
                      const char *reason);
    void (*log)(void *context, const char *level, const char *code, const char *message);
    /* Called once after every row has a receipt and before the first removal.
     * Returning false leaves every source file unchanged. */
    bool (*before_retire)(void *context);
} sr_ports_t;

typedef enum {
    SR_NOTHING_TO_DO = 0,
    SR_COMPLETE,
    SR_COMPLETE_SEEN_PARTIAL, /* Rows retired; some UIDs could not be recorded. */
    SR_INCOMPLETE,            /* Stopped before retirement; sources unchanged. */
    SR_RETIRE_PARTIAL,        /* All rows receipted; a removal failed. */
    SR_REFUSED,               /* Input cannot be transferred exactly; unchanged. */
} sr_result_t;

typedef struct {
    sr_result_t result;
    char code[48];
    uint32_t files, records, malformed, uids, uids_appended, sends, retries;
    uint32_t gaps;            /* Unreadable regions skipped by the transfer */
    uint64_t bytes, unreadable_bytes;
} sr_outcome_t;

/* Sources are retired in the given order after all of them are receipted.
 * Missing or empty sources are skipped. The generation label is derived from
 * the source index and its exact byte length at the start of the run. */
void sr_transfer_and_retire(const char *const *sources, size_t source_count,
                            const char *acked_path, const sr_ports_t *ports,
                            sr_outcome_t *outcome);
const char *sr_result_name(sr_result_t result);
/* True when a row is one the transfer classifies LEGACY_RECOVERY and carries a
 * 64-hex event_uid; that UID is what retirement records as seen. */
bool sr_row_event_uid(const char *row, size_t length, uint8_t uid[SR_UID_BYTES]);
/* Appends binary UIDs in the legacy acked-UID text format, syncing every 256
 * rows; *appended counts only synced rows. */
bool sr_append_uids(const char *acked_path, const uint8_t *uids, uint32_t count, uint32_t *appended);
