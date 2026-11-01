#pragma once
#include <stdbool.h>
/* Compatibility writes legacy queues; only a verified candidate may activate
 * segmented writers. Failure is visible and prevents OTA boot confirmation. */
bool storage_upgrade_init(void);
bool storage_upgrade_ready(void);
bool storage_upgrade_segmented_writes(void);
const char *storage_upgrade_error(void);

const char *storage_upgrade_contract(void);
/* Direct role (ZKT 2.8.1): how the boot judged the other OTA slot
 * (ug_slot_verdict_name) and its otadata state; "" in every other role. */
const char *storage_upgrade_rollback_slot(void);
const char *storage_upgrade_rollback_slot_state(void);
