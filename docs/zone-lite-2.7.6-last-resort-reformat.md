# Zone Lite 2.7.6: last-resort reformat for wedged stores

Status: implemented and verified on the host (section 17), not yet on a
device; the version is not bumped yet. This is the second design round,
after two reviews of the first (commit 7556422): a custody review (C1 to C6,
H1 to H4, M1, M2) and a device review (D1 to D10). Section 16 says what
became of every point. The implementation (eaf7ec3) then had its own first
review round, a custody review (D1 to D3 and a low finding) and a device and
build review (six low findings); section 18 says what became of each.
Branch `release/zkt-2.7.6` from `origin/main` 5de7724 (2.7.5 released, #398
and #400 merged).

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
  only when their bytes read back where the file's index points, or (once
  the file is gone or changed) by the cursor offset taken, and verified,
  when the record was written, or they go to ADD as a gap with the record
  kept for good (C4, C5; round 1 review, D1).
- **The keep set** comes from flash structure alone (H1), and blocks whose
  magic has two set bits or fewer are never chosen (D2: block 983's magic is
  0x0000), nor blocks whose magic has every byte 0x00 or 0xFF (round 1 review
  of the implementation, D3: block 810's is 0x00FF).

What the implementation's round 1 review changed (section 18):

- **Cursor offsets.** A record keeps a queue's cursor offset only when it
  is verified against the file when the record is written, and no offset
  settles a page while the file can still get its bytes back (10.4).
- **An area that cannot be read** while it may hold queue bytes leaves the
  queues' unreadable spans for a boot that can (10.1); nothing is filled or
  recorded as lost.
- **A restore short of room** (T5) waits for the round and runs again with
  its hold alive; only what still cannot go back is recorded, once (7.5).
- **A torn erase** that reads a valid magic is redone on the same image
  (10.1); weak magics include byte-wise ones (6.1).
- **The report** ADD gets covers every run of the boot (11); a run that
  cannot load its byte sources fails before writing; a boot without its
  deadline writes nothing; the geometry is pinned; the stack is measured
  again (12).

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
   set bit and 55 have two (D2), and block 810's is 0x00FF (round 1 review,
   D3). Clearing such a magic changes little or nothing, and a torn erase can
   make it read valid again: the window clears it to 0x0000, and an erase cut
   short leaves each byte erased or not (0x00FF, 0xFF00).
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
| T5 | a restore after a check had no page (a rebuild's header, the rest of a partial rebuild, a changed span's new page): it waits for the round (7.5) | `FAILED`, `restore`, the held bytes recorded and lost |

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
compares them after the round (section 7.4), and a restore that waited for
the round's room runs again before they die (section 7.5).

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
| 40 | 5 x 40 bytes: per object owning a kept data page, `u16 obj`, `u8 class`, `u8 flags` (bit 0: cursor verified), `u32 cursor_offset` (its queue checkpoint's offset when the record was written, kept only when `srp_cursor_verdict` found it valid for the file then: round 1 review, D1), `char name[32]` |
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
2. **Window open.** Clear B's magic (program `0x0000`; B's magic is strong,
   6.1, so this always invalidates it, and an erase cut short cannot make it
   read valid again byte by byte).
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
- its magic is valid and strong: at least three set bits (D2) and not every
  byte 0x00 or 0xFF (round 1 review, D3; 68 blocks of 2,048 are weak, 983
  and 810 among them);
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
| Queues settled or empty with a verified cursor | Not needed: no queue byte moves; a record keeps each queue's cursor offset, verified against the file when it is written, for settlement |
| Operational and dedupe files held | Not needed by the round (nothing is rewritten from RAM); they never enter a window. The hold still guards the check |
| `.pos` written as 0 | Not needed: offsets do not change |
| Its own power-cut harness coverage | Section 13 |

### 7.4 The held-CRC oracle

After the round the store is surveyed again and every held file is compared
with its hold, span by span (bytes that were unreadable when held excepted;
a file whose restore waits for the round, 7.5, reads what the check left
and is not compared). A difference stops reclaiming for this boot, logs
`STORAGE_REPAIR_RECLAIM_MISMATCH`, remounts and puts the held bytes back in
place (2.7.5's `restore_in_place`). The harness proves it never fires.

### 7.5 A restore short of room (T5; round 1 review, D2)

A held file whose restore after a check pass finds no page (SPIFFS_ERR_FULL,
or SPIFFS_ERR_NOT_FOUND from its allocator when its counters still show free
blocks) while a round may follow in the same run waits: nothing is recorded
yet, `STORAGE_REPAIR_RESTORE_WAITING` says so, and the round's target counts
the pages the waiting bytes need. It may be the rebuild's header (the check
freed the file), the rest of a partial rebuild (the partial file takes the
name as before), or a changed span's new page.

