#pragma once
/* ZKT 2.7.7: the confirm-first storage exception (owner decision, 21 October
 * 2026, on 2.7.6's open decision 3: (a) confirm first, as narrowly as
 * possible).
 *
 * A new image whose store arrived wedged (the boot repair deferred for room
 * only, every custody byte on flash, the planner's own count of droppable
 * custody pages covering the shortfall) may mark itself valid although the
 * store's health terms fail, so that 2.7.6's confirmed round, unchanged, can
 * reformat custody blocks in place on the next boot. Every other storage
 * failure, and every other failing boot-health term, rolls back exactly as
 * 2.7.6 does. See docs/zone-lite-2.7.7-confirm-first.md.
 *
 * This module is the decision only: inputs collected by the firmware glue
 * (storage_repair.c, add_connector.c, ota_manager.c) go in, a reason comes
 * out. It has no ESP-IDF dependency so the host tests run it, and the SPIFFS
 * harness feeds it the engine's own report. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Every term must hold this long, without a refusal, before the image
 * confirms (ZONE_LITE_RECOVERY_STABILITY_MS). */
#define SX_STABLE_SECONDS 120U
/* What the confirmation sequence (an acknowledged log, a receipted progress
 * report, the re-check) must still have of the 900 s budget when it starts:
 * two signed HTTPS calls of 15 s, an acknowledgement of 10 s, and margin. */
#define SX_BUDGET_MARGIN_SECONDS 90U
/* A confirmed boot with the round done whose storage terms still fail this
 * long (ADD connected) ends the deployment FAILED UNHEALTHY. */
#define SX_UNHEALTHY_SECONDS (30U * 60U)
/* Controlled restarts per confirmation, and the backoff before the second
 * and the third. */
#define SX_MAX_RESTARTS 3U
#define SX_RETRY_SECONDS_2 (60U * 60U)
#define SX_RETRY_SECONDS_3 (6U * 60U * 60U)
/* The confirmed reformat round's cap (2.7.6, SR_RECLAIM_CAP). */
#define SX_ROUND_BLOCK_CAP 256U
/* The internal heap a confirmed image keeps for TLS and the WebSocket's
 * restart (its 6 KiB stack and TCB, twice while the old one is freed). */
#define SX_INTERNAL_FLOOR_BYTES 16384U
/* An exception device out of ADD's reach this long polls OTA anyway. */
#define SX_OTA_WITHOUT_ADD_SECONDS (10U * 60U)

typedef enum {
    SX_STATE_NONE = 0,
    SX_STATE_EVALUATING,  /* unconfirmed: the gate has not yet held */
    SX_STATE_REFUSED,     /* unconfirmed: a term fails (reason); the image rolls back at the budget's end */
    SX_STATE_ELIGIBLE,    /* unconfirmed: every term holds, stabilizing or confirming */
    SX_STATE_CONFIRMING,  /* the record before esp_ota_mark_app_valid_cancel_rollback */
    SX_STATE_CONFIRMED,   /* valid; the round boot has not reported yet */
    SX_STATE_ROUND_DONE,  /* the round boot left a repaired store */
    SX_STATE_ROUND_SHORT, /* the round boot did not (reason: its outcome) */
    SX_STATE_UNREPAIRED,  /* no further controlled restart can help: deployment FAILED */
    SX_STATE_UNHEALTHY,   /* round done, storage health still failing: deployment FAILED */
    SX_STATE_ABANDONED,   /* CONFIRMING when the budget ran out: rolled back */
    SX_STATE_COUNT,
} sx_state_t;

