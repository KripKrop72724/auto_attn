#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"

/* A read-only map of the attendance store, taken once per boot: every file's
 * size and the bytes the filesystem cannot read back, with the first failing
 * offset and errno. It changes nothing. 2.7.3 could not confirm on 13FL
 * (13 October 2026) and its report could not say which file was damaged.
 *
 * The engine has no ESP-IDF dependency so the host tests execute it; the
 * firmware runs it in a low-priority task a minute after boot. */
#define SI_MAX_FILES 16U   /* reported files: unreadable ones first, then the largest */
#define SI_MAX_NAMES 256U  /* directory entries examined */
#define SI_PAGE_BYTES 256U /* SPIFFS page: an unreadable region is skipped to the next page */
#define SI_CHUNK_PAGES 16U /* pages per read while a file reads back; a failed chunk is read by page */

typedef struct {
    char name[33];
    uint32_t bytes;
    uint32_t unreadable_bytes;
    uint32_t first_error_offset;
    int first_error; /* errno of the first failed open or read; 0 when fully readable */
} si_file_t;

typedef struct {
    bool done;
    bool complete;  /* every entry was examined within the limits */
    int error;      /* errno when the directory itself could not be read */
    uint32_t files; /* entries examined */
    uint32_t unreadable_files;
    uint64_t bytes, unreadable_bytes;
    size_t count;
    si_file_t file[SI_MAX_FILES];
} si_report_t;

typedef struct {
    void *context;
    /* Called between chunks; returning false stops the scan (incomplete). */
    bool (*keep_going)(void *context);
} si_ports_t;

void si_scan(const char *directory, const si_ports_t *ports, si_report_t *report);

/* Firmware: starts the one-shot scan task; storage.inventory appears in the
 * heartbeat once it has finished, and each unreadable file is logged once. */
void storage_inventory_start(void);
bool storage_inventory_append(cJSON *storage);
