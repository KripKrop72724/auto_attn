#pragma once
/* Completes a SPIFFS removal that stopped at a corrupt data page.
 *
 * ESP-IDF's SPIFFS removes a file by first flagging its index header IXDELE,
 * which hides the name at once, and then freeing pages from the end. A data
 * page that fails its header check stops the removal there, and every earlier
 * page stays allocated until a filesystem check. That check can also change
 * other damaged files, so it is not used.
 *
 * This engine runs on the unmounted partition and changes exactly one object:
 * the single IXDELE header with the expected name and the size of a generation
 * ADD already holds in custody. It first reads that object the way SPIFFS
 * reads a file and collects the event UIDs of every complete row outside the
 * receipted unreadable regions; nothing is freed unless their number matches
 * the receipted run. It then frees only pages whose lookup entry and page
 * header both name the object, header last, exactly as SPIFFS deletes a page.
 * The engine has no ESP-IDF dependency so the host tests run it against
 * ESP-IDF's own SPIFFS. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    void *context;
    bool (*read)(void *context, uint32_t address, void *buffer, size_t length);
    bool (*write)(void *context, uint32_t address, const void *buffer, size_t length);
    void (*pace)(void *context); /* Optional; called between batches of flash work */
} so_flash_t;

typedef struct {
    uint32_t offset, length;
} so_region_t;

typedef struct {
    uint32_t partition_size, block_size, page_size;
    const char *name;
    uint32_t receipted_size;       /* Exact size of the receipted generation */
    const so_region_t *unreadable; /* Regions the receipted run could not read */
    size_t unreadable_count;
    uint32_t expected_uids;        /* UIDs of the receipted run */
    uint32_t uid_shortfall_limit;  /* Rows the stopped removal may already have freed */
} so_target_t;

typedef enum { SO_NOTHING_TO_DO = 0, SO_COMPLETE, SO_REFUSED, SO_FAILED } so_result_t;

typedef struct {
    so_result_t result;
    char code[48];
    uint32_t candidates, header_size, object_pages, spans, readable_spans, rows, uids;
    uint32_t pages_freed, pages_skipped;
    uint64_t used_before, used_after; /* Allocated pages x data bytes, as SPIFFS_info */
    uint8_t *uid_bytes;               /* uids x 32 bytes; the caller frees it */
} so_outcome_t;

void so_complete_orphan(const so_flash_t *flash, const so_target_t *target, so_outcome_t *outcome);
const char *so_result_name(so_result_t result);
