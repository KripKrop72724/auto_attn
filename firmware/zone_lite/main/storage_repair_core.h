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
 * 2.7.6, the last resort (storage_repair_redo.h): a run about to hand over
 * a store with no room (normalization still SPIFFS_ERR_FULL, GC-unsafe, a
 * resume blocked for room, a check pass that ran out of room) reformats
 * blocks in place, each one's kept pages first in a verified redo record
 * outside the partition, then remounts the store and runs again. Records the
 * boot recovery could not complete are byte sources for the survey, settled
 * at the end of every run.
 *
 * The engine has no ESP-IDF dependency: the host harness runs it against
 * ESP-IDF 5.5.3's own SPIFFS on an emulated NOR flash with power cuts. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "spiffs.h"
#include "storage_repair_plan.h"
#include "storage_repair_redo.h"

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
    SRR_RESULT_REFORMATTED,        /* reserved: never sent (a run reports the store it leaves) */
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
    /* 2.7.6, the last resort; without `redo` and `remount` a store with no
     * room is left as 2.7.5 leaves it. `redo`'s area_* reach the redo area;
     * its store_* are not used (the engine writes the store through the
     * mount's HAL and reads it back through raw_read). */
    const srx_io_t *redo;
    /* Flash, never the metadata mirror; NULL: the mount's HAL reads flash. */
    bool (*raw_read)(void *context, uint32_t address, void *buffer, uint32_t length);
    /* The metadata mirror read again from flash over a verified block. */
    void (*refresh)(void *context, uint32_t address, uint32_t length);
    /* Unmount and mount the private store again: SPIFFS's counters from a
     * fresh scan after a round. False: the store is not mounted. */
    bool (*remount)(void *context);
    /* A block's window (magic cleared, not yet programmed) opens or closes:
     * the hard deadline defers itself while one is open. */
    void (*window)(void *context, bool open);
    /* NVS available entries now (measured before every run); negative when
     * unknown. */
    int32_t (*nvs_available)(void *context);
    /* Host tests only (NULL on the device): a candidate block a round takes
     * first. Which eligible block goes first is the engine's free choice; a
     * test uses it to put a window over pages it names. */
    bool (*prefer)(void *context, uint32_t block);
} srr_ports_t;

