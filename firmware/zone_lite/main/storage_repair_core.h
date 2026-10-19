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
 *     file is mapped span by span exactly as SPIFFS reads it (a header size
 *     torn by a first append is replaced by the size its pages hold), and
 *     each data span is "perfect" (its page is a finalized, undeleted data
 *     page of this object and span, and the lookup agrees) or not. For an
 *     imperfect span the bytes come from the referenced page when it is this
 *     file's finalized page of the span (deleted or not) and from finalized
 *     copies whose lookup entry names the file, merged byte by byte; a byte
 *     no source holds, or two hold differently, is a gap, recorded with its
 *     exact SPIFFS cause before anything is written.
 *  2. Normalize. Each imperfect span gets a new page holding those bytes (a
 *     gap is filled per srp_fill), each index page that names one is
 *     rewritten by SPIFFS's own page move, duplicate, stale and stray pages
 *     are deleted, a missing index page is rebuilt, entries past the end of a
 *     file are cleared, and a lookup entry that would let the allocator hand
 *     out a used page is corrected. No page another file uses is deleted, no
 *     readable byte is replaced, and each step leaves a state the next run's
 *     survey understands, so a reset at any point loses nothing.
 *  3. Before every check pass the store is surveyed again; only if every
 *     custody file is consistent, and the pass and its restore fit before the
 *     soft deadline, does SPIFFS_check run (it then frees leaked pages and
 *     unsticks GC without deleting a custody file).
 *  4. Held copies (PSRAM, as far as the budget allows) are compared with
 *     what the check left and restored where they differ, never by removing
 *     a live file; a manifest in NVS, kept until every entry is settled, lets
 *     the next boots finish or report exactly what a reset cost.
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
    SRR_RESULT_NOT_RUN = 0,
    SRR_RESULT_CLEAN,              /* nothing needed repair */
    SRR_RESULT_REPAIRED,           /* repaired; every byte was readable */
    SRR_RESULT_REPAIRED_WITH_GAPS, /* repaired; unreadable bytes reported */
    SRR_RESULT_DEFERRED,           /* custody could not be made safe for a check this boot */
    SRR_RESULT_NORMALIZED,         /* normalize-only boot */
    SRR_RESULT_REPORT_ONLY,        /* the attempts guard stopped repairs */
    SRR_RESULT_STOPPED,            /* the deadline stopped the run */
    SRR_RESULT_FAILED,             /* the check kept failing, or a write failed */
    SRR_RESULT_REFORMATTED,        /* reserved: no reformat is implemented in 2.7.5 */
} srr_result_t;
const char *srr_result_name(srr_result_t result);

typedef struct {
    void *context;
    /* Repair memory: PSRAM in the firmware. alloc returns NULL on failure. */
    void *(*alloc)(void *context, size_t bytes);
    void (*release)(void *context, void *pointer);
    /* False stops the run at the next safe point (the deadline). */
    bool (*keep_going)(void *context);
    /* Milliseconds left before keep_going turns false; NULL when unbounded. A
     * check pass starts only when what is left covers it and the restore. */
    uint64_t (*remaining_ms)(void *context);
    void (*yield)(void *context);
    uint64_t (*now_ms)(void *context);
    void (*log)(void *context, const char *level, const char *code, const char *message);
    /* NVS blobs: load returns 1 found (size matched), 0 absent, -1 error. */
    int (*nvs_load)(void *context, const char *ns, const char *key, void *blob, size_t size);
    bool (*nvs_store)(void *context, const char *ns, const char *key, const void *blob, size_t size);
    bool (*nvs_erase)(void *context, const char *ns, const char *key);
    /* Breadcrumb for the RTC record, LED and deadline: called on every phase change. */
    void (*phase)(void *context, srp_phase_t phase, uint32_t pass, bool writing);
    /* Host tests only (NULL on the device): called after each check pass on
     * the repair's mount; true when it changed the store, standing in for a
     * check that altered a held file. */
    bool (*after_check)(void *context, uint32_t pass);
} srr_ports_t;

typedef struct {
    bool pending_verify;     /* unconfirmed image */
    bool previous_failed;    /* the previous boot ended in a crash, watchdog or the repair deadline */
    bool damage_seen;        /* a runtime storage fault was recorded since the last run */
    uint32_t image_tag;      /* identifies the running image for the attempts guard */
    uint64_t hold_budget;    /* bytes the run may hold in memory */
    bool nvs_known;          /* nvs_free_entries was measured */
    uint32_t nvs_free_entries;
    uint32_t check_ms_per_mib; /* expected SPIFFS_check duration per MiB before one has been measured */
} srr_config_t;

enum {
    SRR_FILE_NORMALIZED = 1U << 0,  /* spans or index pages rewritten */
    SRR_FILE_GAPS = 1U << 1,        /* unreadable bytes reported */
    SRR_FILE_RESTORED = 1U << 2,    /* held bytes put back after the check */
    SRR_FILE_REBUILT = 1U << 3,
    SRR_FILE_TAIL = 1U << 4,        /* torn append terminated */
    SRR_FILE_CURSOR_RESET = 1U << 5,
    SRR_FILE_REMOVED = 1U << 6,     /* expendable or catalog */
    SRR_FILE_UNHELD = 1U << 7,      /* custody the budget could not hold */
    SRR_FILE_SIZE_RECOVERED = 1U << 8, /* a torn header size replaced by the size its pages hold */
    SRR_FILE_SUPERSEDED = 1U << 9,  /* after an interrupted run, its owner had replaced it */
};

#define SRR_REPORT_FILES 8U
typedef struct {
    char name[SRP_NAME_BYTES];
    uint8_t cls;
    uint16_t actions;
    uint32_t size, gaps, unreadable_bytes, spans_rewritten;
    /* After a run deferred for room: spans SPIFFS still cannot read (the
     * repair holds their bytes, it could not rewrite them), and the first. */
    uint32_t blocked_spans, blocked_offset;
} srr_report_file_t;

typedef struct {
    srr_result_t result;
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
    uint32_t blocks_compacted, reserve_pages; /* room made: blocks of deleted pages erased; GC reserve pages used */
    uint32_t room_needed, room_erased; /* a run deferred for room: pages the work needs, erased pages there were */
    uint32_t sizes_recovered, strays_removed, files_superseded;
    uint32_t check_passes, check_fix_index, check_fix_lookup, check_delete_orphan_index;
    uint32_t check_delete_page, check_delete_bad_file, check_errors;
    uint32_t check_deleted_live;   /* of the files the check deleted, ones the survey saw as live files */
    int32_t check_last_error;
    uint32_t total_bytes, used_before, used_after;
    uint32_t duration_ms, check_ms;
    char error_stage[24];
    int32_t error_code;
    uint32_t file_count;
    srr_report_file_t file[SRR_REPORT_FILES];
} srr_report_t;

/* Runs the repair on a mounted SPIFFS with no open files. The mount must use
 * srr_check_callback as its check callback. */
void srr_run(spiffs *fs, const srr_config_t *config, const srr_ports_t *ports, srr_report_t *report);
void srr_check_callback(spiffs *fs, spiffs_check_type type, spiffs_check_report report, u32_t arg1, u32_t arg2);

/* For the device's reporter: the NVS records awaiting ADD acknowledgement. */
#define SRR_NVS_NAMESPACE "storage_repair"
