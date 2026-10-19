#pragma once
/* ZKT 2.7.5 boot storage repair: the decisions that need no filesystem.
 *
 * Which class every file of the store belongs to, what an unreadable SPIFFS
 * data span is filled with, whether a legacy queue cursor still describes the
 * repaired bytes, the frozen gap and cursor-reset records that are reported
 * to ADD as queue evidence, the NVS journal and the boot policy. The SPIFFS
 * engine (storage_repair_core.c) and the device glue (storage_repair.c) use
 * this module; it has no ESP-IDF or SPIFFS dependency, so the host tests run
 * it with plain cc. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "legacy_queue.h"

#define SRP_SPAN_BYTES 251U /* SPIFFS data bytes per 256-byte page */
#define SRP_NAME_BYTES 33U  /* SPIFFS object name (32) and its NUL */

/* ---- file classes ---------------------------------------------------- */
typedef enum {
    SRP_CLASS_UNKNOWN = 0,   /* P6: kept byte-exact, never removed by the repair */
    SRP_CLASS_ATTENDANCE,    /* P1: unsettled punches and ADD outboxes */
    SRP_CLASS_EVIDENCE,      /* P2: quarantine rows and journal segments */
    SRP_CLASS_OPERATIONAL,   /* P3: accepted commands, idempotency IDs, rollback cursors */
    SRP_CLASS_DEDUPE,        /* P5: acked event UIDs */
    SRP_CLASS_CATALOG,       /* re-fetchable identity catalog */
    SRP_CLASS_EXPENDABLE,    /* probes and transient cursor stages */
} srp_class_t;

typedef enum {
    SRP_FILL_BINARY = 0,     /* NUL: a record CRC or decryption then fails */
    SRP_FILL_JSON_ROWS,      /* NUL, with row boundaries kept (srp_fill) */
    SRP_FILL_ID_LINES,       /* '\n': one ID per line; NUL would break fgets/strchr */
    SRP_FILL_CURSOR,         /* "0...0\n": an unreadable rollback cursor replays */
} srp_fill_t;

enum {
    SRP_APPEND_TARGET = 1U << 0,  /* rel_open_append refuses a file whose last byte is not '\n' */
    SRP_COMMAND_FAMILY = 1U << 1, /* add_commands.*: merged by add_connector after mount */
    SRP_CATALOG_FAMILY = 1U << 2,
    SRP_SEGMENT = 1U << 3,        /* durable_queue / journal segment: binary records */
};

typedef struct {
    srp_class_t cls;
    srp_fill_t fill;
    uint8_t flags;
    /* NVS namespace and key of the legacy_queue checkpoint, or NULL. */
    const char *lq_namespace, *lq_key;
} srp_kind_t;

/* name is the SPIFFS object name: the VFS path below /storage, leading '/'
 * included ("/pending.jsonl"). */
srp_kind_t srp_classify(const char *name);
const char *srp_class_name(srp_class_t cls);
/* 0 for the most precious class; used to order holds against a budget. */
unsigned srp_class_priority(srp_class_t cls);

/* ---- filling an unreadable range ----------------------------------- */
/* Fills `length` bytes at a gap. prefix is the readable bytes since the last
 * '\n' before the gap (prefix_length 0 when the gap starts a row); suffix the
 * readable bytes after it up to and including the next '\n' (suffix_complete
 * false when the file ends first or another gap interrupts it).
 *
 * JSON rows: NUL, which every drain hands to ADD as MALFORMED evidence. When
 * the prefix is itself a complete JSON object, the first gap byte is '\n' so
 * that row stays attendance; when the suffix is a complete object row, the
 * last gap byte is '\n' so it does too. ID lines: '\n'. Cursor: zeros and a
 * final '\n' (replay from the start). Binary: NUL. */
void srp_fill(srp_fill_t fill, const uint8_t *prefix, size_t prefix_length, const uint8_t *suffix,
              size_t suffix_length, bool suffix_complete, uint8_t *out, size_t length);