typedef struct {
    bool pending_verify;     /* unconfirmed image */
    bool previous_failed;    /* the previous boot ended in a crash, watchdog or the repair deadline */
    bool damage_seen;        /* a runtime storage fault was recorded since the last run */
    uint32_t image_tag;      /* identifies the running image for the attempts guard */
    uint32_t rollback_tag;   /* unconfirmed image: the image tag of the one a reset rolls back to; 0 unknown */
    uint64_t hold_budget;    /* bytes the run may hold in memory */
    bool nvs_known;          /* nvs_available_entries was measured */
    uint32_t nvs_available_entries; /* nvs_stats_t.available_entries: NVS's own GC page left out */
    uint32_t check_ms_per_mib; /* expected SPIFFS_check duration per MiB before one has been measured */
    /* 2.7.6: the redo area as discovery and the boot recovery left it (NULL:
     * none). The engine writes new records only when its state is READY. */
    const srx_area_t *area;
    /* 2.7.6 (round 1 review, low): the area could not be read this boot while
     * the reclaim journal says records may hold queue bytes (ACTIVE, SOURCES
     * or HELD): a queue's unreadable bytes wait for a boot that reads them,
     * never filled or recorded as lost. */
    bool sources_unread;
    /* 2.7.7 (the confirm-first exception's round boots): custody blocks are
     * taken only by a run that held every custody file, so the held-CRC
     * oracle and the restore comparison cover the first custody round. */
    bool custody_needs_hold;
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
    SRR_FILE_REDO_SOURCE = 1U << 10, /* 2.7.6: bytes taken from a redo record */
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

/* 2.7.6: the last resort, over every run of the boot and the boot recovery. */
typedef struct {
    uint8_t area;                  /* srx_area_state_t */
    bool wanted;                   /* a run ended without room */
    char trigger[12];              /* full, gc_unsafe, check_full */
    uint32_t area_foreign, area_first_foreign, area_foreign_crc;
    uint32_t rounds, blocks, pages_freed, pages_kept, erased_after, ms;
    uint32_t redone, sources, settled, reported, records_open, failed, held_mismatch;
    /* The exposure histogram of the last round considered: blocks a round
     * may take now, on an unconfirmed image, only on a confirmed one, and
     * the blocks kept out by an index page, an excluded file or the magic. */
    uint32_t candidates, unconfirmed_blocks, custody_blocks, index_blocks, excluded_blocks, low_magic_blocks;
    char short_reason[12];         /* why a round stopped before its target */
    /* Round 2 review, R2-4: a failed block's record could be made neither a
     * source nor journaled as one; the store was not written again and the
     * VFS must not mount it this boot. */
    bool store_blocked;
    /* 2.7.7, diagnostics (no rule reads them in the engine): the pages the
     * last round considered short (`want`) of the erased pages it aimed for
     * (`target`); the pages the confirmed rules would drop from the custody
     * blocks and from the blocks any image may take; custody blocks a round
     * would not take because a custody file was not held (custody_needs_hold). */
    uint32_t want, target, custody_gain, unconfirmed_gain, held_out;
    /* Excluded blocks by the one reason every excluding page of the block
     * shares (a hidden or unmapped header's object, a live file never put in
     * a window, a reclaimable file's last data page, a page whose header says
     * index under a live lookup entry), the blocks of an open or source
     * record, and blocks with more than one reason; the object that excludes
     * the most blocks (its header's name, hidden ones too) and how many. */
    uint32_t excluded_hidden, excluded_unwindowed, excluded_last_page, excluded_index_flag, excluded_busy;
    uint32_t excluded_mixed, excluded_file_blocks;
    char excluded_file[SRP_NAME_BYTES];
    /* The store as the boot's first survey found it, before anything wrote:
     * erased pages and SPIFFS's free blocks (a store that arrived without
     * room, not one this image's own run filled). */
    uint32_t erased_at_mount, free_blocks_at_mount;
    bool mount_measured;
    /* Source pages and queues whose unreadable spans wait for byte sources
     * (2.7.6 decision 5's precondition) when the boot's last run ended. */
    uint32_t waiting;
} srr_reclaim_report_t;

typedef struct {
    srr_result_t result;
    srp_mode_t mode;
    srp_phase_t phase, previous_phase;
    uint32_t generation, incomplete, failures;
    uint8_t level;                 /* escalation level reached: 0 plain, 1 expendables, 2 settled files, 3 reformat */
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
    uint32_t cpu_steps;            /* the engine's patch lookups and mask scans (host bound; not reported) */
    char error_stage[24];
    int32_t error_code;
    uint32_t file_count;
    srr_report_file_t file[SRR_REPORT_FILES];
    uint32_t runs;                 /* 2.7.6: runs this boot (a round is followed by another run) */
    srr_reclaim_report_t reclaim;
    /* 2.7.7: a manifest was still active when the boot's last run ended; the
     * survey listed every object header (`names_surveyed`) and one of them
     * (visible or hidden) is a journal file's (`journal_names`, ZJ_DEVICE_BASENAME). */
    bool manifest_active, names_surveyed, journal_names;
    /* 2.7.7, per entry of `file` (kept apart so that every struct stays a
     * prefix of the next release's, as the released 2.7.5 engine in the host
     * harness needs): the last blocked span's offset and an order-free digest
     * of the blocked span indices (span_digest.h), so that the inventory's
     * unreadable spans can be compared with them set for set. */
    uint32_t blocked_last[SRR_REPORT_FILES], blocked_digest[SRR_REPORT_FILES];
} srr_report_t;

/* Runs the repair on a mounted SPIFFS with no open files. The mount must use
 * srr_check_callback as its check callback. */
void srr_run(spiffs *fs, const srr_config_t *config, const srr_ports_t *ports, srr_report_t *report);
void srr_check_callback(spiffs *fs, spiffs_check_type type, spiffs_check_report report, u32_t arg1, u32_t arg2);

/* For the device's reporter: the NVS records awaiting ADD acknowledgement. */
#define SRR_NVS_NAMESPACE "storage_repair"
