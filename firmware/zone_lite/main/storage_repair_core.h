#pragma once
/* ZKT 2.7.5 custody-first SPIFFS repair, run at boot on a private mount
 * before the VFS exists (owner decision, 16 October 2026).
 *
 * SPIFFS_check deletes a whole file when one of its index entries names a
 * page it cannot mend, and two copies of an index page (left by a reset in
 * the middle of SPIFFS's own page move) make it delete the file as "multiply
 * referenced". A power cut during any flash write can leave either. So the
 * check only runs on a store whose every file it would keep:
 *
 *  1. Survey (read only). Every page header and lookup entry is read, every
 *     file is mapped span by span exactly as SPIFFS reads it, and each data
 *     span is "perfect" (its page is a finalized, undeleted data page of this
 *     object and span, and the lookup agrees) or not. For an imperfect span
 *     the bytes come from the referenced page when its header still names it,
 *     from another finalized copy of the span, or from a page SPIFFS flagged
 *     deleted but has not erased; only when none exists is the span a gap,
 *     recorded with its exact SPIFFS cause before anything is written.
 *  2. Normalize. Each imperfect span gets a new page holding those bytes (a
 *     gap is filled per srp_fill), each index page that names one is
 *     rewritten by SPIFFS's own page move, duplicate and stale index pages
 *     are deleted, a missing index page is rebuilt, entries past the end of a
 *     file are cleared, and a lookup entry that would let the allocator hand
 *     out a used page is corrected. No page another file uses is deleted, no
 *     readable byte is replaced, and each step leaves a state the next run's
 *     survey understands, so a reset at any point loses nothing.
 *  3. The survey runs again. Only if every custody file is now consistent
 *     does SPIFFS_check run (it then frees leaked pages and unsticks GC
 *     without deleting a custody file); otherwise the run defers the check.
 *  4. Held copies (PSRAM, as far as the budget allows) are compared with
 *     what the check left and restored where they differ; a manifest in NVS
 *     lets the next boot report exactly what a reset during the check cost.
 *  5. Finish: torn appends are terminated, legacy queue cursors that would
 *     wedge their drain or skip undelivered rows are cleared (and reported),
 *     a damaged identity catalog is dropped for ADD to resend, and leftover
 *     probe files are removed.
 *
 * The engine has no ESP-IDF dependency: the host harness runs it against
 * ESP-IDF 5.5.3's own SPIFFS on an emulated NOR flash with power cuts. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "spiffs.h"
#include "storage_repair_plan.h"

typedef enum {
    SR_RESULT_NOT_RUN = 0,
    SR_RESULT_CLEAN,              /* nothing needed repair */
    SR_RESULT_REPAIRED,           /* repaired; every byte was readable */
    SR_RESULT_REPAIRED_WITH_GAPS, /* repaired; unreadable bytes reported */
    SR_RESULT_DEFERRED,           /* custody could not be made safe for a check this boot */
    SR_RESULT_NORMALIZED,         /* normalize-only boot */
    SR_RESULT_REPORT_ONLY,        /* the attempts guard stopped repairs */
    SR_RESULT_STOPPED,            /* the deadline stopped the run */
    SR_RESULT_FAILED,             /* the check kept failing, or a write failed */
    SR_RESULT_REFORMATTED,
} sr_result_t;
const char *sr_result_name(sr_result_t result);

typedef struct {
    void *context;
    /* Repair memory: PSRAM in the firmware. alloc returns NULL on failure. */
    void *(*alloc)(void *context, size_t bytes);
    void (*release)(void *context, void *pointer);
    /* False stops the run at the next safe point (the deadline). */
    bool (*keep_going)(void *context);
    void (*yield)(void *context);
    uint64_t (*now_ms)(void *context);
    void (*log)(void *context, const char *level, const char *code, const char *message);
    /* NVS blobs: load returns 1 found (size matched), 0 absent, -1 error. */
    int (*nvs_load)(void *context, const char *ns, const char *key, void *blob, size_t size);
    bool (*nvs_store)(void *context, const char *ns, const char *key, const void *blob, size_t size);
    bool (*nvs_erase)(void *context, const char *ns, const char *key);
    /* Breadcrumb for the RTC record, LED and deadline: called on every phase change. */
    void (*phase)(void *context, srp_phase_t phase, uint32_t pass, bool writing);
} sr_ports_t;

typedef struct {
    bool pending_verify;     /* unconfirmed image */
    bool previous_failed;    /* the previous boot ended in a crash, watchdog or the repair deadline */
    bool damage_seen;        /* a runtime storage fault was recorded since the last run */
    uint32_t image_tag;      /* identifies the running image for the attempts guard */
    uint64_t hold_budget;    /* bytes the run may hold in memory */
    bool allow_reformat;     /* confirmed image and the build permits the last resort */
} sr_config_t;

enum {
    SR_FILE_NORMALIZED = 1U << 0,  /* spans or index pages rewritten */
    SR_FILE_GAPS = 1U << 1,        /* unreadable bytes reported */
    SR_FILE_RESTORED = 1U << 2,    /* held bytes put back after the check */
    SR_FILE_REBUILT = 1U << 3,
    SR_FILE_TAIL = 1U << 4,        /* torn append terminated */
    SR_FILE_CURSOR_RESET = 1U << 5,
    SR_FILE_REMOVED = 1U << 6,     /* expendable or catalog */
    SR_FILE_UNHELD = 1U << 7,      /* custody the budget could not hold */
};

#define SR_REPORT_FILES 8U
typedef struct {
    char name[SRP_NAME_BYTES];
    uint8_t cls, actions;
    uint32_t size, gaps, unreadable_bytes, spans_rewritten;
} sr_report_file_t;

typedef struct {
    sr_result_t result;
    srp_mode_t mode;
    srp_phase_t phase, previous_phase;
    uint32_t generation, incomplete, failures;
    uint8_t level;                 /* escalation level reached: 0 plain, 1 expendables, 2 settled files */
    bool reformatted, check_skipped, manifest_written, commands_damaged;
    uint32_t files, files_held, files_unheld_custody;
    uint64_t bytes_held;
    uint32_t gaps, unreadable_bytes, records_dropped;
    uint32_t spans_rewritten, index_rewritten, index_created, duplicates_removed, lookup_fixed;
    uint32_t tails_terminated, files_restored, files_rebuilt, cursors_reset;
    uint32_t expendables_removed, settled_removed, interrupted_losses, orphan_index_removed;
    uint32_t check_passes, check_fix_index, check_fix_lookup, check_delete_orphan_index;
    uint32_t check_delete_page, check_delete_bad_file, check_errors;
    uint32_t check_deleted_live;   /* of the files the check deleted, ones the survey saw as live files */
    int32_t check_last_error;
    uint32_t total_bytes, used_before, used_after;
    uint32_t duration_ms, check_ms;
    char error_stage[24];
    int32_t error_code;
    uint32_t file_count;
    sr_report_file_t file[SR_REPORT_FILES];
} sr_report_t;

/* Runs the repair on a mounted SPIFFS with no open files. The mount must use
 * sr_check_callback as its check callback. */
void sr_run(spiffs *fs, const sr_config_t *config, const sr_ports_t *ports, sr_report_t *report);
void sr_check_callback(spiffs *fs, spiffs_check_type type, spiffs_check_report report, u32_t arg1, u32_t arg2);

/* For the device's reporter: the NVS records awaiting ADD acknowledgement. */
#define SR_NVS_NAMESPACE "storage_repair"
