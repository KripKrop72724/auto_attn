#pragma once
#include "zkt_storage_mailbox.h"

/* Queue-store routing uses task identity, never a caller-controlled bypass.
 * Bootstrap queue recovery remains on the app task until this owner starts. */
bool zj_owner_started(void);
bool zj_owner_is_current_task(void);
#include "zkt_journal_state.h"
#include "zone_storage_paths.h"

#define ZJ_DEVICE_DIRECTORY ZONE_STORAGE_BASE
#define ZJ_DEVICE_BASENAME "zktj"
#define ZJ_DEVICE_PREFIX ZJ_DEVICE_DIRECTORY "/" ZJ_DEVICE_BASENAME

typedef struct {
    bool started, ready, operation_running, recovering, checkpoint_recovery_pending;
    bool quiescing, quiesced;
    bool hil_reboot_persistence_incident; /* Boot-sticky experimental-test veto. */
    bool compatibility_checked, writer_allowed;
    zj_delivery_authority_t delivery_authority;
    zj_compat_result_t compatibility;
    zj_operation_t operation;
    zj_result_t last_result;
    uint64_t sampled_uptime_us, operation_started_us, progress_uptime_us;
    uint64_t completed, refused, failures, max_operation_us;
    unsigned occupied, high_watermark;
    /* RAM catalog measurements, published only at owner boundaries. An empty
     * result is invalidated by append admission and recovery. Nonempty record
     * counts are deliberately unknown until a qualified index exists. */
    unsigned pending_appends, journal_segments;
    uint64_t journal_bytes;
    bool inventory_known, verified_empty, append_observed;
    /* Verified absence across all retained ZKT queue domains. This is local
     * custody-transfer evidence, never source/identity/Oracle completion. */
    bool legacy_verified_empty, legacy_append_pending, legacy_read_pending;
    uint32_t legacy_empty_mask, legacy_required_mask;
    uint64_t legacy_inventory_generation;
    bool source_boundary_observed;
    zj_result_t source_boundary_result;
    zsb_record_t source_boundary;
    zj_result_t last_append_result;
    uint64_t last_append_uptime_us;
    int filesystem_error, nvs_error;
    const char *failed_operation;
} zj_owner_health_t;

/* Call once, after encrypted NVS, SPIFFS/legacy budget coordination and the
 * Wi-Fi entropy source are initialized. Only a qualified reader/writer gate
 * may enable this task. Starting it is NOT a compatibility certificate.
 * This module exclusively owns journal files, its NVS namespace and new-image
 * runtime_v1 checkpoint writes in zone_lite, plus new-image catalog reads/mutations
 * and the existing catalog NVS checkpoint. New-image command inbox reads,
 * replacements and their existing NVS checkpoint also run here, along with
 * processed/cancelled command-ID cache operations and retained segmented
 * queue work, plus the ADD, Oracle/blocked flat-file queues, three retained
 * quarantine generations and their existing checkpoints. Per-item custody
 * migration and physical qualification remain separate release obligations. */
bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata);
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket);
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *complete);
bool zj_owner_abandon(uint64_t ticket);
/* Irreversible until reboot. Refuse new work, finish every accepted request
 * (including abandoned callers), then stop recovery and filesystem/NVS work.
 * True means the owner acknowledged completion, not merely request admission.
 * No lock remains held; callers may continue polling retained replies. */
bool zj_owner_quiesce(void);
/* Experimental reboot only: fail without changing admission unless already
 * idle before this monotonic deadline. A true result is immediately final;
 * the caller must record its RAM witness and restart without further I/O. */
bool zj_owner_try_quiesce_before(uint64_t deadline_us, int64_t expires_epoch,
                                 int64_t *accepted_epoch, uint64_t *accepted_us);
/* OTA-task-only exception after acknowledged quiescence. All earlier work
 * must have finished. A separate bounded control slot remains available when
 * ordinary completed replies occupy the mailbox. It commits/readbacks exact
 * intent before selecting the attested bridge; it cannot erase/download.
 * The target is immutable until reboot. Poll the returned ticket normally;
 * abandoning it or submitting ZJ_SELECT_READER through normal admission is
 * prohibited. Timeout does not cancel accepted selection. */
bool zj_owner_select_quiesced_reader(const ota_checkpoint_t *expected, uint64_t *ticket);
bool zj_owner_health(zj_owner_health_t *health);