typedef enum {
    SX_OK = 0,
    /* which boots */
    SX_NOT_PENDING, SX_ROLE, SX_NOT_OTA_IMAGE, SX_ALREADY_USED,
    /* the boot repair deferred for room only */
    SX_REPAIR_MISSING, SX_REPAIR_RESULT, SX_REPAIR_STAGE, SX_REPAIR_GUARD, SX_SHORT_REASON, SX_AREA_NOT_READY,
    SX_AREA_TEST, SX_RECLAIM_FAULT, SX_ROUND_RAN, SX_MANIFEST, SX_ARRIVED_WITH_ROOM, SX_CHECK_DELETED,
    /* custody */
    SX_CUSTODY_UNHELD, SX_CUSTODY_LOST, SX_RECORDS_UNACKED, SX_INVENTORY_PENDING, SX_INVENTORY_UNEXPLAINED,
    SX_CUSTODY_MISSING,
    /* room by the planner's own estimate */
    SX_ROOM_UNKNOWN, SX_CUSTODY_SHORT, SX_CUSTODY_BLOCKS,
    /* runtime storage evidence */
    SX_STORAGE_UNKNOWN, SX_STORAGE_ERROR_CLASS, SX_PROBE_ERROR_CLASS, SX_LED_FATAL, SX_LED_CAUSE,
    SX_STORE_NOT_MOUNTED, SX_STORE_CHANGED,
    /* every other boot-health term */
    SX_HEALTH, SX_HEALTH_UNKNOWN,
    /* ADD and the OTA path */
    SX_ADD_DISCONNECTED, SX_CAPABILITY, SX_OTA_PRECHECK, SX_OTA_SLOT, SX_HEAP,
    /* the confirmation itself */
    SX_STABILIZING, SX_EVIDENCE_UNACKED, SX_RECEIPT, SX_BUDGET, SX_NVS_RECORD, SX_MARK_VALID,
    SX_REASON_COUNT,
} sx_reason_t;

const char *sx_state_name(sx_state_t state);
const char *sx_reason_name(sx_reason_t reason);
/* A reason that says "not known yet" (a lock was busy): it neither refuses
 * nor counts toward the stable window. */
bool sx_reason_unknown(sx_reason_t reason);

/* ---- inputs ---------------------------------------------------------------- */
#define SX_NAME_BYTES 33U
#define SX_REPAIR_FILES 8U
#define SX_INVENTORY_FILES 16U

typedef struct {
    char name[SX_NAME_BYTES];   /* the repair's name, with its leading '/' */
    bool custody;               /* attendance, evidence, operational, dedupe or unknown class */
    bool removed, unheld;
    uint32_t size;
    uint32_t blocked_spans, blocked_offset, blocked_last, blocked_digest;
} sx_repair_file_t;

/* The boot repair's report as the glue merged it over the boot's runs, the
 * redo area and the boot's own facts. Codes are the engine's names. */
typedef struct {
    bool ran;                   /* storage_repair_boot ran and a report exists */
    bool mount_failed;          /* the private mount failed */
    bool storage_allowed;       /* storage_repair_storage_allowed() */
    bool pending_verify;        /* the repair ran on an unconfirmed image */
    bool previous_failed;       /* the attempts guard counted the previous boot */
    char result[24], mode[24], error_stage[24];
    int32_t error_code;
    uint32_t files_unheld_custody, gaps, unreadable_bytes, records_dropped, interrupted_losses;
    bool commands_damaged, manifest_active;
    uint32_t check_deleted_live, check_delete_bad_file;
    uint32_t records_pending;   /* gap and reset records ADD has not acknowledged */
    /* the last resort */
    char area[16], area_test[16], trigger[12], short_reason[12];
    uint32_t rounds, blocks, pages_freed, failed, held_mismatch, records_open, reported, sources, waiting;
    bool store_blocked, mount_measured;
    uint32_t want, target, custody_gain, custody_blocks, erased_at_mount;
    uint32_t file_count;
    sx_repair_file_t file[SX_REPAIR_FILES];
} sx_repair_t;

typedef struct {
    char name[SX_NAME_BYTES];   /* the directory entry's name, no '/' */
    uint32_t bytes, unreadable_bytes, first_error_offset;
    int first_error;
    uint32_t unreadable_spans, last_error_offset, span_digest, span_errors_other;
    bool other_unreadable;
} sx_inventory_file_t;

/* 2.7.4's read-only inventory (storage_inventory.h), finished or not. */
typedef struct {
    bool done, complete;
    int error;
    uint32_t files, unreadable_files;
    uint32_t count;
    sx_inventory_file_t file[SX_INVENTORY_FILES];
} sx_inventory_t;

/* Reclaim queues whose reads may fail on a span the repair holds (bit i:
 * sx_queue_names[i]). */
#define SX_QUEUE_COUNT 5U
extern const char *const sx_queue_names[SX_QUEUE_COUNT];

