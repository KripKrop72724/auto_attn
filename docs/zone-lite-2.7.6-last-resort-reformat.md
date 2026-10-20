# Zone Lite 2.7.6: last-resort reformat for wedged stores

Status: design, on `release/zkt-2.7.6` from `origin/main` 5de7724 (2.7.5
released, #398 and #400 merged). Nothing here is implemented yet.

Owner decision, 16 October 2026, reconfirmed 20 October ("yes start 2.7.6"):
a last-resort reformat is approved when the store cannot otherwise be made
consistent, custody first: every readable unsettled byte survives or reaches
ADD as exact gap evidence, and nothing readable may be lost.

Scope: the last resort for wedged stores (no erased page, GC-unsafe, room
cannot be made), and nothing else unless the last resort needs it. Snapshot
and transport changes belong to a later release.

**Summary.** 2.7.6 reformats in place, one 4 KiB block at a time. The
reformat is limited to the blocks the store needs room in. Each chosen block
is erased (as `SPIFFS_format` and SPIFFS's GC erase it: erase, erase count,
magic). Then every page the store still needs is programmed back at its own
place, byte for byte. Only deleted pages that no repair rule reads become
erased. No file, size, offset, cursor, checkpoint, `.pos` or NVS key changes,
so every image, the rollback images included, reads the same files.

Before a block is erased, its kept pages go into a verified **redo record**.
The records live in the 384 KiB of flash past the last partition, which no
image uses. That puts every byte on flash at every moment, so a power cut at
any write costs nothing on the same image.

A whole-partition `SPIFFS_format` is not built. Its PSRAM-only window
(section 4) is one to two minutes over every held byte, and on this partition
table nothing can close it. Once the store has room, the 2.7.5 engine takes
over unchanged: normalization, the check, the restore, compaction and the
probe.

## 1. Evidence

### 1.1 Peshawar-02 on 2.7.5 (20 October 2026)

The first boot ran from 2.5.2 and was unconfirmed. The run ended DEFERRED:

- **Error:** `error_stage` `full`, `error_code` -10001, 21.3 s, check
  skipped.
- **Store:** used 4,976,828 -> 4,962,772 of 7,703,441 bytes.
- **Holds:** 6 files, 3 held, 3,577,196 bytes held of a 4,357,816-byte budget.
- **Work done:** 153 spans rewritten. Removed: 51 orphan index pages, 4 strays
  and 1 duplicate. 1 cursor reset.
- **Room:** 1 page needed, 0 erased, 0 blocks compacted, 0 reserve pages.
- **Logs:**
  - `STORAGE_REPAIR_LEVEL2_SKIPPED` "none would let a block be erased (3 pages
    needed, 0 erased)".
  - `STORAGE_REPAIR_FULL` "(1 pages needed, 0 erased)".
  - `STORAGE_REPAIR_DEFERRED`.

`/pending.jsonl` is 2,526,978 bytes, has 0 gaps and is fully readable by the
repair. The persistence probe still fails (ENOENT, no free page), so boot
health fails and the image rolls back to 2.5.2 after 900 s
(`BOOT_HEALTH_TIMEOUT`).

The store, from `SPIFFS_info` (used = allocated pages x 251):

| | pages | bytes (x 251) | share |
|---|---|---|---|
| all (2,048 blocks x 15 entries) | 30,720 | 7.71 MB | 100% |
| allocated after the run | 19,772 | 4.96 MB | 64.4% |
| of which held custody data (+ about 118 index pages) | about 14,370 | 3.58 MB | 46.8% |
| of which other: leaked pages of removed files, catalog, probe | about 5,400 | 1.36 MB | 17.6% |
| deleted | 10,948 | 2.75 MB | 35.6% |
| erased | 0 | 0 | 0% |

So P02 has 5.3 deleted pages per block on average, and none is erased. Every
block also holds about 7 custody pages, so neither SPIFFS's GC nor the 2.7.5
compaction has a place to move a page to. Erasing the deleted pages where
they lie is enough: one block would give the 1 page normalization needs.

### 1.2 Others

- **13FL** (2.4.12): 61% used, a 3.8 MB `blocked_identity.jsonl` unreadable
  from about 50 KB, the same probe ENOENT. 2.7.4 rolled back. It is probably
  wedged the same way.
- **The 2.7.5 harness:** seeds 12, 13 and 16 at 8 MB and 6 of the fourth
  pass's fresh seeds end wedged, at 70 to 77% used, with no erased page and a
  damaged queue in every block (2.7.5 doc, section 12). All their rows are on
  flash.

### 1.3 SPIFFS facts the design rests on (ESP-IDF 5.5.3, read from source)

1. **Erase and magic.** `spiffs_erase_block` erases the block, writes the
   erase count (last lookup slot), then the magic (second-to-last slot,
   `0x20140529 ^ page size ^ (block_count - bix)`).
2. **Mount tolerates one block without magic.** `spiffs_obj_lu_scan` accepts
   exactly one block whose magic is wrong ("powered down during an erase"),
   and erases it at the end of the mount. Two such blocks fail the mount with
   `SPIFFS_ERR_NOT_A_FS`.
3. **Free blocks are counted by entry 0.** The mount counts a block as free
   when its entry 0 is free. `spiffs_obj_lu_find_free` decrements
   `free_blocks` when it allocates entry 0. Allocation takes any free entry
   from the cursor on, and refuses below two free blocks unless `cleaning`.
4. **GC scoring assumes a free tail.** `spiffs_gc_find_candidate` stops
   counting a block's entries at its first free entry ("all following entries
   are free also"). `spiffs_gc_quick`, `spiffs_gc_clean`, the check and the
   allocator handle each entry on its own.
5. **What is read.** SPIFFS reads file content only through index entries.
   The 2.7.5 survey reads a page flagged deleted only when it is the page an
   index names (`own_data_page` for `ref`). Byte copies must be finalized,
   undeleted and carry the file's lookup entry. Size recovery reads only
   pages whose lookup entry agrees. So a page whose lookup entry is
   `0x0000`, and that no index names and no rule marks as needed, is read by
   nothing.
6. **Format.** `SPIFFS_format` (and `esp_spiffs_format`, which unmounts,
   formats and remounts) erases all 2,048 blocks one 4 KiB sector at a time
   before anything can be written.
