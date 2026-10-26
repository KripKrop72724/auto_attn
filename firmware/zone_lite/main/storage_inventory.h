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
#define SI_MAX_FILES 16U   /* reported files: unreadable first, then the caller's priority names, then the largest */
#define SI_MAX_NAMES 256U  /* directory entries examined */
/* A SPIFFS data page holds 251 bytes of a file (256 less its 5-byte header),
 * so file offset o lives in data span o / 251. Reads are aligned to spans: an
 * unreadable region is a whole number of spans and starts on a page boundary. */
#define SI_SPAN_BYTES 251U
#define SI_CHUNK_SPANS 16U /* spans per read while a file reads back; a failed chunk is read by span */
#define SI_CHUNK_BYTES (SI_CHUNK_SPANS * SI_SPAN_BYTES)

typedef struct {
    char name[33];
    uint32_t bytes;
    uint32_t unreadable_bytes;
    uint32_t first_error_offset;
    int first_error; /* errno of the first failed open or read; 0 when fully readable */
    /* ZKT 2.7.7: the data spans that would not read back one at a time (each
     * a failed span read, errno in span_errors_other when not EIO): how many,
     * the last one's offset and an order-free digest of their indices
     * (span_digest.h), so the confirm-first exception can compare them with
     * the spans the boot repair holds on flash. other_unreadable: bytes lost
     * any other way (an open or stat failure, a file ending before its size). */
    uint32_t unreadable_spans, last_error_offset, span_digest, span_errors_other;
    bool other_unreadable;
    /* ZKT 2.7.7: one of the caller's priority names (the boot repair's
     * custody files), guaranteed a report slot ahead of merely larger files
     * so the confirm-first gate's CUSTODY_MISSING check never loses a small
     * custody file to the cap (design 14.2 item 6). */
    bool priority;
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
    /* ZKT 2.7.7: names (no leading '/') kept ahead of merely larger files;
     * unreadable files still come first. NULL or 0 means none. */
    const char (*priority)[33];
    uint32_t priority_count;
} si_ports_t;

void si_scan(const char *directory, const si_ports_t *ports, si_report_t *report);

/* Firmware: starts the one-shot scan task; storage.inventory appears in the
 * heartbeat once it has finished, and each unreadable file is logged once. */
void storage_inventory_start(void);
bool storage_inventory_append(cJSON *storage);
/* ZKT 2.7.7: a copy of the finished scan; false while it has not finished. */
bool storage_inventory_snapshot(si_report_t *out);