/* What the running image sees now. */
typedef struct {
    /* qs_health(): known only when its lock was taken */
    bool qs_known, qs_observed, qs_available;
    int last_error, probe_error;
    const char *last_operation, *probe_operation;
    /* the LED's LOCAL_FAILURE incidents this boot, by cause (led_status.h) */
    bool led_known, led_fatal;
    uint32_t local_failures, room_failures, held_read_failures, held_read_queues;
    /* the VFS: mounted, and SPIFFS's used and total bytes now */
    bool spiffs_mounted;
    uint32_t spiffs_used, spiffs_total;
    /* add_connector_boot_health_blockers(): every boot-health term that
     * fails, and whether the check could run (its lock) */
    bool health_known;
    uint32_t health_blockers;
    /* ADD and the OTA path */
    bool add_connected, capability_accepted, ota_precheck_ok, passive_slot_ok;
    uint32_t internal_largest;
} sx_runtime_t;

/* The boot-health terms (add_connector_boot_health_blockers); the exception
 * excepts only SX_HEALTH_EXCEPTED, each bounded by its own runtime term. */
enum {
    SX_H_STORAGE_UPGRADE = 1U << 0,
    SX_H_JOURNAL_RUNTIME = 1U << 1,
    SX_H_SPIFFS_INFO = 1U << 2,
    SX_H_QS_OBSERVED = 1U << 3,
    SX_H_QS_RECOVERY = 1U << 4,
    SX_H_QS_PERSISTENCE = 1U << 5,
    SX_H_QS_LAST_ERROR = 1U << 6,
    SX_H_QS_PROBE_ERROR = 1U << 7,
    SX_H_LED_LOCAL_FAILURE = 1U << 8,
    SX_H_LED_FATAL = 1U << 9,
    SX_H_LOCK = 1U << 10,
    SX_H_ADD_CONNECTED = 1U << 11,
    SX_H_OUTBOX_TASK = 1U << 12,
    SX_H_HEARTBEAT_TASK = 1U << 13,
    SX_H_OUTBOX_BUFFER = 1U << 14,
    SX_H_WORKER_START = 1U << 15,
    SX_H_ORDS_STARTED = 1U << 16,
    SX_H_ORDS_RESOURCE = 1U << 17,
    SX_H_ORDS_TICK = 1U << 18,
    SX_H_OUTBOX_TICK = 1U << 19,
    SX_H_IDENTITY = 1U << 20,
    SX_H_TERMINAL_ONLINE = 1U << 21,
    SX_H_TERMINAL_STABLE = 1U << 22,
    SX_H_TERMINAL_COUNTS = 1U << 23,
};
#define SX_HEALTH_EXCEPTED (SX_H_QS_RECOVERY | SX_H_QS_PERSISTENCE | SX_H_QS_LAST_ERROR | SX_H_QS_PROBE_ERROR | \
                            SX_H_LED_LOCAL_FAILURE)
const char *sx_health_name(uint32_t bit);

typedef struct {
    /* which boots (the glue's facts) */
    bool production_role;       /* the ZKT production image, not a journal, recovery or factory role */
    bool running_pending;       /* the running partition is ESP_OTA_IMG_PENDING_VERIFY */
    bool ota_deployment;        /* the OTA journal is READY_TO_BOOT for this image (version and digest) */
    bool record_terminal;       /* this deployment's exception record already ended (one per deployment) */
    const sx_repair_t *repair;
    const sx_inventory_t *inventory;
    const sx_runtime_t *runtime;
} sx_input_t;

/* The gate: SX_OK when every term holds now, else the first failing term. */
sx_reason_t sx_evaluate(const sx_input_t *input);
/* The parts of it, for the harness and the host tests. */
sx_reason_t sx_repair_verdict(const sx_repair_t *repair);
sx_reason_t sx_inventory_verdict(const sx_repair_t *repair, const sx_inventory_t *inventory);
sx_reason_t sx_runtime_verdict(const sx_repair_t *repair, const sx_inventory_t *inventory, const sx_runtime_t *runtime);
/* The probe's and qs's errors that only say "no room" (H2): the operation
 * with its errno. */
bool sx_probe_error_room(const char *operation, int error);
bool sx_last_error_room(const char *operation, int error);

/* ---- the stable window -------------------------------------------------- */
typedef struct {
    bool open;
    uint32_t since_s;              /* when every term first held (uptime seconds) */
    uint32_t used, total;          /* the store's fingerprint then (SPIFFS used and total bytes) */
} sx_window_t;
/* One evaluation: SX_OK once the gate has held SX_STABLE_SECONDS with the
 * store unchanged; SX_STABILIZING before; the refusal (window closed);
 * SX_STORE_CHANGED (window reopened) when the fingerprint moved. A reason
 * sx_reason_unknown() leaves the window as it is and returns itself. */
