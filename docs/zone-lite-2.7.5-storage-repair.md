# Zone Lite 2.7.5: custody-first SPIFFS repair

Status: implemented on `release/zkt-2.7.5` (not yet versioned, not on HIL).
This document describes what was built. It replaces the first design
(`94136b3`), which two adversarial reviews found unsafe as written, and the
first implementation, which two more reviews found unsafe in eleven and five
places; section 11 lists every review point and what became of it.

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
4. `storage_repair_after_mount()`: the command inbox merge (7.3).
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
- **NVS headroom**: `nvs_get_stats` before the run. The run starts only with
  room to create each journal blob that does not exist yet, to rewrite the
  largest once, and a page (126 entries) for NVS's own GC; otherwise it only
  reports (`REPORT_ONLY`, `error_stage` `nvs_full`), so it never takes the
  room the runtime's checkpoint commits need. A run whose first journal save
  fails stops before any write (`FAILED`, `journal`).
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
id as before.


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
5. The last page of an append target is rewritten when bytes past the end
   are not erased (1.2 item 5).

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

When the damaged files have pages in every block, nothing can be compacted
around them. If their work fits in the erased pages SPIFFS keeps for its GC,
with what compacting the best block will need to spare, normalization
allocates from them as a GC does (`reserve_pages`), and compaction, possible
once the files are consistent, gives them back. Otherwise room is made
(probe leftovers, cursor stages, the catalog, fully settled queue files),
compaction tried again and normalization retried; a store still full defers
the check (`DEFERRED`, `error_stage` full). Before the check and before the
finish's own SPIFFS writes the engine compacts back to three free blocks
where it can, so SPIFFS's GC in those writes has its reserve.

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
a manifest of the held custody files (name, object id, size, CRC, the queue
cursor's generation, the image and whether it was unconfirmed) is committed
and `SPIFFS_check` runs with the engine's callback (counts reports, yields;
never calls SPIFFS: the check holds the lock). A pass that reported an error makes room and runs again:
level 1 removes probe leftovers, cursor stages and the identity catalog;
level 2 retires fully settled queue files (cursor cleared first, as
`lq_reclaim` does). After a pass that ran out of room the store is
compacted. A pass that fixed nothing ends the checks; one that fixed
something is followed by another, which must fix nothing.

### 5.4 Restore

Custody files are held in PSRAM as far as the budget allows (class
priority, whole files). After a pass that changed something, the held files
are compared and put back before anything else, whatever the soft deadline
says (the copies die with the boot; only the hard deadline ends it). A live
file is never removed: differing spans get new pages with the held bytes,
held bytes past its end are appended. A file the check removed is rebuilt
under `<name>.rpr`, read back, and renamed into place, so a reset never
leaves a partial file under the real name. What cannot go back is recorded
(`CHECK_CHANGED`, exact range) before the run fails, and the manifest stays.
A file the repair itself retires (a settled queue at level 2) loses its hold
and its manifest entry first, so neither the restore nor a resume brings it
back. With normalization the check is not expected to change a custody
file; the hold is the net under that expectation. Files the budget could not
hold are protected by R3 alone (the harness runs every scenario with no
holds).

