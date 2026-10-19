# Zone Lite 2.7.5: ZKT platform HIL

2.7.5 is 2.7.4 plus the custody-first SPIFFS repair: at every boot, before the
store is mounted, it makes every custody file readable again without losing a
readable unsettled row, runs SPIFFS's own check only on a consistent store,
puts back anything the check changed, and reports every unreadable byte to
ADD as gap evidence. It accepts 2.7.4 as a rollback predecessor. The design,
its rules and its review history are in
[zone-lite-2.7.5-storage-repair.md](zone-lite-2.7.5-storage-repair.md); the
[2.7.1 runbook](zone-lite-2.7.1-hil.md) still applies to publication,
campaigns, acceptance evidence and rollback.

## Why

On 16 October 2026, 2.7.4 confirmed on Peshawar 06 in place. SLICTOWER 13FL
(from 2.4.12) and Peshawar 02 (from 2.5.2) booted it, failed the persistence
probe with ENOENT on every attempt and rolled back after the 900 s budget
(`BOOT_HEALTH_TIMEOUT`). Its inventory and the host power-cut harness explain
both stores: zone_lite never ran a SPIFFS check, and resets in the middle of
flash writes leave objects half moved, half deleted or with a torn header size
that SPIFFS can then neither read, append to nor remove (section 1 of the
design). On 13FL, `blocked_identity.jsonl` (3.8 MB) is unreadable from about
50,200, its queue head; on P02, `pending.jsonl` (about 2.5 MB) has an EIO page
at 493,968 and about 1.4 MB of the store is leaked outside every file.

## What 2.7.5 does

**Every boot, before the VFS mount** (`storage_repair_boot`, then the command
inbox merge, then the storage-ready gate; the OTA confirmation budget starts
after all of it):

1. Survey every page read-only; recover torn header sizes; map every custody
   file span by span.
2. Record every unreadable range in NVS (file, offset, length, SPIFFS cause)
   before any write; then normalize: unreadable spans are rebuilt from the
   page the index names when it is this file's own finalized page, or from
   finalized copies, and filled (NUL, or `\n` for ID lines) only where no
   source holds the byte. Duplicate headers, stray and orphaned pages, dirty
   tails and leaked pages go; room comes from compaction of blocks where it is
   safe.
3. Only when every custody file is consistent: a manifest of every custody
   file, then `SPIFFS_check`; whatever the check changed is put back from
   PSRAM holds or from the original's pages before anything else runs.
4. Finish: legacy queue cursors that `lq_open_step` would reject are cleared
   (the file replays from 0; ORDS and ADD settle duplicates), torn tails get a
   `\n`, a damaged identity catalog is dropped (ADD resends it), and a probe
   file proves the store when the run repaired something.

Nothing is reformatted. Offsets, names, NVS keys and cursor formats do not
change, so 2.4.12, 2.5.2 and every 2.7 image read the repaired store.

**Guards.** A hard deadline that the repair cannot defeat: 5 min on an
unconfirmed image (a reset there rolls back), 15 min on a confirmed one; the
engine stops itself 45 s earlier. Three runs ended by the deadline, a panic or
a watchdog switch repairs off for that image (`REPORT_ONLY`) until ADD
installs another image. After an incomplete run, an unconfirmed image only
normalizes. Too few free NVS entries for the repair's journal: report only.
The repair never restarts the ESP itself. The LED shows `STORAGE_REPAIR`
(fast white and blue) while the run writes; it is not a fault.

**2.7.4 as a rollback predecessor.** 2.7.5 accepts the exact signed 2.7.4
(application SHA-256 `3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac`,
HIL run 38529352136) as well as 2.7.2 and 2.7.3 in its rollback slot, in the
device guard and in ADD's storage contract. The compiled marker ends
`,2.6.15,2.7.2,2.7.3,2.7.4`: 14 qualified predecessors. Peshawar 06 moves to
2.7.5 in place.

## Boot cost

Not yet measured on a device; P06 measures it first. What is known:

| What | Value | Source |
|---|---|---|
| Clean 8 MiB store at 70% used | about 5 s of flash I/O per boot (about 39 MiB read, mostly the check), plus CPU | flash-cost model over the host harness's operation counts (third boot review) |
| Worst damaged 8 MiB store in the harness | about 26 s of flash I/O, with worst-case 400 ms erases | same model |
| Engine CPU, most damaged file | 422 ms per repair on the host (3.7 M steps, under sanitizers) | review run `cpu`; capped at 512 steps per page in every harness run |
| Largest repair | 4.9 M HAL operations of a 10.4 M cap at 8 MiB | harness, fourth pass |
| First check pass before any is measured | budgeted at 4 s per MiB with the metadata mirror (8 s without) plus 15 s: about 47 s for 8 MiB | `time_for_check`; a pass starts only if it and its restore fit before the soft deadline |

