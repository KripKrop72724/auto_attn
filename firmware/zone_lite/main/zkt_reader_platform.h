#pragma once
#include "zkt_journal_compat.h"

/* Storage-owner-only adapter. Identity is from the opened encryption root;
 * readiness is from this boot's owner/transport/persistence checks. Obtains
 * running/rollback image identity, OTA validation and geometry from ESP-IDF.
 * This call does not start tasks, enable capture or authorize a release. */
zj_compat_result_t zj_reader_platform_check(const char *terminal_serial,
    const uint8_t capture_epoch[16], bool reader_ready, bool delivery_ready,
    bool persistence_verified, bool recovery_pending, bool *writer_allowed);
