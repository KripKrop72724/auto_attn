#pragma once
/* ZKT 2.7.6: the last-resort in-place reformat's redo records.
 *
 * A wedged store (no erased page, nothing 2.7.5 may compact) gets room one
 * 4 KiB block at a time: the block's pages that anything may still read are
 * copied into a verified record in the flash past the last partition (the
 * redo area), the block's magic is cleared, the block is erased, the kept
 * pages are programmed back at their own page indices and the magic last.
 * A reset at any write leaves either a block SPIFFS's mount would erase (no
 * magic) or the finished block; the next boot completes the block from its
 * record on raw flash before anything mounts the store. A record that cannot
 * be completed that way (another writer touched the block) is never replayed:
 * its pages become byte sources for the repair's survey.
 *
 * This module holds the record format, the block plan (what is kept, what is
 * dropped, which blocks may be chosen), the per-block writer and the boot
 * recovery. It has no ESP-IDF or SPIFFS dependency: flash goes through
 * srx_io_t, so the firmware glue, the engine and the host harness share it.
 * Geometry is Zone Lite's: 256-byte pages, 4 KiB blocks, one lookup page. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "storage_repair_plan.h"

#define SRX_PAGE 256U
#define SRX_BLOCK 4096U
#define SRX_ENTRIES 15U               /* pages per block after the lookup page */
#define SRX_MAGIC_OFFSET 252U         /* SPIFFS_MAGIC_PADDR within the block */
#define SRX_ERASE_COUNT_OFFSET 254U   /* SPIFFS_ERASE_COUNT_PADDR within the block */
#define SRX_LOOKUP_BYTES 252U         /* lookup page bytes before the magic */

/* SPIFFS page header flags (active low) and object ids. */
#define SRX_PH_USED 0x01U
#define SRX_PH_FINAL 0x02U
#define SRX_PH_INDEX 0x04U
#define SRX_PH_IXDELE 0x40U
#define SRX_PH_DELET 0x80U
#define SRX_ID_FREE 0xFFFFU
#define SRX_ID_DELETED 0x0000U
#define SRX_ID_IX 0x8000U

/* ---- the record (one 4 KiB slot, little-endian) ----------------------- */
#define SRX_RECORD_MAGIC 0x31525253U  /* "SRR1" */
#define SRX_RECORD_VERSION 1U
#define SRX_HEADER_BYTES 256U
#define SRX_OBJECTS 5U
#define SRX_OBJECT_BYTES 40U
#define SRX_OFF_OBJECTS 40U
#define SRX_OFF_HEADER_CRC 240U
#define SRX_OFF_DONE 244U
#define SRX_OFF_SETTLE 248U
#define SRX_OFF_ATTEMPTS 252U
#define SRX_OFF_LOOKUP 256U
#define SRX_OFF_PAGES 512U
#define SRX_FLAG_UNCONFIRMED 0x01U    /* written by an unconfirmed image: never redone */
#define SRX_OBJECT_CURSOR 0x01U       /* cursor_offset is the queue checkpoint's offset then */
/* settle word: bit e cleared, entry e settled; bit 16+e cleared, entry e
 * reported to ADD as a gap (its record is then kept for good); bit 31
 * cleared, the record is a byte source and never redone. */
#define SRX_SETTLE_SOURCE 0x80000000U
#define SRX_ATTEMPTS 4U               /* redo attempts before a record becomes a source */

typedef struct {
    uint16_t obj;
    uint8_t cls, flags;
    uint32_t cursor_offset;
    char name[SRP_NAME_BYTES];
} srx_object_t;

typedef struct {
    uint32_t reclaim_id, sequence, image_tag;
    uint8_t flags, objects;
    uint16_t block, kept_mask, drop_mask, erase_count, block_count;
    uint32_t old_crc, new_crc;
    srx_object_t object[SRX_OBJECTS];
    /* Progress words, programmed bit by bit with no erase. */
    uint32_t done, settle, attempts;
} srx_record_t;

uint16_t srx_spiffs_magic(uint32_t block_count, uint32_t block);
/* At least three set bits: clearing it always invalidates it, and a torn
 * erase cannot plausibly make it read valid (block 983 of 2,048 has 0x0000). */
bool srx_magic_strong(uint32_t block_count, uint32_t block);
/* The block's lookup page (at least 256 bytes) carries its valid magic. */
bool srx_has_magic(const uint8_t *lookup, uint32_t block_count, uint32_t block);
bool srx_erased(const uint8_t *bytes, size_t length);
uint16_t srx_get16(const uint8_t *p);
uint32_t srx_get32(const uint8_t *p);

