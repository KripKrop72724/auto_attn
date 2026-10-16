# Zone Lite 2.7.5: custody-first SPIFFS repair (design)

Status: design for implementation on `release/zkt-2.7.5`. It implements the
owner decision of 16 October 2026: save every readable unsettled row first,
run the SPIFFS check at boot, restore what the check removed, report fixes,
deletions and gaps to ADD, and reformat only as a last resort once the
readable custody content is held for rewrite. Preserving attendance custody
is the top rule. No readable unsettled punch may be lost, and every
physically unreadable byte must reach ADD as explicit gap evidence (file,
offset, length).

Scope: the ZKT production image (`ZONE_LITE_DIRECT_LEGACY_UPGRADE`, legacy
authority, `ZONE_LITE_QUEUE_OWNER` compiled in but
`zj_runtime_checkpoint_required()` false). The Hikvision family and the
2.6.27 storage recovery role are excluded at compile time.

Line references are to `release/zkt-2.7.5` at the time of writing
(`facc1b3`), and to ESP-IDF 5.5.3 `components/spiffs` as shipped in
`espressif/idf:v5.5.3`.

## 1. Evidence

### 1.1 Root cause

The host power-cut simulator (`research/spiffs-powercut-sim`, commits
`9af938a` and `5413154`) runs ESP-IDF 5.5.3's own SPIFFS with the device
geometry and Kconfig on an emulated NOR flash. Its findings:

- zone_lite never calls `esp_spiffs_check`. A reset during a flash write
  leaves objects unfinalized or half deleted. GC then refuses the damaged
  block for good: `SPIFFS_VALIDATE_OBJIX` returns DELETED, seen as ENOENT, or
  NOT_FINALIZED, IS_INDEX, IS_FREE or DATA_SPAN_MISMATCH, seen as EIO.
  Interrupted deletes leak pages until ENOSPC at low real usage.
- 8 MB, 8 seeds, about 20 mid-write cuts per run:
  - no check: 1 of 8 runs stuck for good, with the leak reaching 68%;
  - `SPIFFS_check` at every mount: 0 stuck and no leak, but 54
    unacknowledged lines lost and 12 whole files deleted;
  - custody first (salvage, check, re-append): 0 stuck, no leak and 1 line
    lost. The unsalvaged legacy file was still deleted once.
- One check costs about 2.1 M HAL reads on 8 MB. On an image already full of
  leaked pages, one check fails with SPIFFS_ERR_FULL.

### 1.2 The field reports fit one damaged data page at a queue head

A SPIFFS page is 256 bytes, of which 5 are the page header
(`sizeof(spiffs_page_header) == 5`, asserted in
`tests/firmware/spiffs_orphan_host.c`). It therefore holds 251 data bytes
(`SPIFFS_DATA_PAGE_SIZE`, spiffs_nucleus.h:228), and file offset `o` lives in
data span `o / 251`. The 2.7.4 inventory read with `_IONBF`
(storage_inventory_core.c:65), so newlib made one `read()` per byte and the
first failing offset it reported is byte-exact:

| Device | File | First failure | Span | Meaning |
|---|---|---|---|---|
| SLICTOWER-13FL | `blocked_identity.jsonl` (3,826,949 B) | about 50,200, ENOENT | 200 x 251 = 50,200 | One referenced data page is flagged deleted (SPIFFS_ERR_DELETED). The blocked cursor is 50,055, so the head row runs into the damaged span: `LEGACY_QUEUE_READ_FAILED identity_blocked peek errno 2 cursor 50055`. The "2 pages" the inventory counted are the two 256-byte inventory pages that this one 251-byte span straddles. |
| PESHAWAR-02 | `pending.jsonl` (~2.51 MB) | 493,968, EIO | 493,968 = 1968 x 251 | One data page with IS_FREE, NOT_FINALIZED, IS_INDEX or DATA_SPAN_MISMATCH. It causes the weeks of ORDS legacy-read backpressure (errno 5). |
| 13FL and P02 | `persistence-probe` | open for write fails with ENOENT | - | The probe object itself is damaged: opening it for write and removing it both fail with ENOENT. The 2.7.4 remove-and-retry (queue_store.c:446) therefore cannot succeed; only the check drops the object. |
| P02 | (no file) | ~1.4 MB used outside every file | - | Pages leaked by interrupted removes. Only the check frees them (2.6.25 showed that `remove()` stops at the first corrupt page after flagging the header IXDELE). |

In both flagship cases the reports fit a single damaged data page. For
P02 the offset is an exact span boundary. For 13FL the reported region is
two inventory pages after one failure, about 488 bytes, which is what one
251-byte span starting at 50,200 produces on the inventory's 256-byte grid.
The salvage confirms this with exact SPIFFS codes. The fixes are:

- the queue becomes readable again when that one span is rewritten;
- the probe becomes usable again after a check;
- the leak is freed by the check.

### 1.3 What `SPIFFS_check` does, and why it cannot be used bare

- **It deletes whole files.** An index that references a data page with a
  wrong header, where no replacement page exists, makes the check delete the
  whole object: spiffs_check.c:670 `index bad, delete object id`, using
  `spiffs_delete_obj_lazy` at spiffs_check.c:156. On 13FL that would be the
  whole 3.8 MB blocked file, and on P02 the whole 2.5 MB pending file. This
  is the simulator's 12 deleted files.
- **It fills bad spans with 0xFF.** A bad index reference gets a newly
  allocated, never-written data page, so the content is 0xFF
  (spiffs_check.c:608-624). In a text queue that is silent garbage, not an
  error.
- **It finalizes torn pages.** An unfinalized page that is referenced is
  finalized as it stands, and its data may be torn.
- **It needs free pages to work.** It allocates replacement pages through
  `spiffs_obj_lu_find_free`. That runs `spiffs_gc_quick` when fewer than 2
  blocks are free, which erases blocks that hold only deleted pages
  (spiffs_nucleus.c:448-462). Otherwise it returns FULL.
- **Its return value hides errors.** `SPIFFS_check` returns only the result
  of the final `spiffs_obj_lu_scan` (spiffs_hydrogen.c:1151-1157). Errors in
  the lookup, index and page passes, FULL included, reach only the check
  callback. So `esp_spiffs_check` (esp_spiffs.c:312) reports success for a
  failed page pass.
- **IDF's callback only logs.** IDF installs `spiffs_api_check`
  (spiffs_api.c:58), which never yields. The check holds the filesystem mutex
  for its whole run.

## 2. Requirements

- **R1 Custody first.** Before anything can delete or rewrite a custody
  file, its readable bytes are held in PSRAM. Every readable unsettled byte
  is then on flash after the repair, at its original offset (a file whose
  settled prefix was not held is the one exception: section 7.6).
- **R2 Gaps are explicit.** Every byte that cannot be read becomes a gap
  record (file, offset, length, SPIFFS error, phase). The gap is held in NVS
  until ADD acknowledges it as queue evidence.
- **R3 Timing.** The repair runs at boot, before any queue owner, cache or
  scan opens a file.
- **R4 No new formats.** No duplicates, and no format or cursor change:
  every image that can roll back (2.4.12, 2.5.2, 2.6.x, 2.7.2 to 2.7.4)
  reads the repaired store unchanged.
- **R5 Reformat last.** Reformat only as a last resort, with preconditions
  that a test can check.
- **R6 Boot safety.** The repair never spends boot-confirmation budget,
  never loops boots and never latches LOCAL_FAILURE for gaps it has reported.
- **R7 Host testing.** Every step is exercised on the host against ESP-IDF
  5.5.3's SPIFFS with power cuts, including cuts during the repair itself.

## 3. Files under /storage (a)

The SPIFFS object name is the VFS path below the mount point, leading `/`
included (storage_recovery.c:192).

