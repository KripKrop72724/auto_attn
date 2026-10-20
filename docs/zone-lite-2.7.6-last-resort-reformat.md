# Zone Lite 2.7.6: last-resort reformat for wedged stores

Status: second design round, after two reviews of the first (commit
7556422): a custody review (C1 to C6, H1 to H4, M1, M2) and a device review
(D1 to D10). Section 16 says what became of every point. Branch
`release/zkt-2.7.6` from `origin/main` 5de7724 (2.7.5 released, #398 and #400
merged). The version is not bumped yet.

Owner decision, 16 October 2026, reconfirmed 20 October ("yes start 2.7.6"):
a last-resort reformat is approved when the store cannot otherwise be made
consistent, custody first: every readable unsettled byte survives or reaches
ADD as exact gap evidence, and nothing readable may be lost.

Scope: the last resort for wedged stores (no erased page, GC-unsafe, room
cannot be made), and nothing else unless the last resort needs it. Snapshot
and transport changes belong to a later release.

**Summary.** 2.7.6 reformats in place, one 4 KiB block at a time, and only
where 2.7.5 would hand the store over without room. A chosen block is erased
and every page anything may still read is programmed back at its own place,
byte for byte; only deleted pages nothing reads become erased. Before a block
is erased its kept pages are in a verified redo record in the 384 KiB of flash
past the last partition, and the block's magic is cleared before the erase
and written last, so a reset at any write costs nothing on the same image: the
next boot completes the block from its record on raw flash, before anything
mounts the store.

What changed from the first round:

- **Unconfirmed boots** open a window only on blocks that hold no page of
  any file (no header anywhere for its object, no index naming it). A reset
  there costs the rollback image nothing it can read (C1). Blocks holding
  custody wait for a confirmed boot or for copy-then-delete compaction once
  this bootstrap gives the store room.
- **Never in a window:** index and header pages, the last data page of any
  file, ADD outboxes and their `.pos`, the command inbox, the processed,
  cancelled and acked ID files, journal and queue segments, and files of
  unknown name.
- **The redo** runs on raw flash before the private mount (D1). It runs only
  when the block, read raw, is without magic, equals the old image or equals
  the target (C2). Otherwise the record is a byte source, and once a source,
  always one. It is bounded by an attempt tally on flash (D5).
- **The round** runs at the end of a run, never between a check pass and its
  restore (H4). The store is remounted afterwards, so SPIFFS's counters come
  from a fresh scan (H3, D1). The 2.7.5 run then starts again. Verification
  reads raw flash and refreshes the metadata mirror (C6).
- **Supersession** never follows a cursor generation, which moves on every
  settled row (C3); that 2.7.5 rule is fixed too. A record's pages settle
  only when their bytes read back where the file's index points, or by the
  cursor offset taken when the record was written, or they go to ADD as a
  gap with the record kept for good (C4, C5).
- **The keep set** comes from flash structure alone (H1), and blocks whose
  magic has two set bits or fewer are never chosen (D2: block 983's magic is
  0x0000).

## 1. Evidence

### 1.1 Peshawar-02 on 2.7.5 (20 October 2026)

The first boot ran from 2.5.2, unconfirmed, and ended `DEFERRED`:

- **Error:** `error_stage` `full`, `error_code` -10001, 21.3 s, check skipped.
- **Store:** 4,976,828 -> 4,962,772 of 7,703,441 bytes used.
- **Holds:** 6 files, 3 held, 3,577,196 bytes of a 4,357,816-byte budget.
- **Work:** 153 spans rewritten; 51 orphan index pages, 4 strays and 1
  duplicate removed; 1 cursor reset.
- **Room:** 1 page needed, 0 erased, 0 blocks compacted, 0 reserve pages.
- **Logs:** `STORAGE_REPAIR_LEVEL2_SKIPPED` "none would let a block be erased
  (3 pages needed, 0 erased)", `STORAGE_REPAIR_FULL` "(1 pages needed, 0
  erased)", `STORAGE_REPAIR_DEFERRED`.

`/pending.jsonl` (2,526,978 bytes) has 0 gaps and is fully readable by the
repair. The persistence probe still fails (ENOENT, no free page), boot health
fails and the image rolls back after 900 s.

| | pages | bytes (x 251) | share |
|---|---|---|---|
| all (2,048 blocks x 15 entries) | 30,720 | 7.71 MB | 100% |
| allocated after the run | 19,772 | 4.96 MB | 64.4% |
| of which held custody data (+ about 118 index pages) | about 14,370 | 3.58 MB | 46.8% |
| of which other: leaked pages of the removed file, catalog, probe | about 5,400 | 1.36 MB | 17.6% |
| deleted | 10,948 | 2.75 MB | 35.6% |
| erased | 0 | 0 | 0% |