/* The target of a block read raw (`old`, 4096 bytes): drop_mask's entries
 * erased (lookup entry 0xFFFF, page 0xFF), the erase count and magic set,
 * every other byte as it was. Returns its CRC-32. */
uint32_t srx_target(const uint8_t *old, uint16_t drop_mask, uint16_t erase_count, uint32_t block_count,
                    uint32_t block, uint8_t *target);
/* A record sector (4096 bytes) from its header fields and the target (the
 * pages of kept_mask's entries go in entry order); progress words erased. */
void srx_encode(const srx_record_t *header, const uint8_t *target, uint8_t *sector);
/* Validates a sector (magic, version, header CRC, the expanded target's CRC
 * against new_crc) and decodes it; `target` (4096 bytes, may be NULL)
 * receives the expanded target. */
bool srx_decode(const uint8_t *sector, srx_record_t *record, uint8_t *target);
/* The kept page of entry e within the sector, or NULL when e is not kept. */
const uint8_t *srx_kept_page(const uint8_t *sector, const srx_record_t *record, uint32_t entry);
unsigned srx_attempts_used(uint32_t attempts);
bool srx_is_source(const srx_record_t *record);
/* Every kept entry settled: the slot may be reused. */
bool srx_retired(const srx_record_t *record);
/* A kept entry was reported and not settled: the record stays for good. */
bool srx_retained(const srx_record_t *record);

/* ---- the block plan ---------------------------------------------------- */
enum {
    SRX_KEEP_NONE = 0,    /* kept as it is, read by nothing (deleted, leaked) */
    SRX_KEEP_CUSTODY,     /* a data page the repair can account for (confirmed images only) */
    SRX_KEEP_EXCLUDED,    /* never in a window */
};
typedef struct {
    uint16_t lu, obj;     /* lookup entry, page header object id */
    uint8_t flags;        /* page header flags */
    bool erased;          /* the whole page reads 0xFF */
    bool referenced;      /* a valid index page of any object names it */
    bool index;           /* its own header says header or index page (not deleted) */
    uint8_t keep;         /* SRX_KEEP_* when kept */
} srx_entry_t;
typedef enum {
    SRX_PLAN_OK = 0,
    SRX_PLAN_MISLABELED,  /* a lookup entry disagrees with its page */
    SRX_PLAN_NO_GAIN,     /* nothing to drop */
    SRX_PLAN_MAGIC,       /* magic not valid now, or two set bits or fewer */
    SRX_PLAN_INDEX,       /* holds a header or index page */
    SRX_PLAN_EXCLUDED,    /* holds a page of a file never put in a window */
    SRX_PLAN_CUSTODY,     /* holds custody: confirmed images only */
} srx_plan_reason_t;
typedef struct {
    uint16_t kept, dropped;
    uint8_t gain, tail;   /* dropped entries; of those, after the last kept one */
    bool custody;         /* a kept page is custody */
    srx_plan_reason_t reason;
} srx_plan_t;
/* A page is dropped only when its lookup entry is 0x0000, its own deleted
 * flag is set and no valid index page names it; entry 0 stays while any
 * later entry stays (SPIFFS counts a block free by its entry 0). */
srx_plan_t srx_plan_block(const srx_entry_t entry[SRX_ENTRIES], bool magic_ok, bool unconfirmed);
const char *srx_plan_reason_name(srx_plan_reason_t reason);

/* ---- flash ------------------------------------------------------------- */
typedef struct {
    void *context;
    uint32_t area_size;      /* bytes of the redo area; 0 when there is none */
    bool (*area_read)(void *context, uint32_t offset, void *buffer, uint32_t length);
    bool (*area_write)(void *context, uint32_t offset, const void *buffer, uint32_t length);
    bool (*area_erase)(void *context, uint32_t offset, uint32_t length);
    /* The store: reads raw (never a cache or mirror); writes and erases may go
     * through the mount's HAL so a mirror follows them. */
    bool (*store_read)(void *context, uint32_t offset, void *buffer, uint32_t length);
    bool (*store_write)(void *context, uint32_t offset, const void *buffer, uint32_t length);
    bool (*store_erase)(void *context, uint32_t offset, uint32_t length);
    /* Optional: a window (magic cleared, not yet programmed) opens or closes;
     * another chance to retry a block that did not verify (false: stop). */
    void (*window)(void *context, bool open);
    bool (*may_retry)(void *context);
    void (*log)(void *context, const char *level, const char *code, const char *message);
} srx_io_t;