| File(s) | Code | Record format | Unsettled range / checkpoint | Class | Hold | Restore |
|---|---|---|---|---|---|---|
| `pending.jsonl` | zone_lite.c:194, `append_line_policy` 3201, `oracle_drain_pending` 7739 | one JSON punch per line (`event_to_json`, 64-hex `event_uid`) | NVS `legacy_queues/ords_pending` lq checkpoint: offset and prefix CRC; range [offset, EOF) | attendance custody | P1 | byte-exact |
| `pending.bak`, `pending.tmp` | `restore_pending_backup_if_needed` 3311 (renamed in when the active file is missing); 2.4.12/2.5.2 rewrites | same | whole file (no cursor of its own) | attendance custody | P1 | byte-exact |
| `blocked_identity.jsonl` | zone_lite.c:197, `blocked_evidence_slice` 7930 | JSON line per held punch | `legacy_queues/blocked` | attendance custody | P1 | byte-exact |
| `blocked_recovery.bak`, `blocked_recovery.tmp` | `restore_blocked_backup_if_needed` 3332 | same | whole file | attendance custody | P1 | byte-exact |
| `add_live.jsonl`, `add_pending.jsonl` | add_connector.c:116-125, `add_legacy_owner_append` 3490, delivery 4489 | JSON line `{"type","payload"}`: attendance_batch, oracle_receipt_batch or queue_evidence, up to 8 KiB | NVS `add_legacy/live`, `add_legacy/reconcile` | attendance custody | P1 | byte-exact |
| `add_live.bak/.tmp`, `add_pending.bak/.tmp` | `restore_outbox_if_needed` 3292 | same | whole file | attendance custody | P1 | byte-exact |
| `ql`, `qb`, `qo`, `qi`, `qr`, `qe`, `qh` + 8 hex + `.q` | queue_store.c:31,242; durable_queue.c | 16-byte header (magic `0x32514c5a`, length, sequence, CRC32) and record; 64 KiB segments; seal record | NVS `durable_queue/<lane>`: (read segment, offset) to (write segment, offset) | attendance custody. Direct-legacy 2.7.x does not write these lanes, but older segmented images may have left some. | P1 (unsettled segments whole) | byte-exact |
| `corrupt_ords.jsonl`, `add_corrupt.jsonl`, `add_corrupt.bak` | `read_quarantine_locked` zone_lite.c:8004 | preserved malformed rows | `legacy_queues/old_qo`, `old_qa`, `old_qb` | evidence custody | P2 | byte-exact |
| `zktj` + 16 hex + `.j` | zkt_journal_store.c:65 | authenticated frames; 64 KiB segments, at most 256 | NVS `zkt_journal` | evidence custody: bridge-era observations, inert under 2.7.x | P2 | byte-exact |
| `add_commands.jsonl` and its `.tmp`, `.bak`, `.producer`, `.spare` generations | add_connector.c:129-131, zkt_catalog_store.h:16-20 | one encrypted ADD command per line, up to 12 KiB; file up to 64 KiB | ft checkpoint `file_tx/commands`: phase, length, digest | operational custody (accepted ADD commands) | P3 | complete valid lines only, through `ft_replace` |
| `processed_commands.txt`, `add_cancelled.txt` | zkt_command_ids.h:10-11, `rel_id_file_contains` | one command ID per line, up to 64 KiB | none (append-only) | idempotency | P3 | byte-exact |
| `add_live.pos`, `add_pending.pos` | `write_outbox_cursor` add_connector.c:3270 | `"%lld\n"`: the rollback images' text cursor | none | idempotency (rollback cursor) | P3 | byte-exact |
| settled prefix [0, cursor) of every lq file | - | - | - | already delivered | P4 | byte-exact if held, else cursor reset (section 7.6) |
| `acked_uids.txt` | zone_lite.c:202, `load_seen_from_file` 3260, `sr_append_uids` | one 64-hex UID per line | none | dedupe | P5 | byte-exact |
| `add_identities.enc`, `.tmp`, `.stage`, `.backup`, `.commit` | add_connector.c:134-138, ft `file_tx/catalog` | encrypted catalog, up to 2 MiB | ft checkpoint | re-fetchable: ADD resends the catalog on every stream connect (web.py:4247) | never held | removed with the ft checkpoint set idle, only when damaged or space is needed |
| `persistence-probe` | queue_store.c:430 | 32 random bytes | none | expendable | never held | removed |
| `add_live.pos.tmp`, `add_pending.pos.tmp` | `write_outbox_cursor` | transient: a lost temporary cursor only replays, as add_connector.c:3281 already accepts | none | expendable | never held | removed |
| `*.rp`, `/repair-probe` (new) | this design | repair scratch | journal | scratch | - | finished or removed by the journal |
| any other name | - | unknown | - | unknown | P6 | byte-exact; never removed by the repair |

Why each class:

- **Attendance custody (P1).** These are every queue domain that the legacy
  inventory requires (zkt_legacy_inventory.h: six segmented lanes, two ADD,
  two attendance and three quarantine generations). Older-generation files
  (`.bak`/`.tmp`) are custody because they are renamed into place and
  drained once the active file retires.
- **Evidence custody (P2).** The quarantine files and journal segments hold
  bytes that ADD may not have yet. The drains transfer them as queue
  evidence before they are retired.
- **Dedupe (P5).** In legacy mode the seen cache is built from pending,
  blocked and `acked_uids.txt` (zone_lite.c:3526-3530). A lost UID lets a
  full reconcile re-queue a punch that was already delivered. That duplicate
  is idempotent at ADD (by `event_uid`), so this is not a custody loss. It
  can, however, regrow `blocked_identity.jsonl` under a 2.5.2 rollback (the
  Peshawar 2.6.25 note). It is therefore held whenever the budget allows
  (0.7 to 1.2 MB in the field). Losing it is reported, not hidden.
- **Small idempotency files (P3).** Losing `processed_commands.txt` or
  `add_cancelled.txt` can let a replayed ADD command run twice. These files
  are at most 64 KiB, so they are always held. The `.pos` files are a few
  bytes.
- **Identity catalog.** It is not attendance. ADD sends it on every
  websocket connect. Until it arrives, identity resolution falls back to the
  terminal's own user table, or holds punches as blocked, which is itself
  custody, not loss.

## 4. When the repair runs (b)

**Decision: run the custody-first check on every boot.**

A boot is skipped only when the attempts guard below says so, or when a
compile-time fallback policy is selected.

The options weighed:

| Policy | Prevents stuck GC and leaks (simulator) | Cost |
|---|---|---|
| never (2.7.4) | no: 1 in 8 stuck, leak to 68% | none |
| only after an observed error | no: damage builds silently until the next failure. 13FL failed for more than a week. | none until damage |
| after "abnormal" resets only | in practice the same as every boot: almost every field reset is a power cut (`ESP_RST_POWERON`/`BROWNOUT`). Legacy mode has no storage quiescence before `esp_restart()`, so even a software reset can split a multi-step SPIFFS operation. | same as every boot |
| **every boot** | yes: 0 stuck, no leak (policies 1 and 5) | see below |

The cost on a clean store:

- **No flash writes.** The salvage and the check only read, and no patch or
  restore is needed. The NVS journal gets two small writes.
- **Time.** About 2.1 M header reads. Without help, the estimate is 30 to 60
  s, which HIL must measure. With the metadata mirror of section 5.3 it is a
  few seconds.
- **No pressure on confirmation.** The time does not count against boot
  confirmation (section 5.1).
- **No lost punches.** The ZKT terminal keeps its punches while the ESP
  boots, and the startup dump reconciles them.

Trigger reasons are still recorded in the journal and the report (section
8):

- `FIRST_RUN`: no journal yet;
- `RESET_<reason>`: from `esp_reset_reason()`;
- `DAMAGE_FLAG`: an NVS flag that runtime code sets the first time a boot
  sees EIO, ENOENT on an existing file, EBADMSG, ENOSPC below 70% use, a
  failed probe, or unreadable inventory bytes. It is set from
  `queue_store.c` `record_queue_result` (248), `qs_local_end_file` (200),
  `qs_local_end_legacy` (217), the probe (382) and the inventory, at most one
  NVS write per boot;
- `RESUME`: the journal is not idle.

The reasons decide how severe the report is and drive two guards:

- **Attempts guard.** NVS counts repairs that started but did not reach
  `DONE`. With three or more consecutive incomplete attempts, alternate boots
  run report-only (salvage reads and inventory, no writes, no check). A
  deterministic crash in the repair then cannot become a boot loop, while
  interruptions by power cuts are still retried on the other boots. A boot
  that reaches `DONE` resets the count.
- **Fallback policy.** A compile-time `SR_POLICY_TRIGGERED` exists in case
  HIL shows the cost is unacceptable. It runs only on FIRST_RUN,
  DAMAGE_FLAG, RESUME, any non-`ESP_RST_SW` reset, and every 7th boot.

## 5. Where in boot, and how it runs (c)

### 5.1 Placement

`storage_init()` (zone_lite.c:3488) becomes:

1. `storage_repair_boot()`. It mounts the partition privately (5.2), runs
   the repair, then unmounts.
2. `seen_cache_init()`. This moved down: it takes 2 MiB of PSRAM
   (`SEEN_UID_CAPACITY` 65,536 x 32 B), which the salvage needs first.
3. `esp_vfs_spiffs_register()` (3500), unchanged, with
   `format_if_mount_failed = false`.
4. `storage_repair_after_mount()`. It handles the command inbox and the
   catalog through the existing stdio `file_transaction.c` code (7.7).