/* ---- legacy queue cursors ------------------------------------------ */
typedef enum {
    SRP_CURSOR_KEEP = 0,       /* absent, or valid for these bytes */
    SRP_CURSOR_CORRUPT,        /* fails its CRC or version: lq_open_step returns DQ_CORRUPT */
    SRP_CURSOR_BEYOND_FILE,    /* offset past the file (or the file is gone) */
    SRP_CURSOR_NOT_ROW_BOUNDARY,
    SRP_CURSOR_PREFIX_MISMATCH,/* the settled prefix changed: another image rewrote it, or a gap */
    SRP_CURSOR_PREFIX_GAP,     /* the settled prefix holds unreadable bytes */
} srp_cursor_verdict_t;

typedef struct {
    void *context;
    /* Exact file bytes [offset, offset + length): false when unreadable. */
    bool (*read)(void *context, uint32_t offset, uint8_t *buffer, size_t length);
} srp_bytes_t;

/* loaded: 0 absent, 1 present (the lq port's load result). The verdict is
 * what lq_open_step would conclude, so a cursor that would wedge the drain
 * with DQ_CORRUPT, or skip bytes it never delivered, is cleared instead. */
srp_cursor_verdict_t srp_cursor_verdict(int loaded, const lq_checkpoint_t *checkpoint, bool exists,
                                        uint32_t size, bool prefix_has_gap, const srp_bytes_t *bytes);
const char *srp_cursor_verdict_name(srp_cursor_verdict_t verdict);

/* ---- frozen records reported to ADD -------------------------------- */
typedef enum {
    SRP_GAP_SALVAGE = 1,            /* unreadable before the repair: filled per srp_fill */
    SRP_GAP_CHECK_INTERRUPTED,      /* a power cut during a check lost these bytes */
    SRP_GAP_RESTORE_INTERRUPTED,
    SRP_GAP_CONTENT_UNVERIFIED,     /* same size, different bytes after an interrupted run */
    SRP_GAP_CHECK_CHANGED,          /* the check altered held bytes it could not restore */
    SRP_GAP_REFORMAT_INTERRUPTED,   /* reserved for a reformat; none in 2.7.5 */
    SRP_GAP_UNHELD_LOST,            /* not held (budget) and changed by the check */
} srp_gap_cause_t;

#define SRP_GAP_MERGED 0x01U        /* several unreadable ranges merged to fit the journal */
#define SRP_RECORD_ACKED 0x80U

typedef struct {
    char name[SRP_NAME_BYTES];
    uint8_t cause, cls, flags, reserved;
    int32_t spiffs_error;   /* first SPIFFS error code in the range (negative), 0 when none */
    uint32_t offset, length, file_size;
    uint32_t generation;    /* the repair run that created it: part of its identity */
} srp_gap_t;

typedef struct {
    char name[SRP_NAME_BYTES];
    uint8_t verdict, flags, reserved[2];
    uint32_t generation;
    lq_checkpoint_t previous; /* exact bytes of the cleared cursor */
} srp_reset_t;

/* ADD queue_evidence identifiers: queue "storage_repair", generation
 * "storage-repair-v1-<instance>", record "gap:<file>:<offset>:<length>:<run>"
 * with the file's leading '/' dropped and every other character outside
 * [a-zA-Z0-9_-] written as '_'. The raw bytes are rendered only from the
 * frozen record, so every resend is byte-identical. Returns false when a
 * buffer is too small. */
bool srp_gap_record_id(const srp_gap_t *gap, char *out, size_t size);
bool srp_reset_record_id(const srp_reset_t *reset, char *out, size_t size);
size_t srp_gap_evidence(const srp_gap_t *gap, char *out, size_t size);
size_t srp_reset_evidence(const srp_reset_t *reset, char *out, size_t size);
const char *srp_gap_cause_name(srp_gap_cause_t cause);

/* A bounded list: an unacknowledged record is never dropped. When the list
 * is full, a new range of a file merges into that file's last unacknowledged
 * record (SRP_GAP_MERGED: its bounds still cover every unreadable byte), and
 * otherwise replaces an acknowledged record. False when nothing could hold it. */