A reset during the check or the restore: the manifest stays in NVS
(`SRP_STATE_MANIFEST`, phase `RESTORE`) until every entry is settled, and
every boot that finds it resumes, whatever phase a reset left; no check runs
while it is active. Entries settle one by one, each saved as it settles, and
an entry that was rebuilt or renamed settles only once a fresh survey reads
the manifest's bytes under its name (three tries, then a gap). Per file: the
live file still starts with the manifest's bytes (intact); a whole rebuild
under the temporary name is renamed into place, after a duplicate header of
it is removed (`SPIFFS_rename` takes the first header of the name it meets,
and a reset during the rebuild's last header update leaves two); a hidden
original (the check had begun deleting it, pages intact) is rebuilt from its
own pages when they match the manifest's size and CRC, and when a new file
took the name meanwhile, a text file's intact rows are appended after the new
rows (`STORAGE_REPAIR_MERGED`); a file whose owner replaced it since is
superseded, no gap (`STORAGE_REPAIR_SUPERSEDED`): its queue cursor's
generation moved (`lq_reclaim` or level 2 retired it), or the manifest is an
unconfirmed image's or another image's (a reset there rolled back, so the
old image ran in between); anything else is reported with its exact range
(`CHECK_INTERRUPTED`, `RESTORE_INTERRUPTED`, `CONTENT_UNVERIFIED`), once.

### 5.5 Finish

- **Cursors** (section 7).
- **Torn tails**: an append target whose unsettled last byte is not `\n`
  gets one `\n` (`rel_open_append` otherwise refuses every later append with
  EBADMSG). A torn command line is left to the inbox merge.
- **Catalog**: when a catalog file had a gap (or level 1 ran), all five
  catalog files are removed and `file_tx/catalog` is set idle
  (`ft_checkpoint_idle`); journaled when the damage is found, repeated after
  a reset, and left for the next run (transaction untouched) if SPIFFS will
  not remove a file yet. ADD resends the catalog on connect.
- **Probe**: only when the run wrote: create, write, read back and remove
  `/repair-probe` (`/repair-probe-2` when SPIFFS will not recreate the
  first). A probe that fails only for room reports a full store, not a
  failed repair.
- **Expendables with a torn header size** (1.2 item 10; probe files and
  cursor stages) are removed page by page by object id, since SPIFFS cannot
  remove them. A leftover `<name>.rpr` rebuild is removed once no manifest
  names its file, or renamed into place when that file is missing.

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
unreadable NVS entry is never overwritten.

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
  `_TAIL_TERMINATED`, `_CATALOG_DROPPED`, `_COMPACTED`, `_COMPACT_STOPPED`,
  `_RESERVE`, `_FULL`, `_PROBE_ALTERNATE`, `_SIZE_TORN`, `_SIZE_RECOVERED`,
  `_DEFERRED`, `_NO_TIME`, `_CHECK_DELETED`, `_SUPERSEDED`, `_MERGED`,
  `_MANIFEST_UNREADABLE`, `_NVS_FULL`, `_JOURNAL_FAILED`,
  `_DISABLED`, `_COMMANDS_MERGED`, `_DONE`, up to 24 lines per boot.
- **Heartbeat** `diagnostics.storage.repair`: result, mode, phases (this run,
  the previous run, the previous boot's breadcrumb, whether its deadline
  fired and whether the repair had returned), counters (sizes recovered,
  stray pages removed and files superseded included), the check's reports
  (including `deleted_live_files`),
  bytes held and the budget, blocks compacted and GC reserve pages used,
  NVS free entries before and after, the largest
  internal block before and after, the stack left on app_main, records
  reported and pending, and up to 8 files with their actions. ADD models it
  leniently (`StorageRepairDiagnostics`): an unreadable object is dropped and
  the heartbeat still accepted.

## 9. Testing

- **Plain cc, every shard** (`tests/firmware/storage_repair_plan_host.c`):
  classes, fills, cursor verdicts, records (exact evidence bytes, record ids,
  bounded lists), journal CRCs, the policy matrix, the budget and the mirror
  (including failed programs and erases).
- **ESP-IDF SPIFFS harness** (`tests/firmware/spiffs_repair_host.c`, run by
  `tools/check_spiffs_repair.py` in `espressif/idf:v5.5.3` in the
  `firmware-production` job, about 25 s; the pytest runs it wherever the
  ESP-IDF sources are). NOR flash with torn programs and erases, NVS blobs
  that survive cuts, the engine through the firmware's ports, and the
  firmware's own `legacy_queue.c`, `file_transaction.c` and `reliability.c`
  through a stdio shim. A cut tears the write it lands in either as a clean
  prefix or bit by bit across the whole write (NOR programs a page's cells
  in parallel: any byte fully, partly or not programmed). Twenty-eight
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
  entry ahead of a queue's pages. Oracles: custody byte for byte; exactly the
  unreadable bytes reported (a merged record may cover readable bytes
  between them); the real drain reads every queue to its end;
  appends accepted and a probe file works; no stray page of a live file;
  SPIFFS's own GC on every third block moves nothing a live file reads; a
  second check fixes nothing and a second repair is clean; a whole-file line
  reader still reads every undamaged row; a queue segment takes its next
  "wb"; the mirror leaves identical flash and report; a cap on HAL
  operations per repair. Special runs: a resume from a hidden header with a
  cut at every one of its writes, torn both ways, after which the file must
  be whole with no gap; the check deleting one held file and
  altering another (a test-only hook after each pass), alone, with the soft
  deadline passing during the check, on a store too full to rebuild
  (`CHECK_CHANGED`, manifest kept, settled once next boot) and with cuts
  after it; manifests superseded by a rolled-back image (queue and non-queue
  files) or by this image's level 2, a hidden original whose name a new file
  took, and a file changed in place; a settled queue retired at level 2 that
  must stay retired through a restore; no check while a custody file's gap
  cannot be recorded; the NVS headroom, journal and check-time guards. Also:
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
  (1.2 item 9: SPIFFS ANDed them before any repair ran). CI runs about 6,000
  power cuts over seven workload seeds of 2,500 ticks (66 to 76% live data;
  seed 832 is the regression of 1.2 item 13), about 400 cut runs inside the
  repair, and the scenarios on the 8 MB store, in parallel (about a minute).
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
  their scenarios fail.
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

## 12. Open decisions and residual risks

Decisions for the owner:

1. **Reformat.** Not implemented. No harness case needed it once
   normalization, orphan removal and the room-making levels were in place.
   If one is wanted later: confirmed image only, every queue file settled or
   empty with a verified cursor, operational and dedupe files held, `.pos`
   written as 0, its own power-cut harness coverage.
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
  cloned 13FL and P02 images), stack high-water mark, NVS free entries and
  the largest internal block, all in the heartbeat. The every-boot repair
  must fit the controlled-reboot test's recovery window (`hil_reboot.py`).