5. Everything else, unchanged and after the repair:
   - `storage_upgrade_init()`, `qs_init()` and the generation restores
     (3516-3524);
   - `load_seen_from_file()`;
   - `storage_inventory_start()` (10860), so the inventory reports the
     repaired store.

`app_main` runs `ota_manager_init()` before storage and
`ota_manager_start()` only after `storage_init()` returns (10867). The
900 s accountable budget and the 4 h cap (boot_confirm.h:15-16) are kept in
`confirm_or_report_rollback()` (ota_manager.c:900-970), which starts later.
The repair therefore spends none of either.

The ESP-IDF bootloader rollback still applies. Any reset during the first
boot of 2.7.5 (PENDING_VERIFY) returns to the previous image. Section 9
covers that case.

No network is needed. Results are reported once ADD connects (section 8).

### 5.2 Private repair mount, not `esp_spiffs_check`

The repair mounts SPIFFS itself, before the VFS exists. It uses ESP-IDF's
own SPIFFS library and HAL:

- `esp_spiffs_t` (spiffs_api.h) with its own mutex and the `storage`
  partition;
- `cfg` copied from `esp_spiffs_init` (esp_spiffs.c:212-219): page 256,
  block 4096, `phys_size` = partition size;
- HAL `spiffs_api_read/write/erase`, or the mirror of 5.3 that wraps them;
- 4 file descriptors, a cache sized as IDF sizes it, and its own check
  callback.

`main/CMakeLists.txt` adds the spiffs component directory and its
`spiffs/src` as private include directories
(`idf_component_get_property(... spiffs COMPONENT_DIR)`). No ESP-IDF source
is copied or overridden.

Why not `esp_spiffs_check()` after `esp_vfs_spiffs_register()`:

1. **It cannot report failure.** Its result is `SPIFFS_check`'s, which
   drops the lookup, index and page pass results (1.3). Only a callback sees
   SPIFFS_CHECK_ERROR, for example FULL, and IDF's callback is fixed at
   mount (esp_spiffs.c:268).
2. **It cannot yield.** `spiffs_api_check` never yields and the check holds
   the mutex for its whole run.
3. **It hides needed state.** The repair needs exact SPIFFS error codes,
   not errno (ENOENT and EIO each merge several codes; esp_spiffs.c:482). It
   also needs `spiffs_stat.obj_id` to map check reports to files, and
   `fs->free_blocks`. The VFS exposes none of these.
4. **Isolation.** Nothing else can open a file while the VFS is not
   registered.
5. **Host parity.** The same engine calls the same SPIFFS API in the host
   harness, as `storage_orphan_core` already does against the raw
   partition.

Considered and rejected: `-Wl,--wrap=spiffs_api_check`. It works, because
`esp_spiffs.o` references the symbol across objects, but it still leaves the
salvage behind VFS and newlib, with errno only and no object IDs.

Two rules for the callback:

- It never calls a SPIFFS API: `spiffs_api_lock` is a non-recursive mutex
  that the check holds.
- It only counts reports and yields.

### 5.3 Metadata mirror HAL (recommended; can be turned off at compile time)

The check's 2.1 M reads are 5-byte page-header reads of type
`SPIFFS_OP_T_OBJ_LU2`, which bypass the SPIFFS cache (spiffs_cache.c:144).
The repair's HAL keeps a PSRAM mirror of every block's lookup page
(256 B x 2,048) and of every page's 5-byte header (15 x 5 B per block).
That is about 660 KiB.

- **Filled** by one sequential pass over the partition in 64 KiB reads
  before mount.
- **Reads** that fall entirely inside a mirrored region are served from
  PSRAM. All others go to flash.
- **Program and erase** update the mirror after the flash operation
  succeeds: `mirror &= data` and `mirror = 0xFF`, the same NOR rules.

Only the private mount touches the partition during the repair, so the
mirror stays coherent. It is freed at unmount. If PSRAM cannot be
allocated, the repair runs without it. The harness proves equivalence: the
same seed with and without the mirror must give identical flash images and
reports.

### 5.4 Yielding and watchdogs

The repair runs on the `app_main` task (priority 1, CPU0). The relevant
watchdog settings are unchanged:

- task WDT: 5 s, watching IDLE0 and IDLE1, panic disabled, no task
  subscribed;
- interrupt WDT: 300 ms.

A check without yields starves IDLE0. That only logs a task WDT warning,
but other CPU0 work at priority 0 and 1 stalls with it.

Rules:

- **In the check callback.** On every PROGRESS report, read
  `esp_timer_get_time()`. If 20 ms have passed since the last yield, call
  `vTaskDelay(1)` (1 ms at `CONFIG_FREERTOS_HZ=1000`).
- **In salvage, patch and restore loops.** Yield every 16 spans or every
  20 ms.
- **Interrupt WDT.** No change is needed. Every flash operation is a single
  ESP-IDF partition call, as at runtime.
- **Progress.** Logged to the console every 10% per check pass
  (callback `arg1` is 0 to 256 per pass).

### 5.5 Storage upgrade and the upgrade guard

The repair does not depend on `storage_upgrade_init()`, which runs after it
and checks only the rollback slot and contract. It changes no format and
writes no `queue_upgrade` NVS key. The storage contract marker stays
`ZONE_STORAGE_CONTRACT_V2:LEGACY`, and release plumbing only adds 2.7.4 to
`BASE`. The repair never removes `zktj*.j`, so `zkt_ota_guard`'s refusal on
journal evidence, the 3FL issue, is unchanged.

## 6. Salvage (d)

### 6.1 Listing and ranges

On the private mount:

1. **List.** `SPIFFS_opendir` lists the files. `SPIFFS_stat` gives each
   one's size and `obj_id`. A size of `0xFFFFFFFF` (undefined length) means
   empty.
2. **Classify** by the table in section 3. Dynamic names (`q*`, `zktj*`)
   are classified by pattern.
3. **Read checkpoints** through an NVS port:
   - `legacy_queues/{ords_pending, blocked, old_qo, old_qa, old_qb}` and
     `add_legacy/{live, reconcile}` (`lq_checkpoint_t`, CRC as in
     `lq_open_step`, legacy_queue.c:33-40);
   - `durable_queue/<lane>` (`checkpoint_valid`, durable_queue.c:40-47);
   - `file_tx/{catalog, commands}` (file_transaction.c load).

   An invalid or missing checkpoint makes the whole file unsettled.
4. **Set each file's hold range.**
   - lq files: [0, size) when the budget allows (P4 prefix included);
     otherwise [cursor, size).
   - Segments: whole files from the read segment to the write segment.
   - Everything else: the whole file.

### 6.2 Page-exact reads

`storage_span_reader` is shared with the inventory, see section 12. It reads
on 251-byte data-span boundaries:

- **Normal path.** `SPIFFS_lseek(k x 251)`, then `SPIFFS_read` of 16 spans
  (4,016 bytes). That is one read call per chunk. The repair always seeks
  explicitly, because SPIFFS does not advance the offset after a failed read
  (spiffs_hydrogen.c:397-415).
- **Failed chunk.** Re-read it one span at a time.
- **Unreadable span.** The span becomes a gap with its exact SPIFFS code:
  DELETED, IS_FREE, NOT_FINALIZED, IS_INDEX, DATA_SPAN_MISMATCH and so on.
- **Failed seek.** The object index page for that range is unreadable. The
  whole index span becomes one gap: spans 0-102 for the header index, and
  124 spans for each later index page (`SPIFFS_OBJ_HDR_IX_LEN`,
  spiffs_nucleus.h:246). The file is reopened and the read continues after
  that span.
- **Failed open or stat.** If the object header is unreadable, the gap is
  `[0, size)`. If even the size is unknown, the gap is reported with length
  0 and cause `HEADER_UNREADABLE`.

Gaps are coalesced per file. A per-file bitmap (1 bit per span) records
which spans are held.

### 6.3 PSRAM budget and priority

The budget is the largest free `MALLOC_CAP_SPIRAM` block minus a 1.5 MiB
reserve, capped at 6 MiB. Internal RAM is never used as a fallback. Content
is held in priority order P1, P2, P3, P4, P5, P6, one contiguous allocation
per file.

| Shortfall | Action |
|---|---|
| everything fits | normal repair |
| P4 to P6 do not fit | Repair continues. A file the check deletes whose prefix is not held is rebuilt from its cursor (7.6). Lost dedupe or unknown files are reported (`DEDUPE_LOST`, `UNKNOWN_LOST`). |
| any P1 to P3 byte does not fit, or one custody file is larger than the largest free block | State `DEFERRED`. No check runs and nothing is removed. The safe pre-check patches (7.1) and tail terminations (7.7) still run, because they need no held bytes. Queues then drain, shrink, and fit on a later boot. |

