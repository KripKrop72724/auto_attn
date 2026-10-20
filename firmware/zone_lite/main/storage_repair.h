#pragma once
/* ZKT 2.7.5: the custody-first SPIFFS repair on the device; 2.7.6: its
 * last-resort in-place reformat (storage_repair_redo.h), whose interrupted
 * blocks are completed from the redo area past the last partition before
 * anything mounts the store.
 *
 * storage_repair_boot() runs from storage_init on app_main before the VFS
 * exists: it mounts the storage partition privately with ESP-IDF's own
 * SPIFFS and HAL (behind a metadata mirror in PSRAM), runs the engine of
 * storage_repair_core.c and unmounts. A one-shot esp_timer restarts the ESP if
 * the run ever outlives its deadline; an unconfirmed image then rolls back, and
 * the attempts guard stops repairs that keep ending that way.
 * storage_repair_after_mount() finishes, through add_connector, what needs the
 * VFS (the command inbox merge). Nothing here holds a task of its own: logs and
 * gap evidence go to ADD from the ADD outbox task, and the result rides on
 * every heartbeat as diagnostics.storage.repair. */
#include <stdbool.h>

#include "cJSON.h"

void storage_repair_boot(void);
/* ZKT 2.7.6: false when a block the last-resort reformat left open could be
 * neither completed nor journaled as a byte source this boot: any mount
 * would erase it, so storage_init must not mount the store (LOCAL_FAILURE,
 * as for a store that will not mount); the next boot tries again. */
bool storage_repair_storage_allowed(void);
void storage_repair_after_mount(void);
/* From the ADD outbox task while connected: a few log lines and at most one
 * evidence record per call, each evidence record held until ADD acknowledges. */
void storage_repair_report_step(void);
bool storage_repair_append(cJSON *storage);
/* A runtime storage fault (EIO, ENOENT on an existing file, EBADMSG, ENOSPC,
 * a failed probe): recorded once per boot for the next boot's report. */
void storage_repair_note_damage(int error);