7. **Flash writes outside the table.** `esp_flash` refuses writes only
   inside the bootloader, the partition table or the running app
   (`esp_partition_main_flash_region_safe`). `esp_partition_register_external`
   registers a range on the internal chip unless it overlaps a partition or
   passes `chip->size`.

The partition table: nvs 0x11000+0x6000, otadata, phy_init, factory
0x20000+0x280000, ota_0 0x2A0000+0x280000, ota_1 0x520000+0x280000, storage
0x7A0000+0x800000. On the 16 MB chip, flash past 0xFA0000 (0x60000 =
384 KiB = 96 sectors) is in no partition.

## 2. Rules

The 2.7.5 rules R1 to R4, R6 and R7 stand. R5 ("no reformat") is replaced by
R5 below, and R8 to R11 are new.

- **R5 In place only.** A block is reformatted only with every page the
  store needs programmed back at its own place. No whole-partition format and
  no `SPIFFS_format`/`esp_spiffs_format` call: the contract test keeps that.
- **R8 Always on flash.** Before a block is erased, its kept pages are in a
  redo record that has been read back and verified. A block's magic is
  cleared before the erase and written last, so a block whose content is
  incomplete never mounts as valid.
- **R9 Only unread pages go.** A kept page keeps its bytes, header, lookup
  entry and page index. A page is dropped only when its lookup entry is
  `0x0000` (deleted) and no rule of the repair needs it.
- **R10 The redo area.** It is the flash past the last partition, used only
  when:
  - the chip and the partition table prove it unused;
  - it reads erased or as records of this format;
  - it is reached through a partition handle registered for exactly that
    range.

  Otherwise there is no reformat.
- **R11 SPIFFS's own invariants.** Entry 0 of a block is freed only with
  the whole block. At most one block is without magic at any moment. SPIFFS's
  counters are adjusted exactly as its own erase adjusts them.

## 3. Trigger

The reformat ("reclaim" in the code) runs only at the point where 2.7.5
would hand the store over without room. The check is that the run itself has
used every means 2.7.5 has, in this boot, and they did not make the room.

| # | Where | 2.7.5 outcome it replaces |
|---|---|---|
| T1 | `normalize_all`, third attempt still `SPIFFS_ERR_FULL`. The run has already compacted, used the GC reserve where the work fits, and run level 2 (or skipped it because `level2_can_free` proved no removal frees a block). | `DEFERRED`, `full` (P02) |
| T2 | The finish: fewer than two free blocks, and erased pages below `spare_for_compaction`. | `DEFERRED`, `gc_unsafe` |
| T3 | The resume: an entry is `RESUME_BLOCKED` for room (unhide, adopt, rename or merge found no erased page). | `RESTORE_DEFERRED` / `MERGE_DEFERRED`, `full` |
| T4 | A check pass failed with `SPIFFS_ERR_FULL`, and `ensure_room` could not reach three free blocks. | level escalation, then `FAILED` |

All of the following must also hold:

- **G1** The mode is `FULL` or `NORMALIZE_ONLY`. In `REPORT_ONLY` the run
  only completes a pending redo (section 7).
- **G2** The redo area is `READY` (section 6.1).
- **G3** Time is left: the soft deadline leaves at least 1 s per planned
  block (the worst case of a sector erase twice, plus programs), plus the
  check estimate. Blocks are taken one at a time and the run stops between
  any two blocks.
- **G4** The NVS headroom check (section 4 of the 2.7.5 doc) includes the
  `reclaim` journal blob.
- **G5** No redo record of an earlier reclaim is still open (section 6.4).
  An open record is first settled as a byte source (section 7.3).
- **G6** On an unconfirmed image, only eligible blocks (section 8).

At most two reclaim rounds run per boot. A round that frees nothing ends the
run as 2.7.5 would (`DEFERRED`), with `STORAGE_REPAIR_RECLAIM_SHORT` giving
the reason.

**Why not "deferred for the same shortage on consecutive boots".** The brief
offers it as a guard. It is neither needed nor possible:

- **Not needed.** The reclaim deletes nothing anything reads (R9), and every
  kept byte is on flash at every write (R8). It is a compaction with a redo
  log, not a gamble that needs a second opinion.
- **Not possible.** An unconfirmed image never sees its own second boot,
  because any reset rolls it back. A "second boot" condition would never
  fire on P02.

A previous run's `SRP_STATE_FULL` with the same shortage signature (P02
carries 2.7.5's) is logged as corroboration and not required.

**Target.** The run reclaims until the erased pages counted by the survey
reach:

- what normalization allocates (`pages_for_work` without its margin);
- plus `spare_for_compaction` (what cleaning the best block consumes);
- plus 30 pages, about two blocks, for SPIFFS's own writes in the check and
  the finish;
- or, for T2, `spare_for_compaction` plus the 30 pages.

For P02 that is about 45 pages, about 10 blocks at the 5 droppable pages a
block averages. A confirmed image is capped at 256 blocks per boot and an
unconfirmed one at 128. Section 12 gives the times.

## 4. Why a whole-partition format is not built

The format the brief describes:

1. Hold every custody, dedupe and operational file in PSRAM, verified span by
   span against a CRC.
2. `esp_spiffs_format` or `SPIFFS_format` on the private mount.
3. Rewrite the files (custody first), journaling each file's completion in
   NVS.

Its window starts at the first erase and ends at the last verified page.
Throughout the window, every held byte not yet rewritten exists only in
PSRAM. On P02:

- **The erase.** `SPIFFS_format` erases 2,048 sectors one at a time:
  - at about 45 ms each typically, that is about 90 s;
  - at the datasheet maximum of about 400 ms, several minutes;
  - a custom erase of the whole range with 64 KiB block erases still takes
    25 to 40 s.

  Nothing can be rewritten before it ends, because SPIFFS mounts only with
  every block's magic in place.
- **The rewrite.** 3.58 MB through SPIFFS is about 14,250 data pages. Each
  costs a page program, its lookup entry and its finalization, plus index
  updates and the read-back: about 20 to 40 s.
- **Total.** About 1 to 2.5 minutes. The P02 boot that wedged is the boot
  after a power cut, and site power often flickers as it returns.