After the round's remount, when the erased pages cover every waiting byte,
the restore runs again (compaction first, as after a check pass) while the
holds are still in memory, and whatever still cannot go back is recorded
then (`STORAGE_REPAIR_RESTORE_RETRIED`). Otherwise (the round could not make
that room, did not run, or could not remount) the waiting bytes are recorded
as they stood: a restore that would stop part way would only take the
round's room from SPIFFS's own writes. Either way before the holds die with
the run. A recorded rebuild's manifest entry is marked reported, so the
resume never records the same bytes a second time (2.7.5 recorded the rebuild
twice: CHECK_CHANGED, then RESTORE_INTERRUPTED).

A reset before the retry leaves the entry unreported, and the next boot's
resume records it, as for any reset during a restore. The round still never
comes between a check pass and its restore (H4): it follows the restore, and
only the retry of a restore that waited for its room comes after it.

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

The redo records hold Zone Lite's SPIFFS geometry (256-byte pages, 4 KiB
blocks, the length magic): pinned at build time (`_Static_assert`s in the
glue and the engine) and checked again before a round, recovery and loading
sources (round 1 review, low 5).

### 9.2 The ring and wear

A new record takes the free slot after the newest sequence. Done and retired
slots are reused; a slot is erased only before reuse and only when it does
not read all `0xFF`. There is no cleanup pass. Wear: at most one sector
erase per record, spread over 96 sectors (D6). Copies of custody bytes stay
in done slots until reuse, with the same exposure as the plaintext store.

### 9.3 The reclaim journal (NVS `storage_repair/reclaim`, new key)

`srp_reclaim_t`: `version`, `flags` (`CLAIMED`: this device has written the
area, never cleared; `ACTIVE`: a round may have open records; `SOURCES`:
records found open must become byte sources; `HELD`: the area holds byte
sources not yet retired, set by recovery from what it inspected and by a
round whose record became a source, round 1 review), `reclaim_id`, `rounds`,
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
| B has a valid magic and reads as the window's erase cut short: every byte the record holds (kept pages, their lookup entries, erased entries, the lookup page's other bytes) is there with bits only set, and some kept byte is not erased (round 1 review, D3) | redo, as above |
| anything else (B has a magic and other content: something mounted and wrote it) | mark it a source (C2) |

B without a valid magic proves no mount happened since the reset (any mount
erases it), so redoing it overwrites only the round's own interrupted
writes. A torn erase that reads a valid magic again (a bit-level tear; a
strong magic cannot byte by byte) is redone on the same image too: a mount's
erase leaves no kept byte, and a page programmed since into a kept or erased
entry clears bits the record has set. Before this rule such a block was
taken for another writer's and stayed mounted half erased. A source is never
redone again (its bit 31 is cleared before anything else happens to it).

If the area cannot be inspected this boot (not registered, unreadable, no
memory) while the journal says `ACTIVE`, recovery writes `SOURCES` to the
journal before returning; if that write fails, the VFS does not mount the
store this boot (`storage_repair_storage_allowed()` false; LOCAL_FAILURE,
as for a failed mount) (C2). The private repair mount is skipped too.

Such a boot cannot read the records' bytes either. When the journal says
they may hold queue bytes (`ACTIVE`, `SOURCES` or `HELD`) the run is told
(`srr_config_t.sources_unread`, `STORAGE_REPAIR_RECLAIM_SOURCES`): a reclaim
queue's unreadable spans wait, neither filled nor recorded, and the file
stays blocked as on a store without room, until a boot that reads the area
puts the bytes back (round 1 review, low: such a boot used to fill them and
tell ADD they were lost). A run that can read the area but cannot allocate
the buffer for its byte sources, or read one, fails before it writes
anything (`sources_memory`, `sources_read`; low 6).

Without the deadline timer (it could not be created) recovery writes
nothing: open records go to the journal as sources, as for an area that
cannot be read, and the boot's breadcrumb is marked returned. The `REFORMAT`
breadcrumb is set only while recovery acts on an open record, so a crash
later in the boot is not taken for the repair's (low 2).

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
A kept page that is deleted but named by the file's index (13FL's head) is a
source the same way. A kept *copy* of span k at another page (an interrupted
GC or page move left it, and another image's SPIFFS GC may have deleted it
since) is a source as 2.7.5 trusts such a copy on flash, but only when the
record's verified cursor offset for X had not passed the span: X's owner
cannot then have read past it, let alone retired X and created another file
under the same id and name.
Where a span reads, instead, as exactly the fill a SALVAGE gap left (a boot
that could not read the area filled it), the record's bytes put it back in
place; the cursor reconciliation then replays from a changed prefix
(duplicates, never a loss) (C4).

### 10.4 Settlement (at the end of every run)

Each kept entry ends in exactly one terminal way (C5):

| Entry | Settled when |
|---|---|
| deleted, leaked, catalog or probe page | at once: nothing to deliver |
| a queue file's data page | **back on flash**: every byte P programmed is present at the page X's index names for span k (more bytes are allowed: an append in place); or, once X is gone or holds other bytes there under its id and name, **settled by offset**: the record's cursor offset, verified when it was written, is past the span's end |
| a queue file's page whose file is gone or changed and that the offset does not cover | **reported**: a `REFORMAT_INTERRUPTED` gap of exactly its range; the record is kept for good |

