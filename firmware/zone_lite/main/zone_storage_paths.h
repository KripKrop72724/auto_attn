#pragma once

/* Existing mounted SPIFFS namespace. Journal readers and downgrade scans must
 * use the same VFS mount as boot initialization and retained attendance. */
#define ZONE_STORAGE_BASE "/storage"
