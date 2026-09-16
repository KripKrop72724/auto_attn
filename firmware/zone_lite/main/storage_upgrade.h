#pragma once
#include <stdbool.h>
/* Compatibility writes legacy queues; only a verified candidate may activate
 * segmented writers. Failure is visible and prevents OTA boot confirmation. */
bool storage_upgrade_init(void);
bool storage_upgrade_ready(void);
bool storage_upgrade_segmented_writes(void);
const char *storage_upgrade_error(void);

const char *storage_upgrade_contract(void);