typedef enum { SRX_WRITE_OK = 0, SRX_WRITE_IO, SRX_WRITE_VERIFY } srx_write_t;
/* Clears the block's magic, erases it, programs the target's non-erased
 * pages, the lookup bytes, the erase count and the magic last, and reads it
 * back raw; up to `retries` more times on a mismatch. A block that never
 * verifies is left without magic. `readback`: 4096 bytes of scratch. */
srx_write_t srx_write_block(const srx_io_t *io, uint32_t block, const uint8_t *target, uint8_t *readback,
                            unsigned retries);
/* Programs a record into a slot (erased first unless it reads all 0xFF) and
 * reads it back. `scratch`: 4096 bytes. */
bool srx_write_record(const srx_io_t *io, uint32_t slot, const uint8_t *sector, uint8_t *scratch);
/* Clears `bits` (programs ~bits) in the progress word at `offset` of a slot,
 * and checks they read cleared. */
bool srx_clear_bits(const srx_io_t *io, uint32_t slot, uint32_t offset, uint32_t bits);

/* ---- the area ---------------------------------------------------------- */
typedef enum {
    SRX_AREA_ABSENT = 0,  /* the chip or the partition table does not prove it unused */
    SRX_AREA_READY,
    SRX_AREA_FOREIGN,     /* a sector neither erased nor ours: never written */
    SRX_AREA_OVERLAP,     /* the external partition could not be registered */
    SRX_AREA_UNREADABLE,
    SRX_AREA_NO_MEMORY,
} srx_area_state_t;
const char *srx_area_name(srx_area_state_t state);

typedef enum {
    SRX_SLOT_ERASED = 0,
    SRX_SLOT_TORN,        /* our own interrupted write (the area is claimed): free */
    SRX_SLOT_FOREIGN,
    SRX_SLOT_DONE,        /* free */
    SRX_SLOT_OPEN,        /* neither done nor a source: recovery's business */
    SRX_SLOT_SOURCE,
    SRX_SLOT_RETIRED,     /* a source whose every entry settled: free */
} srx_slot_t;

#define SRX_SLOTS 96U
typedef struct {
    srx_area_state_t state;
    uint32_t slots;
    uint8_t slot[SRX_SLOTS];
    uint16_t block[SRX_SLOTS];       /* the block of a valid record */
    uint32_t sequence[SRX_SLOTS];
    uint32_t newest_sequence, newest_reclaim;
    uint32_t foreign, first_foreign, foreign_crc;
    /* boot recovery */
    uint32_t redone, reasserted, to_sources, exhausted, failed;
} srx_area_t;

/* Reads every slot and classifies it. Before this device claimed the area,
 * a sector neither erased nor a valid record is foreign (state FOREIGN, its
 * count, the first one and the CRC of its sector); once claimed it is our
 * own torn write. `sector`: 4096 bytes. */
void srx_discover(const srx_io_t *io, bool claimed, uint8_t *sector, srx_area_t *area);
bool srx_slot_free(const srx_area_t *area, uint32_t slot);
/* A block that is the block of a record not yet free (open, source). */
bool srx_block_busy(const srx_area_t *area, uint32_t block);

typedef enum {
    SRX_ACT_NONE = 0,     /* done, or already a source */
    SRX_ACT_SOURCE,       /* another image's, or an unconfirmed image's, or the journal says so */
    SRX_ACT_EXHAUSTED,    /* its attempts used */
    SRX_ACT_REASSERT,     /* the block is the target: magic and erase count again, then done */
    SRX_ACT_REDO,         /* no magic, or the old image: the window again from the record */
    SRX_ACT_CHANGED,      /* something mounted and wrote the block: a source */
} srx_action_t;
srx_action_t srx_decide(const srx_record_t *record, const uint8_t *raw_block, uint32_t block_count,
                        uint32_t running_tag, bool force_sources);

/* Boot recovery on raw flash, before any mount, in every mode: every open
 * record in sequence order, per srx_decide. `work`: 3 x 4096 bytes. Returns
 * the records left open (an I/O error): the caller then writes the journal's
 * SOURCES flag before anything mounts the store, or keeps it unmounted. */
uint32_t srx_recover(const srx_io_t *io, uint32_t block_count, uint32_t running_tag, bool force_sources,
                     uint8_t *work, srx_area_t *area);
