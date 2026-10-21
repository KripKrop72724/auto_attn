# Zone Lite 2.7.6: ZKT platform HIL

2.7.6 is 2.7.5 plus the last resort for wedged stores: when the boot storage
repair is about to hand over a store without room, it reformats blocks in
place, one 4 KiB block at a time, keeping every page anything may still read
byte for byte at its own place and erasing only deleted pages nothing reads.
Each block's kept pages are first written to a verified redo record in the
384 KiB of flash past the last partition, so a reset at any write is
completed by the next boot before anything mounts the store. It accepts 2.7.5
as a rollback predecessor. The design, its rules, the power-cut harness and
the review history are in
[zone-lite-2.7.6-last-resort-reformat.md](zone-lite-2.7.6-last-resort-reformat.md).
The [2.7.5 runbook](zone-lite-2.7.5-hil.md) still applies to the repair's
report, and the [2.7.1 runbook](zone-lite-2.7.1-hil.md) to publication,
campaigns, acceptance evidence and rollback.

## Why

On 20 October 2026 2.7.5 confirmed on Peshawar 06 in place (`CLEAN` in
8.0 s, check 3.9 s, 1.55 MB used). On Peshawar 02 (from 2.5.2) its repair
made every custody file readable but ended `DEFERRED`:

- `error_stage` `full`, `error_code` -10001, 21.3 s, check skipped;
- used 4,976,828 -> 4,962,772 of 7,703,441 bytes; 6 files, 3 held
  (3,577,196 bytes of a 4,357,816-byte budget);
- 153 spans rewritten, 51 orphan index pages, 4 strays and 1 duplicate
  removed, 1 cursor reset, 0 gaps, 0 unreadable bytes;
- `room_needed_pages` 1, `room_erased_pages` 0, `blocks_compacted` 0;
- `STORAGE_REPAIR_LEVEL2_SKIPPED` (3 pages needed, 0 erased),
  `STORAGE_REPAIR_FULL` (1 page needed, 0 erased), `STORAGE_REPAIR_DEFERRED`.

`/pending.jsonl` (2,526,978 bytes) was fully readable, but with no erased
page and no block SPIFFS could garbage-collect, the persistence probe still
got ENOENT, boot health failed and the image rolled back to 2.5.2 after the
900 s budget (09:43:19, `BOOT_HEALTH_TIMEOUT`). On 2.5.2 its ORDS drain now
fails at the pending rewrite with errno 2 (no space). SLICTOWER 13FL (2.4.12,
61% used, `blocked_identity.jsonl` unreadable from about 50 KB) is probably
wedged the same way. This is the crammed-store case of the 2.7.5 design's
decision 1; the owner approved a last-resort reformat on 16 October and again
on 20 October, custody first: every readable unsettled byte survives or
reaches ADD as exact gap evidence.

## What 2.7.6 does

**Every boot, before any mount** (`storage_repair_boot`):

1. Discover the redo area (0xFA0000, 96 sectors, about 50 ms): `READY` only
   when the chip is 16 MiB, every partition ends at or below 0xFA0000, and
   every sector reads erased or as our own record. Otherwise `FOREIGN`,
   `ABSENT`, `OVERLAP`, `UNREADABLE` or `NO_MEMORY`, and nothing is written
   there.
2. Recovery on raw flash: a record a reset left open is completed (its block
   redone from the record) or, when another image may have mounted the store
   since, kept as a byte source for the run (design section 10).
3. 2.7.5's repair, unchanged (survey, gap records, normalization, the check,
   the restore, compaction, finish, probe).

**The round.** When that run would end without room (`trigger` `full`,
`gc_unsafe`, `check_full` or `restore`), it picks blocks whose kept pages are
all safe to rewrite in place, reformats them one by one, remounts and runs the
repair again: at most two rounds and three runs per boot, inside the same
deadlines as 2.7.5 (5 min hard and 255 s soft on an unconfirmed image, 15 min
on a confirmed one). It never calls `SPIFFS_format`, never moves a queue byte
and never touches ADD outboxes and their `.pos`, the command inbox, the
dedupe files, journal or queue segments, files of unknown name or the last
data page of any file.

**Confirmed or unconfirmed.** An unconfirmed first boot (P02 and 13FL, from
their old images) takes only blocks holding no page of any file: deleted pages
no index names and leaked pages of objects with no header. A reset there costs
the rollback image nothing it can read. Blocks that hold custody wait for a
confirmed boot.

