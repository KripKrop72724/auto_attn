#pragma once
#include "zkt_journal_compat.h"

/* Storage-owner-only adapter. Identity is from the opened encryption root;
 * readiness is from this boot's owner/transport/persistence checks. Obtains
 * running/rollback image identity, OTA validation and geometry from ESP-IDF.
 * This call does not start tasks, enable capture or authorize a release. */
zj_compat_result_t zj_reader_platform_check(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, bool *writer_allowed);

zj_compat_result_t zj_reader_platform_update(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, uint32_t target_address,
    uint32_t target_size, const char *target_version);

/* Storage-owner only. Recheck the actual attested bridge, then select that
 * existing slot without erasing or downloading. expected_digest comes from
 * the approved exact-artifact operation, not from a version-only request.
 * Deadline is the local monotonic clock and at most five seconds away.
 * This changes boot selection, not the running image. The coordinator must
 * persist intent before calling, retain uncertainty, and separately arrange a
 * capture-safe restart and post-boot evidence. A timeout never proves failure. */
zj_compat_result_t zj_reader_platform_select(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending,
    const uint8_t expected_digest[32], uint64_t deadline_us);

/* Drained owner only, after FAILED_BOOT_INTENT commits. Verify the exact
 * failed writer and the retained VALID, attested bridge before invoking IDF's
 * failed-boot rollback without rebooting. Check the selected bridge again
 * before returning OK to the coordinator for its separate restart. */
zj_compat_result_t zj_reader_platform_failed_boot(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending,
    const uint8_t expected_writer_digest[32], uint64_t deadline_us);