The repair runs before Wi-Fi starts, so every boot (OTA restart, power-up or
any other reset) is offline in ADD for `duration_ms` longer than before; the
terminal keeps its punches meanwhile. It does not use the 900 s confirmation
budget, which starts after storage. Owner decision 3 of the design keeps the
every-boot policy if the clean-store run is a few seconds and a damaged-store
run is well inside 5 minutes; decide that from P06's and P02's numbers.

### Controlled-reboot window

`hil_reboot.py`'s controlled ESP reboot (issued in observation minute six,
healthy again by minute eight) belongs to the 2.7.0 journal writer's
full-remote HIL. The repair is not built into the journal roles, and only the
2.7.0 writer reports `controlled_esp_reboot_v1` or is admitted by
`hil_reboot._scope`, so the every-boot repair never runs inside that window.
`test_controlled_reboot_window_never_meets_the_every_boot_repair` pins all
three; an image that runs the repair must not become reboot-capable until the
recovery window covers its deadline. A 2.7.5 HIL target has no ADD-side
recovery window: its acceptance is the boot confirmation below.

## What to watch in ADD

After the OTA restart, and after every later boot, the heartbeat carries
`diagnostics.storage.repair` (stored as the firmware sent it):

| Field | Expect | Stop and investigate |
|---|---|---|
| `result` | `CLEAN` (healthy store), `REPAIRED`, `REPAIRED_WITH_GAPS` (bytes no source held) | `FAILED`, `STOPPED`, `REPORT_ONLY`, `MOUNT_FAILED`; `DEFERRED` twice in a row |
| `mode` | `FULL` | `NORMALIZE_ONLY` (an earlier run did not finish), `REPORT_ONLY` |
| `error_stage` | absent | `full`, `gc_unsafe` (store too crammed for SPIFFS's GC), `records`, `manifest`, `time`, `nvs_full`, `journal`, `deadline` |
| `duration_ms`, `check_ms` | seconds, not minutes | anything near the 255 s soft deadline |
| `check.passes`, `check.deleted_live_files`, `check.errors` | 1 or 2 passes; 0 deleted live files | `deleted_live_files` above 0 |
| `gaps`, `unreadable_bytes`, `entries[]` | per file: `gaps`, `unreadable_bytes`, `spans_rewritten`, `actions` | a gap on a file the 2.7.4 inventory read whole |
| `records_pending`, `records_reported`, `records_dropped` | pending falls to 0 as ADD acknowledges; dropped 0 | `records_dropped` above 0 |
| `room_needed_pages`, `room_erased_pages`, `entries[].blocked_spans` | absent | present: the run had no room; the blocked bytes are still on flash |
| `cursors_reset`, `files_superseded`, `sizes_recovered`, `tails_terminated`, `blocks_compacted` | as the store needs | |
| `failures`, `incomplete`, `previous_boot_phase`, `previous_boot_deadline`, `previous_boot_returned` | 0, 0; after a reset, `previous_boot_returned` true | `previous_boot_deadline` true, or `failures` rising |
| `nvs_available_entries_after`, `internal_largest_after`, `stack_free_bytes` | headroom left; stack at least 1.5 KiB | `stack_free_bytes` under 1,536 |

Queue evidence: queue `storage_repair`, generation
`storage-repair-v1-<queue instance>`; one record per unreadable range,
`gap:<file>:<offset>:<length>:<run>` (reason `MALFORMED`), and one per cleared
cursor, `cursor:<file>:<generation>:<run>` (reason `LEGACY_RECOVERY`, with the
old 20-byte checkpoint). Every unreadable byte of a custody file must be under
one of these.

Device logs (`STORAGE_REPAIR_*`, up to 24 per boot): `STARTED` and `DONE`
frame each run; `GAP`, `CURSOR_RESET`, `SIZE_RECOVERED`,
`TAIL_TERMINATED`, `COMPACTED`, `CATALOG_DROPPED`, `UNHIDDEN`, `REBUILT`,
`MERGED`, `SUPERSEDED`, `CHECKED`, `COMMANDS_MERGED` say what it did;
`DEFERRED`, `FULL`, `RESERVE`, `GC_UNSAFE`, `BLOCKED`, `LEVEL2_SKIPPED`,
`MERGE_DEFERRED`, `RESTORE_DEFERRED`, `RESUME_BLOCKED`, `RECORDS_FULL`,
`MANIFEST_FULL`, `NO_TIME`, `NOT_READY` say what it could not do yet;
`CHECK_DELETED`, `SIZE_TORN`, `MANIFEST_UNREADABLE`, `NVS_FULL`,
`JOURNAL_FAILED`, `MOUNT_FAILED`, `DISABLED`, `COMMANDS_FAILED` need a look.

Then, a minute after boot, 2.7.4's `storage.inventory` must find no
unreadable file, the persistence probe must pass, and the 2.7.1 acceptance
evidence applies (boot confirmed within budget, clean boot report, memory,
delivery, device health).

## Release and HIL

Prerequisites: the owner's decision on the crammed-store wedge (design
section 12, decision 1; the repair reports `gc_unsafe` but cannot prevent it),
review, and a green `main`. Then publish 2.7.5 like 2.7.4:
`firmware-hil-candidate.yml` with family `zkt`, the green main SHA, version
`2.7.5` and the ordered nationwide targets. It is `HIL_ONLY`. Keep 2.7.2, 2.7.3
and 2.7.4 published: ADD offers 2.7.5 to a device running one of them only
while that exact release is published. ZONE-BLD9-01's K50 is in 2.7.4's own
18-target extension, not in 2.7.5's nationwide scope.