sx_reason_t sx_window_step(sx_window_t *window, sx_reason_t verdict, uint32_t now_s, uint32_t used, uint32_t total);

/* ---- the record (NVS storage_repair/exception) --------------------------- */
#define SX_RECORD_VERSION 1U
#define SX_RECORD_KEY "exception"
typedef struct {
    uint32_t version;
    uint8_t state;                 /* sx_state_t */
    uint8_t reason;                /* sx_reason_t, or the round's sx_outcome_t when ROUND_SHORT */
    uint8_t restarts;              /* controlled restarts made, counted before each */
    uint8_t round_boots;           /* confirmed boots that ran the round's repair */
    uint8_t crashes;               /* confirmed boots that ended in a panic, watchdog or brownout */
    uint8_t reported;              /* the terminal FAILED report was receipted (UNREPAIRED, UNHEALTHY) */
    uint16_t reserved;
    uint32_t deployment_crc;       /* CRC-32 of the deployment id */
    uint32_t image_tag;            /* the image (app_elf_sha256's first word): confirmation keeps it */
    uint32_t restart_epoch;        /* system time of the last controlled restart; 0 unknown */
    uint32_t want, custody_gain;   /* the evidence it confirmed on */
    uint32_t crc;
} sx_record_t;
void sx_record_seal(sx_record_t *record);
bool sx_record_valid(const sx_record_t *record);
uint32_t sx_deployment_crc(const char *deployment_id);
/* The record belongs to this image and says it confirmed under the exception. */
bool sx_record_confirmed(const sx_record_t *record, uint32_t image_tag);
/* The record ended this deployment's exception (ABANDONED, UNREPAIRED,
 * UNHEALTHY), so no second exception for it. */
bool sx_record_ended(const sx_record_t *record, uint32_t deployment_crc);

/* ---- after a round boot ----------------------------------------------------- */
typedef enum {
    SX_ROUND_OK = 0,      /* REPAIRED, CLEAN or REPAIRED_WITH_GAPS, the store mountable */
    /* a later boot can change these */
    SX_ROUND_TIME, SX_ROUND_CAP, SX_ROUND_REMOUNT, SX_ROUND_STORE_BLOCKED, SX_ROUND_AREA_UNREADABLE,
    SX_ROUND_HOLD, SX_ROUND_WAITING, SX_ROUND_DEFERRED,
    /* no boot can */
    SX_ROUND_MISMATCH, SX_ROUND_GAPS, SX_ROUND_AREA, SX_ROUND_GUARD, SX_ROUND_NO_CANDIDATE, SX_ROUND_FAILED,
    SX_ROUND_COUNT,
} sx_outcome_t;
const char *sx_outcome_name(sx_outcome_t outcome);
bool sx_outcome_retryable(sx_outcome_t outcome);
/* The round boot's report (the confirmed boot's merged report): what it
 * achieved, and so whether another boot could do better. */
sx_outcome_t sx_round_outcome(const sx_repair_t *after);

typedef enum {
    SX_NEXT_NONE = 0,     /* nothing to do (the round is done, or not this image's) */
    SX_NEXT_RESTART,      /* a controlled restart now (the record counts it first) */
    SX_NEXT_WAIT,         /* a restart later (backoff, or the time is unknown) */
    SX_NEXT_UNREPAIRED,   /* report FAILED STORAGE_REPAIR_EXCEPTION_UNREPAIRED and clear the journal */
} sx_next_t;
/* What follows a boot of an exception-confirmed image: the first controlled
 * restart after CONFIRMED, retries only for retryable outcomes after the
 * backoff and with the time known, at most SX_MAX_RESTARTS, none after a
 * crash; otherwise UNREPAIRED. `*wait_s`: seconds until a WAIT may restart
 * (0 when the time is unknown). */
sx_next_t sx_next_step(const sx_record_t *record, bool time_known, uint32_t now_epoch, uint32_t *wait_s);

/* ---- the OTA floor (critique 1) --------------------------------------------- */
/* A target image that reads 2.7.6's redo area: 2.7.6 or later. Anything else
 * would mount a store whose custody may be in the area and erase it. */
bool sx_version_redo_aware(const char *version);
