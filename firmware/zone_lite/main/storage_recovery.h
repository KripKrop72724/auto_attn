#pragma once
/* Dedicated one-shot ZKT storage recovery image (2.6.26). 2.6.24 stopped at the
 * first unreadable region; 2.6.25 skipped them, but SPIFFS kept the pages of
 * Peshawar-02's retired file, which 2.6.26 releases.
 *
 * The image never confirms itself. After the exact Peshawar target transfers
 * its retained blocked-identity queue into ADD custody, the OTA manager reports
 * the outcome and returns to the signed 2.5.2 application in the other slot.
 * Every refusal or interruption leaves the legacy files unchanged. */
#include <stdbool.h>

#define STORAGE_RECOVERY_VERSION "2.6.26"

/* Compile-time role as a constant expression, so shared code keeps every
 * ordinary path referenced while the recovery build skips it. */
static inline bool storage_recovery_image(void)
{
#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
    return true;
#else
    return false;
#endif
}

typedef enum {
    STORAGE_RECOVERY_IDLE = 0,
    STORAGE_RECOVERY_RUNNING,
    STORAGE_RECOVERY_RETIRING, /* Never interrupted by the OTA deadline. */
    STORAGE_RECOVERY_DONE,
} storage_recovery_state_t;

#if defined(ZONE_LITE_STORAGE_RECOVERY_IMAGE) && ZONE_LITE_STORAGE_RECOVERY_IMAGE
/* Before the first mount: release Peshawar-02's hidden, receipted blocked
 * file on the unmounted partition (exact target and deployment only). */
void storage_recovery_prepare(void);
/* Mount the existing partition without the normal queue owners. */
bool storage_recovery_mount(void);
/* After the mount: record the released rows' UIDs in the background. */
void storage_recovery_record_seen(void);
/* Blocking: exact preconditions, custody transfer, retirement, inventory. */
void storage_recovery_run(void);
storage_recovery_state_t storage_recovery_state(void);
/* Stable outcome code for the deployment report; valid once DONE. */
const char *storage_recovery_code(void);
/* Stop between rows. Retirement is never interrupted once it starts. */
void storage_recovery_request_stop(void);
#endif