Field fit at storage_init, assuming about 7.5 MiB of PSRAM free before the
seen cache (an estimate; the report carries `budget_bytes`):

| Device | Held | Fits |
|---|---|---|
| 13FL | blocked 3.83 MB + acked 0.73 MB + processed | 4.6 MB: yes |
| P02 | pending 2.51 MB + acked 1.05 MB + small files | 3.6 MB: yes |
| P06 | 1.5 MB | yes |

### 6.4 Records

The repair restores bytes, not parsed records. It parses only for reporting
and for the command inbox. Per class:

- **JSON-line files** (lq and generations): the salvage counts
  - complete rows: readable and newline-terminated;
  - damaged rows: rows that touch a gap;
  - a torn tail: a final fragment with no `\n`.
- **Segments:** headers and CRCs are walked through the unsettled range
  (the `dq_peek` rules, durable_queue.c:171-213).
- **`acked_uids.txt`:** 64-hex lines.
- **Command inbox:** lines are checked the way
  `command_journal_contains_locked` (add_connector.c:2234) checks them: a
  complete line that decrypts to an object with `command_id`. A line that
  touches a gap or fails is dropped and reported.

### 6.5 How unreadable bytes become evidence

A text-class file is restored byte-exact, and every gap span is written as
NUL bytes (0x00). This is deliberate:

- **Offsets stay valid.** lq cursors and prefix CRCs (legacy_queue.c:67),
  dq checkpoints and journal offsets still point at the right bytes.
- **The existing drains already handle NUL rows.** A row containing NUL is
  classified as corrupt and handed to ADD with its readable fragments, then
  settled:
  - pending: `memchr(event, 0, ...)` gives `CORRUPT_LOCAL_ROW` and queue
    evidence `ords_legacy` (zone_lite.c:7707, 7800);
  - blocked: gives MALFORMED (7983);
  - ADD outboxes: give MALFORMED (add_connector.c:4489-4528);
  - 2.5.2 after a rollback moves such a row to `corrupt_ords.jsonl`.

  The approved 2.6.25 engine also sent rows cut by a gap "as their exact
  readable fragments".
- **The queue head moves again.** Today the unreadable head blocks the queue
  for ever, with `lq_peek` returning DQ_IO. On 13FL the head row becomes one
  MALFORMED evidence record and the queue drains.
- **Long runs are bounded.** A NUL run longer than the reader's buffer is
  cut by `lq_peek` into evidence fragments (`evidence_required`,
  legacy_queue.c:104). Each fragment is settled with
  `lq_settle_evidence`. The buffer is 1 KiB for pending in legacy mode
  (zone_lite.c:7868-7870) and 8 KiB for blocked and the ADD outboxes.

For segments and journal files, a NUL span fails the record CRC or the
frame authentication. Those formats then go through their existing paths:
DQ_CORRUPT holds the lane, and the journal raises a frame exception. That
behaviour is unchanged and listed as a follow-up.

Each gap is also reported directly (section 8). ADD therefore gets both the
exact unreadable range and the damaged row's readable bytes.

## 7. Repair (e)

### 7.1 Pre-check patch: stop the check from deleting files

For every held file with gaps, before the check, each gap span whose SPIFFS
code is DELETED or IS_FREE is rewritten with 251 NUL bytes through
`SPIFFS_write` at `k x 251`. That is a full-page `spiffs_object_modify`:

- it allocates a new page without reading the old one
  (spiffs_nucleus.c:1571);
- it then deletes the old page (1618).

`spiffs_page_delete` (876) does not validate the old page, so the write is
safe in both cases:

- a page already flagged deleted;
- an erased page that nothing owns (no allocation can have taken it,
  because its header still reads free).

The object becomes consistent, and the check no longer deletes it. This
alone makes the 13FL head (DELETED at span 200) and, if its code is IS_FREE,
the P02 page readable.

Other codes are not patched:

- **DATA_SPAN_MISMATCH and IS_INDEX.** The referenced page may belong to a
  live object, and deleting it would damage another file.
- **NOT_FINALIZED.** The check finalizes the page.
- **Index-level gaps.** A modify needs the index page.

These are left to the check, followed by restore from PSRAM. The last span
of a file, when partial, cannot be patched with a full-page modify. It is
handled after the check (7.6).

A patch that fails is recorded and the repair continues. A patch never
touches readable bytes.

### 7.2 Expendables

- **Probe files.** `/persistence-probe` and `/repair-probe`: when present
  and damaged, they are removed before the check (best effort; a damaged
  probe object may only go away in the check).
- **Before the check, when needed.** These files are removed only if
  `SPIFFS_info` shows more than 70% used, or an earlier pass failed with
  FULL:
  - `*.pos.tmp`;
  - the five `add_identities.*` files.
- **Catalog checkpoint.** After the catalog files are removed, the
  `file_tx/catalog` checkpoint is set idle. Phase 0, generation + 1 and the
  CRC are written by a new `ft_checkpoint_idle()` helper in
  file_transaction.c, so the format is not duplicated. Otherwise
  `ft_recover` (file_transaction.c:66) would fail in phase 1 or 2 and latch
  LOCAL_FAILURE (add_connector.c:928-939).

A catalog file that has gaps or that the check removed is handled the same
way at any time. ADD resends the catalog on connect.

### 7.3 Escalation levels

Each level runs one check pass (`SPIFFS_check` on the private mount) and
then `SPIFFS_gc_quick(fs, 0)`. A pass succeeds when its callback reported no
SPIFFS_CHECK_ERROR. After each pass:

- **The pass failed.** Take the next level and run another pass.
- **The pass succeeded.** Verify (7.4), restore (7.6), then run the probes
  (7.5). If a probe fails, take the next level.

There are at most 4 check passes per boot.

| Level | Action before the pass | Window in which held bytes exist only in PSRAM |
|---|---|---|
| L0 | 7.1 patches | Only files the check deletes (index-level damage), until 7.6 rewrites them |
| L1 | Expendables of 7.2 | Same as L0 |
| L2 | Evacuate `acked_uids.txt` (held) | `acked_uids.txt`, until restored |
| L3 | Evacuate held custody files, largest first, until about 10% of the partition is free. Each must be fully held and CRC-checked first. | The evacuated files, until restored |
| L4 | Last-resort reformat (7.8) | See 7.8 |

L2 and L3 remove through `SPIFFS_remove`, which may stop at a corrupt page
and leave an IXDELE header behind. The next pass frees those pages. A held
file that the check removed is within the approved "restore rows the check
removed", and an evacuated file carries the same exposure. L2 and L3 are
reached only when L0 and L1 cannot free space. The simulator suggests that
happens only on images that are already full of leaked pages.

A level that does not complete stops the escalation. The pre-check patches,
expendable removals and NUL fills are not undone. The journal goes to
`FAILED` (stage, code), the store is mounted as it is, and the existing
probe keeps boot health withheld, as in 2.7.4 today.

### 7.4 Verification

If a pass reported no FIX, DELETE or ERROR, no held file changed, and
verification is skipped. Otherwise, for each held file:

1. **File absent.** Restore it by rebuild (7.6).
2. **File present with a different size.** Rebuild.
3. **File present with the same size.** Re-read every held span.
   - Previously readable spans that are equal are fine.
   - Spans that now differ, or are now unreadable, are restored from PSRAM.
   - A previously unreadable span that is now readable (the check found the
     right page, or finalized it) is kept only if it is a binary class, or a
     text class with no 0x00 or 0xFF byte.
   - Otherwise the span becomes NUL. JSON, base64 and ID text never contain
     0xFF: 0xFF is the check's empty filler page or a torn tail.
   - Healed spans are still reported, with cause `HEALED`.

Names that appear after the check but were not listed before are reported
(`FILE_APPEARED`) and left alone. Check reports with `DELETE_BAD_FILE` whose
`obj_id` is not in the listing are reported as `UNLISTED_OBJECT_DELETED`.

### 7.5 Probes

Two probes run after the restore (7.6):

- **GC health probe.** `SPIFFS_gc(fs, 4 x 4096)`. A GC that still refuses a
  damaged block returns an error, which is exactly the stuck state.
- **Persistence probe.** Create, write 64 bytes, flush, close, read back and
  remove `/repair-probe`.

Both must pass for the repair to reach `VERIFIED`. The runtime
`qs_verify_persistence()` probe runs later, as today.

### 7.6 Restore

Every write goes through the private mount. The journal records progress
(7.9).

