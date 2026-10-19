# Zone Lite 2.7.5: custody-first SPIFFS repair

Status: implemented on `release/zkt-2.7.5` (not yet versioned, not on HIL).
This document describes what was built. It replaces the first design
(`94136b3`), which two adversarial reviews found unsafe as written, and the
first implementation, which two more reviews found unsafe in eleven and five
places, a third round found seven custody and four boot defects in, and a
fourth six custody and four boot defects in; section 11 lists every review
point and what became of it. One of them (a store crammed until SPIFFS's GC
has nothing to move pages into) is not fixed: it needs the owner's decision
on a last resort (section 12).

Owner decision, 16 October 2026: save every readable unsettled row first,
run the SPIFFS check at boot, restore what the check removed, report fixes,
deletions and gaps to ADD, and reformat only as a last resort once the
readable custody content is held for rewrite. Preserving attendance custody
is the top rule: no readable unsettled punch may be lost, and every
physically unreadable byte must reach ADD as explicit gap evidence (file,
offset, length).

Scope: the ZKT production images (default and `ZONE_LITE_DIRECT_LEGACY_UPGRADE`).
Not built into the Hikvision family, the 2.6.27 recovery role or the journal
roles (CMake).

## 1. Evidence

### 1.1 Root cause (power-cut simulator, `research/spiffs-powercut-sim`)

zone_lite never ran a SPIFFS check. A reset during a flash write leaves
objects unfinalized or half deleted; GC then refuses the damaged block, and
interrupted removes leak pages until ENOSPC at low real usage. On the 8 MB
store with about 20 cuts per run: no check left 1 of 8 runs stuck; a plain
`SPIFFS_check` at every mount fixed that but deleted 12 whole files and lost
54 unacknowledged lines.

### 1.2 What the host harness found in ESP-IDF 5.5.3's SPIFFS

The harness of section 9 runs ESP-IDF's own SPIFFS sources on an emulated NOR
flash. It showed why a bare check deletes files, and more hazards:

1. **Duplicated index pages.** SPIFFS updates a page by `spiffs_page_move`:
   write the copy, give it its lookup entry, finalize it, delete the source.
   Every append moves the object header this way. A reset before the delete
   leaves two valid copies; the check's page pass then sees the file's data
   pages "multiply referenced" and deletes the file.
2. **The check re-links uncommitted pages.** An append that never committed
   leaves a finalized data page past the end of the file. The check's page
   pass links it into the index (`FIX_INDEX`). The next append writes its
   own page index into that index entry in place, so flash holds the AND of
   two page indexes, and a later read follows it to another page.
3. **Removals the check never finishes.** A removal flags the header first
   and frees pages from the end. If it stops, the index pages past the
   header keep their data pages referenced: the check's index pass skips
   valid index pages, and its page pass cannot find the hidden header to
   delete the object. Those pages leak for good, which fits P02's ~1.4 MB
   used outside every file.
4. **Index entries past the end of a file**, left by any interrupted append,
   are ANDed by the next append's in-place index write (the same mechanism as
   2), and the check chases them as references.
5. **Dirty tails.** An append programs the rest of the last data page in
   place without checking it is erased. Bytes an interrupted append left
   there are ANDed into the next committed row.
6. **A lookup entry that says free under a used page.** The allocator trusts
   the lookup and would program a new page over it.
7. **A torn lookup entry.** NOR can leave some bits of a lookup write
   unprogrammed, so an entry names no object (0xFF09 over a page of object
   1). For a finalized data page that no index references, the check's lookup
   pass searches for the page's index entry, and that search reloads the
   lookup page the pass is walking with another block's. The pass deletes the
   page and reads the rest of the block through the other block's entries;
   where an entry's index marking differs from the real page it deletes the
   real page, and the page pass then deletes the file that loses it (the
   organic soak lost `/acked_uids.txt` this way).
8. **SPIFFS's GC on a damaged or crammed store.** The GC cleans a block by
   moving each data page and pointing the index entry of that span at the
   copy: it points it at whatever page of the span it meets in the block (a
   duplicate, an uncommitted copy), deletes data pages whose index page it
   cannot find, and finds index pages by lookup entry, stale copies
   included. On a store with too few erased pages for a block's moves it
   stops half way, with data pages moved and the index on flash still naming
   the deleted originals. SPIFFS keeps two free blocks for its GC; resets in
   a GC or a nearly full store use them up, and a long-running store keeps
   hundreds of deleted pages spread thinly over every block.
9. **An append after a failed append.** On a crammed store an append can
   fail (ENOSPC) after programming part of its row into the last page past
   the end of the file, and for a longer row after writing data pages and
   index entries past the end; the size stays. The next append programs its
   row over those bytes in place, so the committed row is the AND of two
   rows on flash before any repair runs (row 1856 of soak seed 230:
   `"seq":1855` ANDed with `"seq":1854` reads 1854). The firmware's append
   path keeps appending after a failure. The repair cannot recover those
   bytes; it terminates and delivers what is on flash. Section 12 proposes
   the runtime fix.
10. **A torn header size.** The first append to a new or truncated file
    (pending.jsonl after every `lq_reclaim`, every durable-queue segment
    opened with "wb") programs its header in place over size 0xFFFFFFFF; a
    reset there leaves some of the bits the real size clears still set, in
    any byte (0xFFFF0204, 0x00370204, or 0xFFFFFFFF with the index entries
    programmed). SPIFFS can then neither read, append to, truncate nor
    remove the file, the check keeps it, and every later create of that name
    fails: 13FL's and P02's probe ENOENT, a queue that refuses every punch, a
    segment lane that refuses "wb". Section 5.1 recovers the real size.
11. **Stray pages.** A header program torn on SPIFFS's append path leaves a
    page flagged finalized but still unused (0xFD) with the file's lookup
    entry. The check ignores it; SPIFFS's GC moves every page whose lookup
    entry names the file and points that span's index entry at the copy, so
    a span reads as erased after the next GC (seed 512).
12. **SPIFFS's page counters drift.** Deleting a page whose lookup entry
    said free (item 6) counts it out of the allocated pages it was never
    counted in, so `stats_p_allocated` and `stats_p_deleted` overstate the
    erased pages. Room estimated from them sent a block cleaning into a store
    with one page too few (seed 618).
13. **A half-deleted hidden header.** `spiffs_page_delete` clears the lookup
    entry first; a reset in that write can leave a removed probe's hidden
    header with an entry of 0x0007 for 0x8007 (it now names a data object).
    The check's handling of it is item 7's: it deleted the live pages that
    followed in the block and then a consistent 662 KB pending queue (seed
    832). Lookup hygiene must remove it, and the check must never run while
    any mislabeled page remains.

`SPIFFS_check` also returns only the result of its final lookup scan, so a
failed lookup, index or page pass (FULL included) reaches only the check
callback.

### 1.3 Field reports (2.7.4 inventory, 16 October)

A SPIFFS page holds 251 data bytes. P02's first unreadable offset 493,968 is
exactly span 1968. 13FL's blocked queue fails at about 50,200 (span 200, the
queue head: cursor 50,055). 13FL and P02 also fail their persistence probe
with ENOENT on a damaged probe object. LAHORE-02 hit ENOSPC at 26% used, and
MULTAN-01 and LAHORE-02 EBADMSG. Section 1.2's mechanisms cover all of
these; the run's report names the exact SPIFFS cause per span.

## 2. Rules

- **R1 Custody.** No byte that any page on flash still holds for a file is
  replaced, and no byte is taken from a page that may belong to another file
  or another version of it. A span whose referenced page is bad takes its
  bytes from that page when it is this file's finalized page of the span
  (deleted or with a wrong lookup entry) and from finalized copies whose
  lookup entry names the file; where they differ, the more programmed byte
  wins, and a byte two of them program differently is a gap. Only a byte with
  no source is a gap.
- **R2 Gaps are explicit.** Every unreadable range is recorded in NVS with
  its file, offset, length and SPIFFS cause before any byte is written, and
  sent to ADD as queue evidence.
- **R3 No deleting check.** `SPIFFS_check` runs only when every custody file
  is consistent, so the check has nothing in them to delete.
- **R4 No new formats.** Offsets, names, NVS keys and cursor formats are
  unchanged; every rollback image reads the repaired store.
- **R5 No reformat** in 2.7.5 (section 12).
- **R6 Boot safety.** A deadline the repair code cannot defeat; no restart
  initiated by the repair; no LOCAL_FAILURE for gaps it reported.
- **R7 Proved on the host** against ESP-IDF's SPIFFS with power cuts,
  including cuts inside the repair.

## 3. Where it runs

`storage_init()` (zone_lite.c):