While X (the same id and name) still holds the span unreadable, or filled
where the record's bytes belong, the entry waits: the next run with room
puts the bytes back. No offset settles it then (round 1 review, D1: a stale
cursor that was never verified once settled 3,514 readable bytes, the record
retired, and the next run filled the span). When every entry is settled the
record is retired (bit 31 stays, `settle` says retired); a record with a
reported entry is never reused (its bytes stay in the area). Rows can be
delivered twice; none is lost.

## 11. Reporting to ADD

**Logs** (within the 24 lines a boot keeps):

| Code | Level | Says |
|---|---|---|
| `STORAGE_REPAIR_RECLAIM` | WARN | n blocks reformatted in place for the trigger: m deleted pages erased, k kept; e erased pages now |
| `STORAGE_REPAIR_RECLAIM_REDONE` | WARN | block b completed from the redo area after a reset (or re-asserted) |
| `STORAGE_REPAIR_RECLAIM_SOURCES` | ERROR | records that became byte sources, and why; byte sources a run could not load (it fails before writing); WARN: the area cannot be read and may hold queue bytes, which wait |
| `STORAGE_REPAIR_RECLAIM_SETTLED` | WARN | source pages settled, reported or still waiting |
| `STORAGE_REPAIR_RECLAIM_SHORT` | WARN | stopped before the target (no candidate, time, cap, slots), with the histogram |
| `STORAGE_REPAIR_RECLAIM_UNAVAILABLE` | ERROR | the redo area is not `READY`; the store stays as 2.7.5 leaves it |
| `STORAGE_REPAIR_RECLAIM_FAILED` | ERROR | a slot or block that would not verify |
| `STORAGE_REPAIR_RECLAIM_MISMATCH` | ERROR | the held-CRC oracle fired |
| `STORAGE_REPAIR_RESTORE_WAITING` | WARN | a held file's restore had no page and waits for the round (7.5) |
| `STORAGE_REPAIR_RESTORE_RETRIED` | WARN/ERROR | restores that waited: how many went back after the round, or why they were recorded as they stood |