#define SRP_MAX_GAPS 24U
#define SRP_MAX_RESETS 8U
typedef struct {
    uint32_t count;
    srp_gap_t gap[SRP_MAX_GAPS];
} srp_gaps_t;
typedef struct {
    uint32_t count;
    srp_reset_t reset[SRP_MAX_RESETS];
} srp_resets_t;
bool srp_gaps_add(srp_gaps_t *gaps, const srp_gap_t *gap);
bool srp_resets_add(srp_resets_t *resets, const srp_reset_t *reset);
uint32_t srp_gaps_unacked(const srp_gaps_t *gaps);
uint32_t srp_resets_unacked(const srp_resets_t *resets);

/* ---- journal ------------------------------------------------------- */
typedef enum {
    SRP_PHASE_IDLE = 0,     /* no run, or the last one finished */
    SRP_PHASE_SURVEY,       /* reading only */
    SRP_PHASE_NORMALIZE,    /* consistency writes that keep every readable byte */
    SRP_PHASE_CHECK,        /* SPIFFS_check running on the private mount */
    SRP_PHASE_RESTORE,      /* restoring held bytes the check changed */
    SRP_PHASE_FINISH,       /* tails, cursors, catalog, probes */
    SRP_PHASE_REFORMAT,     /* reserved: no reformat is implemented in 2.7.5 */
} srp_phase_t;
const char *srp_phase_name(srp_phase_t phase);

#define SRP_STATE_VERSION 1U
enum {
    SRP_STATE_CATALOG_CLEAR = 1U << 0,  /* catalog files being removed; its transaction still to set idle */
    SRP_STATE_COMMANDS_DAMAGED = 1U << 1, /* add_connector must merge the command inbox */
    SRP_STATE_DAMAGE_SEEN = 1U << 2,    /* a runtime storage fault since the last full run */
    SRP_STATE_REPAIR_DISABLED = 1U << 3,/* the attempts guard stopped writing repairs */
    SRP_STATE_MANIFEST = 1U << 4,       /* a manifest describes the run in progress */
    SRP_STATE_FULL = 1U << 5,           /* the last finished run deferred for room (shortage says what) */
};
typedef struct {
    uint32_t version;
    uint32_t generation;          /* incremented by every run that starts */
    uint32_t phase;               /* srp_phase_t of the run in progress */
    uint32_t incomplete;          /* consecutive runs that started and did not finish */
    uint32_t failures;            /* of those, the ones a crash, watchdog or the deadline ended */
    uint32_t flags;
    uint32_t last_result;         /* srp_result_t of the last finished run */
    uint32_t image_tag;           /* first word of the app digest that set REPAIR_DISABLED */
    uint32_t shortage;            /* with SRP_STATE_FULL: what the run that deferred for room lacked
                                   * (a CRC of the pages it needed, the erased pages and the files) */
    uint32_t crc;
} srp_state_t;
void srp_state_seal(srp_state_t *state);
bool srp_state_valid(const srp_state_t *state);

/* Every custody file at the start of a check, held or not: enough to tell,
 * after a reset in the middle of it, which bytes a power cut may have cost.
 * It stays in NVS (SRP_STATE_MANIFEST) until every entry is settled: found
 * intact, put back, superseded by the file's own owner, or reported as a gap.
 * No check runs when the custody files do not all fit, or when the gap list
 * could not take one record per entry. */