1. `storage_repair_boot()`: private mount, the engine, unmount.
2. `seen_cache_init()` (2 MiB of PSRAM, after the repair's holds are freed).
3. `esp_vfs_spiffs_register()`, `format_if_mount_failed = false` as before.
4. `storage_repair_after_mount()`: the command inbox merge (7.1).
5. `add_connector_storage_ready()`: until now the ADD supervisor (which
   starts before storage_init and restores the command inbox every second)
   treats the inbox as unavailable, never as empty.
6. Everything else unchanged.

`ota_manager_start()` runs after storage_init, so the 900 s confirmation
budget and the 4 h cap start after the repair. The ESP-IDF bootloader
rollback still applies: any reset during an unconfirmed image's first boot
returns to the old image.

## 4. Device glue (`storage_repair.c`)

- **Private mount** with ESP-IDF's SPIFFS library and an `esp_spiffs_t` of
  its own (its mutex is what `SPIFFS_LOCK` takes). The spiffs component's
  private headers come in through CMake; no ESP-IDF source is copied.
- **HAL**: `esp_partition_*` through the metadata mirror and one internal
  4 KiB bounce buffer (esp_flash allocates up to 16 KiB internal per read
  into PSRAM and programs from PSRAM 32 bytes at a time). Yields
  (`vTaskDelay(1)`) every 20 ms inside the HAL, so no single SPIFFS call
  starves IDLE0.
- **Metadata mirror** (`storage_repair_mirror.c`): every lookup page and page
  header in PSRAM (672 KiB for 8 MB), allocated first. The check's 2.1 M
  header reads are served from RAM. Programs and erases go to flash first
  and update the copy by NOR rules; after a failed one the affected bytes are
  read back from flash. The harness proves byte-identical results with and
  without it.
- **Memory**: repair state in PSRAM (64 KiB hold chunks); budget = free PSRAM
  less 2.5 MiB reserve and 640 KiB engine, capped at 7 MiB. SPIFFS's own
  small buffers are internal and freed before the VFS mounts.
- **Deadline**: a one-shot `esp_timer` armed before the mount: 5 min on an
  unconfirmed image (a reset there rolls back), 15 min otherwise. Its
  callback records the phase in RTC memory and calls `esp_restart()`. The
  engine stops itself at its next safe point 45 s earlier (`STOPPED`), except
  in the restore after a check pass, and tells the engine how much time is
  left (`remaining_ms`), so a check pass starts only when it and its restore
  fit.
- **NVS headroom**: `nvs_get_stats` before the run. Its `available_entries`
  (which in ESP-IDF 5.5.3 already leaves out the page NVS keeps for its own
  GC; `free_entries` counts it) must cover creating each journal blob that
  does not exist yet, one rewritten copy of the largest, and 64 entries the
  runtime keeps for its own checkpoint commits and new keys
  (`SRP_NVS_RUNTIME_RESERVE`); otherwise the run only reports
  (`REPORT_ONLY`, `error_stage` `nvs_full`). The heartbeat reports free and
  available entries before and after. A run whose first journal save fails
  stops before any write (`FAILED`, `journal`).
- **Rollback target**: on an unconfirmed image, the first word of the app
  digest of the image a reset returns to goes into the manifest (5.4): the
  other OTA slot's while an otadata entry the bootloader accepts (not
  INVALID or ABORTED) still selects it, otherwise the factory app's (ESP-IDF
  5.5.3's bootloader falls back to it; the first OTA after a factory flash
  has no other entry).
- **RTC breadcrumb**: phase, check pass, whether the deadline fired, and
  whether `storage_repair_boot` returned, in a record appended after the boot
  report's trace (the 2.7.1 and 2.7.3 layouts are unchanged).
- **LED**: `STORAGE_REPAIR` (fast white/blue) while the run writes.
- **No task**: the ADD outbox task, which already sends acknowledged queue
  evidence on its 8 KiB stack, sends the log lines and one evidence record
  per pass, outside a priority-delivery hold, backing off a minute after a
  failed transfer. The pending and acknowledged record counts are kept in RAM
  (counted after the run and on each acknowledgement), so with nothing
  pending the task reads no NVS; the records and the rendered evidence go in
  one PSRAM block, not on the task's stack.

### 4.1 Policy (`srp_policy`)

Every boot runs the repair. A run that starts and does not finish counts as
incomplete; it counts as a failure only when the previous boot's breadcrumb
shows the repair's own deadline fired, or a panic, watchdog or CPU lockup
came before the repair had returned (never a power cut, which is what the
repair is for, and never an unrelated crash after a stopped run).

- 3 consecutive failures: repairs stop for the running image (report only:
  mount and `SPIFFS_info`, no file read, nothing written) until ADD installs
  another image, which tries again.
- An unconfirmed image after an incomplete run: normalize only (no check).

## 5. The engine (`storage_repair_core.c`)

### 5.1 Survey (read only)