**Heartbeat.** `diagnostics.storage.repair` is the boot's report over every
run (round 1 review, low 1): the last run's outcome (result, mode, phase,
stage, holds, the store it leaves), the first run's starting point
(`used_before`, the previous phase, the attempts guard), the work of every
run added up (spans rewritten, strays, cursors reset, gaps, check passes,
...), the room figures of the last run that lacked room, and each file's
actions and counts merged (its blocked spans the last run's). Before, it was
the last run's alone: P02's first 2.7.6 run (153 spans, 1 page needed, 0
erased) would have reached ADD as 51 spans with no room figures. It gains
`level: 3` when a round ran, `runs`, and a `reclaim` object sent every boot: `area` (the state),
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
Measured on the host (section 17): the check pass after a round on the
8 MiB P02 store deletes about 6,700 leaked pages and reads every page header
about seven times per 512-page range (15.7 million HAL reads, against 2.1
million for a pass with nothing to delete); on the device those reads are
mirror hits, but HIL must measure the pass.
P02 (unconfirmed): 2.7.5's 21 s run, the round, then a second run with the
check over 8 MB, deleting the leaked pages, the restore comparison of 3.58 MB
held, compaction and the probe: **about 60 to 90 s** (D9), inside the 255 s
soft deadline of an unconfirmed image. The 900 s confirmation budget starts
after `storage_init`. HIL measures `reclaim.ms` and `duration_ms`.

PSRAM: four 4 KiB buffers for the round, three for recovery. Internal: the
external partition's list item (about 100 bytes, freed on return).

Stack (round 1 review, low 3; the figures this section gave before were
wrong). Measured with ESP-IDF 5.5.3's GCC on the production build, every
repair and SPIFFS source compiled again with `-fcallgraph-info=su`; the
deepest static chain from `storage_repair_boot`, indirect calls aside:

| | 2.7.5 (5de7724) | eaf7ec3 | now |
|---|---|---|---|
| deepest chain | 3,232 B | 4,016 B | 3,584 B |
| through | `srr_run`, `restore_held`, `unhide_as` | `run_once`, `run_checks`, `restore_held`, `restore_in_place`, `rewrite_index` | `run_once`, `settle_waits`, `restore_held`, `restore_in_place`, `rewrite_index` |
| `srr_run` | 928 B | 48 B | 48 B |
| `run_once` | (in `srr_run`) | 1,552 B | 944 B |
| `reclaim_round` | | (in `run_once`) | 528 B |
| `storage_repair_boot` | 432 B | 432 B | 448 B |

The round's candidate plan, record encoder, held comparison and settlement
are kept out of `run_once`'s frame (`SR_NOINLINE`); each chain ends in
SPIFFS's own `spiffs_page_move` and cache frames. The review measured 3,104 B
and 3,888 B leaving SPIFFS's leaf frames out: the same 128 B less. P02's
4,112 B free of 8 KiB came from a run that skipped the check, so it is not
2.7.5's worst case: HIL must read `stack_free_bytes` on P02's first 2.7.6
boot and require at least 2,048 B.

## 13. Harness

Everything runs in `spiffs_repair_host.c` against ESP-IDF 5.5.3's SPIFFS
(`check_spiffs_repair.py`); `reclaim` runs every part below, `reclaim-<part>`
one of them, `REVIEW_FULL=1` cuts at every write instead of sampling.

- **The redo area:** a second emulated NOR region of 96 sectors under the
  same power-cut model (every write and erase counted, both tear modes),
  kept beside the NVS model so every snapshot takes it too.
- **`reclaim-p02`**, at 1 and 8 MiB: a store built like P02 after 2.7.5 (no
  erased page; every block holding a page of the damaged pending queue or of
  a file whose removal stopped after its header, a fifth of the store; its
  orphan index pages; deleted header and index copies everywhere; 51 spans
  of one index page (12 at 1 MiB) whose referenced pages are deleted while
  valid copies remain, so 2.7.5 refers each to its copy and then needs
  exactly one page, the index rewrite; four strays past the end; a stale
  header copy; a cursor past its file). The released 2.7.5 engine
  (`tests/firmware/fixtures/storage_repair_2_7_5`, byte-identical to 0954fd3)
  must first reproduce P02's report on it: at 8 MiB exactly the field's
  (`DEFERRED`, `full`, -10001, 1 page needed, 0 erased, 153 spans
  rewritten, 4 strays, 1 duplicate, 1 cursor reset, orphans removed,
  `LEVEL2_SKIPPED`, check skipped).
  2.7.6 must then end `REPAIRED` with no gap, confirmed, unconfirmed, and
  with no holds at all (13FL's case).
- **Gc-unsafe and full stores** (`review-gc-unsafe`, `review-level2`,
  `review-unheld-crammed`): each runs twice, without a redo area (2.7.5's
  outcome kept: `DEFERRED`) and with one (reformatted, healthy, the hidden
  file back whole in the same boot).
- **Oracles** on every reformatted block, page by page, when the round
  remounts: every kept entry byte-identical with its lookup entry at its
  index; every dropped one was a deleted page no valid index page named and
  is now erased under a free entry; entry 0 kept while a later entry is; the
  magic valid (or, for a block that would not verify, absent with a source
  record); no weak-magic block touched; on an unconfirmed image no page of
  any file kept. After the boot (`verify_reclaimed`): every block's magic
  and erase count, SPIFFS's own free-block count equal to the flash's, no
  written page under a free lookup entry, no open record, the journal idle,
  every model file byte-exact, three free blocks or more, no stray page,
  clean tails, appends and a probe file, SPIFFS's GC on every third block
  moving nothing a live file reads, and a clean next repair.
- **`reclaim-cuts`**: a cut at every write and erase of the round (P02 and
  the gc-unsafe store, confirmed), both tears; the next boots must leave
  custody byte-exact with no gap record at all.
- **`reclaim-rollback`**: the same on an unconfirmed image, where each cut
  returns to the old image: a plain mount (SPIFFS's remedy), every file
  listed and read byte for byte against the store as the round began, the
  old image's punches, an outbox row and a probe file; then 2.7.6 again.
  Once more with the released 2.7.5 repair running as the old image: it must
  report no gap on any file.
- **`reclaim-redo`**: a cut at each of the first 24 writes of a boot's redo,
  both tears.
- **`reclaim-area`**: a foreign area (never written, the store left as 2.7.5
  leaves it), a torn first record in a claimed area with a full ring of done
  records, another image writing the block after a cut (sources, back in
  place, retired), a worn cell that never verifies (a source, back from it
  the next run), four browned-out redos (the tally), report-only completing a
  redo, the area unreadable with an active journal (sources journaled, the
  filled spans put back the next boot), and a refused journal (the store not
  mounted that boot).
- **`reclaim-mirror`**: the same flash, redo area and report with and
  without the metadata mirror; the remount keeps the mirror as the device's
  does (one mirror per boot) and checks it against one read fresh from flash
  at every remount; P02's round cut through the mirror (round 1 review, low
  4).
- **Round 1 review of the implementation** (section 18): `reclaim-stale`
  (a cut in a window over 14 pages of the queue, block 133, then a boot that
  cannot read the area, then boots that can, with a stale cursor, a cursor
  before the damage, a verified cursor past the window, and on the gc-unsafe
  store: every readable byte back, gap records only for unreadable bytes,
  records keep only verified cursors), `reclaim-restore` (T5: a held file
  freed or changed by the check on a store without room, with and without a
  redo area, and one the round cannot make room for), `reclaim-torn` (six
  torn erases that read a valid magic, redone), `reclaim-sources` (byte
  sources without memory), `reclaim-p02` (the boot's report over its runs,
  also with 2.7.6 straight on P02's store). The host-only `prefer` port
  lets a test put a round's first window over a block it names (any eligible
  block is the engine's free choice); the device leaves it NULL.
- **Organic:** a store that ends wedged is now a failure (the partition is
  no longer grown to read it); CI's prefilled 8 MiB seeds and the long seeds
  must end healthy.

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
- A reported entry keeps its record (and slot) for good, and nothing yet
  delivers or releases it (C4). Once all 96 slots hold retained or open
  records, rounds stop with `short` `slots` and the device behaves as 2.7.5.
- The `CLAIMED` flag lives in NVS: a device whose NVS is erased sees its own
  torn slots as foreign, and its area stays `FOREIGN` (never written again)
  until the owner decides; its valid records are still read and honoured.
- A redo given up after four attempts (supply that cannot sustain an erase)
  turns into sources; the spans stay unreadable to the application until a
  run has room for them.
- Held bytes a restore cannot put back even after the round (the round could
  not make room for all of them, as on a store crammed with files never put
  in a window) are recorded once, before the hold dies (7.5); a reset before
  the retry costs the held bytes, recorded by the next boot's resume, as for
  any reset during a restore.
- A device whose redo area cannot be read while it holds byte sources keeps
  the affected queues blocked (their unreadable spans wait) until a boot
  reads the area.
- A record written while a queue's cursor could not be verified (an
  unreadable span before it) never settles by offset: an entry whose file is
  then gone is reported, its record kept for good.
- A round leaves a run's work for the next run of the same boot, so a boot
  may run the repair three times (two rounds); the deadlines bound it.
- None of this has run on a device.

## 16. Review points and what became of them

| Point | Verdict | What changed, or why not |
|---|---|---|
| C1 unconfirmed window | Accepted, option (b) | Section 8. Option (a) is listed for the owner. A per-block exposure histogram goes to ADD before anything is written. The rollback harness checks every file byte for byte through plain SPIFFS after the remedy (any reader of any image) and runs the firmware's legacy queue workload; running 2.5.2's and 2.4.12's `zone_lite.c` themselves is not done: they need FreeRTOS, ESP-IDF and HTTP, and with no page of any file in a window their reads are identical by construction, which is what the oracle checks |
| C2 redo without proof | Accepted | 10.1: raw condition; sources are sticky; journal `SOURCES` or no mount |
| C3 generation is not retirement | Accepted | 10.3 and 10.4 use offsets only; 2.7.5's `resume_file` no longer supersedes a hidden remnant, or a vanished file, on a moved generation (its tests now expect the remnant back, or a record) |
| C4 readable bytes reduced to offsets | Accepted in part | 10.3, 10.4: delivery in place (the record's page is a source for the span the index names, also a recorded copy while the cursor had not passed it), settlement by offset, the SALVAGE-fill restore in place, and otherwise a record whose bytes are kept for good (never retired or erased). Not built: rebuilding rows from a record span plus the current neighbouring bytes and appending them through `merge_rows`. That case is a file that is gone or changed under the record; its neighbours then belong to another file or generation, so the rebuilt rows would be a guess spliced into the ORDS stream, and a misframed row is worse than a reported gap whose bytes stay on the device. ADD gets the gap and the record id; delivering a retained record (or acknowledging it) is a later, owner-driven step |
| C5 deadlock, not total | Accepted, delivery as a reported outcome | G5 allows other blocks with the open record's pages kept; "back on flash" is a subset test (an append in place settles); a re-created name with a reused object id fails the record's name or cursor test and is reported; 10.4 is total with "reported" (record kept) as the terminal case in place of "deliver" (see C4) |
| C6 mirror reads | Accepted | R12, 5.3 steps 6 and 8, the raw magic check |
| H1 keep set | Accepted, except one clause | R9 and 6.1 from flash structure; the index-flag test reads the page header. Not taken: "it is not a header or index page" for pages whose own deleted flag is set and whose lookup entry is `0x0000`: no SPIFFS path and no 2.7.x rule reads such a page (1.3 item 6), and keeping them would keep most of the gain (every append's header move leaves one). Index and header pages that are not deleted keep their block out entirely |
| H2 any reset, flicker | Accepted in part | Deadline gate and deferral (5.3, 10.1). Not taken: a 60 s uptime delay for unconfirmed windows, since with C1 (b) such a window exposes nothing readable |
| H3 counters | Accepted | Remount after recovery and after every round |
| H4 between check and restore | Accepted | The round runs only at the end of a run |
| M1 recovery and NVS | Accepted | 9.1: records read in every readable state |
| M2 the 2.7.5 stand-in | Accepted | The released engine, vendored byte-identical, runs as the old image |
| D1 redo after the mount | Accepted | 10.1 before any mount |
| D2 low magics | Accepted | 6.1; the round oracle fails any reformatted block with two set magic bits or fewer, and the 8 MiB P02 run must keep such blocks out (67 at 8 MiB) |
| D3 wrong bytes, appends | Accepted | Section 8 corrected; last data pages never in a window |
| D4 torn magic or done | Accepted | 10.1 re-asserts both |
| D5 brownouts | Accepted | The attempts tally; BROWNOUT counted |
| D6 area wear and time | Accepted | 9.2 |
| D7 hard deadline in a window | Accepted | 5.3 |
| D8 P06 cannot vouch for P02's tail | Accepted | 9.1 reports foreign detail; decision 2; P02's first boot may be discovery |
| D9 estimates | Accepted | Section 12 |
| D10 NVS writes | Accepted | G4 |

## 17. Implementation and verification (20 October 2026)

**Commits** on `release/zkt-2.7.6` (each pushed):

| Commit | What |
|---|---|
| fc39f2e | this design, second round |
| dd402a2 | `storage_repair_redo.c`: record, plan, writer, recovery; the reclaim journal; `srm_refresh`; plain cc tests |
| 4c6eddc | C3: no supersession by a moved cursor generation (2.7.5's resume) |
| ae4037b | the round in the engine, sources and settlement; the harness's redo area, P02 builder, oracles, cuts, rollbacks; the vendored released 2.7.5 engine |
| 51dfbdc | the device glue: discovery, recovery before any mount, ports, deadline deferral, mount gate, heartbeat |
| 3c546a6 | ADD: `StorageRepairReclaim`, `runs` |
| 31b8897 | GCC format-truncation fix, the namespace host's stub |
| 29e094b | P02 builder exact to the field report; recorded copies as sources |
| eaf7ec3 | this section, the first time |
| a400229 | the round 1 review of the implementation: every finding (section 18), with its regression tests |
| 38b52ec | the log line for a queue that waits for an area that cannot be read |
| 2d26d08 | section 18 and the sections the fixes changed |
| f3988b8 | `reclaim-stale`'s cursor before the damage kept before it at 8 MiB (a test error, not the engine's) |
| 98c7953 | the verification after the round 1 review, below (the wedged seeds added next) |

**Results at eaf7ec3** (host, ESP-IDF 5.5.3 SPIFFS, ASan and UBSan; Apple
clang natively and GCC in `espressif/idf:v5.5.3`; the results after the
round 1 review follow these):

- The CI harness configuration (`check_spiffs_repair.py`, every entry,
  including the new 8 MiB reclaim entries): passes natively and in the IDF
  image (about 4 minutes on 8 cores).
- P02 at 8 MiB: the released 2.7.5 reproduces the field report exactly;
  2.7.6 then reformats 3 blocks (confirmed) or 5 blocks holding nothing any
  image reads (unconfirmed: 731 such candidates of 2,048), and ends
  `REPAIRED` with no gap, also with no holds at all.
- Cut at every write and erase of the round (`REVIEW_FULL=1`): at 1 MiB, P02
  48 and gc-unsafe 92 cuts confirmed, 188 through the old image and 188
  through the released 2.7.5 unconfirmed; at 8 MiB, P02 48 and gc-unsafe 96
  confirmed, 150 and 150 through the old images. Every one: custody
  byte-exact, no gap record, every file read identical by plain SPIFFS after
  an unconfirmed cut.
- 48 cuts inside a boot's redo; the area cases (foreign, torn record and
  full ring, another writer, worn cell, four browned-out redos, report-only
  redo, unreadable area, refused journal) at 1 and 8 MiB; mirror
  equivalence.
- Organic workload, fresh and long seeds: 8 MiB seeds 12, 13 and 16 at
  20,000 ticks (wedged on 2.7.5) end healthy with 0 committed rows lost
  (9 to 10 rounds, 66 to 95 blocks each); 8 MiB prefilled fresh seeds 111 to
  119 and 211 to 214: 0 rows lost, none wedged, none handed over GC-unsafe;
  1 MiB fresh seeds 311 to 318: 0 lost (no round needed); 620 fresh cut
  runs of the scenarios.
- `tests/firmware` in the IDF image: 376 passed. ADD: the diagnostics,
  heartbeat, storage contract and recovery tests (479 passed), ruff, the
  OpenAPI and repository contracts.
- `idf.py build`: production, Hikvision, and the direct legacy role (2.7.5
  version, as CI builds it) all build; `-fstack-usage` as in section 12.

**Results after the round 1 review** (f3988b8; the same host setup):

- Each finding's proof (section 18): the reviewer's reproductions on
  eaf7ec3 and on the fixed code; each regression test fails with its fix
  taken out of the final code (the record-time cursor check and the
  settlement rule of D1 one at a time, the low finding's hold-back, D2's
  deferral and its reported mark, D3's torn-erase redo, low 1, low 4, low 6)
  and passes with it; the exhaustive tear check fails on eaf7ec3's weak-magic
  rule; the two glue contract tests (low 2, low 5) fail on eaf7ec3's
  sources.
- The CI harness configuration (`check_spiffs_repair.py`, all 12 entries,
  the 1 MiB entry now running the round 1 parts too): passes natively (6
  minutes on 8 cores) and in `espressif/idf:v5.5.3` with GCC (22 minutes,
  the host loaded).
- Cut at every write and erase of the round (`REVIEW_FULL=1`): at 1 MiB, P02
  48 and gc-unsafe 92 cuts confirmed, 188 through the old image and 188
  through the released 2.7.5 unconfirmed; at 8 MiB, 48 and 96 confirmed, 150
  and 150 through the old images. Every one: custody byte-exact, no gap
  record. 48 cuts inside a boot's redo at 8 MiB; the area cases at 1 and 8
  MiB; mirror equivalence, the kept mirror checked at every remount and
  P02's round cut through it at 1 and 8 MiB; the round 1 parts at 8 MiB too
  (the stale-cursor cases over block 1,067 and, on the gc-unsafe store,
  block 1,600; T5; torn magics; sources without memory).
- P02 at 8 MiB: the released 2.7.5 reproduces the field report; 2.7.6
  reformats 3 blocks (confirmed) or 5 (unconfirmed), keeps 68 weak magics out
  (block 810 among them), and ends `REPAIRED` with no gap; the boot's report
  carries 204 spans rewritten (153 + 51) and the first run's room figures.
- Organic workload (20,000 ticks at 8 MiB; the seed is the one the harness
  prints, `--first-seed` plus 11): seeds 12, 13 and 16 (wedged on 2.7.5) end
  healthy with 0 committed rows lost and no gap record (9 to 10 rounds, 66 to
  95 blocks each, as at eaf7ec3); fresh seeds 23, 24, 27, 622, 623 and 723
  the same (1 to 27 rounds, 7 to 189 blocks); prefilled 8 MiB seeds 122 to
  125 (3,000 ticks) and 1 MiB seeds 322 to 329 and 822 to 823: 0 lost, none
  handed over GC-unsafe.
- `tests/firmware` in the IDF image: 377 passed; `test_hikvision_api_host`
  ran past its 60 s limit with the host loaded and passes alone (43 s); this
  branch does not touch the Hikvision code.
- ADD: the 8 backend shards (`scripts/run_ci_tests.py` against Postgres 16
  and Redis 7, as `backend-tests` runs them): 4,736 passed, 669 skipped, and
  `--verify-plans` confirms complete, disjoint coverage. 41 of the skips run
  in CI but not here (39 need `jq` for the deploy step, 2 a git checkout);
  the rest are skipped in CI too (583 want
  `ADD_SAFE_REPAIR_TEST_DATABASE_URL`, the others other qualification
  databases or ESP-IDF sources). Ruff with CI's paths, the frontend OpenAPI
  contract and the repository contract pass. ADD's code is unchanged since
  3c546a6: the merged report has the same fields.
- `idf.py build` in `espressif/idf:v5.5.3`: production, the 2.7.5 direct
  legacy role (its identity and exact rollback contract marker), Hikvision
  (its family descriptor; 3 warnings, in files this branch does not touch),
  recovery 2.6.27 (identity, contract marker), the 2.7.0 journal writer and
  the 2.6.23 bridge (identities, startup stack budgets): all build, the
  repair's roles with 0 warnings; the production image is 1,441,792 bytes
  (45% of its 2.5 MiB slot free). Stack: section 12.

Not run here: a device (HIL), and the release step (version, upgrade guard
digests, signing).

## 18. Round 1 review of the implementation (eaf7ec3) and what became of it

Two reviews of eaf7ec3: a custody review (refuted it: three defects losing
readable bytes, D1 to D3, and a low finding) and a device and build review
(no blocking defect, six low findings). Every finding is accepted and fixed
in a400229 and 38b52ec. Each fix has a host test that fails with that fix
taken out of the final code and passes with it (the second column of the
proof); where the reviewer left a reproduction (`rv276-repro.patch`: modes
`rvH1`, `rvT5`, `rv810`) it was run on eaf7ec3 and on the fixed code (the
first column).

| Finding | What changed | Proof: the reviewer's reproduction, eaf7ec3 then fixed; the regression test without the fix |
|---|---|---|
| **D1** (high) a record settles on a queue cursor that was never verified, and its bytes are then lost | A record keeps a queue's cursor offset only when `verified_cursor` finds it valid for the file when the record is written (present, its CRC good, `srp_cursor_verdict` KEEP: prefix readable, prefix CRC matching), once per queue per round. `settle_sources` never settles by offset while the file (its id and name) holds the span unreadable, or filled where the record's bytes belong: the entry waits and the next run with room puts the bytes back. Offset settlement only once the file is gone or holds other bytes there (10.4) | `RV_STALE=1 rvH1 --kib=1024`: 3,514 readable bytes lost (offset 110,189, spans 439 to 452); fixed: 0 lost. `reclaim-stale` with the record-time check taken out: "record 4 keeps the queue's cursor offset although it could not be verified"; with the settlement rule taken out: "/pending.jsonl byte 110189 changed 6e -> 00" (a verified cursor past the window) |
| **D2** (medium) T5 makes room, but the held bytes have already been dropped | A restore after a check that finds no page waits for the round (7.5): nothing recorded, the round's target counts the waiting pages, and the restore runs again after the round while the holds live, when the erased pages cover it; only what still cannot go back is recorded, once. A recorded rebuild's manifest entry is marked reported, so the resume adds no second record. SPIFFS_ERR_NOT_FOUND from the allocator counts as no room. The held-CRC oracle skips a file whose restore waits | `RV_VICTIM=/processed_commands.txt rvT5 --kib=1024`: the command dedupe file missing after 17 blocks reformatted, two records for the same range (CHECK_CHANGED, RESTORE_INTERRUPTED); fixed: byte-exact, no record. `reclaim-restore` without the deferral: "freed with a redo area: ... 1 gap records"; without the reported mark: "the resume recorded the same bytes again (2 records)" |
| **D3** (medium) the weak-magic rule misses block 810 (0x00FF) at 8 MiB | A magic with every byte 0x00 or 0xFF is weak (68 of 2,048 blocks, 983 and 810 among them). On the same image a block with a valid magic that reads as the window's erase cut short (every byte the record holds there with bits only set, a kept byte not yet erased) is redone, not taken for another writer's. The cut sweeps cannot reach block 810 any more (it is never eligible); the exhaustive tear check over every block of 64- to 4,096-block stores and the round oracle (every reformatted block strong) cover it instead | `RV_UNCONF=1 RV_P02_LEAK=35 RV_PREFER=810 RV_N=4 rv810 --kib=8192`: 4 of 4 tears that left the magic valid lost or changed `/processed_commands.txt` for the old image; fixed: block 810 is never taken ("block 810 not taken first"). The exhaustive tear check on eaf7ec3's `srx_magic_strong`: blocks 810 of 2,048 and 2,858 of 4,096 strong yet tear-valid; fixed: none. `reclaim-torn` without the torn-erase rule: "0 redone, 1 sources"; with it 6 of 6 redone |
| **Low** a boot that cannot read the area records SALVAGE gaps for bytes the next boot restores | The journal's new `HELD` flag (byte sources not yet retired) with `ACTIVE` and `SOURCES` tells a boot that cannot read the area that records may hold queue bytes (`srr_config_t.sources_unread`): a reclaim queue's unreadable spans wait, neither filled nor recorded, and `STORAGE_REPAIR_RECLAIM_SOURCES` says why (10.1) | `RV_STALE=0 rvH1`: a SALVAGE record of 3,514 bytes the next boot restored; `RV_H1_GCU=1`: 1,255 bytes; fixed: only span 2's bytes, which nobody could read. `reclaim-stale` with the hold-back taken out: "3514 readable bytes reported unreadable" |
| **Low 1** the heartbeat reports only a boot's last run | `merge_report`: the boot's report over every run (11) | `reclaim-p02` without the merge: "2 runs, 12 spans rewritten (12 damaged), 0 pages needed, 0 erased" (the last run's); with it 48 spans (3 x 12 in the first run, 12 in the second) and 1 page needed, 0 erased; at 8 MiB 204 spans (153 + 51) |
| **Low 2** a leftover REFORMAT breadcrumb, and recovery running without a deadline | Without the deadline timer recovery writes nothing (open records go to the journal as sources) and the breadcrumb is marked returned; the REFORMAT breadcrumb is set only while recovery acts on an open record (10.1) | Device glue, not reachable by the host harness: `test_a_boot_without_its_deadline_writes_nothing_and_returns_its_breadcrumb` fails on eaf7ec3, passes |
| **Low 3** the stack figures in the doc are wrong | Corrected and measured again (12); the round's plan, record encoder, held comparison and settlement are kept out of `run_once`'s frame: the deepest chain is 3,584 B (eaf7ec3 4,016 B, 2.7.5 3,232 B, all with SPIFFS's leaf frames). HIL must see `stack_free_bytes` of at least 2,048 B on P02's first 2.7.6 boot | `-fcallgraph-info=su` on the production build, section 12 |
| **Low 4** the harness does not remount the way the device does | The harness's remount keeps the mirror and checks it against one read fresh from flash at every remount; P02's round is also cut through the mirror | `reclaim-mirror` with the old remount: "2 mirror inits, 0 kept-mirror checks for 1 rounds" |
| **Low 5** nothing checks the page and block geometry | `_Static_assert`s on the page size, block size and length magic in the glue and the engine; the round, recovery's registration and loading sources check the mounted geometry (9.1) | `test_the_redo_geometry_is_pinned_at_build_time_and_checked_before_any_round` fails on eaf7ec3, passes |
| **Low 6** a failed buffer allocation drops the redo sources silently | A run whose area holds a source record fails before writing when it cannot allocate the buffer (`sources_memory`) or read a record (`sources_read`) | `reclaim-sources` without the fix: "REPAIRED_WITH_GAPS ... 1 gap records" (the span filled and recorded as lost); with it FAILED before writing, and the next boot puts the bytes back |
| Note: 864 B more static internal RAM (`s_area`) | Not a defect; left as it is (the boot's area table, used during `storage_repair_boot`) | |

Still open, as the device review listed them (release blockers, not
defects): the version bump, the ADD storage contract versions and the
direct-legacy BASE marker including 2.7.5 (so devices on 2.7.5 can take
2.7.6), the upgrade-guard digests, signing, and a hardware-in-loop run,
with the stack and the time on P02.