One target at a time; start the next only once the previous one is accepted
or its failure is understood:

1. **Peshawar 06, in place from 2.7.4.** The lowest-risk store: expect
   `CLEAN` (or `REPAIRED` with compaction or old damage), 0 gaps. It gives the
   clean-store `duration_ms` and `check_ms`, stack, NVS and internal-memory
   numbers. Its next boot (whenever the site power-cycles it; there is no
   remote ESP reboot outside the 2.7.0 writer's HIL) must report `CLEAN`.
2. **Peshawar 02, from 2.5.2.** Expect `REPAIRED` or `REPAIRED_WITH_GAPS`,
   with `pending.jsonl`'s gap records starting at 493,968 (span 1968; none
   there if a copy of the page survives), leaked pages freed (`used_after`
   well below `used_before`), the probe passing, and the queue draining to
   ORDS.
3. **SLICTOWER 13FL, from 2.4.12.** Expect `blocked_identity.jsonl`'s head
   (about 50,200) back from the deleted page the index still names, or a gap
   there; a `CURSOR_RESET` if 2.4.12 rewrote the file after 2.7.x settled part
   of it (the queue then replays from 0); the probe passing.

SLICTOWER 3FL stays on 2.7.2 (it refuses OTA with
`JOURNAL_LEGACY_EVIDENCE_PRESENT`); 2.7.5 reaches it by USB at a later office
visit.

## Rollback expectations

For a device whose predecessor is 2.4.12 or 2.5.2 (13FL, P02 and the like):

- **2.7.5 cannot prove health within 900 s** (for example a store still
  deferred for room): it marks itself invalid and the bootloader returns to
  the predecessor; ADD records `ROLLED_BACK` with `BOOT_HEALTH_TIMEOUT`. A
  reset during the first boot (power cut, the repair's deadline) rolls back at
  once.
- **What stays:** everything the repair already wrote, all of it readable by
  the old image: rebuilt pages, recovered sizes, NUL or `\n` fills in
  unreadable spans, a `\n` after torn tails, cleared cursors (the old image
  replays those queues from 0), the catalog files removed, fewer leaked pages.
  The old image does not repair; damage it makes is repaired when 2.7.5 runs
  again.
- **Gap records** are kept in NVS (`storage_repair`, which older images never
  touch) and go to ADD only while 2.7.5 runs. Check `records_pending` before a
  rollback is expected; whatever is still pending is sent at the next 2.7.5
  boot.
- **An interrupted run's manifest** stays in NVS. The next 2.7.5 boot settles
  it first: anything of an original still on flash comes back (merged after
  the rows a rolled-back image wrote meanwhile, which can deliver some rows
  twice, never lose one); a file the old image replaced is superseded, not
  reported.
- **A store left crammed** (`gc_unsafe`, or `DEFERRED` with `full` every boot)
  is design decision 1: its rows are on flash, but no image can store new
  punches until the owner's last resort exists. Stop that device's HIL there.
- Do not cancel a campaign while the device is `OFFERED` or `DOWNLOADING`,
  and never erase connector storage outside the approved custody paths.

A 2.7.4 device (P06) that rolls back returns to 2.7.4, which reads the same
store and runs 2.7.4's inventory over it.