#define SRP_MANIFEST_FILES 24U
enum {
    SRP_MANIFEST_CURSOR = 1U << 0,   /* cursor_generation holds the file's lq checkpoint generation */
    SRP_MANIFEST_SETTLED = 1U << 1,  /* nothing more to do for this entry */
    SRP_MANIFEST_REPORTED = 1U << 2, /* what did not go back is already a gap record */
    SRP_MANIFEST_RESTORING = 1U << 3,/* the repair began putting it back: rebuild_id, restored */
};
/* manifest flags */
#define SRP_MANIFEST_PENDING_IMAGE 0x01U /* written by an unconfirmed image: a later boot follows a rollback */
typedef struct {
    char name[SRP_NAME_BYTES];
    uint8_t cls, flags, reserved;
    uint16_t object_id;
    uint16_t rebuild_id;      /* object id of the repair's own rebuild of it; 0 when none */
    uint32_t size, crc;
    uint32_t cursor_generation;
    uint32_t restored;        /* leading bytes of that rebuild read back as the original's */
} srp_manifest_file_t;
typedef struct {
    uint32_t version, generation, count;
    uint32_t image_tag, flags;
    /* With SRP_MANIFEST_PENDING_IMAGE: the image a reset rolls back to (the
     * first word of its app digest; 0 when unknown). That image, running
     * confirmed, resumes the manifest at its first boot, before anything
     * else writes, so the manifest is current, not stale. */
    uint32_t rollback_tag;
    srp_manifest_file_t file[SRP_MANIFEST_FILES];
    uint32_t crc;
} srp_manifest_t;
void srp_manifest_seal(srp_manifest_t *manifest);
bool srp_manifest_valid(const srp_manifest_t *manifest);

/* A held file the check removed is rebuilt under its name plus this suffix
 * and renamed into place, so a reset during the rebuild never leaves a
 * partial file under the real name. Such a file is expendable once no
 * manifest names it. */
#define SRP_RESTORE_SUFFIX ".rpr"

/* NVS entries (32 bytes each) a run needs beyond the page NVS keeps for its
 * own garbage collection (compare with nvs_stats_t.available_entries, which
 * already leaves that page out; free_entries counts it): room to create each
 * journal blob that does not exist yet, room for one rewritten copy of the
 * largest (NVS writes the new copy before it erases the old), and
 * SRP_NVS_RUNTIME_RESERVE entries the runtime keeps for its own checkpoint
 * commits and new keys. */
#define SRP_NVS_RUNTIME_RESERVE 64U
uint32_t srp_nvs_entries_needed(bool state, bool gaps, bool resets, bool manifest);

uint32_t srp_crc32(uint32_t crc, const void *data, size_t length);

/* ---- policy -------------------------------------------------------- */
typedef enum {
    SRP_MODE_FULL = 0,       /* survey, normalize, check, restore, finish */
    SRP_MODE_NORMALIZE_ONLY, /* no SPIFFS_check this boot */
    SRP_MODE_REPORT_ONLY,    /* mount and measure only: nothing read file by file, nothing written */
} srp_mode_t;
const char *srp_mode_name(srp_mode_t mode);

#define SRP_GUARD_FAILURES 3U
typedef struct {
    uint32_t incomplete;      /* consecutive incomplete runs before this one */
    uint32_t failures;        /* of those, ended by a crash, watchdog or the deadline */
    bool disabled;            /* the guard stopped writing repairs */
    bool same_image;          /* the image that set the guard is the one running */
    bool pending_verify;      /* this image is not yet confirmed */
} srp_policy_input_t;
typedef struct {
    srp_mode_t mode;
    bool disable;             /* record REPAIR_DISABLED for this image now */
    bool clear_disable;       /* another image runs: it tries again */
} srp_policy_t;
/* After SRP_GUARD_FAILURES consecutive runs ended by a crash, a watchdog or
 * the repair deadline (never by a power cut, which is what the repair is
 * for), writing repairs stop for the running image until ADD installs another
 * one, which tries again. On an unconfirmed image after an incomplete run the
 * run normalizes only: a check that cannot finish there would only roll the
 * image back. */
srp_policy_t srp_policy(const srp_policy_input_t *input);

/* ---- budget -------------------------------------------------------- */
typedef struct {
    uint32_t size;
    uint8_t cls;
    bool hold;           /* out: held in PSRAM */
} srp_budget_file_t;
/* Holds files in class priority, largest last within a class, while they
 * fit in budget; returns the bytes held. */
uint64_t srp_budget(srp_budget_file_t *files, size_t count, uint64_t budget);