Neither order nor journal closes the window:

- **Rewriting custody first** helps little: `/pending.jsonl` alone is 71% of
  the held bytes.
- **Per-file journaling** only makes the report exact. The bytes held in
  PSRAM die with the cut (`REFORMAT_INTERRUPTED`, owner's rule: reported, but
  lost).

On an unconfirmed image the same cut is worse: it returns the device to
2.5.2 on an empty or half-written store.

- 2.5.2 creates fresh files and keeps writing.
- Its seen-UID set, built from `pending`, `blocked` and `acked_uids`, is gone,
  so its FULL_RECONCILE requeues punches.
- The NVS cursors point past the new files.

The format also needs every custody byte to fit the hold budget. P02 fits
(3.58 of 4.36 MB). 13FL probably does not (a 3.8 MB queue plus its other
files), and over the budget the rule is no format at all.

Closing the window needs a copy of every held byte that survives a power
cut, and none of the places on this partition table can take one:

| Place | Size | Usable |
|---|---|---|
| The inactive OTA slot | 2.5 MiB | No. On an unconfirmed boot it is the rollback image, and too small anyway. |
| Factory | 2.5 MiB | No. It is the bootloader's last resort. |
| NVS | 24 KiB | No. |
| The unpartitioned tail | 384 KiB | Too small for the held bytes. |

What can be made safe is one block at a time. A block's needed pages (at
most 14 x 256 bytes) fit one 4 KiB redo record. Keeping them at their own
page indices means no index changes, so blocks are independent and each one
is atomic through its record. The whole-partition format is the case of
that operation over every block with nothing kept, which custody forbids.

The owner may still ask for a whole-partition format later, for a device
whose redo area is unusable (section 15). It would need its own design round
and harness, and the preconditions of the owner's sketch: confirmed image,
every custody byte held, `.pos` written as 0.

## 5. The in-place reformat

### 5.1 Which pages stay

The engine first runs a fresh survey and its lookup hygiene. A block that
still holds a mislabeled entry is not reformatted. The `needed` set is
`lookup_hygiene`'s, factored out into `needed_pages()`:

- every plausible header;
- every chosen index page;
- every referenced page (`ref`) of a mapped live object;
- every first source (`patch.from`);
- every page of a hidden object an unsettled manifest entry claims;
- added for the reclaim: every deleted page whose header names a span that is
  not perfect in a live custody object.

For entry e of block B, with lookup entry `lu`:

- `lu == 0xFFFF` (erased): stays erased.
- `lu == 0x0000` and the page is not needed: **dropped**.
- Anything else (a used entry, or a deleted page that is needed): **kept**.
- **Entry 0 rule:** if entry 0 would be dropped while any later entry is
  kept, it is kept (as the deleted page it is), so SPIFFS's free-block count
  stays exact (1.3 item 3).

A block with nothing dropped is not a candidate. A block with nothing kept is
erased (its record holds no page).

### 5.2 Which blocks

Candidates are ranked:

1. **Tail gain** first: dropped entries after the last kept entry free a
   contiguous tail, so SPIFFS's GC scoring stays exact (1.3 item 4).
2. Then **total gain**.
3. On an unconfirmed image, **custody exposure** comes before both: fewer
   kept custody data pages first (section 8).

Blocks are taken greedily until the target is met. After the run, compaction
cleans blocks with holes first (`block_cost` already reads every entry), so
the holes a reclaim leaves are consumed by the next moves. The harness oracle
that SPIFFS's own GC moves nothing a live file reads runs on reclaimed stores
too (section 13).

### 5.3 One block

Buffers are in PSRAM (`r->block` and one 4 KiB record buffer). Flash goes
through the existing HAL, the metadata mirror and the internal bounce buffer.
For block B:

1. **Read and build.** Read B (4,096 bytes) and compute `old_crc`. Build the
   target image:
   - the lookup page with dropped entries set to `0xFFFF`;
   - the erase count set to `fs->max_erase_count`, and the magic
     `SPIFFS_MAGIC(fs, B)`;
   - every other lookup byte as it was;
   - kept pages copied;
   - dropped pages set to `0xFF`.

   Compute `new_crc`.
2. **Write the record** (section 6.2) to a free slot: erase the slot,
   program it, read it back, compare. A slot that does not verify is skipped,
   and a second failure stops the reclaim (`RECLAIM_FAILED`).
3. **Clear B's magic:** program `0x0000` at `SPIFFS_MAGIC_PADDR(B)`. From
   here until step 7, any mount (any image) sees B without magic and erases
   it whole. No mount can ever read a half-erased or half-programmed page as
   valid.
4. **Erase B.**
5. **Program the kept pages** in entry order, 256 bytes each.
6. **Program the lookup entries** (the first 30 bytes), then the erase count.
7. **Program the magic.** B is now complete.
8. **Read B back** and compare it with the target image. On a mismatch,
   repeat from step 3 at most twice. After that, stop the reclaim, leave the
   record open and log `RECLAIM_FAILED`. The record's pages become sources
   (7.3); a worn sector is outside SPIFFS's design anyway.
9. **Program the record's `done` word** (`0x00000000`).
10. **Update SPIFFS's state:**
    - drop B's pages from the SPIFFS cache (as `compact` does);
    - `stats_p_deleted -= dropped`;
    - `free_blocks++` only if nothing was kept;
    - `max_erase_count++`, with SPIFFS's wrap.

    The harness checks that these equal a fresh mount's counts.

After the round: `survey()`, then `plan_damage()`, then the caller's 2.7.5
step continues. That step is normalization (T1), compaction then the
gc_unsafe verdict (T2), the resume (T3), or the next check pass (T4).

### 5.4 Why custody holds

- **Kept pages are unchanged.** Every kept page has the same bytes, header,
  lookup entry and page index after the round, so every index entry still
  names the same bytes.
- **Dropped pages were read by nothing.** By 1.3 item 5, no SPIFFS read path
  of any version and no 2.7.x repair rule reads a dropped page. They become
  erased pages under free entries, exactly what a GC erase leaves.
- **No byte is ever only in RAM.** Between step 3 and step 9 every kept byte
  is in the verified record. A cut on the same image redoes the block
  (section 7).