The 51 orphan index pages 2.7.5 removed leave about 1.4 MB of data pages that
no header and no index names (leaked). Blocks written while that file was
written hold its data pages and its deleted header copies and no custody page:
those are the blocks an unconfirmed boot may reclaim (section 8). How many
P02 has is not known; the first unconfirmed 2.7.6 boot reports it before it
writes anything (the exposure histogram, section 11).

### 1.2 Others

- **13FL** (2.4.12): 61% used, a 3.8 MB `blocked_identity.jsonl` unreadable
  from about 50 KB, the same probe ENOENT. Probably wedged the same way, with
  custody over the hold budget (the reclaim does not need the hold).
- **The 2.7.5 harness:** seeds 12, 13 and 16 at 8 MiB and 6 of the fourth
  pass's fresh seeds end wedged at 70 to 77% used, with no erased page and a
  damaged queue in every block, every row still on flash.

### 1.3 SPIFFS facts the design rests on (ESP-IDF 5.5.3, read from source)

1. **Erase and magic.** `spiffs_erase_block` erases the block, writes the
   erase count (offset 254 of the lookup page), then the magic (offset 252,
   `0x20140529 ^ 256 ^ (block_count - bix)`, 16 bits).
2. **Mount tolerates one block without magic.** `spiffs_obj_lu_scan`
   accepts one block whose magic is wrong and erases it at the end of the
   mount; two fail it with `SPIFFS_ERR_NOT_A_FS`. Any mount by any image
   therefore erases a block a reset left without magic.
3. **Low magics.** At 8 MiB block 983's magic is 0x0000, 11 blocks have one
   set bit and 55 have two (D2). Clearing such a magic changes little or
   nothing, and a torn erase can make it read valid again.
4. **Free blocks are counted by entry 0.** The mount counts a block as free
   when its entry 0 is free; the allocator decrements `free_blocks` when it
   takes an entry 0.
5. **GC scoring assumes a free tail.** `spiffs_gc_find_candidate` stops
   counting at a block's first free entry. GC capacity uses the global
   counters, which stay exact with holes.
6. **What is read.** SPIFFS finds index pages by lookup entry (`obj|0x8000`)
   and data through index entries; `SPIFFS_VALIDATE_DATA` does not check the
   object id. The 2.7.5 survey takes index pages only with the deleted flag
   clear, reads a deleted page only when an index names it, and takes copies
   only when their lookup entry names the file. A page whose lookup entry is
   `0x0000`, whose own deleted flag is set and that no valid index page names
   is read by nothing, whatever kind of page it was.
7. **Flash past the table.** `esp_partition_register_external` registers a
   range on the main chip unless it overlaps a partition or passes the chip
   size; `esp_flash` refuses writes only inside the bootloader, the table or
   the running app.

The partition table ends at 0xFA0000 (storage 0x7A0000+0x800000). On the
16 MB chip the 384 KiB from 0xFA0000 (96 sectors) is in no partition, and no
table in git ever covered it.

## 2. Rules

The 2.7.5 rules R1 to R4, R6 and R7 stand; R5 ("no reformat") is replaced.

- **R5 In place only.** A block is reformatted only with every page the
  store needs programmed back at its own place. No `SPIFFS_format` or
  `esp_spiffs_format` anywhere (contract test).
- **R8 Always on flash.** A block's kept pages are in a verified redo record
  before its magic is cleared; the magic is cleared before the erase and
  programmed last, so an incomplete block never mounts as valid.
- **R9 Only unread pages go** (1.3 item 6). A page is dropped only when its
  lookup entry is `0x0000`, its header's deleted flag is set, and no valid
  index page of any object, visible, hidden, orphaned or implausible, names
  it. Entry 0 is kept while any later entry is kept (1.3 item 4).
- **R10 The redo area** is the 384 KiB past the table, used only when the
  chip and table prove it unused, it reads erased or as our own records (or
  our own torn writes once this device has claimed it), and only through a
  partition handle registered for exactly that range.
- **R11 Exposure.** A window (from clearing a block's magic to programming
  it) opens only on an eligible block (section 6); on an unconfirmed image
  only on a block holding no page of any file (section 8).
- **R12 Raw truth.** Every decision about a block's state is taken from raw
  flash, never from the metadata mirror or SPIFFS's cache, and SPIFFS's
  counters are never patched: the store is remounted.

## 3. Where it runs

```
storage_repair_boot (every boot)
  discover the redo area             (section 9)
  redo recovery on raw flash          (section 10)   <- before any mount
  private mount, srr_run:
    run 1 (2.7.5 unchanged)
    the store ends without room -> round (section 7) -> remount -> run 2
    at most two rounds, three runs
  unmount; the VFS mounts the store only when recovery allows it
```

A **round** runs at the end of a run, after its restores and its finish,
when that run is about to hand the store over without room:

