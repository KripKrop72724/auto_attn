#pragma once
/* A RAM copy of SPIFFS's metadata for the boot repair: every block's lookup
 * page and every page's 5-byte header. SPIFFS_check reads those about
 * 2.1 million times on the 8 MB store (5-byte header reads bypass SPIFFS's
 * cache); from PSRAM that is seconds instead of the estimated 30-60 s of
 * flash calls. Reads that fall entirely inside mirrored bytes are served from
 * RAM; every other read goes to flash. Programs and erases go to flash first
 * and then update the copy with NOR's own rules (a program can only clear
 * bits, an erase sets 0xFF); after a failed program or erase the affected
 * mirrored bytes are read back from flash, since a failed NOR operation may
 * still have changed some bits. Only the private repair mount uses the
 * partition while the mirror exists. No ESP-IDF dependency: the host harness
 * proves a repair through the mirror leaves the same flash and report. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    void *context;
    bool (*read)(void *context, uint32_t address, void *buffer, uint32_t length);
    bool (*write)(void *context, uint32_t address, const void *buffer, uint32_t length);
    bool (*erase)(void *context, uint32_t address, uint32_t length);
} srm_flash_t;

typedef struct {
    srm_flash_t flash;
    uint32_t size, block_size, page_size, blocks, pages;
    uint8_t *lookup;  /* blocks x page_size: each block's lookup page */
    uint8_t *headers; /* pages x 5: each page's header */
    bool valid;
    uint32_t hits, misses, refreshes;
} srm_mirror_t;

#define SRM_HEADER_BYTES 5U

/* Bytes the caller allocates (PSRAM in the firmware) for lookup and headers. */
size_t srm_lookup_bytes(uint32_t size, uint32_t block_size, uint32_t page_size);
size_t srm_header_bytes(uint32_t size, uint32_t page_size);
/* Fills the copy with one pass of reads through scratch (at least one block). */
bool srm_init(srm_mirror_t *mirror, const srm_flash_t *flash, uint32_t size, uint32_t block_size,
              uint32_t page_size, uint8_t *lookup, uint8_t *headers, uint8_t *scratch, size_t scratch_size);
bool srm_read(srm_mirror_t *mirror, uint32_t address, void *buffer, uint32_t length);
bool srm_write(srm_mirror_t *mirror, uint32_t address, const void *buffer, uint32_t length);
bool srm_erase(srm_mirror_t *mirror, uint32_t address, uint32_t length);
/* ZKT 2.7.6: the mirrored bytes of [address, address + length) read again
 * from flash (after writes whose result the caller verified on raw flash:
 * a program that reported success may not have stuck). */
bool srm_refresh(srm_mirror_t *mirror, uint32_t address, uint32_t length);