**2.7.5 as a rollback predecessor.** 2.7.6 accepts the exact signed 2.7.5
(application SHA-256 `430e2daf29dd99ff006a476ff584e4201493d661cee67e4b4f9ba15545b17a40`,
HIL run 39371984527, git `c5b4d7a`) as well as 2.7.2, 2.7.3 and 2.7.4 in its
rollback slot, in the device guard and in ADD's storage contract. The digest
is ADD's published `zone-lite-2.7.5` record, and Peshawar 06 and Karachi 01
report the same running image. The compiled marker ends
`,2.6.15,2.7.2,2.7.3,2.7.4,2.7.5`: 15 qualified predecessors, 148 characters
of the 160 ADD accepts in `storage.upgrade_contract`. Two more 2.7
predecessors fit before that limit has to grow (ADD first).

## Boot cost

Not measured on a device. From the design (section 12) and the host harness:

| What | Expected |
|---|---|
| Discovery of the redo area | about 50 ms, every boot |
| One block reformatted | about 115 ms; 0.9 s worst, 1.8 s with two retries (G3 reserves 2.5 s) |
| A healthy store (no round) | 2.7.5's figure plus discovery: P06 took 8.0 s on 2.7.5 |
| P02's first boot, unconfirmed | about 60 to 90 s: 2.7.5's 21 s run, the round, then a second run with the check over 8 MB (deleting about 1.4 MB of leaked pages), the restore comparison of 3.58 MB held, compaction and the probe |

The host's check pass after a round on the 8 MiB P02 store reads every page
header about seven times per 512-page range (15.7 million HAL reads); on the
device those are metadata-mirror hits, so HIL must record `check_ms`. The
900 s confirmation budget starts after storage, as in 2.7.5.

### Controlled-reboot window

Unchanged from 2.7.5: `hil_reboot.py`'s controlled reboot belongs to the 2.7.0
journal writer, which builds neither the repair nor the round, and only that
image is reboot-capable or admitted.
`test_controlled_reboot_window_never_meets_the_every_boot_repair` now pins
2.7.6 among the platform images that cannot take it.

## What to watch in ADD

The heartbeat's `diagnostics.storage.repair` is now the boot's report over
every run: the last run's outcome (`result`, `mode`, `error_stage`, holds, the
store it leaves), the first run's starting point (`used_before`,
`previous_phase`), the work of every run added up (spans rewritten, strays,
cursors reset, gaps, check passes), the room figures of the last run that
lacked room, and each file's actions merged. Read the
[2.7.5 table](zone-lite-2.7.5-hil.md#what-to-watch-in-add) for those fields;
2.7.6 adds:

| Field | Expect | Stop and investigate |
|---|---|---|
| `level` | 3 when a round reformatted a block | |
| `runs` | 1 without a round; 2 or 3 with one | |
| `reclaim.area` | `READY` | `FOREIGN` (with `area_foreign_sectors`, `area_first_foreign`, `area_foreign_crc`; design decision 2), `UNREADABLE`, `NO_MEMORY`, `ABSENT`, `OVERLAP` |
| `reclaim.trigger` | absent on a healthy store; `full` on P02 | |
| `reclaim.short` | absent | `candidate` (not enough eligible blocks), `room`, `time`, `cap`, `slot` or `slots`, `nvs`, `journal`, `magic`, `read`, `memory`, `area`, `geometry` |
| `reclaim.rounds`, `blocks`, `pages_freed`, `pages_kept`, `erased_after` | 1 or 2 rounds; `erased_after` at least the target (P02: about 45 pages) | `blocks` 0 with a `trigger` |
| `reclaim.candidates`, `unconfirmed_blocks`, `custody_blocks`, `index_blocks`, `excluded_blocks`, `low_magic_blocks` | the exposure histogram, sent with a `trigger`: on an unconfirmed image only `unconfirmed_blocks` can be taken | `unconfirmed_blocks` 0 on an unconfirmed boot (decision 3) |
| `reclaim.redone`, `sources`, `settled`, `reported`, `records_open` | 0 on a first boot without a reset | `reported` above 0 (bytes recorded as lost), `records_open` above 0 after a boot that read the area |
| `reclaim.failed`, `held_mismatch` | 0 | anything above 0 |
| `reclaim.ms`, `duration_ms`, `check_ms` | P02: `duration_ms` about 60 to 90 s over all runs, the round itself seconds | anything near the 255 s soft deadline |
| `stack_free_bytes` | at least 2,048 (about 2.5 KB expected on the deepest path) | under 2,048 |
| `entries[].actions` | `REDO_SOURCE` only after a reset, a rollback or a block that would not verify (bytes put back from a redo record) | |

Device logs (24 lines per boot over all runs; `log_lines_dropped` counts the
rest, the heartbeat report is complete): `STORAGE_REPAIR_RECLAIM_EXPOSURE`
(the histogram, before anything is written), `STORAGE_REPAIR_RECLAIM` (n
blocks reformatted for the trigger, pages erased and kept, erased pages now),
`STORAGE_REPAIR_RECLAIM_REDONE`, `STORAGE_REPAIR_RECLAIM_SETTLED`,
`STORAGE_REPAIR_RESTORE_WAITING` and `STORAGE_REPAIR_RESTORE_RETRIED` say what
it did; `STORAGE_REPAIR_RECLAIM_SHORT` and
`STORAGE_REPAIR_RECLAIM_UNAVAILABLE` say what it could not do;
`STORAGE_REPAIR_RECLAIM_FAILED`, `STORAGE_REPAIR_RECLAIM_MISMATCH` and
`STORAGE_REPAIR_RECLAIM_SOURCES` (ERROR) need a look.

Queue evidence: as 2.7.5 (`storage_repair`, one record per unreadable range,
reason `MALFORMED`). A redo record's page that could neither go back nor
settle by a verified cursor offset is reported once with cause
`REFORMAT_INTERRUPTED`, and its bytes stay in the redo area.

## Release and HIL

Prerequisites: the owner's word on the open decisions of design section 15
(3, 5 and 6 at the time of writing), review, and a green `main`. Then publish
2.7.6 like 2.7.5: `firmware-hil-candidate.yml` with family `zkt`, the green
main SHA, version `2.7.6` and the ordered nationwide targets. It is
`HIL_ONLY`. Keep 2.7.2 to 2.7.5 published: ADD offers 2.7.6 to a device
running one of them only while that exact release is published. Only one
`ACTIVE` or `PAUSED` campaign is allowed per zone: cancel a leftover one
first (never while the device is `OFFERED` or `DOWNLOADING`). The fleet
reservation guard allows two unsettled upgrades at a time.

One target at a time; start the next only once the previous one is accepted
or its failure is understood.

### 1. Peshawar 02, from 2.5.2 (unconfirmed first boot)

Before the campaign, note P02's last 2.5.2 heartbeat (store used, the ORDS
drain error) and that its running image is the exact 2.5.2. Its rollback slot
will hold 2.5.2: the OTA writes 2.7.6 over the slot that held 2.7.5. Avoid
power-cycling the device during the first few minutes after the OTA restart
(the LED shows `STORAGE_REPAIR`, fast white and blue, while the repair
writes).