- **Span restore (file present).** For each span to restore:
  `SPIFFS_open(RDWR)`, `SPIFFS_lseek`, a 251-byte `SPIFFS_write`, then
  `SPIFFS_fflush`. A partial last span uses `SPIFFS_ftruncate` to `k x 251`
  followed by an append of the held bytes, NUL for a gap, up to the original
  size. SPIFFS truncate (`spiffs_object_truncate`) tolerates a DELETED page
  or a free index entry but stops at any other damage. That is also why
  `remove()` stopped on P02 in 2.6.25. Then, or if a span write fails, the
  file is rebuilt.
- **Rebuild by swap (file present, enough free space).** Write
  `<name>.rp` from PSRAM, flush, re-read and compare its CRC. Record
  `SWAP_READY`, remove the original and rename `.rp` to the name. No window
  exists in which the bytes are only in PSRAM.
- **Rebuild in place (file absent, or not enough space).** Create the file
  and write the held range in 4 KiB writes, with `SPIFFS_fflush` and a
  journal progress record every 256 KiB.
  - **lq file held from 0.** Byte-exact. The cursor is untouched, and its
    prefix CRC stays valid unless the prefix itself had a gap.
  - **lq file held only from its cursor.** First commit the cleared
    checkpoint `{version 1, generation + 1, offset 0, prefix_crc 0}`
    through a new shared `lq_checkpoint_cleared()` helper in
    legacy_queue.c. Then write [cursor, size). This is the order
    `lq_reclaim` uses: clear before the file changes (legacy_queue.c:171-183).
    Settled rows are not rewritten, so nothing is replayed.
  - **Segments and journal files.** Byte-exact; NVS is untouched.

Restore never appends a record that is still present: it writes only spans
that are missing or different, at their own offsets, or recreates a file
that is gone. The simulator's re-append, with per-record dedupe, is replaced
by this. Re-appending would move offsets, which breaks the offset-addressed
segment and journal formats and lq prefix CRCs.

Gaps in a settled prefix: after the NUL fill, the prefix CRC no longer
matches, so `lq_open_step` takes the existing checkpoint-evidence path. The
corrupt cursor goes to ADD, the cursor resets and the file replays from 0.
The repair does not re-sign the prefix CRC. A 2.5.2 rollback can rewrite the
file, and a re-signed stale cursor would then skip unsettled rows. The
replay is idempotent at ADD.

### 7.7 Tails, cursors and checkpoints

- **Torn tail.**
  - **Which files.** The append targets:
    - pending, blocked, both ADD outboxes;
    - `acked_uids.txt`, `processed_commands.txt`, `add_cancelled.txt`;
    - the quarantine files.
  - **The fault.** `rel_open_append` (reliability.c:48-57) refuses to
    append to a file whose last byte is not `\n` and returns EBADMSG. So one
    torn append, or a NUL-filled last span, blocks every later append. This
    is the EBADMSG seen on MULTAN-01 and LAHORE-02.
  - **The fix.** When the file has an unsettled tail (size greater than its
    cursor) that does not end in `\n`, the repair appends one `\n`, as
    `sr_append_uids` already does for the acked log
    (storage_recovery_core.c:428). The fragment becomes a complete malformed
    row that the drain hands to ADD.
  - **Fully settled files are left alone.** `lq_reclaim` removes them.
- **Missing lq file with a nonzero cursor.** `lq_peek` returns DQ_IO for
  ever when the file is missing and the cursor is nonzero
  (legacy_queue.c:85). This can happen when the check deleted a fully
  settled file. The repair commits the cleared checkpoint.
- **Command inbox** (`storage_repair_after_mount`, VFS and stdio). If any
  command-family file had gaps, a torn tail or was removed:
  1. Run the existing `ft_recover` with the `file_tx/commands` port.
  2. If that fails, take the complete valid lines of the best generation:
     the active file, else the stage, else the backup.
  3. Write them to `add_commands.tmp` and call `ft_replace` (93), the path
     add_connector already uses (5306-5340). It is power-cut safe and
     host-tested.
  4. Dropped lines become gap records with cause `COMMAND_LINE_DROPPED`.
- **Catalog.** As in 7.2.

### 7.8 Reformat (L4)

Preconditions, all required:

1. L0 to L3 ran and the last pass still reports errors, or the GC health
   probe or persistence probe still fails.
2. Every P1 to P3 file is fully held: no `DEFERRED` state and no budget
   overflow.
3. Every held buffer still matches its manifest CRC just before the erase.
4. The journal shows no reformat in the last 24 h, unless the previous
   reformat was interrupted.
