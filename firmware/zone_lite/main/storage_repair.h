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
#include <stdint.h>

#include "cJSON.h"
#include "storage_exception.h"

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

/* ---- ZKT 2.7.7: the confirm-first storage exception's glue ----------------
 * (storage_exception.h holds the decision; docs/zone-lite-2.7.7-confirm-first.md)
 * Storage-repair builds only. */
/* The boot repair's merged report as the gate reads it, with this boot's
 * facts (the private mount, the VFS mount rule, the attempts guard, the redo
 * area's program/erase test, the gap records ADD has not acknowledged). */
bool storage_repair_exception_repair(sx_repair_t *out);
/* 2.7.4's inventory as the gate reads it; false while it has not finished. */
bool storage_repair_exception_inventory(sx_inventory_t *out);
/* The exception's NVS record (storage_repair/exception) as this boot left it
 * (after a round boot's update); false when there is none. */
bool storage_repair_exception_record(sx_record_t *out);
/* Seals, stores and reads back the record; true only when the read-back
 * matches (the gate's D5). */
bool storage_repair_exception_store(sx_record_t *record);
/* This boot runs an image that confirmed under the exception (the record
 * says so and the running image is valid). */
bool storage_repair_exception_active(void);
/* The first word of the running image's app_elf_sha256 (confirmation keeps
 * it), and whether the running image is unconfirmed. */
uint32_t storage_repair_image_tag(void);
bool storage_repair_running_pending(void);
/* Programs and erases the VFS's SPIFFS has made since boot (the stable
 * window's store fingerprint, with SPIFFS's used and total bytes). */
uint32_t storage_repair_store_writes(void);
/* This unconfirmed boot may use the exception (its repair deferred for room
 * only and the redo area passed its test): the persistence probe writes
 * nothing this boot (queue_store.h, qs_hold_probe). */
bool storage_repair_hold_probe(void);
/* The OTA floor (critique 1): this device's redo area or reclaim journal
 * holds anything, or the image confirmed under the exception, or the store
 * stays unmounted: only an image that reads the redo area may be installed. */
bool storage_repair_ota_floor(void);
/* The boot repair's survey of every object header: false when it did not
 * run; `journal_names` whether any header (visible or hidden) is a journal
 * file's (the OTA guard's evidence while the VFS is unmounted). */
bool storage_repair_survey(bool *journal_names);