**What to expect on the first boot.** The first run is 2.7.5's: about 21 s,
the same report as on 20 October unless 2.5.2 changed the store since (it had
no room, so little), ending without room. Then:

1. `STORAGE_REPAIR_RECLAIM_EXPOSURE`: the histogram, with "unconfirmed image".
   The harness's P02 model has 731 blocks holding nothing any image reads out
   of 2,048; the real number is first known here.
2. `STORAGE_REPAIR_RECLAIM`: a few blocks (the model: 5) reformatted, about
   45 erased pages, then a remount.
3. The second run: the check over the whole store, the restore comparison,
   compaction and the probe, ending `REPAIRED` with 0 gaps. The check deletes
   the leaked pages, so `used_after` should fall by roughly their 1.4 MB.
4. The probe passes, a minute later 2.7.4's `storage.inventory` finds no
   unreadable file, the boot confirms within the 900 s budget, and
   `pending.jsonl` (about 2.5 MB) drains to ORDS.

**How to read the report**, in this order:

1. `reclaim.area` is `READY`. If not, the store stays as 2.7.5 left it
   (`STORAGE_REPAIR_RECLAIM_UNAVAILABLE`) and the image rolls back; stop. A
   `FOREIGN` area is reported, never claimed: owner decision 2.
2. `reclaim.trigger` is `full` (or `gc_unsafe`), `runs` 2 or 3, `level` 3:
   the round ran. `room_needed_pages` and the first run's spans are in the
   boot's report even though the last run had room.
3. `reclaim.short` is absent and `erased_after` reaches the target. `short`
   `candidate` with `unconfirmed_blocks` 0 (or too few) means P02 holds no
   block an unconfirmed image may take: the result stays `DEFERRED`, nothing
   is lost, and the image rolls back after 900 s. Stop: getting custody blocks
   reclaimed needs owner decision 3 ((a) a narrow boot-health exception so the
   round runs on a confirmed boot, or (b) custody windows on an unconfirmed
   boot).
4. `reclaim.failed`, `held_mismatch`, `records_open` and `reported` are 0.
   A failed block (a worn cell) is closed and its bytes become a source the
   next run puts back; report it. `held_mismatch` above 0 means the held-CRC
   oracle fired: stop.