5. The journal manifest, gaps and `REFORMAT_PENDING` are committed.
6. **Recommended, owner decision (section 14):** ADD has acknowledged every
   held P1 and P2 record as queue evidence before the erase. Queue
   `storage_repair`, generation `storage-repair-v1-<instance>-<gen>`, one
   record per row, reason `LEGACY_RECOVERY`, or `MALFORMED` for fragments
   (the sr engine's rules). Boot has no network, so this means two boots:
   - the first boot holds, transfers to ADD, records `REFORMAT_AUTHORIZED`
     and restarts;
   - the second boot salvages again, checks that the content matches the
     acknowledged manifest, then formats.

Order:

1. `SPIFFS_unmount`, then `SPIFFS_format`, then `SPIFFS_mount` on the
   private structure. The format erases all 2,048 blocks: tens of seconds,
   to be measured on HIL.
2. Journal `FORMATTED`.
3. Rewrite in priority order, P1 first:
   - lq unsettled ranges only, each after its cleared checkpoint commits;
   - segments and journal files byte-exact, with NVS untouched;
   - then P2, P3, P5.
4. Journal progress every 256 KiB.
5. Tails (7.7), probes (7.5), journal `REFORMAT_RESTORED`, then `DONE`.

Mount failure (`SPIFFS_ERR_NOT_A_FS` and similar) is not a reformat trigger.
Nothing can be read through SPIFFS then. The repair reports `MOUNT_FAILED`
and storage_init keeps today's fail-safe: no format, online delivery only.

### 7.9 Journal

The journal lives in NVS namespace `storage_repair`, which older images
never open. Each key is committed atomically. Order: manifest, then gaps,
then state.

| Key | Content | Size |
|---|---|---|
| `state` | version, generation, phase, trigger mask, reset reason, level, attempts, consecutive incomplete, last result/stage/code, flags, CRC | 64 B |
| `manifest` | Up to 32 entries: name[32], size, hold_from, cursor, restored_through, crc32 of the held bytes, obj_id, class, flags (`held`, `patched`, `swap_ready`, `rebuilt`, `tail_fixed`) | up to 1.8 KiB |
| `chunks` | crc32 per 64 KiB of held range, P1 to P3 files, at most 128 | up to 512 B |
| `gaps` | Up to 48 records (file index, cause, SPIFFS code, offset, length), plus acknowledged count and overflow count | up to 600 B |
| `damage` | runtime damage flag and first errno | 8 B |

Wear and capacity:

- **Writes.** The state is written at start and at end of every boot. The
  manifest is written only when it differs from the stored blob (compared
  before writing).
- **Capacity.** Before writing the manifest, `nvs_get_stats` must show
  three times the needed entries free. Otherwise the repair keeps only the
  state and gaps, holds the manifest in RAM, and reports `NVS_COMPACT`. The
  free entry count is reported (section 8) so HIL can size it.

### 7.10 Power loss at each step

On the first boot of 2.7.5 (PENDING_VERIFY), any reset also returns to the
previous image: see section 9.

| Phase when reset | On flash and in NVS | Next 2.7.5 boot | Custody exposure |
|---|---|---|---|
| `STARTED`, `SALVAGED` | Unchanged; nothing written yet | Starts over | none |
| `PATCHED` | Only unreadable spans rewritten, by SPIFFS's own operation | Starts over; a half-done modify is mended by the check | none (the spans were already unreadable) |
| `CHECKING` (pass n) | The check deletes lazily (IXDELE, page flags) and erases only all-deleted blocks when fewer than 2 blocks are free | Starts over. Files in the old manifest that are now missing, shorter or with chunk-CRC mismatches become gaps with cause `CHECK_INTERRUPTED` (exact ranges) | Bytes of files the check deleted. Pages stay physically present unless `gc_quick` erased them, so raw recovery is a follow-up (section 14). |
| `RESTORING` | Restored spans and files; `restored_through`; `.rp` swaps | Finishes ready swaps. Keeps whatever is present and matches the chunk CRCs. The rest of each partly rebuilt file becomes `RESTORE_INTERRUPTED` gaps | Not-yet-rewritten bytes of files the check deleted or L2/L3 evacuated. GC may already have erased the old pages. |
| `VERIFIED` | Repaired store | Reports only | none |
| `REFORMAT_PENDING` | Unchanged | Salvages again; preconditions are re-evaluated | none |
| `FORMATTED`, `REWRITING` | Everything erased; rewritten prefix | All unrewritten held ranges become `REFORMAT_INTERRUPTED` gaps (exact). With precondition 6 those rows are already in ADD custody, and the gap records name the generation. | Without precondition 6: every held byte not yet rewritten |

Every possible loss leaves an exact gap record in NVS, so no loss is silent.
Real exposure needs a power cut inside a window that opens only when the
check deleted a custody file, or the repair evacuated one. A clean store
never opens a window, and neither does a store whose damage the pre-check
patch resolves. A window lasts from seconds (span restores) to about a
minute (in-place rebuild of a multi-megabyte file), and longer for L4. HIL
measures these.

## 8. Reporting to ADD (f)

### 8.1 Log codes

Subsystem `storage`. The repair runs before the network, so a reporter task
(priority idle + 1, 4 KiB stack, started from storage_init) waits for
`add_connector_is_connected()`. It then sends these through
`add_connector_log()`:

| Code | Level | When |
|---|---|---|
| `STORAGE_REPAIR_STARTED` | INFO | Every run: triggers, reset reason, files, bytes to hold, budget, policy |
| `STORAGE_REPAIR_SALVAGED` | INFO / WARN | Held files and bytes, gaps and unreadable bytes, budget shortfall |
| `STORAGE_REPAIR_PATCHED` | WARN | Pre-check NUL spans per file, with SPIFFS codes |
| `STORAGE_REPAIR_CHECKED` | INFO / WARN | Per pass: duration, reports (fix_index, fix_lookup, del_orphan_index, del_page, del_bad_file), errors with SPIFFS codes, used before/after |
| `STORAGE_REPAIR_RESTORED` | WARN | Per file: span-restored, rebuilt, swapped, cursor reset |
| `STORAGE_REPAIR_GAP` | WARN, ERROR above 16 KiB per file | One per gap, up to 16 per boot. Every gap is also sent as evidence (8.2). |
| `STORAGE_REPAIR_TAIL_TERMINATED` | WARN | File and offset |
| `STORAGE_REPAIR_EXPENDABLE_REMOVED` | INFO | Files and bytes |
| `STORAGE_REPAIR_REFORMATTED` | ERROR | Reason, files and bytes rewritten, duration |
| `STORAGE_REPAIR_DEFERRED` | WARN | Budget or attempts guard |
| `STORAGE_REPAIR_RESUMED` | WARN | Previous phase from the journal |
| `STORAGE_REPAIR_FAILED` | ERROR | Stage and code |
| `STORAGE_REPAIR_NOT_NEEDED` | INFO | Once per boot when the check reported nothing |

### 8.2 Gap evidence

Every gap record goes to ADD through `add_connector_transfer_queue_evidence`
(add_connector.c:3780), which waits for ADD's durable acknowledgement:

- queue `storage_repair`;
- generation `storage-repair-v1-<qs instance>-<journal generation>`;
- record ID `gap:<file>:<offset>:<length>`, with the file name's leading
  `/` dropped and `.` written as `_`, so it matches `^[a-zA-Z0-9_:-]+$`;
- reason `MALFORMED`, the existing literal (precedent:
  `storage_recovery_core.c` `report_gap`, line 177);
- raw bytes: this JSON.

```
{"type":"storage_repair_unreadable","file":"/blocked_identity.jsonl","offset":50200,
 "length":251,"spiffs_error":-10004,"cause":"SALVAGE","file_size":3826949,
 "unsettled_from":50055,"class":"attendance","phase":"PATCHED"}
```

`cause` is one of:

- `SALVAGE`, `HEALED`, `CHECK_REMOVED_UNHELD`;
- `CHECK_INTERRUPTED`, `RESTORE_INTERRUPTED`, `REFORMAT_INTERRUPTED`;
- `COMMAND_LINE_DROPPED`, `HEADER_UNREADABLE`;
- `DEDUPE_LOST`, `UNKNOWN_LOST`.

The acknowledged count goes back to NVS. Unacknowledged gaps are retried on
every connect and every boot. ADD stores them as `PRESERVED_UNRESOLVED`
queue evidence (queue_evidence.py), visible through the existing device
queue-evidence API. No ADD change is needed for evidence.

### 8.3 Heartbeat

The heartbeat adds `diagnostics.storage.repair`, appended by
`storage_repair_append(storage)` next to `storage_inventory_append`
(add_connector.c:3065). It follows the same rule: partial health is never
published (`check_diagnostics_allocations.py` gets a stub).

```
"repair": {"state":"COMPLETE_WITH_GAPS","phase":"VERIFIED","policy":"EVERY_BOOT",
  "triggers":["RESET_POWERON","DAMAGE_FLAG"],"generation":7,"level":0,"attempts":0,
  "duration_ms":41250,"check_ms":36900,"mirror":true,
  "files_held":6,"bytes_held":4612345,"budget_bytes":6291456,"unheld_bytes":0,
  "unreadable_bytes":251,"gaps":1,"gaps_reported":1,"gaps_overflow":0,
  "spans_patched":1,"files_rebuilt":0,"files_removed_by_check":1,"tails_terminated":0,
  "expendable_removed":1,"used_before":4930000,"used_after":3540000,
  "check":{"passes":1,"fix_index":0,"fix_lookup":3,"delete_orphaned_index":2,
           "delete_page":5512,"delete_bad_file":1,"errors":0,"last_error":null},
  "reformatted":false,"nvs_free_entries":312,"error_stage":null,"error_code":null,
  "files":[{"name":"/blocked_identity.jsonl","action":"PATCHED","gaps":1,"unreadable_bytes":251}]}
```

Bounds:

- `files` holds at most 8 entries;
- `triggers` holds at most 8;
- strings match `^[A-Z_]+$` (names: printable, up to 32 characters).

`state` is one of `NOT_NEEDED`, `COMPLETE`, `COMPLETE_WITH_GAPS`,
`DEFERRED`, `REPORT_ONLY`, `FAILED`, `REFORMATTED` or `MOUNT_FAILED`.

### 8.4 ADD schema

In `apps/add_backend/zk_add/schemas.py`, three new models:

- `StorageRepairFile`
- `StorageRepairCheck`
- `StorageRepairDiagnostics`

All fields are optional and bounded. Strings use `max_length` plus
`^[A-Z_]+$`, or `PRINTABLE_TEXT` for names, rather than `Literal`, so a
later firmware state never fails validation.

`StorageDiagnostics` gains:

```python
repair: StorageRepairDiagnostics | None = None

@field_validator("repair", mode="before")
@classmethod
def readable_repair(cls, value):
    try:
        return StorageRepairDiagnostics.model_validate(value) if value is not None else None
    except ValidationError:
        return None
```

This mirrors `readable_inventory` (schemas.py:215-221): an unreadable repair
object is dropped and the heartbeat is still accepted.

`device_health.py` (optional, same PR) shows a warning when:

- `state` is `FAILED`, `DEFERRED` or `MOUNT_FAILED`, or
- `gaps_reported < gaps` for more than one hour.

A repair that reported every gap never degrades a device.

### 8.5 Boot health

Gaps are evidence, not a fault. The repair latches no LOCAL_FAILURE for
gaps it has recorded, so `add_connector_boot_health_ready` (4973-4990) and
confirmation proceed once the normal probe and audit pass. A repair that
leaves the store unusable fails the existing probe and is withheld as
today, and 2.7.5 rolls back within its budget.

## 9. Rollback after a repair (g)

- **Formats.** No file format, name, NVS namespace or key that an older
  image reads is changed. The only effects older images can see are:
  - NUL spans in damaged rows;
  - a `\n` after torn tails;
  - lq checkpoints cleared in exactly the cases where `lq_reclaim` would
    clear them;
  - the catalog `file_tx` checkpoint set idle.
- **2.4.12 and 2.5.2.** They read pending and blocked whole-file with
  `fgets`. A NUL row truncates at the NUL and fails to parse. 2.5.2 moves it
  to `corrupt_ords.jsonl` (`CORRUPT_LOCAL_ROW`). 2.4.12 is expected to do
  the same, and oracle A7 checks a whole-file reader. Rows already settled by
  2.7.x replay as they do today. `acked_uids.txt` and `*.pos` are intact.
  They gain from the repair: freed leaks and a working GC end the
  P02/13FL ENOSPC and backpressure.
- **2.6.x and 2.7.2 to 2.7.4.** They behave exactly as 2.7.5 does after the
  repair.
- **Older images do not check.** New damage can build up under them. When
  2.7.5 returns, it salvages and checks again.
- **Reset during the first boot of 2.7.5.** The bootloader returns to the
  previous image with the store mid-repair:
  - files the check deleted and the repair had not yet rewritten are
    missing, and their held bytes were in PSRAM;
  - the NVS journal survives, so the next 2.7.5 install reports them
    exactly (`CHECK_INTERRUPTED` or `RESTORE_INTERRUPTED`);
  - the old image's writes may meanwhile GC the deleted pages.

  This is the residual risk of section 14. The pre-check patch keeps it
  small, because data-page damage no longer makes the check delete files.
- **Leftovers.** A leftover `*.rp` file is ignored by older images and
  finished or removed by the 2.7.5 journal.

## 10. Host test strategy (h)

### 10.1 Pure engine tests

`tests/firmware/storage_repair_host.c` and
`test_storage_repair_host.py` test `storage_repair_plan.c`, the engine's
SPIFFS-free half. They use plain `cc` with ASan and UBSan, like
`test_storage_recovery_host.py`, so they run in the tests/firmware shards
without ESP-IDF. They cover:

- classification;
- hold ranges from lq, dq and ft checkpoints;
- budget and priority;
- gap coalescing;
- journal encode and decode with CRC;
- resume decisions for every phase;
- the attempts guard;
- record-ID sanitising;
- report JSON bounds.

### 10.2 Real SPIFFS harness

`tests/firmware/spiffs_repair_host.c` and
`tests/firmware/tools/check_spiffs_repair.py` run in a new CI step:
`docker run espressif/idf:v5.5.3 python tests/firmware/tools/check_spiffs_repair.py`,
next to `check_spiffs_orphan.py` in the `firmware-production` job.

- **SPIFFS build.** ESP-IDF's own SPIFFS sources from
  `$IDF_PATH/components/spiffs/spiffs/src`. The existing
  `tests/firmware/spiffs_host/spiffs_config.h` matches the device: page
  256, cache and write cache, magic, page check, GC max runs 10. Built as
  `check_spiffs_orphan.py` builds them: `-w`, `-fno-sanitize=alignment`,
  ASan and UBSan.
- **Flash model.** The simulator's NOR model: program clears bits, erase
  sets 0xFF, and a cut tears a program (random prefix and one partial byte)
  or an erase (random bytes), then `longjmp`. All RAM state is dropped,
  PSRAM holds included.
- **NVS model.** A map of atomic blobs that persists across simulated
  boots.
- **Real readers.** A small stdio shim maps fopen, fread, fseek, fwrite,
  stat, rename, remove and opendir onto the SPIFFS API under `/storage`. The
  real `legacy_queue.c`, `durable_queue.c`, `file_transaction.c` and
  `reliability.c` then drain and verify the repaired image, as
  `test_storage_recovery_host.py` does with `-Dfread=...`.

Damage scenarios, each run over many seeds:

| Scenario | Construction |
|---|---|
| 13FL-like | A blocked file of about half the image (1.5 to 4 MB in the 8 MB soak). The data page at span s, just after the cursor, is flagged deleted but still referenced. The probe object has a not-final or deleted index header. Unrelated rows are appended after. |
| P02-like | A pending file of about a third of the image (2.5 MB in the soak) with one referenced data page erased (IS_FREE), or reassigned to another object (DATA_SPAN_MISMATCH, which must not be patched). About 18% of pages leaked by cuts during `SPIFFS_remove` of large files. |
| Leaked / full | Leaked pages up to 68 to 75% used, so pass 1 hits FULL. Escalation L1, then L2, then L3. L4 only with forced preconditions. |
| Index damage | An object index page (span 1 or higher) is deleted or torn, so the check mends or deletes; the rebuild and swap paths run. |
| Torn tails | A half-written row with no `\n` on each append target; EBADMSG before the repair, none after. |
| Duplicates | A copied page gives multiple references, so the check deletes; the restore path runs. |
| Organic | The simulator's workload, extended with every file class (lq cursors, outboxes, quarantine, commands with ft, catalog with ft, segments, acked), with about 20 cuts per run. |
| Cuts inside the repair | A cut at a random HAL operation inside salvage, patch, check, restore or format. Then boots resume from flash and NVS only, until `DONE`. |
| Budget | A tiny budget: `DEFERRED` must not delete anything. |
| Mirror | Every scenario re-run with the metadata mirror. Flash images and reports must be identical. |

Oracles, checked after every completed repair:

- **A1 Custody.** Every record the model knows was committed (fsync
  returned) and unsettled, and that an independent pre-repair page read
  found readable, is present byte-exact at its offset. The one exception is
  the prefix-less rebuild, where it is at `offset - cursor` with a cleared
  cursor. A cut inside the repair is allowed to lose a record only if a gap
  with cause `*_INTERRUPTED` covers it. The harness counts those cases, and
  the soak reports them per policy.
- **A2 Gaps.** The reported gaps cover exactly the bytes unreadable before
  the repair, plus interrupted ranges. No readable surviving byte is
  reported as lost.
- **A3 Drains.** The real lq drains read each queue to its end with no
  DQ_IO. Rows touching a gap come back with NUL or `evidence_required`.
  `dq_audit_step` completes every lane without a gap, and `ft_recover`
  succeeds.
- **A4 Health.** `rel_open_append` succeeds on every append target, and the
  persistence and GC probes pass. A 2,000-tick follow-on workload sees no
  ENOSPC below 75% used and no stuck GC. `SPIFFS_info` used is within one
  block per file of the live bytes: no leak.
- **A5 Idempotence.** A second `SPIFFS_check` after `DONE` reports no FIX or
  DELETE.
- **A6 Removals.** Only expendables are ever removed outside the check and
  L2/L3, and every evacuated file is back.
- **A7 Rollback reader.** A 2.5.2-style whole-file reader parses every
  repaired text file; NUL rows are the only malformed rows.
- **A8 Reformat.** Runs only under its preconditions. After `DONE` every
  held record is present. A cut during the format or rewrite yields exact
  `REFORMAT_INTERRUPTED` gaps.

Size and runtime:

- **CI.** 1 MB images, 48 seeds across the scenarios and 16 seeds of cuts
  inside the repair. Budget under 2 minutes in the job.
- **Soak.** `tests/firmware/tools/spiffs_repair_soak.py` is run by hand: 8
  MB, 8 or more seeds with about 20 cuts. It prints the simulator's metrics
  (stuck, leak, lost lines, check cost) for policy 0, policy 1, the
  research policy 5 and 2.7.5, as release evidence.

### 10.3 Contract and boot-order tests

`tests/firmware/test_storage_repair_boot_contract.py` asserts on the
source:

- in `storage_init`, `storage_repair_boot()` comes before
  `seen_cache_init()` and `esp_vfs_spiffs_register()`;
- `format_if_mount_failed` is still false;
- `SPIFFS_format` appears only in the guarded reformat function;
- the check callback calls no `SPIFFS_` function and yields;
- the repair is compiled out for Hikvision and the recovery role.

ADD tests go in `tests/unit/test_storage_repair_diagnostics.py`:

- a lenient `repair` object is accepted and an invalid one is dropped;
- the heartbeat is still accepted;
- the optional device-health warning.

## 11. Expected outcome on known devices

| Device | Expected |
|---|---|
| 13FL | Patch span 200 of the blocked file (DELETED) to NUL. The check drops the damaged probe object and frees leaks; the blocked file stays in place. The probe passes. The head row (50,055) drains as one MALFORMED evidence record and the queue drains. One gap is reported, expected to be 251 bytes. |
| P02 | Span 1968 of pending: IS_FREE is patched before the check. With DATA_SPAN_MISMATCH, the check deletes pending.jsonl and it is rebuilt from PSRAM by swap or in place. The leak of about 1.4 MB is freed and the backpressure ends. |
| P06 | The legacy EIO is handled the same way. |
| FAISALABAD-02 (ENOENT appends at 10%) | Header or index damage on an append target, mended or rebuilt. |
| LAHORE-02 (ENOSPC at 26%) | Leak freed. |
| MULTAN-01, LAHORE-02 (EBADMSG) | Torn tails terminated, or DQ_CORRUPT shown as gaps. |
| 3FL | Journal segments are held and kept; OTA refusal unchanged. |

## 12. Other 2.7.5 items

1. **Inventory reads.** `storage_inventory_core.c` `scan_file` (56) moves
   from stdio with `_IONBF` (one `read()` per byte on newlib) to
   `open`/`read`/`lseek` through the shared `storage_span_reader`:
   - it reads 4,016-byte chunks on 251-byte data-span boundaries, which is
     one read call per chunk;
   - a failed chunk is re-read per span;
   - first-error offsets are therefore exact data-page boundaries, and
     unreadable bytes are counted per span (251, not 256).

   The 2.7.4 two-pass budget, the yield cadence and the 32-bit `off_t`
   handling stay. `storage_inventory_host.c` changes with it.
2. **OTA header buffer.** `ota_manager.c` `signed_request`: the
   `esp_http_client_config_t http` at line 244 gets
   `.buffer_size_tx = 1024`. The poll's signed headers exceed the default
   512, which logs `HTTP_HEADER: Buffer length is small to fit all the
   headers` every 60 s. A source assertion goes into the existing OTA test.
3. **Recommended, owner decision: `CONFIG_SPIFFS_USE_MTIME=n`** in
   `sdkconfig.zkt.defaults`.
   - Today every writable open rewrites the object index header for the
     modification time (`vfs_spiffs_update_mtime`, esp_spiffs.c:552-554).
     Every append therefore costs one extra header rewrite, which is one
     more power-cut window of the kind that leaves unfinalized and
     half-deleted headers.
   - No firmware code reads `st_mtime`, the on-flash format is unchanged,
     and older images keep writing the field harmlessly.
   - The soak measures the damage rate with and without it.

## 13. Implementation file plan

The commits are small and logical. Tests land with their code.

1. **Shared span reader and inventory.**
   - `main/storage_span_reader.h/.c`: new. Chunked reads aligned to 251-byte
     spans, per-span fallback and gap coalescing, behind a read port with
     two adapters: SPIFFS API, and VFS `open`/`read`/`lseek`.
   - `main/storage_inventory_core.c`: use it, with no stdio.
   - `main/CMakeLists.txt`: the new source.
   - `tests/firmware/storage_inventory_host.c` and
     `test_storage_inventory_host.py`: span semantics.
2. **OTA buffer.**
   - `main/ota_manager.c`: `.buffer_size_tx = 1024`.
   - An assertion in an existing OTA source test.
3. **Checkpoint helpers.**
   - `main/file_transaction.c/.h`: `ft_checkpoint_idle()` (phase 0,
     generation + 1, CRC).
   - `main/legacy_queue.c/.h`: `lq_checkpoint_cleared()`. Optionally
     `storage_recovery.c` `before_retire` reuses it.
   - Host tests in `tests/firmware/file_transaction_host.c` and
     `legacy_queue_host.c`.
4. **Engine.** Two new files, neither with an ESP-IDF dependency.
   - `main/storage_repair_plan.h/.c` is pure logic with no SPIFFS
     dependency, so plain `cc` can test it. It covers:
     - classification;
     - hold ranges from lq, dq and ft checkpoints;
     - budget and priority;
     - gap coalescing;
     - the journal state machine and its encode/decode;
     - resume decisions and the attempts guard;
     - record-ID sanitising;
     - the report struct.
   - `main/storage_repair_core.h/.c` drives the SPIFFS API. It includes
     `spiffs.h` and `spiffs_nucleus.h` and is tested by the harness of
     10.2. It covers:
     - salvage into port-allocated memory;
     - pre-check patch by SPIFFS code;
     - expendables;
     - the escalation driver: the check through a port that runs
       `SPIFFS_check` plus check-event counters;
     - verification;
     - restore: span, swap, in place;
     - tails;
     - lq and dq checkpoint fixes through the NVS port;
     - probes;
     - reformat.
   - Ports: NVS blob load/store, memory alloc/free, yield, clock, log,
     remount/format.
   - The metadata mirror HAL lives here as a wrapper over raw
     read/write/erase ports.
   - `tests/firmware/storage_repair_host.c` and
     `test_storage_repair_host.py`: the tests of 10.1 for
     `storage_repair_plan.c`.
5. **Real SPIFFS harness.**
   - `tests/firmware/spiffs_repair_host.c`: NOR model, stdio shim,
     scenarios, oracles.
   - `tests/firmware/tools/check_spiffs_repair.py`.
   - `tests/firmware/tools/spiffs_repair_soak.py`: manual.
   - `.github/workflows/add-ci.yml`: the docker step in
     `firmware-production`.
6. **Device glue and boot order.**
   - `main/storage_repair.h/.c`: new.
     - The private mount: `esp_spiffs_t`, `spiffs_api_*` HAL or the
       mirror, the yielding check callback.
     - PSRAM budget.
     - NVS ports for `storage_repair`, `legacy_queues`, `add_legacy`,
       `durable_queue` and `file_tx`.
     - Triggers: `esp_reset_reason`, the journal, the attempts guard.
     - `storage_repair_after_mount()`: the command inbox through
       `ft_recover`/`ft_replace`, and the catalog checkpoint.
     - The damage flag API: `storage_repair_note_damage()`.
     - The reporter task: logs and gap evidence.
     - `storage_repair_append()` for the heartbeat.
   - `main/zone_lite.c` `storage_init`: the new order of 5.1.
   - `main/queue_store.c`: `storage_repair_note_damage()` in
     `record_queue_result`, `qs_local_end_file`, `qs_local_end_legacy` and
     probe failure.
   - `main/storage_inventory.c`: the damage flag on unreadable bytes.
   - `main/add_connector.c`: `storage_repair_append(storage)` in the
     heartbeat.
   - `main/CMakeLists.txt`:
     - sources;
     - the spiffs component directory and `spiffs/src` as private
       includes;
     - `esp_partition` in REQUIRES;
     - repair sources excluded for Hikvision and the recovery role.
   - `tests/firmware/tools/check_diagnostics_allocations.py`: the stub.
   - `tests/firmware/test_storage_repair_boot_contract.py`.
7. **ADD.**
   - `apps/add_backend/zk_add/schemas.py`: the repair models and the
     lenient field.
   - Optional `device_health.py` rule.
   - `tests/unit/test_storage_repair_diagnostics.py`.
8. **Prevention (if approved).** `sdkconfig.zkt.defaults`:
   `CONFIG_SPIFFS_USE_MTIME=n`, with a CI grep in `add-ci.yml`'s
   build-defaults step.
9. **Release 2.7.5.** As for 2.7.4 (`89bb404`):
   - `firmware/zone_lite/CMakeLists.txt` version;
   - the 2.7.4 application SHA-256 as a qualified predecessor in
     `upgrade_guard.c/.h`;
   - `storage_upgrade.c` `BASE` gains `2.7.4`;
   - ADD `storage_contract.py`, `ota.py` and the deploy scripts;
   - `tests/unit/test_platform_27_release.py`,
     `test_storage_contract.py` and `test_hil_publication.ps1`;
   - `docs/zone-lite-2.7.5-hil.md`.

Lint and CI for every commit:

- `python -m ruff check apps/add_backend apps/add_provisioner apps/provisioning_companion tests scripts firmware/zone_lite/tools/provision_zone_lite.py`
- `tests/firmware/*` and `tests/unit/*` (in `add-test-env:local`, with
  `-e PYTHONPATH=/work/apps/add_backend`)
- `scripts/check_repository_contract.py`
- the ESP-IDF 5.5.3 production build

## 14. Owner decisions and residual risks

Decisions requested:

1. **Gate L4 on ADD custody.** Recommended: reformat only after ADD has
   acknowledged every held P1 and P2 record, which takes two boots. Without
   the gate, held content survives the format and rewrite only in PSRAM, and
   a power cut then loses every unrewritten byte. The loss would still be
   reported exactly. L0 to L3 should resolve every known field case, so the
   gate should rarely matter.
2. **`CONFIG_SPIFFS_USE_MTIME=n`.** Recommended.
3. **The every-boot policy.** This is the default. Keep it if HIL measures
   the whole repair on a clean store at about 60 s or less; a few seconds is
   expected with the mirror. Otherwise switch to `SR_POLICY_TRIGGERED`.

Residual risks and follow-ups:

- **Exposure windows.** Only damaged stores have them: power cut inside
  `CHECKING` or `RESTORING` while a deleted or evacuated file is held only
  in PSRAM (7.10), or a reset during the first boot of 2.7.5 followed by
  writes under the previous image. Both are reported exactly.
  - **Follow-up.** Deep recovery: rebuild such objects from raw flash by
    `obj_id` and span, using `storage_orphan_core` and the manifest's
    `obj_id` and chunk CRCs, which are recorded now so no format change is
    needed later.
- **Damaged segmented-lane records.** These still hold their lane with
  DQ_CORRUPT. A segmented evidence path is a follow-up. None of the three
  2.7.4 inventories lists a segment file.
- **Hikvision stores.** Separate design.
- **NVS capacity.** Verify the 24 KiB partition's free entries on HIL
  (`nvs_free_entries` in the report) before 2.7.5 leaves HIL.