Torn sizes first (1.2 item 10). A header size that claims spans nothing
traces (no finalized data page of the object and no index entry naming one),
or that cannot fit the partition, or 0xFFFFFFFF in a header whose own entries
are programmed, is the tear of a first append when the real size is still
inside it bitwise: the real size is where the last traced page's bytes end
(text files hold no 0xFF), or for binary files the smallest size at or after
the last programmed byte whose bits the torn size contains. The header is
rewritten with that size (`STORAGE_REPAIR_SIZE_RECOVERED`, file action
`SIZE_RECOVERED`). A size no page traces and no bit pattern explains is
left (`STORAGE_REPAIR_SIZE_TORN`). Expendables with a torn size go by object
id as before. A text file's torn size can also stay inside its last data
span (only low bits left set): its last page then ends in erased bytes the
size claims, which no committed append leaves, so the size is recovered
where the bytes end when the torn size contains it bitwise and no index
entry names a later span. A binary file's 0xFF bytes are data: inside its
last span its size is left as it is. Size recovery reads only pages whose
lookup entry agrees (a page whose entry was deleted may be an earlier
file's that reused the object id), and takes the most programmed one.


One pass reads every block: lookup entries, page headers, and whether each
lookup-free page is really erased. Objects are built from valid index
headers; each object is mapped span by span as SPIFFS reads it. A span is
**perfect** when its page is a finalized, undeleted data page of this object
and span whose lookup entry agrees; for text files its bytes must also hold
no erased (0xFF) byte. `SPIFFS_VALIDATE_DATA` does not check the object id;
the survey does, so a page of another file is never read as this file's
bytes.

An imperfect span is resolved (R1) from two kinds of source: the page the
index names, when it is this object's *finalized* data page of the span
(flagged deleted, as on 13FL's head, or with a wrong lookup entry), and
finalized copies whose lookup entry names the object (an interrupted GC move
or page move). Their bytes are merged: a byte one source holds and another
reads erased (a copy taken before later appends) is the programmed one; two
different programmed values are a gap (`SR_ERR_SOURCES_DISAGREE`). Never a
source: an unfinalized page (its write did not finish; when the index names
one, the page it named was erased and its place reused), a page flagged
deleted that the index does not name (SPIFFS reuses a removed file's object
id, so it may be the earlier file's), a copy whose lookup entry was deleted
(a page caught mid-delete). A text byte no source holds programmed is a gap.
A merged result equal to a perfect copy is referenced as it is.

The survey also chooses one header per object (duplicates: most perfect
references, then larger recovered size, then the non-temporary name) and one
index page per index span, and marks for removal: the other copies, index
pages past the end, index entries past the end, index pages of no live file,
and every *stray* page (1.2 item 11): a page whose lookup entry and header
name a live file and that is none of its header, chosen index pages or
referenced data pages, whatever its flags (data pages past the end
included). Pages of a span not yet rewritten stay until it is, since they may
be its bytes; normalization runs up to three rounds so the strays of spans it
rewrote go too.

### 5.2 Normalize

Before any write, every gap range is recorded in NVS, and the catalog and
command-inbox obligations are journaled (a run that fails after normalizing
must not leave the next one unaware). Then:

1. Lookup hygiene, before the first allocation (the resume's included),
   before the restore after a check, and again after normalization: a needed
   page with a free lookup entry gets its entry; any other used page there
   is deleted; a page whose lookup entry names another object (1.2 items 7
   and 13) is deleted unless the survey needs it, and then its bytes move to
   a new page and it is deleted after. A hidden object's pages are needed
   only while an unsettled manifest entry names it.
2. Stale headers, duplicate and orphaned index pages, and data pages past
   the end are deleted.
3. Each imperfect span gets a new data page (written unfinalized,
   finalized, read back; a page that does not read back is deleted and
   another used). A gap is filled per section 6.
4. Each index page that names such a span, or holds an entry past the end,
   is rewritten with SPIFFS's own `spiffs_page_move`; a missing index page
   is created. `spiffs_object_modify` is never used: it deletes the old
   page, which may belong to another file.
5. The last page of every live file is rewritten when bytes past its end
   are not erased (1.2 item 5): SPIFFS appends in place to any file (queue
   segments take their next record with "r+b", the command inbox, every
   append target).

No page another live file uses is deleted and no readable byte replaced.
Every step leaves a state the next survey understands, so a reset at any
point loses nothing (proved in section 9).

Normalization allocates pages through the nucleus, which does not run GC
and refuses to allocate with fewer than two free blocks. Room comes from
**compaction**, the engine's use of SPIFFS's own block cleaning (1.2 item 8)
on blocks where it is safe: every page erased, deleted and no byte source of
a damaged file, or the referenced page of a consistent file every one of
whose lookup entries is a referenced page. Blocks are taken by net gain
(deleted pages plus the group index pages the block holds, less one index
write per group of data pages) and only when their moves, plus one index
rewrite per group and two pages, fit in the erased pages elsewhere, counted
from the survey (1.2 item 12). A cleaning that stops half way anyway (data
pages moved, the index naming the deleted originals) is surveyed again
before anything else, and normalization points the index at the moved pages
from the room the margin kept.

A block holding only deleted and erased pages moves nothing and is erased
whatever the margin says. When the damaged files have pages in every block,
nothing else can be compacted around them. With fewer free blocks than
SPIFFS's own allocation needs, normalization allocates from the erased pages
SPIFFS keeps for its GC, as a GC does, whenever what it allocates fits in
them (`reserve_pages`; `STORAGE_REPAIR_RESERVE` says when compacting the
best block afterwards would need more): a custody file SPIFFS cannot read is
worse than a store that stays crammed, and compaction, possible once the
files are consistent, gives what it can back. Otherwise room is made
(level 2: probe leftovers, cursor stages, the catalog when a catalog file
exists, fully settled queue files), compaction tried again and
normalization retried. Level 2 runs only when its removals could let a
block be erased (removing a file only deletes pages; a block left with
nothing else, or whose remaining pages' moves fit in the erased pages
elsewhere, is what compaction can erase), and not when the last run
deferred for exactly the same shortage (state flag `FULL` with a signature
of the pages the work needed, the erased pages and the files, in the
journal's `shortage` field; every run starts with the flag clear and sets it
only when it defers for room itself): `STORAGE_REPAIR_LEVEL2_SKIPPED`. A
store still full defers the check (`DEFERRED`, `error_stage` `full`) and
reports the pages the work needed and the erased pages there were
(`room_needed_pages`, `room_erased_pages`) and, per custody file, the spans
SPIFFS cannot read and the first of them (`blocked_spans`,
`blocked_offset`, `STORAGE_REPAIR_BLOCKED`): their bytes stay on flash, and
the next run with room makes them readable. Before the check and before the
finish's own SPIFFS writes the engine compacts back to three free blocks
where it can, so SPIFFS's GC in those writes has its reserve. Compaction on
a consistent store is upkeep, not a repair: by itself it leaves the result
`CLEAN` (`blocks_compacted` still counts it), writes no probe and lights no
LED.

Patches (the new content of imperfect spans) are kept sorted by span and
found by binary search; the unreadable-byte scans walk them patch by patch.
The engine counts these steps (`cpu_steps`, host only): a file with
thousands of damaged spans once cost billions of linear-search steps, minutes
of CPU on the device outside every pacing and deadline check.

### 5.3 Check

Just before the manifest, and again before every later pass, the store is
surveyed afresh (compaction or a restore may have moved pages since),
whatever that shows is normalized, every custody file must be consistent
(R3) and no lookup entry may be mislabeled (1.2 item 13); otherwise the run
is `DEFERRED`: no check this boot. A pass
also starts only when the time left before the soft deadline covers it and
the restore after it: the measured duration of the last pass (plus a
quarter), or 4 s per MiB with the metadata mirror and 8 s without, plus 15 s
and the held bytes at 64 KiB/s (`error_stage` `time` when it does not). Then
a manifest of every live custody file, held or not (attendance, evidence,
operational, dedupe and files of unknown name, in class priority, up to 24:
name, object id, size, CRC, read from flash for an unheld file, and the
queue cursor's generation; the image, whether it was unconfirmed and then
the image a reset rolls back to) is committed and `SPIFFS_check` runs with the engine's callback (counts reports, yields;
never calls SPIFFS: the check holds the lock). No check runs when the
custody files do not all fit in the manifest (`error_stage` `manifest`), or
when the gap list lacks room for one record per entry until ADD has
acknowledged some (`records`): a file left out would have no resume after a
reset in the check, and an entry whose record cannot be kept would settle
with nothing reported. A pass that reported an error makes room and runs again:
level 1 removes probe leftovers, cursor stages and the identity catalog;
level 2 retires fully settled queue files (cursor cleared first, as
`lq_reclaim` does). After a pass that ran out of room the store is
compacted. A pass that fixed nothing ends the checks; one that fixed
something is followed by another, which must fix nothing.

### 5.4 Restore

Custody files are held in PSRAM as far as the budget allows (class
priority, whole files); a hold also notes which bytes nobody could read when
it was taken. A later survey of the same run that cannot read a byte the
hold has (a source page erased meanwhile) takes it from the hold, inside a
merged gap record's bounds too; only the bytes unreadable when the file was
held stay gaps. After a pass that changed something, the held files are
compared and put back before anything else, whatever the soft deadline says
(the copies die with the boot; only the hard deadline ends it):

- A live file is never removed: differing spans get new pages with the
  held bytes, held bytes past its end are appended. When the file's own
  bytes past the hold are unreadable, every span the restore left different
  is one record (flagged merged when they are apart).
- A file the check only began deleting (`SPIFFS_check`'s `DELETE_BAD_FILE`
  clears the header's IXDELE flag and frees nothing itself) comes back by a
  copy of its header with the flag set again: one page, SPIFFS's own page
  move, from the GC reserve when need be (`STORAGE_REPAIR_UNHIDDEN`). Every
  such file, held or not, comes back first, before any rebuild writes:
  SPIFFS's GC in those writes wipes the data pages of an object whose header
  it cannot find. An unheld one that is no longer whole has its unreadable
  bytes recorded first (`UNHELD_LOST`, its own offsets) and the rest comes
  back. An unheld one whose header cannot come back (no erased page at all)
  stays unsettled, the run fails and the manifest stays: the next boot's
  resume brings it back (`STORAGE_REPAIR_RESTORE_DEFERRED`).
- A file the check removed outright is rebuilt under `<name>.rpr`. Its
  manifest entry names the rebuild (`RESTORING`, its object id) before a
  byte is written; the rebuild is read back; a missing tail is recorded
  (`CHECK_CHANGED`) before the partial file takes the name; a rename that
  finds no room leaves it under its temporary name for the next boot. While
  an unsettled entry names it, such a copy is custody of its original's
  kind: never expendable, never purged, and neither normalized nor recorded
  on its own (its unreadable bytes are recorded where its rows go).
- What cannot go back is recorded (`CHECK_CHANGED` for a held file,
  `UNHELD_LOST` for an unheld one whose original is gone), the run fails
  and the manifest stays. An entry is marked reported only once its record
  is kept.

A file the repair itself retires (a settled queue at level 2) loses its hold
and its manifest entry first, so neither the restore nor a resume brings it
back. With normalization the check is not expected to change a custody
file; the hold is the net under that expectation. Files the budget could
not hold are protected by R3 and the manifest (the harness runs every
scenario with no holds).

A reset during the check or the restore: the manifest stays in NVS
(`SRP_STATE_MANIFEST`, phase `RESTORE`) until every entry is settled, and
every boot that finds it resumes, whatever phase a reset left; no check runs
while it is active. The resume compacts first (never a hidden original's
block). Entries settle one by one, each saved as it settles, and an entry
that was rebuilt, renamed or unhidden settles only once a fresh survey reads
the manifest's bytes under its name (three tries, then a gap). Never settled
while the original or the repair's copy of it is on flash and cannot go
back yet: the entry stays and no check runs. Per file, in this order:

1. The live file still starts with the manifest's bytes: settled.
2. The original whole under a hidden header (pages matching the manifest's
   size and CRC): its header comes back. When a new file took the name
   meanwhile (a rolled-back image created it), a text file's original is
   **adopted**: its header comes back under `<name>.rpr` (one page;
   `RESTORING` and `ADOPTED` with its object id first), where SPIFFS's GC in
   any later write moves its pages as any file's instead of wiping them, and
   step 3 merges it. A binary original is recorded whole (`CHECK_CHANGED`).
   Any other object under the temporary name goes first (a rebuild the
   intact original makes redundant, an earlier run's leftover).
3. The manifest's copy under the temporary name (the repair's rebuild or
   the adopted original). Its pages past the committed size count while a
   later span's page follows them: the rebuild writes whole pages and
   SPIFFS commits the size after each write, but SPIFFS's append finalizes a
   new page's header before its bytes, so the last page written when a reset
   came may be torn (its bytes join the recorded tail). A whole copy goes
   into place; a partial one goes into place after the rest is a record
   (`RESTORE_INTERRUPTED`, from its first unreadable byte). When a new file
   has the name, the copy's rows go after the new file's (`merge_rows`):
   - the new file's own damage is normalized first, its own records first:
     an interrupted append's bytes past its end would be ANDed into the
     first merged row (1.2 item 5), an entry past its end into an index;
   - the copy's unreadable bytes, and what it lacks of the original, are
     recorded at the places its rows take in the new file, before a byte is
     written, with where they begin journaled (`MERGING`, `restored`);
   - room for the whole append without SPIFFS's GC (more than three free
     blocks through every page it allocates, made by compaction), or nothing
     is written and the entry stays (`STORAGE_REPAIR_MERGE_DEFERRED`,
     `full`): SPIFFS's GC before normalization is 1.2 item 8's hazard;
   - a later boot continues a merge whose bytes so far read back, or begins
     it again after a file that changed meanwhile (duplicate rows, never lost
     ones);
   - settled only once every merged byte reads back (`STORAGE_REPAIR_MERGED`);
     the copy goes afterwards as an expendable.
4. What is left of the original under a hidden header when pages of it are
   gone (SPIFFS's GC in an older image's writes wipes the data pages of a
   hidden object): superseded only when its owner settled the file (the
   queue cursor's generation moved). Otherwise its unreadable bytes are
   records (its own offsets) and the rest comes back by its header, or is
   adopted and merged as in steps 2 and 3 when a new file has the name.
5. Nothing of it on flash. The repair's own restore was under way
   (`RESTORING`): the bytes it did not verify are a record
   (`RESTORE_INTERRUPTED`). A file its owner replaced since is superseded,
   no gap (`STORAGE_REPAIR_SUPERSEDED`): its queue cursor's generation moved
   (`lq_reclaim` or level 2 retired it), a new object took the name, or the
   manifest is stale: written by another image, or by an unconfirmed image
   (a reset there rolled back, so the old image ran in between). An
   unconfirmed image's manifest is not stale for the confirmed image it
   names as its rollback target: that image resumes it at its first boot,
   before anything else writes (once both run the repair, as after a 2.7.6
   update rolls back to 2.7.5). Anything else is reported with its exact
   range (`CHECK_INTERRUPTED`, `RESTORE_INTERRUPTED`, `CONTENT_UNVERIFIED`),
   once.

An entry whose record does not fit (24 unacknowledged) stays unsettled in
the manifest (`STORAGE_REPAIR_RESUME_BLOCKED`, `DEFERRED`, `records`) until
ADD has acknowledged some.

### 5.5 Finish

- **Cursors** (section 7).
- **Torn tails**: an append target whose unsettled last byte is not `\n`
  gets one `\n` (`rel_open_append` otherwise refuses every later append with
  EBADMSG). A torn command line is left to the inbox merge, and a copy
  waiting under the temporary name is left as it is (its tail is a record
  already: a `\n` there would replace the original's next byte). Not while a
  hidden original an unsettled entry waits for is on flash and three blocks
  or fewer are free (SPIFFS's GC in the append would wipe it).
- **Catalog**: when a catalog file had a gap (or level 1 ran), all five
  catalog files are removed and `file_tx/catalog` is set idle
  (`ft_checkpoint_idle`); journaled when the damage is found, repeated after
  a reset, and left for the next run (transaction untouched) if SPIFFS will
  not remove a file yet. ADD resends the catalog on connect.
- **Probe**: only when the run repaired something: create, write, read back
  and remove `/repair-probe` (`/repair-probe-2` when SPIFFS will not
  recreate the first). A probe that fails only for room reports a full
  store, not a failed repair. No probe on a store left with fewer than two
  free blocks: SPIFFS's GC inside the probe's writes can run out of erased
  pages half way through a block (1.2 item 8) and leave a queue unreadable.
  Such a store with fewer erased pages than cleaning the best block needs is
  handed over as not repaired: `DEFERRED`, `error_stage` `gc_unsafe`, with
  the pages needed and erased (`STORAGE_REPAIR_GC_UNSAFE`), even when the
  run wrote nothing; the application's next writes would run SPIFFS's GC
  into exactly that (section 12). With enough erased pages it stays
  `REPAIRED` with `error_stage` `full`.
- **Expendables with a torn header size** (1.2 item 10; probe files and
  cursor stages) are removed page by page by object id, since SPIFFS cannot
  remove them. A leftover `<name>.rpr` copy is removed once no unsettled
  manifest entry names it, or renamed into place when its file is missing.

## 6. Fills

| Kind | Files | Fill |
|---|---|---|
| JSON rows | pending, blocked, ADD outboxes and their generations, quarantine | NUL; every drain hands a row with NUL to ADD as MALFORMED evidence. When the readable bytes before the gap are a complete JSON object, the first fill byte is `\n`; when the bytes after it are a complete row, the last fill byte is `\n`, so those rows stay attendance. |
| ID lines | `processed_commands.txt`, `add_cancelled.txt`, `acked_uids.txt` | `\n` (a NUL breaks `fgets`/`strchr` in `rel_id_file_contains`) |
| Rollback cursor | `add_live.pos`, `add_pending.pos` | `0...0\n` (replay) |
| Binary | segments, journal, commands, catalog, unknown | NUL (a record CRC or decryption then fails) |

Offsets never move, so lq cursors, dq checkpoints, journal offsets and the
rollback images' `.pos` cursors keep pointing at the same bytes.

## 7. Legacy queue cursors

For `pending.jsonl`, `blocked_identity.jsonl`, the three quarantine files
and both ADD outboxes, the checkpoint is checked against the final bytes
with `lq_open_step`'s own rules (`srp_cursor_verdict`): version and CRC, the
cursor within the file, a version-1 cursor on a row boundary, and the prefix
CRC. A cursor that fails (a settled-prefix gap, or a file 2.4.12 rewrote
after 2.7.x settled part of it, as on 13FL) would wedge the production drains
with DQ_CORRUPT and LOCAL_FAILURE; it is cleared (`lq_checkpoint_cleared`)
and the retained file replays from 0, which ORDS (409) and ADD (event_uid)
settle once. The old 20-byte checkpoint goes to ADD as evidence. An
unreadable NVS entry is never overwritten. With 8 reset records ADD has not
acknowledged, the new record is dropped (counted in `records_dropped`, said
in the `CURSOR_RESET` line) but the cursor is still cleared: a replay costs
no row, a wedged drain stops every row.

### 7.1 Command inbox merge (`command_inbox_merge.c`)

When the repair filled an unreadable span of the command inbox or found its
last line torn (journaled as `SRP_STATE_COMMANDS_DAMAGED`), or a merge file
an interrupted merge left exists, `add_connector` rebuilds the inbox under
its command lock after the VFS mount. Every line that decrypts to a command
is kept once per `command_id`, from the active file, its backup, its stage
and the merge file, in that order. A NUL ends a line as `\n` does: a fill
that swallowed a line's newline must not glue the intact line after it to
the damaged one (a line cut short does not decrypt and is dropped). Nothing
is removed before every kept line is on flash in `/add_commands.mrg`, which
is only ever appended to and synced; then the transaction is set idle, the
backup and stage go, the lines are staged and `ft_replace` installs them,
and the merge file goes last. A reset at any point leaves every command in
a file the next merge reads. The owner-mode `.producer` and `.spare` files
are not merged (that mode is off in 2.7.5). The harness runs this same code
on its emulated SPIFFS.

## 8. Reporting

- **Gap and cursor-reset records** (NVS `storage_repair/gaps`, `/resets`):
  frozen at creation with their own run generation and the file name, and
  rendered byte-for-byte the same on every resend (ADD rejects an evidence
  identity reused with other bytes). Queue `storage_repair`, generation
  `storage-repair-v1-<queue instance>`, record
  `gap:<file>:<offset>:<length>:<run>` or `cursor:<file>:<generation>:<run>`,
  reason MALFORMED (gaps) or LEGACY_RECOVERY (cursor resets). Bounded at 24
  gaps and 8 resets: a full list widens that file's own unacknowledged record
  of the same run (flagged merged, bounds still covering every unreadable
  byte) and never drops one; acknowledged records are reused. Unreadable runs
  less than a page apart (bytes a torn program left scattered) are one record
  flagged merged from the start. A file whose ranges cannot all be recorded
  (24 unacknowledged records of earlier runs) is left as it is and no check
  runs until ADD has acknowledged some (R3).
- **Logs**: `STORAGE_REPAIR_STARTED`, `_GAP`, `_CHECKED`, `_CURSOR_RESET`,
  `_TAIL_TERMINATED`, `_CATALOG_DROPPED` (only when a catalog file was
  removed), `_CATALOG_DEFERRED`, `_COMPACTED`, `_COMPACT_STOPPED`,
  `_RESERVE`, `_FULL`, `_LEVEL2_SKIPPED`, `_GC_UNSAFE`, `_MERGE_DEFERRED`,
  `_RESTORE_DEFERRED`, `_BLOCKED`, `_PROBE_ALTERNATE`, `_SIZE_TORN`,
  `_SIZE_RECOVERED`, `_DEFERRED`, `_NOT_READY`, `_NO_TIME`, `_CHECK_DELETED`,
  `_UNHIDDEN`, `_REBUILT`, `_SUPERSEDED`, `_MERGED`, `_MANIFEST_FULL`,
  `_RECORDS_FULL`, `_RESUME_BLOCKED`, `_MANIFEST_UNREADABLE`, `_NVS_FULL`,
  `_JOURNAL_FAILED`, `_MOUNT_FAILED`, `_DISABLED`, `_COMMANDS_MERGED`,
  `_COMMANDS_FAILED`, `_DONE`, up to 24 lines per boot.
- **Heartbeat** `diagnostics.storage.repair`: result, mode, phases (this run,
  the previous run, the previous boot's breadcrumb, whether its deadline
  fired and whether the repair had returned), counters (sizes recovered,
  stray pages removed and files superseded included), the check's reports
  (including `deleted_live_files`),
  bytes held and the budget, blocks compacted and GC reserve pages used,
  the pages a run deferred for room (`full`) or handed over without room for
  SPIFFS's GC (`gc_unsafe`) needed and had, NVS free and available
  entries before and after, the largest
  internal block before and after, the stack left on app_main, records
  reported, pending and dropped, and up to 8 files with their actions (and,
  for a file a run without room left unreadable, its blocked spans and the
  first one's offset). ADD stores it as the firmware sent it (by alias, so a
  file's `class` keeps its name) and models it
  leniently (`StorageRepairDiagnostics`): an unreadable object is dropped and
  the heartbeat still accepted.

## 9. Testing

- **Plain cc, every shard** (`tests/firmware/storage_repair_plan_host.c`):
  classes, fills, cursor verdicts, records (exact evidence bytes, record ids,
  bounded lists), journal CRCs, the policy matrix, the budget and the mirror
  (including failed programs and erases).
- **ESP-IDF SPIFFS harness** (`tests/firmware/spiffs_repair_host.c`, run by
  `tools/check_spiffs_repair.py` in `espressif/idf:v5.5.3` in the
  `firmware-production` job, about 3 minutes on eight cores; the pytest
  runs a short configuration wherever the ESP-IDF sources are). NOR flash
  with torn programs and erases, NVS blobs that survive cuts, the engine
  through the firmware's ports, and the firmware's own `legacy_queue.c`,
  `file_transaction.c`, `reliability.c` and `command_inbox_merge.c` through
  a stdio shim. A cut tears the write it lands in either as a clean
  prefix or bit by bit across the whole write (NOR programs a page's cells
  in parallel: any byte fully, partly or not programmed). Thirty-one
  scenarios: 13FL's deleted head with and
  without a surviving copy, P02's erased and foreign pages, span mismatch,
  duplicate header and index pages, a missing index page, entries and dirty
  bytes past the end, torn tails, a free lookup entry under a used page,
  erased bytes in a text page, a stopped removal with a damaged probe, stale
  cursors, a damaged catalog, a torn lookup entry ahead of another file's
  pages, a store with one free block whose damaged queue sits in every
  block (normalization from the GC reserve), a probe file with a torn
  header size, a recreated pending queue and a "wb" segment whose first
  append tore the size (five bit patterns), a recreated queue that reuses the
  old object id with a lost page and the old file's page caught mid-delete,
  a binary span whose stale copy and deleted referenced page differ, two
  copies that disagree on one byte, stray pages (0xFD, 0xFE, 0xFF, past the
  end, an index page), a referenced place reused by an unfinalized page, and
  scattered erased bytes, a hidden probe header with a half-cleared lookup
  entry ahead of a queue's pages, a recreated queue whose first append's torn
  size stayed inside its last page (five bit patterns), a queue segment with
  programmed bytes past its end that must take its next `r+b` record, and a
  store with no free block and five erased pages whose damaged queue's work
  fits only without a compaction spare. Oracles: custody byte for byte; exactly the
  unreadable bytes reported (a merged record may cover readable bytes
  between them); the real drain reads every queue to its end;
  appends accepted and a probe file works; no stray page of a live file;
  SPIFFS's own GC on every third block moves nothing a live file reads; a
  second check fixes nothing and a second repair is clean; a whole-file line
  reader still reads every undamaged row; a queue segment takes its next
  "wb"; no live file's last page holds a programmed byte past its end; the
  mirror leaves identical flash and report; caps on HAL operations and on the
  engine's CPU steps (512 per page of the store) per repair. Special runs: a resume from a hidden header with a
  cut at every one of its writes, torn both ways, after which the file must
  be whole with no gap; the check deleting one held file and
  altering another (a test-only hook after each pass), alone, with the soft
  deadline passing during the check, on a store too full to rebuild
  (`CHECK_CHANGED`, manifest kept, settled once next boot) and with cuts
  after it; the check removing a held file on a full store while changing
  an unheld one (the run fails with both records and keeps its manifest);
  manifests superseded by a rolled-back image (queue and non-queue
  files) or by this image's level 2, a hidden original whose name a new file
  took, a file changed in place, and an unconfirmed newer image's manifest
  resumed by the image it rolled back to (current) or by another (stale); a
  settled queue retired at level 2 that
  must stay retired through a restore; no check while a custody file's gap
  cannot be recorded; the NVS headroom, journal and check-time guards.
  Review runs (`review`, the third review round's cases, each failing on the
  engine before its fix; section 11): every custody file in the manifest,
  overflow and record room; a full gap list for the restore and the resume;
  a hidden original's resume at 88% and 80% full and its in-run restore at
  85%, with cuts at sampled writes, torn both ways; a 430 KB hidden queue in
  a store 79% full; a partial rebuild with a cut at every fourth write after
  the check; a healthy store that is only compacted (`CLEAN`, no LED); the
  command inbox merge keeping every intact line across a filled span and
  losing no command with a cut at each of its writes; readable bytes inside
  a merged record's bounds taken from the hold. The fourth round's, each
  failing on the engine before its fix: a stale `FULL` flag (`full-flag`),
  a file with thousands of damaged spans (`cpu`: its CPU time per repair is
  bounded at 8 MiB and more, by hand), level 2 on a store it cannot help
  (`level2`), a store SPIFFS's GC cannot clean handed over (`gc-unsafe`), the
  merge after a new file's dirty tail (`merge-dirty`), on a crammed store
  (`merge-crammed`: kept until there is room, then merged whole), of an
  original SPIFFS's GC partly wiped (`merge-partial`, and superseded when its
  cursor moved), with a cut at every ninth write of the resume at 80% full
  (`merge-cuts`), a whole rebuild whose name a rolled-back image took
  (`rebuild-name`), an unheld file hidden on a store with no erased page
  (`unheld-crammed`, in the run and through a resume), a partial rebuild
  waiting for room under its temporary name while a new file has the name
  (`rebuild-waits`: untouched by the finish's tail termination, then merged),
  and a cut at every
  fifth write of the repair's rebuild (`salvage`; `REVIEW_FULL=1` runs every
  write, with four tears for the rebuild). Also:
  every scenario with no holds and with allocation failing at four levels;
  the boot policy; a power cut at a random write inside the repair
  (sometimes two) until a run finishes, with every readable byte back at its
  offset or named by a gap record, missing or truncated files included; and
  the simulator's workload to a crammed store: odd seeds also retire and
  recreate the pending queue whenever its drain catches up while a blocked
  queue fills the store, and a "wb" segment lane is rewritten; a committed
  row missing after a repair must lie under a gap record created after it
  was committed, the queues must take appends and the segment its next "wb".
  Rows committed after a failed append in the same boot are counted apart
  (1.2 item 9: SPIFFS ANDed them before any repair ran). A queue a run
  without room left unreadable (the run reported the room it needed and the
  queue's blocked spans, and the flash agrees) is checked at the next
  verification; when the workload ends on one, it runs on without cuts
  (three stretches of 200 ticks, each with a boot's repair), and a store
  still wedged is checked with room (the partition grown by 64 erased
  blocks, test only): every row must be there or under a gap record. CI runs
  about 6,000 power cuts over seven 1 MB workload seeds of 2,500 ticks (66 to
  76% live data; seed 832 is the regression of 1.2 item 13), the 8 MB store
  prefilled to 70% with 3,000 ticks of cuts for seeds 14 and 16 (the third
  boot review's crammed store; seed 16 ends wedged), about 470 cut runs
  inside the repair, the special and review runs, and the scenarios on the
  8 MB store, in parallel.
  A mutation check of the engine (dev tool,
  not in CI) confirms the harness fails without the restore, the resume, a
  whole rebuild, the manifest, holds, compaction's pins, R3, the resume's
  CRC check, the source rules, stray removal, size recovery, the merge,
  the time, NVS and journal guards, stale-manifest handling, the restore
  past the soft deadline, the retired-file rule, gap merging and
  compaction's margin.
  Longer soaks (not in CI) ran the workload to 76% live data with a cut
  every 900 and 2,500 flash operations, where the store runs crammed with
  deleted pages: they found 1.2 items 7 to 9, and cuts inside the repair's
  own probe found item 10. With normalization disabled,
  every scenario and the workload fail; without the reserve the full-store
  scenario defers; without the torn-entry hygiene or the torn-size purge
  their scenarios fail. The fourth pass's long run (seeds never used before,
  not in CI): 146 workload seeds (120 at 1 MB and 10 at 2 MB with average
  cut gaps of 150 to 2,500 flash operations and 2,500 to 4,000 ticks; 12 at
  8 MB prefilled to 68 to 72%; 4 at 8 MB of 5,000 to 20,000 ticks), 125,098
  power cuts and 106,238 repairs with 0 committed rows lost and none needing
  a gap record (6 seeds ended wedged and read whole with room); every
  scenario with 20 fresh seeds at 1 MB and 3 at 8 MB; 1,302 cut runs inside
  the repair (40 fresh seeds per scenario at 1 MB, 2 at 8 MB); the special
  and review runs at 2 and 8 MB, the mirror and budget runs at 8 MB. The
  largest repair took
  5.1 M HAL operations of the 10.4 M cap at 8 MB. The fourth round's pass
  (at `4ae1e0b`): CI's configuration in `espressif/idf:v5.5.3` and again
  natively with Apple clang 21 (identical cut and repair counts); the review
  runs at 2 and 8 MB and the special runs at 8 MB; the merge-cut and salvage
  runs at every write (1,362 and 1,692 runs); the third round's every-write
  sweeps again (resume at 88%, 3,146 cuts; at 80%; restore at 85%, 394;
  partial rebuild, 2,434; the review's store layouts for the restore order);
  1,302 fresh cut runs inside the repair at 1 and 8 MB; every scenario with
  10 fresh seeds, the mirror and budget runs at 8 MB; 14 fresh workload seeds
  (12 at 1 and 2 MB with cut gaps of 150 to 900, 4 at 8 MB prefilled), and
  the review's 8 MB seeds 11 to 15 at 20,000 ticks: 0 committed rows lost
  everywhere; seeds 12 and 13 and two fresh prefilled 8 MB seeds (6112,
  6121) end wedged (section 12). The largest repair took 4.9 M HAL operations
  and 3.7 M CPU steps (557 ms of host CPU under the sanitizers) at 8 MB.
- **Contracts** (`test_storage_repair_boot_contract.py`): boot order, CMake
  scope, no format call, the check callback, deadline before mount, no new
  task, PSRAM-only holds, the storage-ready gate, the reporter outside a
  priority hold and reading no NVS with nothing pending.

## 10. Rollback

No format, name, NVS namespace or key that an older image reads changes.
What older images can see: NUL or newline fills in damaged rows, a `\n` after
torn tails, cursors cleared where `lq_open_step` would have rejected them,
the catalog transaction set idle, removed probe and catalog files, and fewer
leaked pages. 2.4.12 and 2.5.2 read the repaired files with their own line
readers; the harness's line-reader oracle checks every undamaged row is still
a row of its own. Older images do not repair: damage they make is repaired
when 2.7.5 runs again.

## 11. Review points and what became of them

Custody review (15 points):

| # | Point | Disposition |
|---|---|---|
| 1 | NUL patch destroys data the check would recover | Accepted. Copies and deleted-but-intact pages are found first; fill only with no source. |
| 2 | IS_FREE patch can delete its own new page | Accepted differently: `spiffs_object_modify` is never used and no referenced page is ever deleted, so a reused page is simply the new one; every new page is read back. |
| 3 | No checkpoint-evidence path for pending/blocked; stale cursors | Accepted: section 7, with the old checkpoint as evidence. Holds are whole files, never derived from a cursor. |
| 4 | `.pos` stale after prefix-less rebuild or L4 | Rejected for 2.7.5: neither exists; offsets never change. A future reformat must write `.pos` = 0. |
| 5 | Finishing a swap after a rollback image ran | Accepted in effect: there is no swap. Residual: a rollback image changing files between an interrupted run and the next 2.7.5 boot yields `CONTENT_UNVERIFIED` records (over-reporting, never deletion). |
| 6 | Interrupted rename: two headers, one object | Accepted: one header per object id is kept before the check; either name is a legitimate state of one rename for `ft_recover`. Covered by the duplicate-header scenario and the workload's renames, not by a cut at every rename call site. |
| 7 | Holds discarded on failure; L3 an ungated partial reformat | Partly: L3 removed; level 2 retires only fully settled queue files. Holds are not kept past the boot: a run that cannot restore a held file records what did not go back as a `CHECK_CHANGED` gap and reports FAILED; R3 and compaction before the restore make that case unexpected. |
| 8 | Avoidable PSRAM-only windows | Accepted: the check never runs on an inconsistent custody file, index entries are remapped without deleting pages, and resume salvages by object id. |
| 9 | Cross-linked pages read as custody | Accepted: object id checked per span; a foreign page is a gap, never patched. |
| 10 | Command inbox fallback | Accepted: merge of active, backup and stage by command_id after `ft_checkpoint_idle`, under the command lock, journaled. The owner-mode `.producer`/`.spare` files are not merged (that mode is off in 2.7.5). Catalog clearing is journaled and idempotent. |
| 11 | Gap evidence not byte-stable; NVS overflow | Accepted, except the SPIFFS overflow file: a bounded NVS list that widens a file's own unacknowledged record keeps exact bounds without a new file. |
| 12 | Two-boot L4 gate unworkable | Accepted: no reformat, no repair restart. |
| 13 | NUL fill turns complete punches into evidence | Partly: row boundaries are kept (section 6). Synthesizing batch rows from damaged lines rejected (evidence keeps the bytes; inventing attendance from damaged batches risks wrong punches). A full-history reconcile after an attendance gap belongs to ADD (the firmware has no local trigger): follow-up. Real 2.4.12/2.5.2 readers in the harness rejected (entangled with the whole firmware); a line-reader oracle stands in. |
| 14 | Dirty tails | Accepted. |
| 15 | Probes every boot; mirror after failure; journal generations | Accepted. |

Boot review (D1-D15):

| # | Point | Disposition |
|---|---|---|
| D1 | No rollback on a hang; unbounded repair | Accepted: timer deadline, soft stop, HAL cap in the harness, report-only reads nothing, guard counts crash/watchdog/deadline ends, normalize-only on an unconfirmed image after an incomplete run. Repairs stay off until ADD installs another image (not until an ADD command clears them: no command exists for that). |
| D2 | ADD supervisor touches /storage during the repair | Accepted: storage-ready gate; merge through add_connector under its lock; contract test. |
| D3 | Restart on an unconfirmed image | Accepted: the repair never restarts the ESP itself (only the deadline does). |
| D4 | Deferred states keep 13FL's blocker | Accepted: alternate probe name; 64 KiB chunks; mirror counted first; a budget shortfall no longer defers the check (harness: no holds). |
| D5 | First-boot PSRAM-only window | Accepted in substance (R3); `.rp` copies not needed. No `gc_quick` or SPIFFS GC from the engine: compaction only (5.2). |
| D6 | GC probe proves nothing | Accepted: no GC probe; a second pass must fix nothing. |
| D7 | NVS footprint | Accepted (second round, B2): about 2.3 KB peak (state, manifest of 12 files, 24 gaps, 8 resets), manifest erased once settled, free entries reported before and after, and a headroom check before any write (section 4). |
| D8 | Reporter task | Accepted: none. |
| D9 | Internal RAM | Accepted. |
| D10 | Stack | Accepted: no large stack buffers (largest frame `srr_run` 1,168 B by `-fstack-usage`); high-water mark reported; HIL gate 1.5 KiB. |
| D11 | Timing basis | Partly: mirror first; HIL measures clean and cloned 13FL/P02 stores. Adjacent span restores are not merged. |
| D12 | Yields | Accepted: in the HAL wrapper. |
| D13 | Stale mirror after failure | Accepted. |
| D14 | Every-boot probe | Accepted. |
| D15 | Invisible repair | Accepted: LED state and RTC breadcrumb. |

Second custody review of the implementation (A1-A11):

| # | Point | Disposition |
|---|---|---|
| A1 | A torn first-append size left the file broken and reported CLEAN; a plausible torn size filled megabytes and spliced old bytes | Accepted: section 5.1 recovers the size from the pages and rewrites the header (also a size left 0xFFFFFFFF over programmed entries, found by the bit-level tear model); no span past the last traced page is filled. Scenarios `torn_size` (five bit patterns, 1 and 8 MB) and `torn_segment`; the workload retires and recreates its queue. |
| A2 | An earlier file's deleted pages spliced in with no gap | Accepted: only the page the index names may be a deleted source; a copy must be finalized with the file's own lookup entry (not mid-delete); erased bytes are filled only from agreeing sources. Scenario `reincarnation` (object id reused, sometimes a mid-delete page of the earlier file). |
| A3 | Binary files took a stale copy | Accepted: the referenced page is a source even when deleted, and sources merge byte by byte (more programmed wins; disagreement is a gap). Scenarios `binary_stale` and `copies_disagree`. |
| A4 | 0xFD pages left for SPIFFS's GC to point an index at | Accepted: every stray page of a live file is deleted before the check and SPIFFS's own writes (section 5.1). Scenario `strays` plus two oracles on every scenario: no stray page of a live file, and SPIFFS's GC over the store moves nothing a live file reads. |
| A5 | A block cleaning that stopped half way was ignored and the check ran on an inconsistent file; restore failure unreported | Accepted: any cleaning that started is surveyed again; the store is surveyed, normalized and checked consistent just before the manifest and before every pass; a restore failure records `CHECK_CHANGED` and keeps the manifest. The cleaning in the reviewer's seeds stopped because SPIFFS's page counters had drifted (1.2 item 12): room now comes from the survey, with a margin to re-point a stopped cleaning's index pages. |
| A6 | A reset during the resume lost the file | Accepted: the manifest flag (not the phase) drives the resume, the phase stays `RESTORE` until it is retired, entries settle one by one and only once verified, and rebuilds go to `<name>.rpr`, lose any duplicate header, and are renamed into place; a live file is never removed for a restore; lookup hygiene runs before the resume. The reviewer's own resume-cut scenario, rerun on this engine, first still lost the file at writes 411 and 412 (the duplicate-header rename) and reported ten whole-file gaps (an allocation over a used page whose lookup said free); both are fixed, and it now ends with all 677 cut points intact. Special run: a cut at every write of the resume, torn both ways, must leave the file whole. |
| A7 | The restore resurrected a file the repair retired | Accepted: the hold and the manifest entry go before the file. Special run with a check that changed something. |
| A8 | A stale manifest produced false gaps | Accepted: the manifest records the image, whether it was unconfirmed, and each queue file's cursor generation; a file replaced by its owner is superseded, not a gap. A hidden original whose name a new file took keeps its rows (appended for text files). Five manifest cases. |
| A9 | The harness missed custody-critical mutations | Accepted: the strict oracle for cuts inside the repair, the per-row oracle for the workload (a gap must lie over the row and postdate it), a workload that retires and recreates its queue and rewrites a "wb" segment, a bit-level tear model, the special runs above, and CI to crammed stores. A mutation check confirms each listed mutation now fails the harness; two remain uncaught by design (removing the `!deferred` term or one of two source filters is masked by the defence that repeats it). |
| A10 | Held files kept a stale "gaps recorded" shortcut | Accepted: a re-plan fills bytes readable when the hold was taken from the hold and records any range not yet recorded this run. Not reachable deterministically in the harness (a source erased between surveys); covered by reasoning. |
| A11 | A check pass started with no time check | Accepted with B1. |

Second boot review of the implementation (B1-B5):

| # | Point | Disposition |
|---|---|---|
| B1 | The soft deadline dropped the holds after a pass; no time check before one | Accepted: the restore after a pass ignores the soft deadline (only the hard one ends it); a pass starts only when its expected duration and the restore fit (section 5.3). Special runs: the soft deadline passing during the check, and a check that would not fit. |
| B2 | No NVS headroom check; a failed first journal save ignored | Accepted: section 4; guards tested. |
| B3 | The reporter read NVS every outbox pass and ignored the priority hold | Accepted: counts in RAM, the call after the hold; contract test. |
| B4 | An unrelated crash after a stopped run counted as a repair failure | Accepted: the breadcrumb records that the repair returned. |
| B5 | 784 B of buffers on the outbox task's stack | Accepted: one PSRAM block per send. |
| - | Version gates (`UG_DIRECT_VERSION`, the 2.7.4 digest) and the HIL reboot window | Outside this change: they belong to tagging 2.7.5 (section 12). |

Found by the strengthened harness while proving the fixes:

| Finding | Fix |
|---|---|
| The repair's own new page, torn and unfinalized at the place an erased referenced page had, was read as that span's bytes (cuts inside the repair, bit-level tears) | Unfinalized pages are never sources (5.1); scenario `reused_ref`. |
| Scattered unreadable bytes filled the 24-record gap list across runs and wedged a file | Runs less than a page apart are one merged record (8); scenario `scattered_bytes`. |
| A first append's header torn with the size still 0xFFFFFFFF but entries programmed lost its row | Recovered like any torn size (5.1). |
| SPIFFS's page counters drift (1.2 item 12); a block cleaning ran out of pages half way (seed 618) | Room counted from the survey, with a margin (5.2). |
| A half-deleted hidden header made the check delete a consistent queue (1.2 item 13, seed 832) | Hidden objects are protected only for the manifest; no check over a mislabeled page; seed 832 in CI. |
| The resume renamed a stale duplicate header, and allocated over a used page whose lookup said free | Section 5.4 and A6. |

Third custody review of the implementation (C1-C7 and its notes, at
`77256f4`). Every fix has a harness run that fails on the engine before it
(each row names that engine) and passes after; the `review` runs are in
CI's first run. The fixes of the fourth pass (after the third round's
fix-up) are `06d8e42` to `cbe9e32`.

| # | Point | Disposition |
|---|---|---|
| C1 | The manifest named the first 12 held custody files in flash order, never `acked_uids.txt` or a file of unknown name; a file left out had no resume, so a reset right after the check hid it lost it silently | Fixed (`653386e`): every live custody file, held or not, in class priority, up to 24 (an unheld file's CRC read from flash); more, and no check runs (`manifest`). Review run `coverage` (13 and 16 files, `acked_uids.txt`, an unknown name, overflow); fails on `77256f4` (`acked_uids.txt` and the unknown name lost whole). |
| C2 | Restoring or resuming a file the check only hid failed on a fuller store, sometimes silently: the rebuild needed the file's size in room, SPIFFS's GC in its writes wiped the hidden original, a partial rebuild took the name before its tail was recorded, and the next boot superseded it as the owner's new file | Fixed (`653386e`, `927f12a`): the header comes back (one page, from the GC reserve if need be), in the run and the resume, held or not; a rebuild is named in its manifest entry before a byte is written and its missing tail recorded before it takes the name; a rename without room keeps it as `.rpr`; the resume compacts first, never takes the repair's own rebuild for a new file, and never settles an entry whose intact original cannot come back yet; a failed restore stays failed beside unheld records (5.4). Review runs `resume` (88% and 80% full, cuts at sampled writes, torn both ways), `restore` (85%), `hidden` (430 KB in a store 79% full), `rebuild` (a cut at every fourth write after the check) fail on `77256f4`; special run "held and unheld restore" fails on `c76afcb`. |
| C3 | With 24 unacknowledged records a dropped record still marked its entry reported or settled | Fixed (`653386e`): an entry settles only once its record is kept; no manifest (no check) unless the list has room for one record per entry (`records`); a failed in-place restore is one record per file. Review run `records`; fails on `77256f4`. |
| C4 | The inbox merge removed the stage and backup before the merged lines were on flash (37 of 38 cut points lost commands) | Fixed (`8d32554`): section 7.1. Review run `window` (a cut at each of the merge's 52 writes, torn both ways, then the next boot's repair and merge): without the merge file it loses a command at the first write. |
| C5 | A gap ending exactly on a line boundary swallowed the newline; the merge dropped the intact line after it | Fixed (`8d32554`): a NUL ends a line. Review run `boundary` (four placements): without it, line 4 at offset 502 (160 bytes) is dropped, the review's exact case. |
| C6 | Dirty tails were cleaned only for append targets; a queue segment's next `r+b` record was ANDed | Fixed (`203b858`): every live file (5.2 item 5), and an oracle on every scenario, cut and budget run that no live file's last page holds a programmed byte past its end. Scenario `segment_dirty_tail`; fails on `653386e`. |
| C7 | A torn first-append size that stayed inside the last data page was taken for a SALVAGE gap and a junk row | Fixed for text files (`203b858`, 5.1). A binary file's size inside its last span is left as it is (its 0xFF bytes are data): it can only over-report. Scenario `torn_size_inside` (five bit patterns); fails on `653386e`. |
| note | A full cursor-reset list dropped the record and still cleared the cursor | Kept, now logged (`1c062a6`): section 7. |
| note | `recover_size` took the last data page without checking its lookup entry | Fixed with C7. |
| note | `restore_in_place` could stop without reporting | Fixed (`653386e`): one record over every span the restore left different. |
| note | "An unconfirmed image's manifest is stale" will be wrong once the image it rolls back to also runs the repair | Fixed (`131c196`): the manifest names the rollback target, which resumes it as current (5.4). Manifest cases 5 (rolled back to this image: `CONTENT_UNVERIFIED`) and 6 (another image: superseded); case 5 fails on `927f12a`'s engine. |
| A1 | Partly (C7 and the `recover_size` note) | Fixed with C7. |
| A3 | "More programmed wins" can take a stale byte from a page SPIFFS was modifying in place | Not changed: only segment sealing modifies a data page in place, and that image (segmented writes) is not the production image (section 12). |
| A6, A8 | Not complete on fuller stores; the supersede rule turned C2 into silent loss | Fixed with C2. |
| A9 | The harness missed C1-C7 | Fixed: the `review` runs and the scenarios `torn_size_inside`, `segment_dirty_tail` and `thin_store` run in CI. |
| A10 | Readable bytes inside a merged record's bounds could still end up filled | Fixed (`06d8e42`): a hold notes which bytes were unreadable when it was taken, and a later survey takes every other byte from the hold (5.4). Review run `merged` (a copy that held the bytes between two scattered runs erased as normalization starts, then compaction and a fresh survey); fails on `927f12a`'s engine at byte 1279, the first readable byte inside the bounds. |

Third boot review of the implementation (E1-E4, at `77256f4`):

| # | Point | Disposition |
|---|---|---|
| E1 | The 8 MB store crammed by the workload (no free block, 6 to 18 erased pages, deleted pages spread thin) deferred every boot with a queue SPIFFS could not read and no record: the reserve rule wanted the work plus a compaction spare, compaction's margin rejected every block; level 2 and `CATALOG_DROPPED` (with no catalog) repeated every boot; a 2.7.5 install would roll back | Partly (`f09b5e7`, 5.2 and 5.5; the fourth boot review's H1 below): the GC reserve whenever the work fits; blocks of only deleted and erased pages erased whatever the margin; no probe file below two free blocks; level 2 not repeated for the same shortage; the catalog untouched when there is none; a run still deferred reports the room it needed and had and each custody file's blocked spans (in the heartbeat and the log, not as gap records: those bytes are on flash and the next run with room makes them readable). Of the review's seeds 11 to 15 (`--kib=8192 --organic=1 --ticks=20000 --gap=2500 --first-seed=0..4`), 11, 14 and 15 pass; 12 and 13 still end wedged (a queue unreadable, every boot deferred; their rows are on flash and read whole with room, 0 lost). An earlier version of this row said all five passed: the harness accepts a wedged store after checking its rows with room, and that was counted as a pass. In CI (`54a3ea9` adds seed 14): seeds 14 and 16 at 8 MB, prefilled to 70% and then 3,000 ticks of cuts; both leave a queue unreadable on `8d32554`'s engine and pass now. Seeds 12 and 16 prefilled end wedged (0 erased pages; their rows checked with room, none lost): a residual of a crammed store (section 12). The workload's allowance for a queue left unreadable for room now ends in a check of every row (`54a3ea9`). |
| E2 | The NVS headroom check counted NVS's reserved page twice | Fixed (`a783bc7`): section 4; the heartbeat reports available entries; ADD accepts them. Plan host test. |
| E3 | Routine compaction on a healthy store reported REPAIRED, wrote a probe and lit the repair LED every boot | Fixed (`54a43da`): compaction alone is upkeep (5.2). Review run `healthy`; fails on `a783bc7` (REPAIRED, two lit phases). |
| E4 | ADD stored a repair entry's `class` as `class_` | Fixed (`c76afcb`): dumped by alias; the unit test asserts the stored key. |

Fourth custody review of the implementation (G1-G6, at `6af714a`). The
review's runs are `review` runs now (section 9), each failing on the engine
before its fix:

| # | Point | Disposition |
|---|---|---|
| G1 | The resume appended a hidden original's rows to the new file a rolled-back image created before the new file's dirty tail was cleaned: the first merged row was ANDed, the run settled and the check freed the original | Fixed (`30fdfca`): `merge_rows` normalizes the new file first (its own records first) and settles only once every merged byte reads back (5.4 step 3). Review run `merge-dirty`; fails on `8b26ad0` (77 of 78 rows). |
| G2 | A merge that failed for room settled the entry, with a record in the original's offsets, and the check freed the intact original | Fixed (`30fdfca`): the original is adopted under the temporary name (one page; SPIFFS's GC moves its pages there instead of wiping them), the room for the whole append without SPIFFS's GC is made or nothing is written and the entry stays, records name the places the rows take in the new file, and where they begin is journaled so a later boot continues or begins again. Review runs `merge-crammed` (kept until there is room, then whole) and `merge-cuts` (a cut at every ninth write of the resume at 80% full; every write by hand, 1,362 runs); both fail on `8b26ad0`. |
| G3 | An original SPIFFS's GC had partly wiped (an older image's writes) was superseded with no record | Fixed (`30fdfca`): a hidden remnant is superseded only when its owner settled the file (the cursor generation moved); otherwise its unreadable bytes are records and the rest comes back or is merged (5.4 step 4). Review run `merge-partial` (and its superseded case); fails on `8b26ad0`. |
| G4 | The repair's own whole rebuild under the temporary name was purged once a rolled-back image created the name again | Fixed (`30fdfca`): the manifest's copy is never purged; its rows go after the new file's. Review run `rebuild-name`; fails on `8b26ad0` (the rebuild purged, a gap from offset 0). |
| G5 | The in-run restore of a file the budget did not hold settled when its header could not come back, and the check freed every byte | Fixed (`30fdfca`): it stays unsettled and the run fails, as the resume does; every hidden original comes back by its header before any rebuild's writes (the review's unreproduced order risk). Review run `unheld-crammed` (in the run and through a resume); fails on `8b26ad0`. |
| G6 | Pages of the rebuild past its committed size were dropped as strays, and a torn first-append size made the copy an expendable that was purged | Fixed (`30fdfca`): the copy is custody of its original's kind while a manifest entry names it, and its pages past the committed size count while a later span's page follows them. SPIFFS's append finalizes a page's header before its bytes, so the last page written may be torn: the review counted it (up to a whole 4,016-byte write); it is recorded instead. Found with it: a header torn to span 0xFFFF wrapped the 16-bit span trace to 0. Review run `salvage` (a cut at every fifth write of the rebuild; every write with four tears by hand, 1,692 runs); fails on `8b26ad0`. |

Fourth boot review (H1-H4, at `6af714a`):

| # | Point | Disposition |
|---|---|---|
| H1 | E1 not resolved: seeds 12 and 13 end wedged (a queue unreadable, every boot deferred, no write possible), the harness accepts it and the doc reported them passing; the repair hands over a store with fewer than two free blocks as REPAIRED; level 2 dropped the catalog and still had no erased page; the doc's wedge percentages, "nothing is deleted" and "the admission budget keeps stores out" were wrong | Open for the owner (section 12, decision 1): a last resort or a runtime write gate. Done (`e2c0c83`): a store handed over with fewer than two free blocks and fewer erased pages than SPIFFS's GC needs to clean a block is `DEFERRED`, `gc_unsafe`, with the room (5.5); level 2 runs only when its removals could let a block be erased (5.2); E1's row and section 12 corrected. Review runs `gc-unsafe` (CLEAN on `30fdfca`) and `level2` (the catalog dropped on `30fdfca`). Seeds 12 and 13 still end wedged with 0 rows lost; the workload now counts the GC-unsafe hand-overs (1 to 6 per seed). |
| H2 | A stale `FULL` flag turned level 2 off for good; a run with no work kept it | Fixed (`d2eab2a`): every run starts with the flag clear and sets it only when it defers for room, with a signature of the shortage; level 2 is skipped only for the same shortage (5.2). Review run `full-flag`; fails on `6af714a` (DEFERRED at level 0 where a fresh journal retires the settled queue). |
| H3 | The engine's CPU time grew with the square of a file's damaged spans (linear patch lookups per byte): 3.9 billion steps, seconds of host CPU, minutes on the device outside every deadline check | Fixed (`8b26ad0`): patches sorted by span, binary search, scans patch by patch; the harness caps the engine's steps at 512 per page in every run. Review run `cpu` at 8 MiB: 6,073 ms per repair on `d2eab2a`, 422 ms (3.7 M steps) now. |
| H4 | The rollback tag came from the other OTA slot even when the bootloader would return to the factory app | Fixed (`d3c4ff6`): the other slot only while an otadata entry the bootloader accepts selects it, otherwise the factory app (section 4). Contract test; fails on `6af714a`. |

Found while fixing the fourth round:

| Finding | Fix |
|---|---|
| A partial rebuild waiting under the temporary name for a merge with room (custody of the queue's kind) had a `\n` appended by the finish's tail termination; the merge took it for the original's next byte, which no record named | `1612af0`: a copy an unsettled entry names is left as it is. Review run `rebuild-waits`; fails on `a32090a`. |
| SPIFFS's append writes a new page's header finalized before its bytes, so "finalized" does not mean "complete" for the last page an interrupted append wrote | The rebuild's salvage stops before that page (G6); the survey reads past a committed size nowhere else. |
| A data page header torn to span 0xFFFF wrapped the 16-bit span trace to 0, so size recovery saw no page | `30fdfca`: spans past the partition are ignored. |

Third verification (V1-V6, at `77256f4`):

| # | Point | Disposition |
|---|---|---|
| V1 | The version gate before tagging: `UG_DIRECT_VERSION` "2.7.4", the fleet's 2.7.4 digest in `ug_direct_predecessor_matches`, `project(zone_lite VERSION 2.7.4)`, the CI direct-legacy step and its marker list | Open, for the 2.7.5 release commit (as `89bb404` did for 2.7.4: also ADD's `storage_contract.py`, `deploy/add/firmware-storage-contract.ps1`, `test_hil_publication.ps1`, `test_storage_upgrade_host.py`). The full 2.7.4 application SHA-256 (`3e4bb613...`, HIL run 38529352136) is not in the repository. |
| V2 | The retired-file special run failed in its own setup at 2 and 8 MB | Fixed (`f09b5e7`): its setup scales with the store; `special` and `review` pass at 2 and 8 MB. The fresh long run of this pass also found `torn_size_inside` stopping in its own setup for fresh seeds (no bit could tear inside the span); fixed in `cbe9e32`. |
| V3 | New test files had no CI duration weights | Fixed (`f02d072`): the seven files without a weight have one, scaled from a local run of the eight shards; the other weights are unchanged. |
| V4 | `test_hikvision_anchor_host.py` failed under gcc 13.3 (`-Werror=misleading-indentation` in its generated `main.c`; also on `main`, skipped by CI) | Fixed (`afa003c`): the statement is on its own line. |
| V5 | `--first-seed` did not change the scenarios' seeds, so "fresh" scenario seeds repeated CI's | Fixed (`54a3ea9`): scenario seeds follow `--first-seed`; CI's runs keep theirs. |
| V6 | A device test | Open: section 12. |

## 12. Open decisions and residual risks

Decisions for the owner:

1. **A wedged store needs a last resort or a write gate (open).** The
   reformat is not implemented (the owner accepted leaving the last resort
   out of 2.7.5), and the fourth boot review (H1) showed what that leaves:
   a store the workload crams until SPIFFS's GC has nothing to move pages
   into wedges (residual risks below), and the 2.7.5 repair cannot get it
   out. The repair now says so at the boot that hands such a store over
   (`DEFERRED`, `gc_unsafe`), before the wedge, but cannot prevent it.
   Options, each needing the owner's decision and its own power-cut harness
   coverage:
   - **A runtime write gate**: the admission check (`qs_local_admit_locked`)
     refuses writes while the store is GC-unsafe (fewer than two free blocks
     and fewer erased pages than the best block's cleaning needs), so
     SPIFFS's GC never starts a cleaning it cannot finish; drains and
     `lq_reclaim` (deletes, no allocation) go on. The runtime sees only
     `esp_spiffs_info` (allocated pages), so it needs the repair's verdict
     or its own read of the lookup pages, and a way to leave the gated state
     without a reboot (the next boot's compaction ends it). New punches wait
     wherever a refused admission leaves them today (the live ceiling's
     behaviour): that is the owner's call.
   - **A RAM-staged compaction** of one block: its live pages copied to a
     power-cut-safe place (none exists on this partition table; NVS is too
     small for a 4 KiB block with its own headroom), the block erased and
     written back without its deleted pages.
   - **A reformat** with every custody byte held (or every queue file
     settled or empty with a verified cursor): confirmed image only,
     operational and dedupe files held, `.pos` written as 0.
2. **`CONFIG_SPIFFS_USE_MTIME=n`.** Not changed. Every writable open
   rewrites the header to update the time (another page move per append, 1.2
   item 1); turning it off removes that window and changes no format.
3. **Every-boot policy.** Implemented. Keep it if HIL measures the clean-store
   run at a few seconds (the mirror's estimate) and the damaged-store run
   well inside 5 minutes.
4. **The attempts guard** disables repairs for an image after three runs
   ended by a crash, a watchdog or the deadline, until ADD installs another
   image. An ADD command to re-enable would need firmware and ADD work.
5. **Appends after a failed append (1.2 item 9).** Not changed in 2.7.5:
   the repair runs at boot and cannot undo an AND made at runtime. Proposal
   for the next release: `reliability.c` latches a file whose append failed
   and refuses further appends to it until the next boot's repair has
   cleaned its tail (`storage_repair_note_damage` already marks the boot),
   so the row stays with its producer instead of being ANDed. An AND of two
   rows can in principle still parse as a row with wrong field values; this
   is the strongest reason to make the change.

Residual risks:

- None of this has run on a device. HIL must measure duration (clean,
  cloned 13FL and P02 images), stack high-water mark, NVS free and available
  entries and the largest internal block, all in the heartbeat. The
  every-boot repair must fit the controlled-reboot test's recovery window
  (`hil_reboot.py`).
- Before tagging (section 11, V1): the direct-legacy role checks
  `UG_DIRECT_VERSION "2.7.4"` (`upgrade_guard.h`); the fleet's 2.7.4 digest
  must be added to `ug_direct_predecessor_matches`, ADD's storage contract
  and the CI contract marker, `project(VERSION)` bumped and the CI
  direct-legacy step moved to 2.7.5, or a 2.7.5 direct-legacy image fails its
  storage upgrade check and rolls back.
- **A wedged store (decision 1).** The harness's workload has no admission
  budget and fills until SPIFFS refuses. On such a crammed store SPIFFS's GC
  in the application's writes can stop half way again after a repair (1.2
  item 8) until no erased page is left, with a damaged queue whose pages are
  in every block. The review's seeds 12 and 13 (8 MB, 20,000 ticks) end that
  way, as do CI's seed 16 prefilled and 6 of the third pass's 146 fresh
  seeds; the review measured the stores at 70.4 to 77.4% used when they
  wedged (the figures the harness prints at the end, 68 to 75%, are after it
  grows the partition to check the rows). Then no write gets room: the repair
  cannot compact any block (each holds the damaged file's pages) and has no
  page for the work, and SPIFFS's own GC has nothing to move pages into.
  Every boot defers and reports the queue's blocked spans; ADD's resend of a
  catalog cannot be stored either. The harness proves the queue's rows are
  all on flash for every seed that ends this way (given room, one repair
  makes it read whole, no row lost), but the device cannot deliver them: no
  new punch can be stored, a 2.7.5 install rolls back (its storage health
  cannot pass), and the device stays wedged on the old image too. The
  runtime's admission budget does not keep stores out of this state: live
  writes stop at about 68% of an 8 MB store (75% less 512 KiB), but
  recovery-class writes (transfers into `/pending.jsonl`, receipts, command
  settlement) are admitted up to 75%, and the wedges came at 70 to 77%. The
  boot that hands such a store over now reports it (`gc_unsafe`: 1 to 6 such
  hand-overs per 8 MB seed of 20,000 ticks), and level 2 no longer drops the
  catalog for nothing; the store is still not saved (decision 1 above).
- The resume's merge of an original (or the repair's copy) after a new
  file's rows begins again after a file that changed meanwhile, so a merge
  interrupted and then appended to by a rolled-back image can deliver some
  rows twice (ORDS and ADD settle duplicates once); a merge that cannot get
  room for the whole append without SPIFFS's GC waits (the entry stays, no
  check runs) for a boot with room.
- A hidden original whose header cannot come back at all (no erased page,
  not even the GC reserve's) stays hidden: the application's SPIFFS GC can
  still wipe its data pages before a later boot, which then records what is
  gone. Adoption under the temporary name (one page) protects every other
  case.
- The repair's rebuild interrupted by a reset loses its last written page
  (SPIFFS's append finalizes a page before its bytes, so it may be torn): up
  to 251 bytes, recorded as `RESTORE_INTERRUPTED`.
- A reset during the first 2.7.5 boot returns to the old image, which keeps
  writing. A manifest that boot left is an unconfirmed image's: what no
  longer matches is superseded, not reported, unless anything of the
  original is still on flash (its hidden header, whole or not, or the
  repair's copy of it); what is left of an original that is no longer whole
  is superseded only when its queue cursor moved. A file that only changed in place after an
  interrupted run on a confirmed image is reported whole
  (`CONTENT_UNVERIFIED`): the manifest keeps one CRC per file.
- Later releases: 2.7.5 resumes an unconfirmed newer image's manifest as
  current when it is that image's rollback target, so a newer image must keep
  the manifest layout and `SRP_STATE_VERSION` (2.7.5 retires a manifest it
  cannot read with `STORAGE_REPAIR_MANIFEST_UNREADABLE` and no records).
- Holds do not survive the boot: if the check unexpectedly altered a held
  file and the restore failed, those bytes are reported, not kept; a later
  boot puts them back only if the store still holds them (a hidden original).
- The torn-size recovery picks, for a binary file, the smallest size at or
  after its last programmed byte that the torn size contains: a first append
  whose data ended in 0xFF bytes past such a size loses them; a torn binary
  size inside the last span is left, so its erased tail is reported as a gap
  (over-reporting). Text files (every queue) end where their bytes end.
- A gap list of 24 records ADD has not acknowledged blocks recording new
  damage and any check: such a file stays as it is, a manifest entry stays
  unsettled, and the check waits until ADD acknowledges some. A full
  cursor-reset list drops the evidence record (counted, logged) but still
  clears the cursor.
- The time estimate for a check pass is a constant per MiB until a pass is
  measured; HIL should confirm 4 s/MiB with the mirror and 8 s without.
- Damaged durable-queue or journal segment records still hold their lane
  with DQ_CORRUPT (unchanged behaviour). A segment's in-place sealing is the
  one place SPIFFS modifies a data page in place; a stale copy of such a page
  can win the byte merge (A3). Segmented writes are not the production image.
- SPIFFS's own GC in the application's writes can still stop half way on a
  crammed store (1.2 item 8). The repair leaves three free blocks where
  compaction can make them, takes the GC reserve when nothing else fits the
  work, and the next boot repairs what such a GC leaves; a store with fewer
  than two free blocks gets no repair probe file, and one with too few
  erased pages for SPIFFS's GC to clean a block is reported `gc_unsafe`.
- The nucleus's own allocation runs `gc_quick` when free blocks drop below
  two outside the reserve path; it can erase a block of deleted pages that
  were byte sources. The plan and the hold already have those bytes in RAM,
  so only a reset in that window turns them into a recorded gap.
- A truncated file's deleted pages are read back as its content when its
  header still names them (an interrupted truncate keeps the old bytes);
  duplicates replay idempotently at ORDS and ADD.
- The command inbox merge keeps at most 512 commands and the inbox's size
  limit; the owner-mode `.producer` and `.spare` files are not merged.