5. The last run: `result` `REPAIRED` (or `REPAIRED_WITH_GAPS` with gap
   records only for bytes no source held: P02 had none on 2.7.5), no
   `error_stage`, `check.passes` 1 or 2, `check.deleted_live_files` 0,
   `room_needed_pages` absent, `records_dropped` 0.
6. Record `duration_ms`, `reclaim.ms`, `check_ms`, `stack_free_bytes`
   (at least 2,048; P02's first boot has no T5 retry, so it will not cover the
   deepest path by about 208 B), `nvs_available_entries_after` and
   `internal_largest_after`.
7. Then the 2.7.1 acceptance evidence: boot confirmed, clean boot report,
   memory, delivery, device health. P02's next boot (whenever the site
   power-cycles it) must report `CLEAN` with `reclaim.area` `READY` and no
   `trigger`.

### 2. SLICTOWER 13FL, from 2.4.12

Check what 13FL runs and its last heartbeat first. Expect what 2.7.5's
runbook described for its first run (`blocked_identity.jsonl`'s head, about
50,200, back from the deleted page the index still names, or a gap there; a
`CURSOR_RESET` if 2.4.12 rewrote the file after 2.7.x settled part of it),
then the round as on P02. 13FL's custody may exceed the hold budget; the
round does not need the hold. Read the report as for P02.

### 3. Then the 2.7.5 devices, in place

Peshawar 06 and Karachi 01 run 2.7.5. On a healthy store 2.7.6 must report
what 2.7.5 did plus `reclaim.area` `READY`, `runs` 1 and no `trigger`; the
redo area of a device that never ran 2.7.6 reads erased.

SLICTOWER 3FL stays on 2.7.2 (it refuses OTA with
`JOURNAL_LEGACY_EVIDENCE_PRESENT`); 2.7.6 reaches it by USB at a later office
visit. ZONE-BLD9-01's K50 is in 2.7.4's own 18-target extension, not in the
nationwide scope.

## Rollback expectations

For a device whose predecessor is 2.4.12 or 2.5.2 (P02, 13FL):

- **2.7.6 cannot prove health within 900 s** (for example `short`
  `candidate`): it marks itself invalid and the bootloader returns to the
  predecessor; ADD records `ROLLED_BACK` with `BOOT_HEALTH_TIMEOUT`. A reset
  during the first boot rolls back at once.
- **What stays** is everything 2.7.5's repair writes (its runbook lists it)
  and every block the round reformatted. On an unconfirmed boot those blocks
  held nothing any image reads, so the old image reads every file exactly as
  before, and it finds their deleted pages erased: room it did not have.
- **A reset inside a block's window** on an unconfirmed boot: the old
  image's mount erases that block, which held nothing it reads. The record
  was written by an unconfirmed image, so no later boot redoes it; it is
  retired.
- **A reset inside a window on a confirmed 2.7.6**: the next 2.7.6 boot
  completes the block from its record before anything mounts the store. A
  confirmed image is not rolled back by the bootloader.
- **The redo area and the reclaim journal** (NVS `storage_repair/reclaim`)
  are outside everything older images read or write. Gap records stay in NVS
  (`storage_repair`) and go to ADD only while 2.7.5 or 2.7.6 runs; check
  `records_pending` before a rollback is expected.
- **Torn SPIFFS erases before an old image (owner decision 6).** SPIFFS's
  own garbage-collection erase (inside the probe's write, or the run's
  compaction) does not clear a block's magic first. A power cut that tears
  one, followed by a rollback to 2.4.12 or 2.5.2, which write through plain
  SPIFFS before anything cleans up, can program over a page whose lookup
  entry reads free. In the harness (P02 at 1 MiB, salt 31) 2 of 8 such cuts
  lost 341,740 bytes with no gap record (`/pending.jsonl`'s header renamed,
  the queue unreadable), 2 lost 502 bytes covered by gap records and 4 lost
  nothing. This is the same exposure as 2.7.5's own unconfirmed boot and
  every GC erase of those images in normal operation; a 2.7.5 or 2.7.6 image
  cleans such pages before anything writes. It needs a cut at exactly that
  erase on the first boot, so keep P02's and 13FL's power stable then.
- **A store a round could not give room** (`short`) stays as 2.7.5 left it:
  its rows are on flash and nothing is lost, but no image can store new
  punches there until the owner decides (decision 3). Stop that device's HIL
  there.
- Never erase connector storage outside the approved custody paths.

A 2.7.5 device (P06, Karachi 01) that rolls back returns to 2.7.5, which runs
its own repair, lookup hygiene included, before the VFS mounts.