- Before tagging: the direct-legacy role checks `UG_DIRECT_VERSION "2.7.4"`
  (`upgrade_guard.h`); the fleet's 2.7.4 digest must be added to
  `ug_direct_predecessor_matches` and the CI contract marker, or a 2.7.5
  direct-legacy image fails its storage upgrade check and rolls back.
- A reset during the first 2.7.5 boot returns to the old image, which keeps
  writing. A manifest that boot left is an unconfirmed image's: what no
  longer matches is superseded, not reported, unless the original is still
  intact in a hidden object. A file that only changed in place after an
  interrupted run on a confirmed image is reported whole
  (`CONTENT_UNVERIFIED`): the manifest keeps one CRC per file.
- Holds do not survive the boot: if the check unexpectedly altered a held
  file and the restore failed, those bytes are reported, not kept; a later
  boot puts them back only if the store still holds them (a hidden original).
- The torn-size recovery picks, for a binary file, the smallest size at or
  after its last programmed byte that the torn size contains: a first append
  whose data ended in 0xFF bytes past such a size loses them. Text files
  (every queue) end where their bytes end.
- A gap list of 24 records ADD has not acknowledged blocks recording new
  damage; such a file stays as it is (and the check waits) until ADD
  acknowledges some.
- The time estimate for a check pass is a constant per MiB until a pass is
  measured; HIL should confirm 4 s/MiB with the mirror and 8 s without.
- Damaged durable-queue or journal segment records still hold their lane
  with DQ_CORRUPT (unchanged behaviour).
- SPIFFS's own GC in the application's writes can still stop half way on a
  crammed store (1.2 item 8). The repair leaves three free blocks where
  compaction can make them, and the next boot repairs what such a GC leaves;
  a store whose deleted pages are spread so thinly that no block gains stays
  crammed, and the runtime's admission budget is what keeps stores out of
  that state.
- The nucleus's own allocation runs `gc_quick` when free blocks drop below
  two outside the reserve path; it can erase a block of deleted pages that
  were byte sources. The plan already holds those bytes in RAM, so only a
  reset in that window turns them into a recorded gap.
- A truncated file's deleted pages are read back as its content when its
  header still names them (an interrupted truncate keeps the old bytes);
  duplicates replay idempotently at ORDS and ADD.