| # | The run ended with | 2.7.5 outcome it replaces |
|---|---|---|
| T1 | normalization's third attempt still `SPIFFS_ERR_FULL` (`error_stage` `full`) | `DEFERRED`, `full` (P02) |
| T2 | fewer than two free blocks and fewer erased pages than SPIFFS's GC needs (`gc_unsafe`) | `DEFERRED`, `gc_unsafe` |
| T3 | a resume entry blocked for room (`error_stage` `full`) | `RESTORE_DEFERRED` / `MERGE_DEFERRED` |
| T4 | a failed check pass with `SPIFFS_ERR_FULL` | `FAILED` |
| T5 | the restore after a check could not bring a file back (no page for its header) | `FAILED`, `restore`, the manifest kept |

and all of these hold:

- **G1** the mode is `FULL` or `NORMALIZE_ONLY`, the run did not stop at the
  soft deadline, and fewer than two rounds ran this boot;
- **G2** the redo area is `READY`;
- **G3** time: before each block, the soft deadline leaves the block's worst
  case (2 s: two sector erases, about 20 programs, the read-back, two
  retries) plus what the next run's check and restore need (2.7.5's
  `time_for_check` estimate);
- **G4** NVS: `srp_nvs_entries_needed` plus the reclaim journal's creation
  and three rewrites (D10), measured again before every run;
- **G5** the block is not the block of any record that is not retired.

Then the next run starts from scratch (generation + 1): survey,
normalization, the hold, the check, the restore, compaction, the finish and
the probe, with room. Holds die with the run that took them; the first run
compares them after the round (section 7.4).

**Why not "deferred for the same shortage on consecutive boots".** Not
needed: the reclaim deletes nothing anything reads (R9) and every kept byte
is on flash at every write (R8). Not possible: an unconfirmed image never sees
its own second boot.

**Target.** Erased pages, counted by the survey, until they reach the larger
of what normalization allocates and the room the run said it needed, plus
`spare_for_compaction`, plus 30 pages for SPIFFS's own writes (P02: about 45
pages). Caps: 256 blocks per boot on a confirmed image, 128 on an
unconfirmed one.

## 4. Why a whole-partition format is not built

`SPIFFS_format` erases all 2,048 sectors before anything can be rewritten
(about 90 s typical, minutes at the datasheet maximum), and rewriting P02's
3.58 MB takes another 20 to 40 s. For one to two and a half minutes every
held byte is only in PSRAM, so a power cut loses it, and on an unconfirmed
boot it hands 2.5.2 an empty store (its seen-UID set gone, its cursors past
new files). The format also needs custody to fit the hold budget (13FL
probably does not). Nothing on this partition table can hold a power-cut-safe
copy of 3.6 MB:

| Place | Size | Usable |
|---|---|---|
| The inactive OTA slot | 2.5 MiB | No: the rollback image, and too small |
| Factory | 2.5 MiB | No: the bootloader's last resort |
| NVS | 24 KiB | No |
| The unpartitioned tail | 384 KiB | Too small for the held bytes |

One block at a time can be made safe: its kept pages (at most 14 x 256 bytes)
fit one 4 KiB record, and keeping them at their page indices means no index
changes, so blocks are independent. The whole-partition format would be that
operation over every block with nothing kept, which custody forbids. It
stays an owner option for a device whose redo area is unusable, with its own
design round and harness.

## 5. The in-place reformat of one block

### 5.1 The target

From the block read raw, for each of its 15 entries:

- `lu == 0xFFFF` and the page erased: stays erased;
- dropped (R9): lookup entry `0xFFFF`, page all `0xFF`;
- kept: everything else, byte for byte at its own page index;
- the lookup page's other bytes as they were, the erase count set to
  SPIFFS's current `max_erase_count`, the magic `SPIFFS_MAGIC(block)`.

A block with a mislabeled entry (a free lookup entry over a page that is not
erased, or a lookup entry naming another object than its page) is never a
candidate, nor is one where nothing would be dropped.

### 5.2 The record (one 4 KiB slot, little-endian)