- **File content does not move.** File offsets, legacy queue cursors (their
  prefix CRCs cover the same bytes), `.pos` files, durable-queue and journal
  checkpoints, and file transactions (stage, backup, commit files and their
  NVS checkpoints) are all untouched. The owner's sketch writes `.pos` as 0
  because a format moves content; here nothing moves, so `.pos` is left as
  it is.

The hold is not load-bearing for the reclaim. Where files are held, the run
still compares them after the round: a fresh survey must give every held
file its held CRC. A difference stops further reclaiming, restores in place
from the hold (2.7.5's `restore_in_place`) and is reported. That is an
independent oracle on the device, and the harness proves it never fires.

## 6. The redo area

### 6.1 Discovery (`storage_repair.c`, every boot, before the private mount)

1. `esp_flash_get_physical_size(esp_flash_default_chip)` and
   `esp_flash_default_chip->size` are both at least 0x1000000.
2. Every partition in the table ends at or below 0xFA0000.
3. `esp_partition_register_external(NULL, 0xFA0000, 0x60000, "srp_redo",
   ESP_PARTITION_TYPE_DATA, 0x99, &redo)` returns `ESP_OK`. This also proves
   no overlap. The handle is deregistered when `storage_repair_boot` returns.
4. Its 96 sectors are read through the bounce buffer (about 50 ms). How a
   sector is judged depends on whether this device has used the area before:
   - **Before the first use** (no `claimed` flag in the journal, 6.4): every
     sector must read entirely `0xFF`, or hold a valid record of this format
     (magic, version and header CRC). Anything else is foreign.
   - **After the first use** (`claimed` set): the area has only ever been
     written by this code, so a sector that is neither erased nor a valid
     record is one of our own torn writes. It is a free slot, erased before
     reuse. A torn program can tear the magic itself, so the magic alone
     cannot tell our torn write from foreign content.

The state is reported every boot as one of:

- `READY`;
- `FOREIGN` (any foreign sector);
- `ABSENT` (chip or table check failed);
- `OVERLAP` (registration refused);
- `UNREADABLE` (a read failed).

Only `READY` allows a reclaim, and nothing is ever written to an area in any
other state. The area is reached only through the `redo` partition handle,
never by a raw `esp_flash_write`; the contract test keeps that. The engine
sees it as a port (`srr_redo_port_t`: size, read, write, erase), NULL when not
ready. The harness gives it an emulated 96-sector NOR region with the same
power-cut model as the store.

The 2.7.6 heartbeat reports this state on every device from its first 2.7.6
boot. P06 goes first, so before P02 needs the area we will know it reads
`READY` on the fleet's modules.

### 6.2 Record layout (one 4 KiB sector, little-endian)

| Offset | Field |
|---|---|
| 0 | `u32 magic` `0x31525253` ("SRR1") |
| 4 | `u16 version` 1, `u16 header_bytes` 256 |
| 8 | `u32 reclaim_id` (the repair generation that started the reclaim) |
| 12 | `u32 sequence` (monotonic within the reclaim) |
| 16 | `u32 image_tag` (first word of the writer's app digest) |
| 20 | `u8 flags` (bit 0: written by an unconfirmed image), `u8 objects` (0 to 4) |
| 22 | `u16 block`, `u16 kept_mask`, `u16 drop_mask` (bit e = entry e) |
| 28 | `u16 erase_count` (what step 6 programs), `u16 reserved` |
| 32 | `u32 old_crc`, `u32 new_crc` (CRC-32 of B before, and of the target) |
| 40 | 4 x 50 bytes: object table, one entry per object owning a kept custody data page: `u16 obj`, `u8 class`, `u8 flags` (bit 0: cursor generation valid), `u32 cursor_generation`, `u32 size`, `char name[33]`, 5 pad |
| 240 | `u32 header_crc` (over bytes 0 to 239) |
| 244 | `u32 done` (`0xFFFFFFFF` open; any bit cleared means done, programmed only after step 8 verified) |
| 248 | `u32 retired` (same convention, for records settled as sources) |
| 252 | reserved `0xFFFFFFFF` |
| 256 | the target lookup page (256 bytes) |
| 512 | the kept pages in entry order, 256 bytes each (at most 14) |

A record is valid when the magic, version and `header_crc` hold, and the CRC
of its target image (lookup page plus kept pages expanded by the masks)
equals `new_crc`. A block with more than four custody objects among its kept
data pages is not a candidate. The table is what the conflict rules need:
name, cursor generation and size.

### 6.3 The ring

A new record goes to the next slot after the last sequence. The slot must be
free: erased, invalid, done or retired. An open record is never overwritten.
When the reclaim ends and its journal is `IDLE`, the done slots are erased,
best effort, about 45 ms each. That leaves the area erased, so an interrupted
erasure leaves only invalid or done slots. Wear: one sector erase per
reclaimed block, spread over 96 sectors.

### 6.4 The journal (NVS `storage_repair/reclaim`, new key)

`srp_reclaim_t` holds:

- `version`, `reclaim_id`;
- `state`: `IDLE`, `ACTIVE` or `SOURCES`;
- `image_tag` and `flags` of the image that started it: bit 0 pending image,
  bit 1 `claimed` (the area has been written by this device; never cleared);
- `next_sequence`, `blocks`, `pages_freed`;
- `crc`.

It is about 40 bytes, 4 NVS entries. Its writes:

- `ACTIVE` with `claimed` before the first slot is written, so a torn first
  record is never taken for foreign content (6.1);
- `SOURCES` when a record cannot be redone (section 7);
- `IDLE` when every record is done or retired.

It is not written per block: the records' `done` words are the per-block
journal.

The 2.7.5 layouts stay byte for byte, as R4 and the 2.7.5 doc's "later
releases" note require: `srp_state_t`, `srp_manifest_t`, the gap and reset
lists and `SRP_STATE_VERSION`. During a round, `state.phase` is
`SRP_PHASE_REFORMAT`, the value 2.7.5 reserved. 2.7.5 accepts it as a valid
phase and counts an interrupted round as an incomplete run. The new key is
ignored by every older image.

## 7. Power cuts at every write

### 7.1 Recovery at boot

Recovery runs first, in every mode. It comes after the private mount (whose
own remedy may have erased B) and before the survey, the lookup hygiene and
the resume.

| Cut at | Flash after the cut | Next boot, same confirmed image | Next boot after a rollback (section 8) |
|---|---|---|---|
| journal `ACTIVE` | no record | no record of the id: journal `IDLE` | the same |
| slot erase or record program | slot invalid; B untouched | invalid slot ignored; journal `IDLE` (no open record) | the same |
| clearing B's magic | B intact, magic cleared or torn | mount erases B (remedy) or B intact; record valid and open: **redo** | old image's mount erases B; see 7.3 |
| B's erase | B partly erased, no magic | mount erases B; **redo** | old image's mount erases B; see 7.3 |
| kept pages, lookup, erase count | B partly programmed, no magic | mount erases B; **redo** | the same as above |
| B's magic | magic torn: no magic | mount erases B; **redo** | the same as above |
| after the magic, before or during `done` | B complete; `done` open or torn | B's CRC equals `new_crc`: program `done` (a torn `done` already counts as done) | B complete: the old image reads every file as before |
| journal `IDLE`, slot erasure | records done, slots erased or torn | `ACTIVE` with every record done: `IDLE` | the same |

**Redo** erases B, programs it from the record (steps 3 to 9 of 5.3, with
the recorded erase count) and verifies it, then continues the round if the
target is not yet met. A redo is clean only when all of these hold:

- the journal is `ACTIVE`;
- the record is the newest open one of its id;
- the record's `image_tag` equals the running image's;
- the record's pending flag is clear.

On a confirmed image that is always the case after a cut: the repair runs at
the very start of every boot, so nothing else can have written the store in
between.

A redo is attempted in every mode, `REPORT_ONLY` and `nvs_full` included: it
completes an interrupted write and is not a new repair. Without NVS room it
skips the journal update, since the `done` word is enough. The RTC
breadcrumb records `SRP_PHASE_REFORMAT`. If two consecutive boots ended in a
crash, a watchdog or the deadline during the redo itself, the record is left
as a source (7.3) and the attempts guard takes over, so a redo can never
boot-loop the device.

### 7.2 Redo verification

The record is used only as its CRCs allow. B is compared with `new_crc`
before anything is erased (already done: just program `done`). Any other
content is overwritten, because on the clean path nothing but this record's
own interrupted writes or SPIFFS's mount remedy can have changed B.

### 7.3 A record that cannot be redone: its pages as sources

This happens on an unconfirmed image after a rollback, after a failed
verify, or after a redo given up by the guard. The journal goes to `SOURCES`.
The record is not replayed; its kept pages serve the survey.

A kept data page P of record R, with header (obj o, span k), stood at page
index p. It is a byte source for span k of a live object X when all of these
hold:

- (a) R is valid and not retired.
- (b) X has object id o, X's name equals R's table entry for o, and for a
  legacy queue file X's cursor generation equals the recorded one. The
  owner has not retired and recreated it.
- (c) X's chosen index names p for span k (`X.ref[k] == p`).
- (d) The page now at p is not `own_data_page(o, k)`: it was erased by the
  remedy, or reused by the old image.

P is then the page the index names, as it was. That is the source 2.7.5
already trusts (R1: "the page the index names, when it is this file's
finalized page of the span"); only its bytes come from the record. It merges
by the 2.7.5 byte rules (more programmed wins, a disagreement is a gap).
Normalization then gives the span a new page and an index rewrite.

Every custody data page of an open record ends in exactly one of these ways
(*settled*):

- **back on flash:** the bytes at p equal P;
- **used as a source:** the span is now perfect at another page;
- **superseded:** X's cursor generation moved (its owner retired the file),
  so there is no gap, as in 2.7.5's rule for remnants;
- **recorded:** a `REFORMAT_INTERRUPTED` gap of `(name, k x 251,
  min(251, size - k x 251))`, unless a gap record of that file already covers
  the range (2.7.5's own `SALVAGE`, when 2.7.5 was the rollback image).

When all its custody pages are settled, the record's `retired` word is
programmed. A record still open keeps the journal at `SOURCES`, and no new
reclaim starts (G5).

Header and index pages: on an unconfirmed image they are never in a record
(section 8). After a failed verify on a confirmed image, a kept header or
index page that has no valid copy on flash is written to a new page with its
own lookup entry, as `unhide_as` does. The survey keeps one copy per object
and drops the other.

## 8. Confirmed or unconfirmed

**Decision.** The in-place reformat may run on the first boot of an
unconfirmed image, with the extra limits below.

- **A wedged store can never confirm.**
  - Its persistence probe fails, so boot health fails, and the image rolls
    back after 900 s. P02 did this on 2.7.4 and on 2.7.5.
  - "Confirmed only" therefore means a wedged device is never repaired
    remotely. P02 and 13FL have remote access only.
  - P02 will boot 2.7.6 unconfirmed, from 2.5.2, with 2.5.2 as its rollback
    target.
- **The design does not need the boot to survive.**
  - Every kept byte is on flash at every write (R8).
  - On an unconfirmed image the one exposure is a power cut between clearing
    B's magic and writing it back: about 60 ms per block (an erase and about
    15 programs).
  - The bootloader then returns to the old image. Its mount erases B, so it
    sees up to 14 pages missing. They read as unreadable spans, the class of
    damage the store already has; they never read as wrong bytes (R8, step
    3).
  - The bytes stay in the redo record. The next 2.7.6 or later boot takes
    them as sources (7.3), or records them exactly when the old image
    changed the file.
  - On P02 the exposure is about 10 blocks x 60 ms, about 0.6 s, inside a
    boot that already runs 2.7.5's unconfirmed writes (normalization) with
    the same class of rollback exposure.
- **The alternatives are worse.**
  - A whole-partition format on an unconfirmed image (section 4) would give
    the old image an empty store after a cut.
  - Confirming the image before the reformat would weaken boot health for
    the very devices whose state is least known. It would also need a
    restart path the repair does not have (R6). And the local-failure gate
    cannot pass on a wedged store anyway.

**Limits on an unconfirmed image:**

- **(i) Eligible blocks.** No kept page carries an index lookup entry (no
  header or index page of any object). A record therefore never has to
  restore a name or a size after a rollback.
- **(ii)** At most four custody objects among the kept data pages (6.2).
- **(iii) Order.** Blocks with fewer kept custody data pages first, then by
  gain.
- **(iv)** The cap of 128 blocks per boot.
- **(v)** The 5-minute deadline of an unconfirmed image (2.7.5 section 4).

On P02 about 118 index pages are spread over 2,048 blocks, so limit (i)
excludes about 6% of blocks.

**Rollback images that run the repair.** A device confirmed on 2.7.5 that is
offered 2.7.6 rolls back to 2.7.5 after a cut. 2.7.5 does not know the redo
record. Its survey sees B's missing pages, records `SALVAGE` gaps and fills
those spans. A later 2.7.6 finds the index naming the fill pages, so (c)
fails, and the record's pages settle as recorded (already covered by 2.7.5's
gaps). That case is reported, not restored: a residual (section 15).

## 9. The custody hold

Unchanged from 2.7.5:

- **What is held:** every live custody file, by class priority, as far as
  the budget goes.
- **The budget:** free PSRAM less 2.5 MiB and 640 KiB, capped at 7 MiB
  (4.36 MB on P02).
- **Verification:** span by span on the way in, with a CRC per file.
- **What it is for:** the net under the check and the manifest.

The reclaim does not depend on it:

- No held byte is rewritten from RAM.
- A store whose custody exceeds the budget (13FL) can still be reclaimed.
- The brief's rule "never reformat over the budget" belongs to the
  whole-partition format, which is not built.

The hold adds one check after every round: a fresh survey must give every
held file its held CRC (5.4).

## 10. What older images see

**After a finished reclaim (with or without the rest of the repair):**

- The same files, names, sizes and bytes, at the same page indices.
- More erased pages, some of them holes inside blocks.
- After the check, the leaked pages are deleted too, and compaction turns
  them into free blocks.
- NVS unchanged except the new `storage_repair/reclaim` key.
- The redo area erased.

Image by image:

- **2.5.2, 2.4.12 and the factory app:** mount it with their own SPIFFS. The
  magic is valid and the free-block count is exact (R11). Their readers see
  the same rows, and their GC now has room.
- **2.7.4:** the same, and its inventory reports the store readable.
- **2.7.5:** the same; its repair finds the store consistent (`CLEAN`, or
  upkeep compaction).

None of them reads the redo area or the new key.

**After a cut in a block's window, on an unconfirmed image:**

- The old image mounts and erases B (SPIFFS's remedy for one block without
  magic).
- Reads of spans whose pages were in B fail, as reads of damaged spans
  already do. B is now a free block the old image may write into.
- It cannot read a partly erased page as valid (5.3, step 3).
- 2.7.6, installed again, settles the record by 7.3.

## 11. Reporting to ADD

**Logs** (within the 24 lines a boot keeps):

| Code | Level | Says |
|---|---|---|
| `STORAGE_REPAIR_RECLAIM` | WARN | "n blocks reformatted in place for <trigger>: m deleted pages erased, k pages kept; e erased pages now" |
| `STORAGE_REPAIR_RECLAIM_REDONE` | WARN | "block b completed from the redo area after a reset" |
| `STORAGE_REPAIR_RECLAIM_SOURCES` | ERROR | "block b changed by another image: x pages taken from the redo area, y bytes recorded, z superseded" |
| `STORAGE_REPAIR_RECLAIM_SHORT` | WARN | stopped before the target (time, cap, no candidate), with the numbers |
| `STORAGE_REPAIR_RECLAIM_UNAVAILABLE` | ERROR | "the redo area is <state>; no reformat; the store stays wedged" |
| `STORAGE_REPAIR_RECLAIM_FAILED` | ERROR | a slot or block that would not verify |

**Heartbeat.** `diagnostics.storage.repair` gains `level: 3` when a reclaim
ran, and a `reclaim` object, sent every boot:

- `area`: the redo area state;
- `blocks`, `pages_freed`, `pages_kept`;
- `redone`;
- `sources` (spans taken from the redo area), `conflicts`;
- `records_open`;
- `erased_after`;
- `ms`.

`result` keeps its 2.7.5 meaning, which describes the store at the end:
`REPAIRED`, `REPAIRED_WITH_GAPS`, `DEFERRED`. `REFORMATTED` stays reserved
and is never sent. A file that took bytes from the redo area gets the action
`REDO_SOURCE`.

**Evidence.** Bytes a record could not put back go out as gap records with
cause `REFORMAT_INTERRUPTED`, the value 2.7.5 reserved. They use the same
frozen format, identity, bound and acknowledgement as every other gap
(2.7.5 section 8), and the same "no record room, no settlement" rule.

**ADD changes:**

- `StorageRepairReclaim` (lenient, every field optional, `area` matching
  `UPPER_CODE`);
- the `reclaim` field on `StorageRepairDiagnostics`;
- a unit test in `tests/unit/test_storage_repair_diagnostics.py`.

Queue evidence needs no change: the cause is inside the raw bytes.

## 12. Boot-time cost

Per reclaimed block (typical, worst case):

| Step | Typical | Worst |
|---|---|---|
| slot erase and 4 KiB program, read back | ~50 ms | ~420 ms |
| magic clear, B erase, ~15 programs, read back | ~60 ms | ~430 ms |
| `done` word | <1 ms | ~3 ms |
| **block** | **~115 ms** | **~0.9 s** |

| Run | Estimate |
|---|---|
| Redo area discovery, every boot | ~50 ms |
| P02 (target about 45 pages, about 10 blocks) | 2.7.5's 21.3 s, the reclaim (~1.2 s), normalization of 1 page, the check on 8 MB with the mirror (P06 took 3.9 s; P02 also deletes about 5,400 leaked pages, about 3 to 8 s), the restore comparison of 3.58 MB held (~5 s), compaction (up to 48 blocks, ~5 s), the finish: **about 40 to 55 s** |
| Cap, confirmed image (256 blocks) | ~30 s typical, time-gated before the soft deadline |
| Cap, unconfirmed image (128 blocks) | ~15 s typical, inside the 5-minute deadline |

The 900 s confirmation budget starts after `storage_init` returns (2.7.5
section 3), so the reclaim does not use it. HIL measures every row (the
heartbeat's `reclaim.ms` and `duration_ms`).

Memory and NVS:

- PSRAM: one more 4 KiB record buffer.
- No new internal allocation beyond the redo partition's list item, about
  100 bytes, freed on return.
- Stack: no frame over 512 bytes; the `-fstack-usage` gate from 2.7.5 D10
  applies.
- NVS: the journal blob needs 4 entries, added to
  `srp_nvs_entries_needed`.

## 13. Harness

Everything runs in `spiffs_repair_host.c` against ESP-IDF 5.5.3's SPIFFS, as
in 2.7.5.

**The redo model.** A second emulated NOR region of 96 sectors. It shares
`flash_ops`, so every cut sweep counts redo-area writes and erases, both tear
modes and torn erases.

**Wedged-store builders, from raw structure at 1 and 8 MiB:**

- **`p02`.** P02's state:
  - prefilled to 64% allocated, 0 erased;
  - about 5.3 deleted pages per block, spread;
  - `pending.jsonl` of 2.5 MB whose 153 spans were rewritten by a previous
    normalization while one index page was not;
  - 51 orphan index pages with about 1.4 MB of their leaked data pages;
  - a torn probe file, `acked_uids.txt`, both outboxes and cursors.

  The engine first runs with the reclaim off. It must reproduce P02's report:
  `DEFERRED`, `full`, 1 page needed, 0 erased, `LEVEL2_SKIPPED`. Then 2.7.6
  runs.
- **`deep`.** A damaged queue in every block needing hundreds of pages
  (seeds 12/13/16's shape).
- **`gc-unsafe`.** Consistent files, 0 erased pages: T2.
- **`unheld-crammed`.** 2.7.5's review case: a hidden original with no
  erased page; T3.
- **`head-deleted`.** Deleted pages at entry 0 of every block: the entry-0
  rule.
- **`over-budget`.** Custody larger than the hold budget; the run must still
  reclaim.

**Oracles,** on every reclaim run, on top of all of 2.7.5's:

- every kept page byte-identical, with its lookup entry, at its page index;
- every dropped page was `0x0000` and not needed, and is now erased under a
  free entry;
- the entry-0 rule holds;
- magic and erase count are valid in every block;
- SPIFFS's counters after the round equal a fresh mount's;
- the run does not end `DEFERRED` for room (`p02`, `gc-unsafe`,
  `unheld-crammed`, `deep` within its cap);
- the store ends with at least three free blocks;
- the redo area ends erased and the journal `IDLE`;
- held CRCs are unchanged after each round;
- the store mounts with ESP-IDF's plain SPIFFS;
- SPIFFS's own GC on reclaimed stores moves nothing a live file reads and
  stops nowhere half way.

**Cut sweeps:**

- **Confirmed.** A cut at every write and erase of the reclaim. Each run
  takes the same snapshot, tears both ways, then boots the repair until it
  finishes. Custody must be byte-exact with **zero** gaps: the window is
  closed.
  - In CI: every third operation at 1 MiB for `p02` and `gc-unsafe`, and
    every seventh at 8 MiB.
  - By hand: every operation (`REVIEW_FULL=1`).
- **Rollback interleave.**
  1. A cut at every operation of the window, in a run with the pending
     flag.
  2. The old image's boot: a plain mount (SPIFFS's remedy), then a
     2.5.2-shaped stretch of the firmware's own `legacy_queue.c` and
     `reliability.c` workload on the shim (appends, the probe file, a
     retired and recreated queue).
  3. 2.7.6 again.

  Every committed row must be in its file or under a gap record created
  after it was committed (`REFORMAT_INTERRUPTED` or `SALVAGE`). Rows the old
  image wrote survive. Record pages are used as sources wherever (a) to (d)
  hold.

  A second variant runs 2.7.5's engine (this engine with the reclaim and the
  redo port off) as the old image: reported, never silent.
- **Redo area states.**
  - `FOREIGN` (one foreign byte in one sector of an unclaimed area): no
    write to the area and no reformat, reported.
  - A first record torn with its magic torn, in a claimed area: a free slot,
    not `FOREIGN`, and the next reclaim proceeds.
  - Torn record headers; a ring wrap; an open record of an older id blocking
    a new reclaim (G5).
  - `REPORT_ONLY` completing a redo; the guard after two crashed redos.
- **Organic workload.** The 8 MB prefilled runs of CI (seeds 3 and 5 of
  `--prefill=70`) and seeds 12, 13 and 16 at 20,000 ticks (by hand) must end
  **not wedged**. Reclaims happen inside the cut loop. A wedge is now a
  failure, not an allowance. The workload's existing per-row oracle stands.
- **Also:**
  - the mirror (identical flash and report with and without it);
  - the HAL and CPU caps (the reclaim's operations counted);
  - the time gate (a soft deadline mid-round stops between blocks; the next
    boot continues).

**Plan host tests (plain cc):**

- record encode, validation and CRCs;
- the target image from masks, and the entry-0 rule;
- keep and drop on synthetic lookup pages;
- block ranking and eligibility (confirmed, unconfirmed);
- the redo decision table of 7.1 and 7.3;
- the journal CRC and NVS entries.

**Contracts** (`test_storage_repair_boot_contract.py`):

- no `SPIFFS_format`, `esp_spiffs_format`, or `format_if_mount_failed =
  true`;
- the redo area is written only through the registered handle, whose bounds
  are exactly 0xFA0000+0x60000;
- discovery happens before any write to the area;
- the redo is applied before the VFS mount and in `REPORT_ONLY`;
- the handle is deregistered on return;
- no new task;
- the heartbeat field names, which the ADD schema test accepts.

**Mutation check (dev tool, not CI).** The harness must fail with each of
these: no record, magic written first or not cleared first, entry 0 freed, a
needed deleted page dropped, `done` before the read-back, a redo without the
image and pending checks, source rule (c) or (d) removed.

**CI cost.** About 2 to 3 more minutes in parallel threads of the
`firmware-production` job, which today runs about 3 minutes on eight cores.
The new runs are separate `CI_RUNS` entries.

## 14. The owner's preconditions sketch, against this design

| Sketch (2.7.5 section 12) | Here |
|---|---|
| Confirmed image only | Replaced: the first unconfirmed boot is allowed, with section 8's limits. A wedged store can never confirm. |
| Every queue file settled or empty with a verified cursor | Not needed: no queue byte moves, every cursor stays valid as it is. |
| Operational and dedupe files held | Not needed by the reclaim (nothing is rewritten from RAM). The 2.7.5 hold still covers the check, and its CRCs are checked after every round. |
| `.pos` written as 0 | Not needed: offsets do not change, `.pos` stays valid. Writing 0 would make every rollback image replay its outbox for nothing. |
| Its own power-cut harness coverage | Section 13: every write of the round, both tear modes, the rollback interleave, wedged stores like P02, at 8 MiB. |

## 15. Residual risks and open decisions

**Decisions this design takes, for the owner to confirm in review:**

1. The approved reformat is done in place, block by block. The
   whole-partition format is not built (section 4).
2. The redo area is the 384 KiB past the last partition (R10, 6.1). Nothing
   else on the partition table can hold a block's pages through a power cut
   without risking NVS, the rollback image or the factory app.
3. The reclaim may run on an unconfirmed first boot (section 8).

**Open:**

- A device whose redo area is not `READY` stays wedged on 2.7.6. Two fixes
  are possible, either needing its own design and harness:
  - an NVS-staged variant for blocks with few kept pages (NVS has about 265
    available entries on P02; a 4-page block needs about 36);
  - the whole-partition format of section 4, on a confirmed image.

  The heartbeat tells us before P02 needs it.

**Residual risks:**

- **Unconfirmed cut in a window.** A cut inside a block's window on an
  unconfirmed image returns to the old image with up to 14 pages unreadable
  to it until 2.7.6 runs again. If 2.7.5 is that image, it fills those spans
  and reports them as `SALVAGE`, and the redo record then cannot restore
  them: reported, not restored. Expected exposure: about 0.6 s on P02.
- **Holes.** A reclaim leaves holes inside blocks. SPIFFS's GC scores a
  block only up to its first free entry, so until compaction or the GC
  consumes the holes, a GC may pick a block with more pages than it counted.
  Mitigations: tail gain first, compaction prefers blocks with holes, and the
  GC oracle on reclaimed stores.
- **Leaked pages.** Pages of removed files that are not flagged deleted are
  not dropped. The check deletes them once the store is consistent (P02's
  1.4 MB). A store whose only garbage is such pages, with no deleted pages,
  gains nothing from a reclaim. That is not the field shape, where deleted
  pages are 35% of P02.
- **Out of reach of any reformat:**
  - a store full of live custody;
  - an unmountable store (two blocks without magic, outside the repair as in
    2.7.5).
- **Worn sectors.** A worn sector that will not verify keeps its record open
  as sources. SPIFFS has no bad-block handling, and later writes to that
  sector can still fail.
- **Copies in the redo area.** Custody bytes sit in the redo area until a
  round ends. They have the same exposure as the plaintext SPIFFS store
  (there is no flash encryption), and are erased afterwards.
- **The partition-table assumption.** It cannot change over the air.
  Discovery re-proves it every boot.

## 16. File plan

**Firmware** (`firmware/zone_lite/main`):

| File | Change |
|---|---|
| `storage_repair_redo.h`, `storage_repair_redo.c` (new, plain C, no ESP-IDF) | record layout, seal and validation, target image from masks, the entry-0 rule, block ranking and eligibility, the redo decision of 7.1 and 7.3, ring slot choice |
| `storage_repair_plan.h`, `.c` | `srp_reclaim_t` and its seal and validation; `srp_nvs_entries_needed` with the reclaim blob; the name of the area state; no layout change to existing structures |
| `storage_repair_core.h` | `srr_redo_port_t` in `srr_ports_t`; `redo_state` in `srr_config_t`; `reclaim_*` report fields; `SRR_FILE_REDO_SOURCE` |
| `storage_repair_core.c` | `needed_pages()` factored out of `lookup_hygiene`; the reclaim (target, selection, `reclaim_block`, verification, SPIFFS cache and counters); redo recovery first in `srr_run` (all modes); records as sources in `build_candidates`/`resolve_span`, their settlement and retirement; triggers T1 to T4; held-CRC check after a round; logs and report |
| `storage_repair.c` | redo area discovery and registration, the port through the bounce buffer with yields, deregistration, the heartbeat `reclaim` object and `REDO_SOURCE` action |
| `CMakeLists.txt` | `storage_repair_redo.c` in the ZKT repair roles |

**Release commit** (later stage, as `0954fd3` did for 2.7.5):

- project version 2.7.6; `UG_DIRECT_VERSION` "2.7.6";
- 2.7.5's application SHA-256 (`430e2daf…`) in `upgrade_guard.c`,
  `apps/add_backend/zk_add/storage_contract.py`,
  `deploy/add/firmware-storage-contract.ps1` and the signing and publication
  scripts;
- the CI direct-legacy marker list and the HIL candidate workflow;
- `docs/zone-lite-2.7.6-hil.md`.

**ADD:**

- `apps/add_backend/zk_add/schemas.py`: `StorageRepairReclaim` and the
  `reclaim` field;
- `tests/unit/test_storage_repair_diagnostics.py`.

**Tests:**

| File | Change |
|---|---|
| `tests/firmware/spiffs_repair_host.c` | redo-area NOR model, wedged builders, reclaim scenarios and oracles, cut sweeps, rollback interleave, area states, organic runs that must not end wedged |
| `tests/firmware/tools/check_spiffs_repair.py` | compile `storage_repair_redo.c`; new `CI_RUNS` |
| `tests/firmware/storage_repair_plan_host.c` | record, journal, decision and selection tests |
| `tests/firmware/test_storage_repair_boot_contract.py` | section 13's contracts |

No new test files are planned, so the CI duration weights stay as they are.

**Docs:**

- this document;
- `docs/zone-lite-2.7.5-storage-repair.md`: decision 1 of section 12 points
  here;
- `docs/zone-lite-2.7.6-hil.md`, in the release stage: P06 first (`area`
  `READY`, `CLEAN`), then P02 (expected `RECLAIM`, then `REPAIRED`, then
  confirmed), then 13FL.