| Offset | Field |
|---|---|
| 0 | `u32 magic` `0x31525253` ("SRR1") |
| 4 | `u16 version` 1, `u16 header_bytes` 256 |
| 8 | `u32 reclaim_id`, `u32 sequence` |
| 16 | `u32 image_tag` (first word of the writer's app digest) |
| 20 | `u8 flags` (bit 0: written by an unconfirmed image), `u8 objects` |
| 22 | `u16 block`, `u16 kept_mask`, `u16 drop_mask` |
| 28 | `u16 erase_count`, `u16 block_count` |
| 32 | `u32 old_crc`, `u32 new_crc` (CRC-32 of the block before, and of the target) |
| 40 | 5 x 40 bytes: per object owning a kept data page, `u16 obj`, `u8 class`, `u8 flags` (bit 0: cursor valid), `u32 cursor_offset` (its queue checkpoint's offset when the record was written), `char name[32]` |
| 240 | `u32 header_crc` (bytes 0 to 239) |
| 244 | `u32 done` (`0xFFFFFFFF` open; any bit cleared: done) |
| 248 | `u32 settle`: bit e cleared, entry e settled; bit 16+e cleared, entry e reported to ADD; bit 31 cleared, the record is a byte source |
| 252 | `u32 attempts`: one more bit cleared before each redo attempt (D5) |
| 256 | the target's lookup page |
| 512 | the kept pages in entry order, 256 bytes each (at most 14) |

Valid: magic, version, `header_crc`, and the CRC of the target expanded from
the record equals `new_crc`. Progress words are programmed bit by bit with no
erase; a torn program only clears fewer bits, which the next boot completes.

### 5.3 The writes

For block B (buffers in PSRAM; B through the SPIFFS HAL, so the metadata
mirror follows; every read-back raw):

1. **Record.** A free slot (erased, our own torn write, done or retired) is
   erased only if it does not already read all `0xFF` (D6), programmed and
   read back raw. A slot that does not verify is skipped; a second failure
   ends the round (`RECLAIM_FAILED`).
2. **Window open.** Clear B's magic (program `0x0000`; B's magic has three or
   more set bits, so this always invalidates it).
3. Erase B.
4. Program the kept pages, the lookup bytes, then the erase count.
5. Program the magic. **Window closed.**
6. Read B back raw and compare all 4 KiB with the target. On a mismatch,
   repeat 2 to 6 at most twice, re-checking G3 first. After that, clear B's
   magic, mark the record a byte source and end the round: the remount erases
   B, and the record's pages serve the next run (section 10.3).
7. Program `done` and read it back (programmed again if a bit is missing).
8. Refresh the mirror for B from raw flash.

Before the first window of a round, every block's magic is read raw: if any
block other than B lacks a valid magic, the round does not start (two blocks
without magic would make the store unmountable for every image; C6).

While a window is open the hard deadline does not restart the ESP: it defers
itself once by 2 s (H2, D7). Nothing else writes the store during a round.

## 6. Which blocks

### 6.1 Eligibility (every image)

A block is a candidate only when all of these hold:

- something is dropped (R9) and no entry is mislabeled (5.1);
- its magic is valid and has at least three set bits (D2);
- it is not the block of any record that is not retired (G5);
- no kept page is a header or index page, judged by the page's own header
  (object id with the index flag, deleted flag clear), not by its lookup
  entry (H1);
- every kept page is one of:
  - a deleted page (lookup `0x0000`) that no valid index page names: read by
    nothing;
  - a page of an object with no header anywhere on flash (visible, hidden or
    with torn flags) that no index of a headered object names: leaked, read
    by nothing, freed by the next check;
  - on a confirmed image only, a data page, not the last one, of a live file
    whose bytes the repair can always account for: `/pending.jsonl`,
    `/blocked_identity.jsonl`, `/corrupt_ords.jsonl`, `/add_corrupt.jsonl`,
    `/add_corrupt.bak` (append-only, drained by cursor), the identity
    catalog (re-fetchable) and probe files.

Everything else keeps its block out: ADD outboxes and their `.pos`, the
command inbox family, `processed_commands.txt`, `add_cancelled.txt`,
`acked_uids.txt`, journal and queue segments, `.bak`/`.tmp` copies,
the repair's own `.rpr` copies, hidden files a manifest may need, files of
unknown name, and the last data page of every file (an append would fail on
it, or program into another file's page reused at that index; C1, D3).

### 6.2 Order and selection

Candidates are ranked by tail gain (dropped entries after the last kept one,
which leave SPIFFS's free-tail invariant exact), then total gain, then block
index, and taken greedily until the target is met, a cap is hit or G3 fails.
The next run's compaction and the harness's GC oracle cover the holes a
reclaim leaves inside blocks (1.3 item 5).

## 7. The round

### 7.1 Steps

1. Phase `SRP_PHASE_REFORMAT` (the value 2.7.5 reserved and accepts) in the
   journal and the RTC breadcrumb.
2. A fresh survey; the index references of every valid index page; the
   eligibility of every block; the exposure histogram (section 11).
3. The reclaim journal (NVS `storage_repair/reclaim`): `CLAIMED | ACTIVE`,
   the next `reclaim_id`, before the first slot is written.
4. Raw magic check (5.3), then block by block (5.3).
5. The journal's `ACTIVE` cleared once every record of the round is done or
   a source.
6. The held-CRC oracle (7.4), then remount.

### 7.2 Why custody holds

- Every kept page keeps its bytes, header, lookup entry and page index: every
  index entry names the same bytes, every cursor's prefix CRC covers the
  same bytes, `.pos` files, checkpoints and file transactions are untouched.
- Dropped pages are read by nothing (R9).
- Between clearing B's magic and programming `done`, every kept byte is in a
  verified record; on the same image a reset is completed by the next boot's
  redo before anything mounts the store (section 10).

### 7.3 What the owner's sketch asked for

| Sketch (2.7.5 section 12) | Here |
|---|---|
| Confirmed image only | Unconfirmed boots touch only blocks holding no page of any file (section 8) |
| Queues settled or empty with a verified cursor | Not needed: no queue byte moves; a record keeps each queue's cursor offset for settlement |
| Operational and dedupe files held | Not needed by the round (nothing is rewritten from RAM); they never enter a window. The hold still guards the check |
| `.pos` written as 0 | Not needed: offsets do not change |
| Its own power-cut harness coverage | Section 13 |

### 7.4 The held-CRC oracle

After the round the store is surveyed again and every held file is compared
with its hold, span by span (bytes that were unreadable when held excepted).
A difference stops reclaiming for this boot, logs
`STORAGE_REPAIR_RECLAIM_MISMATCH`, remounts and puts the held bytes back in
place (2.7.5's `restore_in_place`). The harness proves it never fires.

## 8. Confirmed or unconfirmed

A wedged store can never confirm (its probe fails, boot health fails, the
image rolls back after 900 s), and P02 and 13FL are remote-only. So the
unconfirmed first boot may reclaim, but only blocks whose kept pages are
read by nothing: deleted pages no index names, and leaked pages of objects
with no header.

The first round claimed a cut in a window costs the old image "up to 14
pages unreadable, never wrong bytes". That was wrong (C1, D3): 2.5.2 is not a
passive reader. Its ADD outbox count stops at the first read error and
then removes the outbox; its command dedupe stops early and re-executes
commands; its seen-UID load stops early and requeues punches; an append into
a file whose last page was erased fails or, with that page index reused by
another file's same span, programs into the other file. With this rule none
of that can start: after a reset in a window the old image's mount erases a
block that held nothing any reader reads, so every file reads exactly as
before (the harness checks every file byte for byte through plain ESP-IDF
SPIFFS after the remedy, which covers 2.5.2's and 2.4.12's readers alike),
and the block is one more free block for its writes.

A record written by an unconfirmed image is never redone: the image that ran
after the reset (always the rollback image) has mounted the store. Its pages
are nothing to deliver, so it is retired.

**If P02 has no such block**, 2.7.6 leaves it as 2.7.5 did and says so
(`STORAGE_REPAIR_RECLAIM_SHORT` with the exposure histogram). Getting custody
blocks reclaimed there then needs the owner's decision (section 15):
- (a) a narrow boot-health exception so the round runs on a confirmed boot
  (a wedged store, custody intact, ADD connected, the OTA path proven), where
  a reset costs nothing; or
- (b) accept custody windows on an unconfirmed boot (the first round's
  design), with the exposure above.

## 9. The redo area

### 9.1 Discovery (`storage_repair.c`, every boot, before any mount)

1. `esp_flash_get_physical_size` and the default chip's `size` are both at
   least 16 MiB, and every partition in the table ends at or below 0xFA0000;
   otherwise `ABSENT`.
2. `esp_partition_register_external(NULL, 0xFA0000, 0x60000, "srp_redo",
   DATA, 0x99)` succeeds (this also proves no overlap); otherwise `OVERLAP`.
   The handle is deregistered before `storage_repair_boot` returns.
3. All 96 sectors are read (about 50 ms). Before this device has written the
   area (no `claimed` flag in the journal), a sector must read all `0xFF` or
   as a valid record, or it is foreign. Once claimed, a sector that is
   neither is our own torn write: a free slot.
4. The state: `READY`, `FOREIGN` (with the foreign sectors' count, the first
   one's index and the CRC of its first 4 KiB, D8), `ABSENT`, `OVERLAP`,
   `UNREADABLE`, or `NO_MEMORY`.

Valid records are read in every state where the area can be read (M1).
`FOREIGN` blocks new records only; programming the progress words of our own
records is allowed. A foreign tail is never claimed without the owner (D8).

### 9.2 The ring and wear

A new record takes the free slot after the newest sequence. Done and retired
slots are reused; a slot is erased only before reuse and only when it does
not read all `0xFF`. There is no cleanup pass. Wear: at most one sector
erase per record, spread over 96 sectors (D6). Copies of custody bytes stay
in done slots until reuse, with the same exposure as the plaintext store.

### 9.3 The reclaim journal (NVS `storage_repair/reclaim`, new key)

`srp_reclaim_t`: `version`, `flags` (`CLAIMED`: this device has written the
area, never cleared; `ACTIVE`: a round may have open records; `SOURCES`:
records found open must become byte sources), `reclaim_id`, `rounds`,
`blocks`, `pages_freed`, `crc`. About 32 bytes, 4 NVS entries. Written once
before a round's first record, once after it, and by recovery (section 10).
Older images ignore the key; the 2.7.5 layouts (`srp_state_t`,
`srp_manifest_t`, the gap and reset lists, `SRP_STATE_VERSION`) are
unchanged byte for byte.

## 10. Recovery: power cuts at every write

### 10.1 Before any mount, every boot, every mode

For each valid record that is neither done nor a source, in sequence order:

| Condition | Action |
|---|---|
| written by an unconfirmed image, by another image, or the journal says `SOURCES` | mark it a source |
| four attempts already used (D5) | mark it a source |
| B raw equals the target (`new_crc`) | program the erase count and magic again with the same values (D4), verify raw, program `done` |
| B raw has no valid magic, or equals the old image (`old_crc`) | one more attempt bit; then 5.3 steps 2 to 7 from the record |
| anything else (B has a magic and other content: something mounted and wrote it) | mark it a source (C2) |

B without a valid magic proves no mount happened since the reset (any mount
erases it), so redoing it overwrites only the round's own interrupted
writes. A source is never redone again (its bit 31 is cleared before
anything else happens to it).

If the area cannot be inspected this boot (not registered, unreadable, no
memory) while the journal says `ACTIVE`, recovery writes `SOURCES` to the
journal before returning; if that write fails, the VFS does not mount the
store this boot (`storage_repair_storage_allowed()` false; LOCAL_FAILURE,
as for a failed mount) (C2). The private repair mount is skipped too.

| Cut at | Next boot, same confirmed image | After a rollback |
|---|---|---|
| journal, slot erase or record program | no valid open record: nothing | the same |
| clearing B's magic, erasing B, programming B, B's magic | B without magic or equal to the old image: **redo** | only unconfirmed windows: B held nothing any image reads |
| after the magic, before or during `done` | B equals the target: re-assert, `done` | the same |

### 10.2 Brownouts and the attempts guard

`ESP_RST_BROWNOUT` now counts with panics and watchdogs for the 2.7.5
attempts guard when the previous boot's repair had not returned (D5). A redo
that browns out the supply every boot is bounded by its own tally: after four
attempts the record becomes a source and the boot proceeds.

### 10.3 A record as a byte source

When B was written by something else (or would not verify, or ran out of
attempts), the mount erases it and the record's kept data pages are the bytes
SPIFFS can no longer read. A kept data page P of object o, span k, at page
index p is a source for span k of the live object X when:

- X has object id o and the name the record's object table gives o;
- X's index names p for span k;
- the page now at p is not o's own data page of span k.

It merges by the 2.7.5 rules (more programmed wins; a disagreement is a gap)
as the page the index names, and normalization gives the span a new page.
Where a span reads, instead, as exactly the fill a SALVAGE gap left (a boot
that could not read the area filled it), the record's bytes put it back in
place; the cursor reconciliation then replays from a changed prefix
(duplicates, never a loss) (C4).

### 10.4 Settlement (at the end of every run)

Each kept entry ends in exactly one terminal way (C5):

| Entry | Settled when |
|---|---|
| deleted, leaked, catalog or probe page | at once: nothing to deliver |
| a queue file's data page | **back on flash**: every byte P programmed is present at the page X's index names for span k (more bytes are allowed: an append in place); or **settled by offset**: the record's cursor offset is past the span's end |
| a queue file's page whose file is gone or changed and that the offset does not cover | **reported**: a `REFORMAT_INTERRUPTED` gap of exactly its range; the record is kept for good |

A span still unreadable for room waits for a later run. When every entry is
settled the record is retired (bit 31 stays, `settle` says retired); a
record with a reported entry is never reused (its bytes stay in the area).
Rows can be delivered twice; none is lost.

## 11. Reporting to ADD

**Logs** (within the 24 lines a boot keeps):

| Code | Level | Says |
|---|---|---|
| `STORAGE_REPAIR_RECLAIM` | WARN | n blocks reformatted in place for the trigger: m deleted pages erased, k kept; e erased pages now |
| `STORAGE_REPAIR_RECLAIM_REDONE` | WARN | block b completed from the redo area after a reset (or re-asserted) |
| `STORAGE_REPAIR_RECLAIM_SOURCES` | ERROR | records that became byte sources, and why |
| `STORAGE_REPAIR_RECLAIM_SETTLED` | WARN | source pages settled, reported or still waiting |
| `STORAGE_REPAIR_RECLAIM_SHORT` | WARN | stopped before the target (no candidate, time, cap, slots), with the histogram |
| `STORAGE_REPAIR_RECLAIM_UNAVAILABLE` | ERROR | the redo area is not `READY`; the store stays as 2.7.5 leaves it |
| `STORAGE_REPAIR_RECLAIM_FAILED` | ERROR | a slot or block that would not verify |
| `STORAGE_REPAIR_RECLAIM_MISMATCH` | ERROR | the held-CRC oracle fired |

**Heartbeat.** `diagnostics.storage.repair` gains `level: 3` when a round
ran, `runs`, and a `reclaim` object sent every boot: `area` (the state),
`area_foreign_sectors`, `area_first_foreign`, `area_foreign_crc` (when
foreign), `trigger`, `rounds`, `blocks`, `pages_freed`, `pages_kept`,
`erased_after`, `redone`, `sources`, `settled`, `reported`, `records_open`,
`failed`, `ms`, and the exposure histogram `candidates`, `unconfirmed_blocks`
(eligible on an unconfirmed image), `custody_blocks`, `index_blocks`,
`excluded_blocks`, `low_magic_blocks`. `result` keeps its 2.7.5 meaning;
`REFORMATTED` stays reserved. A file that took bytes from the redo area gets
the action `REDO_SOURCE`.

**Evidence.** Reported entries are gap records with cause
`REFORMAT_INTERRUPTED` (the value 2.7.5 reserved), in the frozen format.

**ADD:** `StorageRepairReclaim` (lenient, every field optional, codes as
`UPPER_CODE`, an unreadable object dropped) and the `reclaim` and `runs`
fields on `StorageRepairDiagnostics`.

## 12. Cost

| Step | Typical | Worst |
|---|---|---|
| slot erase (when not already erased), 4 KiB program, read-back | ~50 ms | ~420 ms |
| magic clear, B erase, ~16 programs, read-back | ~60 ms | ~430 ms |
| `done`, mirror refresh | ~2 ms | ~5 ms |
| **block** | **~115 ms** | **~0.9 s; 1.8 s with two retries** |

Discovery about 50 ms every boot; the raw magic check about 40 ms per round.
P02 (unconfirmed): 2.7.5's 21 s run, the round, then a second run with the
check over 8 MB, deleting the leaked pages, the restore comparison of 3.58 MB
held, compaction and the probe: **about 60 to 90 s** (D9), inside the 255 s
soft deadline of an unconfirmed image. The 900 s confirmation budget starts
after `storage_init`. HIL measures `reclaim.ms` and `duration_ms`.

PSRAM: three 4 KiB buffers for the round and recovery. Internal: the
external partition's list item (about 100 bytes, freed on return).

## 13. Harness

Everything runs in `spiffs_repair_host.c` against ESP-IDF 5.5.3's SPIFFS:

- **The redo area:** a second emulated NOR region of 96 sectors under the
  same power-cut model (every write and erase counted, both tear modes).
- **Builders** at 1 and 8 MiB:
  - `p02`: no erased page, every block holding a page of the damaged pending
    queue or of a leaked file, orphan index pages over about a fifth of the
    store, deleted header copies everywhere, a REFER-only damaged span.
    The released 2.7.5 engine (`tests/firmware/fixtures/storage_repair_2_7_5`,
    byte-identical to 0954fd3) must reproduce P02's report on it (`DEFERRED`,
    `full`, 1 page needed, 0 erased, `LEVEL2_SKIPPED`); then 2.7.6, both
    unconfirmed and confirmed, must end healthy;
  - `deep`, `gc-unsafe`, `unheld-crammed`, `head-deleted`, `over-budget`,
    `low-magic` (block 983 at 8 MiB: never chosen).
- **Oracles** on every reclaim, on top of 2.7.5's: kept pages byte-identical
  at their indices; dropped pages were unread and are now erased under free
  entries; entry 0; magic and erase count in every block; SPIFFS's counters
  after the remount equal a fresh mount's; no `DEFERRED` for room; at least
  three free blocks at the end; journal not active; held CRCs unchanged;
  SPIFFS's GC on reclaimed stores moves nothing a live file reads.
- **Cut sweeps:** a cut at every write and erase of the round and of the
  recovery, both tears, at 1 and 8 MiB. Confirmed: custody byte-exact, zero
  gaps. Unconfirmed: the rollback interleave (a plain mount, every file read
  byte for byte against the store before the round, a stretch of the
  firmware's own legacy queue workload, then 2.7.6 again), and the same with
  the released 2.7.5 engine as the old image.
- **Area and records:** foreign, a torn first record, a ring wrap, records
  turned sources by another writer, by a failed verify and by the attempts
  tally, `REPORT_ONLY` completing a redo, the area unreadable with an active
  journal.
- **Organic:** CI's prefilled 8 MiB runs must end not wedged; the 2.7.5 wedge
  allowance (growing the partition) is gone.

## 14. Files

| Area | Files |
|---|---|
| New | `storage_repair_redo.h`, `.c`: record codec, block eligibility and plan, slot classes, recovery, the per-block writer (no ESP-IDF, host-tested) |
| Engine | `storage_repair_core.h`, `.c`: ports, the round, sources, settlement, report; the C3 fix |
| Plan | `storage_repair_plan.h`, `.c`: `srp_reclaim_t`, its NVS entries (additive only: the vendored 2.7.5 engine links against it) |
| Mirror | `storage_repair_mirror.h`, `.c`: `srm_refresh` |
| Glue | `storage_repair.c`, `.h`: discovery, recovery before the mount, ports, deadline deferral, the mount gate, the heartbeat |
| App | `zone_lite.c`: the VFS mount waits for `storage_repair_storage_allowed()` |
| Build | `CMakeLists.txt` |
| ADD | `schemas.py`, `tests/unit/test_storage_repair_diagnostics.py` |
| Tests | `spiffs_repair_host.c`, `check_spiffs_repair.py`, `storage_repair_plan_host.c`, `test_storage_repair_host.py`, `test_storage_repair_boot_contract.py`, the 2.7.5 fixture |

The release commit (version, 2.7.5's digest in the upgrade guard, storage
contract and signing scripts, HIL runbook) is a later step, as `0954fd3` did
for 2.7.5.

## 15. Open decisions and residual risks

**For the owner:**

1. In place, block by block; no whole-partition format (section 4).
2. Writing the 384 KiB past the last partition (R10). A tail that reads
   foreign is reported, never claimed: claiming one needs the owner's word,
   after ADD has the report (D8).
3. Unconfirmed boots: only blocks holding nothing any image reads. If P02
   (or 13FL) has none, the choice in section 8 ((a) a confirm-first
   boot-health exception, (b) custody windows on unconfirmed boots) is the
   owner's. P02's first 2.7.6 boot may therefore be a discovery run that
   rolls back.
4. The 2.7.5 supersession rule changes (C3): a file whose queue cursor moved
   is no longer taken as retired. Its readable remnant comes back, and its
   rows may be delivered twice.

**Residual risks:**

- A device whose area is not `READY` stays wedged on 2.7.6 as on 2.7.5.
- Holes inside blocks skew SPIFFS's GC scoring until compaction absorbs
  them; GC capacity stays exact (1.3 item 5); the harness's GC oracle runs on
  every reclaimed store.
- Leaked pages are kept, not dropped: a store whose only garbage is leaked
  pages gains nothing until a check deletes them.
- Out of reach: a store full of live custody; an unmountable store.
- A worn sector that will not verify becomes a source and later writes to it
  can still fail (SPIFFS has no bad-block handling).
- A reported entry keeps its record (and slot) for good.
- A redo given up after four attempts (supply that cannot sustain an erase)
  turns into sources; the spans stay unreadable to the application until a
  run has room for them.
- None of this has run on a device.

## 16. Review points and what became of them

| Point | Verdict | What changed, or why not |
|---|---|---|
| C1 unconfirmed window | Accepted, option (b) | Section 8. Option (a) is listed for the owner. A per-block exposure histogram goes to ADD before anything is written. The rollback harness checks every file byte for byte through plain SPIFFS after the remedy (any reader of any image) and runs the firmware's legacy queue workload; running 2.5.2's and 2.4.12's `zone_lite.c` themselves is not done: they need FreeRTOS, ESP-IDF and HTTP, and with no page of any file in a window their reads are identical by construction, which is what the oracle checks |
| C2 redo without proof | Accepted | 10.1: raw condition; sources are sticky; journal `SOURCES` or no mount |
| C3 generation is not retirement | Accepted | 10.3 and 10.4 use offsets only; 2.7.5's `resume_file` no longer supersedes a hidden remnant, or a vanished file, on a moved generation (its tests now expect the remnant back, or a record) |
| C4 readable bytes reduced to offsets | Accepted | 10.3, 10.4: delivery in place, settlement by offset, otherwise a record with the bytes kept for good; the SALVAGE-fill restore |
| C5 deadlock, not total | Accepted | G5 allows other blocks; 10.4 is total; "back on flash" is a subset test |
| C6 mirror reads | Accepted | R12, 5.3 steps 6 and 8, the raw magic check |
| H1 keep set | Accepted, except one clause | R9 and 6.1 from flash structure; the index-flag test reads the page header. Not taken: "it is not a header or index page" for pages whose own deleted flag is set and whose lookup entry is `0x0000`: no SPIFFS path and no 2.7.x rule reads such a page (1.3 item 6), and keeping them would keep most of the gain (every append's header move leaves one). Index and header pages that are not deleted keep their block out entirely |
| H2 any reset, flicker | Accepted in part | Deadline gate and deferral (5.3, 10.1). Not taken: a 60 s uptime delay for unconfirmed windows, since with C1 (b) such a window exposes nothing readable |
| H3 counters | Accepted | Remount after recovery and after every round |
| H4 between check and restore | Accepted | The round runs only at the end of a run |
| M1 recovery and NVS | Accepted | 9.1: records read in every readable state |
| M2 the 2.7.5 stand-in | Accepted | The released engine, vendored byte-identical, runs as the old image |
| D1 redo after the mount | Accepted | 10.1 before any mount |
| D2 low magics | Accepted | 6.1, and the `low-magic` harness case |
| D3 wrong bytes, appends | Accepted | Section 8 corrected; last data pages never in a window |
| D4 torn magic or done | Accepted | 10.1 re-asserts both |
| D5 brownouts | Accepted | The attempts tally; BROWNOUT counted |
| D6 area wear and time | Accepted | 9.2 |
| D7 hard deadline in a window | Accepted | 5.3 |
| D8 P06 cannot vouch for P02's tail | Accepted | 9.1 reports foreign detail; decision 2; P02's first boot may be discovery |
| D9 estimates | Accepted | Section 12 |
| D10 NVS writes | Accepted | G4 |
