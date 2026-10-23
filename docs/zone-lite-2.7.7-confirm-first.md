# Zone Lite 2.7.7: confirm first under a narrow storage exception

Status: implemented on `release/zkt-2.7.7` (version not bumped yet), with the
first review round's two critiques folded in: section 13 lists every point,
what changed and what was rejected and why. Where sections 1 to 11 and section
13 disagree, section 13 is what the code does. Nothing has run on a device.
Branch `release/zkt-2.7.7` from `origin/main` 2dbd314 (2.7.6 merged, #401).
The HIL runbook is [zone-lite-2.7.7-hil.md](zone-lite-2.7.7-hil.md).

Owner decision, 21 October 2026, on 2.7.6's open decision 3
([zone-lite-2.7.6-last-resort-reformat.md](zone-lite-2.7.6-last-resort-reformat.md),
sections 8 and 15): **(a) confirm first**. An unconfirmed image may mark
itself valid although storage health fails, but only through a narrow
boot-health exception: a wedged store, custody intact, ADD connected, the OTA
path proven. Implement exactly that, as narrowly as possible.

Scope of 2.7.7: that exception, what it needs to be safe and visible, and
the WebSocket (and IPC) task stacks. Nothing else.

**Summary.** On P02 the first 2.7.6 boot found no block an unconfirmed image
may take and 67 blocks a confirmed image may take, so it rolled back. 2.7.7
adds one more way to confirm: when the boot repair ended `DEFERRED` only for
room, every custody byte is accounted for on flash, the planner's own
estimate says the custody blocks give the room, ADD has acknowledged the
evidence and the boot, the OTA path needs no writable store, and every other
boot-health term passes, the image records why, tells ADD, marks itself
valid and restarts once at a capture safepoint. The next boot is a confirmed
2.7.7 boot, and 2.7.6's confirmed round, unchanged, reformats custody blocks
in place, after which the second run checks, compares, compacts and probes
the store and ends `REPAIRED`. Every other storage failure rolls back
exactly as in 2.7.6. Restarts are bounded and backed off; a device whose
store the round still cannot repair stays on 2.7.7, connected and
OTA-capable, and its deployment ends `FAILED` with its own reason so that
ADD can offer a fix.

## 1. Evidence

### 1.1 Peshawar-02, first 2.7.6 boot (21 October 2026, 13:29 UTC, from 2.5.2)

ESP `e0:72:a1:d7:05:c4`, connector `bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e`,
unconfirmed. Repair run 2 (the boot's report):

| Field | Value |
|---|---|
| result, mode | `DEFERRED`, `FULL` |
| error | `error_stage` `full`, `error_code` -10001, 21.4 s, check skipped |
| holds | 6 files, 3 held, 3,577,196 bytes of a 4,357,816-byte budget; `files_unheld_custody` 0 |
| custody | `gaps` 0, `unreadable_bytes` 0, `records_dropped` 0 |
| work | 153 spans rewritten |
| room | `room_needed_pages` 1, `room_erased_pages` 0 |
| store | 4,962,772 of 7,703,441 bytes used |
| resources | `stack_free_bytes` 3,640; internal largest 102,400 (boot), 63,488 (runtime); NVS available 264 -> 265 |
| reclaim | `area` `READY`, `trigger` `full`, `short` `candidate`, `rounds` 0, `candidates` 0, `unconfirmed_blocks` 0, `custody_blocks` 67, `index_blocks` 88, `excluded_blocks` 1,817, `low_magic_blocks` 68, `ms` 2,570; `failed`, `held_mismatch`, `records_open`, `reported` 0 |

Logs, in order: `STORAGE_REPAIR_RECLAIM_EXPOSURE` "full: 34 pages short;
blocks: 0 candidates (0 holding nothing any image reads, 67 custody), 88 with
an index page, 1817 excluded, 68 with a weak magic; unconfirmed image",
`STORAGE_REPAIR_RECLAIM_SHORT` "no round: no candidate block",
`STORAGE_REPAIR_BLOCKED` "/pending.jsonl: 51 spans SPIFFS cannot read, the
first at offset 493968; their bytes stay on flash until there is room",
`STORAGE_REPAIR_DEFERRED`; at 13:31:08 2.7.4's inventory logged
`STORAGE_FILE_UNREADABLE` "pending.jsonl: 12801 of 2526978 bytes unreadable,
first at offset 493968 (errno 5)". 12,801 bytes is exactly 51 spans of 251
bytes, and 493,968 is span 1,968: every byte the VFS cannot read is one of
the 51 spans the repair holds on flash (valid copies the index does not name
yet; the index rewrite that names them is the one page the store lacks).

The deployment went `BOOTED_PENDING` `WAITING_FOR_RUNTIME_HEALTH`; the image
rolls back to 2.5.2 after the 900 s budget (about 13:45 UTC).

### 1.2 What the histogram says

`plan_candidates` (storage_repair_core.c) runs `srx_plan_block` over every
block with `unconfirmed = false` and sorts each block into one bucket. The
plan's reasons are tried in this order, the first one wins:

1. `MISLABELED`: a lookup entry disagrees with its page. Not counted.
2. `NO_GAIN`: nothing to drop (no deleted page that no valid index names).
   Not counted.
3. `MAGIC`: the block's magic is invalid or weak (`low_magic_blocks`).
4. Over the kept pages: any live header or index page -> `INDEX`
   (`index_blocks`); else any page `keep_class` calls `SRX_KEEP_EXCLUDED`
   -> `EXCLUDED` (`excluded_blocks`, with `OK` blocks of an open or source
   record).
5. Otherwise `OK`: `custody_blocks` when a kept page is custody (a confirmed
   image may take it), else `unconfirmed_blocks` (any image may take it).

On P02: 67 + 88 + 1,817 + 68 = 2,040 counted blocks; the other 8 of 2,048
are `NO_GAIN` (or `MISLABELED`). All 68 weak-magic blocks of an 8 MiB store
had something to drop. **Every counted `OK` block has at least one dropped
page**: `srx_plan_block` returns `NO_GAIN` before any other reason when
nothing is dropped (after its entry-0 rule).

### 1.3 Why the model said 731 and the device 0

`keep_class` sorts a kept page by its owner (the object named by its lookup
entry, or for a deleted page by the index that names it):

| Kept page | Class |
|---|---|
| deleted, named by no valid index | `NONE` (read by nothing) |
| of an object with no header anywhere on flash (visible, hidden or torn) | `NONE` (leaked) |
| of an object that has a header but is not live, or not mapped (a hidden header left by a removal that stopped, an implausible one) | `EXCLUDED` |
| of a live file that is not `reclaimable_file` (ADD outboxes and `.pos`, the command inbox family, `processed_commands.txt`, `add_cancelled.txt`, `acked_uids.txt`, journal and queue segments, `.bak`/`.tmp`/`.rpr`, unknown names) | `EXCLUDED` |
| a page whose own header says index while its lookup entry names a live file | `EXCLUDED` |
| the last data page of a reclaimable file | `EXCLUDED` |
| any other data page of `/pending.jsonl`, `/blocked_identity.jsonl`, `/corrupt_ords.jsonl`, `/add_corrupt.jsonl`, `/add_corrupt.bak`, the identity catalog or a probe file | `CUSTODY` |

So a block holds "nothing any image reads" only if *every* kept page in it
is `NONE`. The 2.7.6 harness built P02 by writing the half-removed file
alone first and then deleting its header outright: its pages were leaked
(`NONE`) and clustered, so about a third of the store qualified. Measured
again on this branch (`check_spiffs_repair.py reclaim-p02 --kib=8192`):

| | candidates | nothing read | custody | index | excluded | weak |
|---|---|---|---|---|---|---|
| 2.7.6 model, unconfirmed | 731 | 731 | 719 | 92 | 438 | 68 |
| 2.7.6 model, confirmed | 1,438 | 729 | 709 | 94 | 448 | 68 |
| **P02, 21 October** | **0** | **0** | **67** | **88** | **1,817** | **68** |

On the device nearly every block (1,817 of 2,048) holds at least one
`EXCLUDED` page. Two mechanisms, both in the code above, give that; the
field report cannot tell them apart:

- **E1: interleaved files that are never windowed.** 2.5.2 appends to
  `/pending.jsonl` for every punch, to `acked_uids.txt` for every delivery
  and to its ADD outboxes, and each append also moves the file's header. The
  allocator fills pages in write order, so these files' pages are mixed
  through every block. P02's other two held files hold 1,050,218 bytes
  (about 4,200 pages, two per block on average).
- **E2: a removal that stopped after flagging its header.** SPIFFS's remove
  flags the header (IXDELE) first and frees pages from the end (2.7.5
  design, 1.2 item 3). If it stops, the header stays on flash, hidden: its
  object "has a header", is not live, and every one of its leaked data pages
  is `EXCLUDED`, not `NONE`. 2.7.5 removed P02's 51 orphan index pages, and
  the 2.7.6 design attributes about 1.4 MB "used outside every file" to that
  removal; if its header is hidden rather than deleted, those roughly 5,500
  pages, written over months between the live files, exclude almost every
  block.

A prototype builder (scratch, section 9.2) reproduces the field histogram
with E2 plus a moderate E1, and nothing else changed:

| Prototype (8 MiB, interleaved writes, hidden header) | candidates | nothing read | custody | index | excluded | weak | pages short |
|---|---|---|---|---|---|---|---|
| model A, unconfirmed 2.7.6 | 0 | 0 | 62 | 86 | 1,830 | 67 | 35 |
| model B, unconfirmed 2.7.6 | 0 | 0 | 75 | 82 | 1,820 | 68 | 35 |

With E1 alone (header deleted, side files appended every iteration) the
excluded count reaches 1,700 with about 190 custody blocks; matching 1,817
needs the hidden header or an even denser never-windowed file. 2.7.7 adds
the breakdown that settles it (section 6.3): the first 2.7.7 boot on P02
reports which reason, and which file, excludes how many blocks, before it
writes anything.

Neither mechanism is something the confirmed rules should take: a hidden
header may be a manifest's remnant, and the never-windowed files are exactly
the ones 2.7.6 keeps out of every window on every image. 2.7.7 changes no
planner rule.

### 1.4 SLICTOWER 13FL

2.4.12, 61% used, a 3.8 MB `blocked_identity.jsonl` unreadable from about
50 KB, the probe ENOENT. Its first 2.7.6 boot has not run. Whether it can use
the exception depends on facts only that boot reports: the gate requires
`files_unheld_custody` 0 (its custody may exceed the 4,357,816-byte hold
budget) and `gaps` 0 (its unreadable head must come back from a page on
flash). Section 11 lists it as an open point.

### 1.5 What the code does today

**Boot health (ZKT, non-local confirmation path)**,
`add_connector_boot_health_ready()`:

- `storage_upgrade_ready()` and `zj_runtime_boot_ready()`;
- storage: `esp_spiffs_info` succeeds; `qs_health()` observed,
  `recovery_complete`, `persistence_verified`, no `last_error`, no
  `persistence_probe_error`; the LED is neither `LOCAL_FAILURE` nor `FATAL`;
- ADD: `s_connected`; the outbox and heartbeat tasks and the outbox buffer;
  no worker start failure; the ORDS worker started, not waiting for a
  resource, ticking within 90 s; the outbox ticking within 90 s;
- identity: a catalog generation, or a terminal snapshot with users;
- terminal: online, `ONLINE` or `RECOVERING` stable for 2 min
  (`ZONE_LITE_RECOVERY_STABILITY_MS`), user and attendance counts known.

**Confirmation** (`ota_manager.c`, `confirm_or_report_rollback`): report
`BOOTED_PENDING` `WAITING_FOR_RUNTIME_HEALTH` until ADD acknowledges it; then
every 5 s `boot_confirm_step` over boot health, `add_connector_boot_local_failure()`
and `add_connector_boot_environment_ready()`. `ACCEPT`: report
`BOOTED_PENDING` `RUNTIME_HEALTHY`, `esp_ota_mark_app_valid_cancel_rollback()`,
journal `RECONCILING`, report `RECONCILING`, then
`acknowledge_pending_success()`. `EXPIRED` (900 s accountable) or `CAPPED`
(4 h): report `FAILED` and `esp_ota_mark_app_invalid_rollback_and_reboot()`.

**`RECONCILING` -> `SUCCEEDED`** (`acknowledge_pending_success`) needs
`add_connector_ota_reconcile_ready()`: full boot health **and** certified
ADD source coverage with the cursor at the terminal's attendance count.
While it is not ready, `ota_task` loops back before `fetch_assignment`: a
deployment stuck in `RECONCILING` blocks every further OTA to that device,
and ADD cannot cancel a `RECONCILING` deployment (campaign cancel touches
only `PENDING`, `OFFERED` and `DOWNLOADING`; revoking the release only pauses
campaigns: nothing in ADD moves a `RECONCILING` deployment, only the device
does; section 13, H3).

**The OTA path of 2.7.7** (not the 2.6.17 bridge, not the 2.7.0 writer, so
`uses_local_boot_confirmation()` is false), call by call:

| Step | Code | What it needs |
|---|---|---|
| poll | `ota_task`: `advance_failed_boot_rollback`, `advance_reader_rollback` | nothing for 2.7.7 (bridge and writer only) |
| assignment | `fetch_assignment`: `signed_request` (HTTPS, HMAC), `save_journal` | NVS `zone_ota` |
| capability | `report_capability` | HTTPS |
| precheck | `perform_update` -> `zj_ota_before_download` -> `legacy_evidence`: `nvs_open("zkt_journal")` read-only; `qs_local_read_begin` (the budget lock); `opendir`/`readdir(ZJ_DEVICE_DIRECTORY)` | NVS read; **a SPIFFS directory read through the mounted VFS** |
| download | `esp_https_ota_*` into the next OTA slot; `save_journal` every 64 KiB | the OTA partition; NVS; TLS in PSRAM (`CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC`) |
| verify, select | `esp_partition_get_sha256`; `save_journal` `READY_TO_BOOT`; `esp_https_ota_finish` | the partition; NVS; otadata |
| restart | `wait_for_capture_safepoint`: `add_connector_claim_ota_restart` (30 min, then regardless); no `zj_owner_quiesce` for 2.7.7 | nothing |
| report | `report_state` -> `post_json` | HTTPS |

No step writes the store. One step reads it: the legacy-evidence directory
listing, which fails (`JOURNAL_OTA_STORAGE_UNAVAILABLE`) when the VFS is
not mounted. Heartbeats (`heartbeat_task`, RAM) and logs
(`add_connector_log`, sent straight over the WebSocket; the repair's own 24
lines are kept in RAM) need no store either. Section 3 gates on the read;
section 5.3 removes it for boots whose store is not mounted.

## 2. Rules

The 2.7.5 rules R1 to R4, R6, R7 and 2.7.6's R5, R8 to R12 stand unchanged.

- **R13 One exception, exactly gated.** An image whose storage health fails
  may confirm only through the gate of section 3, evaluated on its first,
  unconfirmed boot of an ADD OTA deployment. Every other path, and every
  other storage failure, behaves exactly as in 2.7.6.
- **R14 Evidence before the irreversible step.** Before
  `esp_ota_mark_app_valid_cancel_rollback()`, ADD has acknowledged a
  heartbeat carrying the gate's evidence and the `BOOTED_PENDING` report
  naming the exception, with a receipt for this deployment and image; every
  gap record of the store is acknowledged; the exception's own record is on
  NVS.
- **R15 Confirming changes no reformat rule.** The custody round is 2.7.6's
  confirmed round, unchanged: the same eligibility (6.1 there), the same
  redo records, the same recovery. Confirmation only makes `pending_verify`
  false on the next boot.
- **R16 Bounded.** One controlled restart per confirmation, at most three
  in all with 1 h and 6 h backoff and only after progress (section 4.4). No
  restart loop; the repair's own attempts guard still bounds crashes.
- **R17 Manageable.** Heartbeats, logs and OTA never need a writable store,
  and a deployment confirmed under the exception never blocks OTA for good
  (section 4.5).

## 3. The gate

Evaluated by a new pure module, `storage_exception.c` (no ESP-IDF;
host-tested), from inputs the glue collects. It returns `ELIGIBLE` or the
first failing reason (a code reported to ADD, section 7.1). Every term must
hold, and all of them together for 120 s
(`ZONE_LITE_RECOVERY_STABILITY_MS`) across at least two heartbeats ADD
acknowledged. P02's 21 October values are in the last column, with the new
fields of section 6.3 marked "new".

### 3.1 Which boots

| # | Term | P02 |
|---|---|---|
| W1 | ZKT production image (not Hikvision, not the bridge, writer, recovery, journal-writer or factory-trial roles) | yes |
| W2 | the running partition is `ESP_OTA_IMG_PENDING_VERIFY` and the repair ran with `pending_verify` | yes |
| W3 | the OTA journal is `READY_TO_BOOT` for a deployment, its `target_version` is the running version and its `image_sha256` equals the running partition's SHA-256 (the image arrived by this ADD OTA deployment, not by USB) | yes |
| W4 | no exception record of this deployment says `UNREPAIRED` (one exception per deployment) | yes |

### 3.2 The repair deferred for room only

| # | Term | P02 |
|---|---|---|
| A1 | `result` `DEFERRED`, `mode` `FULL` | yes |
| A2 | `error_stage` `full` (code -10001) or `gc_unsafe`, and `reclaim.trigger` the same | `full` |
| A3 | `reclaim.short` `candidate` (no block the unconfirmed image may take, or not enough of them) | `candidate` |
| A4 | `reclaim.area` `READY` | `READY` |
| A5 | `reclaim.failed`, `held_mismatch`, `records_open`, `reported`, `sources` all 0; `store_blocked` false | 0 |
| A6 | the attempts guard did not act (`previous_failed` false; not `REPORT_ONLY`, `STOPPED`, `NORMALIZED`), the private mount succeeded, `storage_repair_storage_allowed()`, and the VFS is mounted (`esp_spiffs_info` succeeds) | yes |
| A7 | `check_deleted_live` 0, `check_delete_bad_file` 0 | 0 (check skipped) |

### 3.3 Custody intact

| # | Term | P02 |
|---|---|---|
| C1 | `files_unheld_custody` 0, and no file entry has `UNHELD` | 0 |
| C2 | `gaps` 0, `unreadable_bytes` 0, `records_dropped` 0, `interrupted_losses` 0, `commands_damaged` false | 0 |
| C3 | every gap record in NVS acknowledged by ADD (`records_pending` 0) | 0 |
| C4 | every unreadable byte is held on flash and reported: 2.7.4's inventory has finished and is complete; every file it finds unreadable is a file the repair reported `BLOCKED`, with `unreadable_bytes` at most `blocked_spans` x 251, `first_error_offset` at or past `blocked_offset`, errno `EIO`; no other file unreadable | 12,801 = 51 x 251 at 493,968 |

### 3.4 Room by the planner's own estimate

| # | Term | P02 |
|---|---|---|
| P1 | `reclaim.want` (pages short, already logged in the exposure line) above 0 | 34 (new field) |
| P2 | `reclaim.custody_gain`, the pages the confirmed rules would drop from the custody blocks, at least `want` | at least 67 (new field) |
| P3 | `custody_blocks` at least 1 and at most the confirmed cap (256) | 67 |

P2 is the planner's own count: `custody_gain` is the sum of `plan.gain` over
exactly the blocks a confirmed round would rank (section 6.2).

### 3.5 Runtime storage evidence that only says "no room"

The storage terms of boot health are the ones excepted; what they show must
be explained by the deferral.

| # | Term | P02 (expected) |
|---|---|---|
| S1 | `qs_health()` observed and available | yes |
| S2 | `persistence_probe_error` 0, `ENOENT` or `ENOSPC` (no free page); `last_error` 0, `ENOSPC` or `ENOENT`; read failures allowed only as C4 explains them | probe `ENOENT` |
| S3 | the LED is not `FATAL`; `LOCAL_FAILURE` only if every incident of this boot was raised by a store write that failed for room (a new cause count, section 6.4) | to be read on P02's 2.7.6 heartbeats |

### 3.6 ADD and the OTA path

| # | Term |
|---|---|
| D1 | the ADD WebSocket is connected (`add_connector_is_connected`; authenticated by the device token at the upgrade) |
| D2 | a heartbeat was acknowledged by ADD within the last 45 s (three heartbeat periods; new acknowledgement tracking, section 6.4). Every heartbeat of a boot whose gate is evaluating carries `ota.storage_exception`, so an acknowledged one proves ADD stored the state the operator reads (14.2 item 1 simplified "carried `ELIGIBLE`" to "any heartbeat": the ack handler cannot see the payload it acknowledged, and the evaluating boot's heartbeats all carry the exception) |
| D3 | `BOOTED_PENDING` with `error_code` `STORAGE_REPAIR_EXCEPTION` was accepted with a receipt that `ota_progress_receipt_matches` (deployment, state `BOOTED_PENDING`, target version, `application_sha256` equal to the running digest) and `confirm` true. Revoking 2.7.7 in ADD therefore stops any exception not yet taken |
| D4 | the OTA precheck passes now: `zj_ota_before_download`'s legacy-evidence check returns no error (read-only) |
| D5 | NVS writes work: the exception record is written and read back (section 4.1, step 3) |

### 3.7 Every other boot-health term

`add_connector_boot_health_ready()` is split into named terms
(`add_connector_boot_health_blockers()`, a bit mask, same conditions, same
order) without changing its result. The exception requires every term to
pass except exactly these: `qs` `recovery_complete`, `persistence_verified`,
`last_error`, `persistence_probe_error` (bounded by S2) and LED
`LOCAL_FAILURE` (bounded by S3). In particular `storage_upgrade_ready`,
`zj_runtime_boot_ready`, `esp_spiffs_info`, the LED's `FATAL`, ADD, the
workers, identity and the terminal terms all still apply.

### 3.8 What the gate does not do

It never changes `boot_confirm_step`'s budget: the gate is checked inside
the same loop, against the same 900 s accountable budget and 4 h cap. If it
is not `ELIGIBLE` when the budget runs out, the image reports `FAILED`
`BOOT_HEALTH_TIMEOUT` and rolls back exactly as 2.7.6 does, and the
heartbeat has said why (`REFUSED` with the reason) all along.

## 4. Confirming, and what follows

### 4.1 The sequence (unconfirmed boot)

1. The boot repair runs as in 2.7.6 (with the new report fields).
2. `confirm_or_report_rollback` reports `BOOTED_PENDING`
   `WAITING_FOR_RUNTIME_HEALTH` as today and loops every 5 s. Full boot
   health still wins: if it passes, the 2.7.6 path runs unchanged.
3. Otherwise the gate is evaluated; the heartbeat carries its state
   (`EVALUATING`, `REFUSED` + reason, `ELIGIBLE` + since). After 120 s of
   `ELIGIBLE` and D2:
   1. NVS `storage_repair/exception` := `CONFIRMING` (deployment CRC, image
      tag, the gate's evidence summary), committed and read back (D5).
   2. Report `BOOTED_PENDING` `STORAGE_REPAIR_EXCEPTION` with an
      `error_message` summing up the evidence; require the receipt (D3).
      Failure: try again next loop (the budget keeps counting).
   3. Evaluate the gate once more; if it no longer holds, the record goes
      back to `NONE` and the loop continues.
   4. `esp_ota_mark_app_valid_cancel_rollback()`. Failure: as 2.7.6
      (`FAILED`, rollback).
   5. Record `CONFIRMED`; journal `RECONCILING`; report `RECONCILING`
      `STORAGE_REPAIR_EXCEPTION` (best effort); log
      `STORAGE_REPAIR_EXCEPTION_CONFIRMED`.
   6. Wait for a capture safepoint (`add_connector_claim_ota_restart`, then
      regardless after 30 min, as an OTA restart does); record
      `restarts` 1 and the time; `esp_restart()`.

### 4.2 Why restart rather than run the round in the same boot

- The engine runs on a private mount before the VFS exists, and nothing else
  may write the store during a round (R12; the held-CRC oracle and every
  harness oracle assume it). At runtime about ten owners hold the store
  (queue store, legacy queues, outboxes, the command inbox, the catalog, the
  inventory, the seen cache) and there is no protocol to quiesce them and
  remount SPIFFS. Building one is a large change with no harness.
- Redo recovery runs before any mount. A reset during a runtime round would
  be completed by the next boot anyway, so the boot path has no exposure the
  runtime path would avoid.
- Deadlines, the attempts guard, breadcrumbs, the LED and the mount gate are
  wired for `storage_repair_boot` only.
- After `mark_app_valid` any reset reaches the same confirmed boot, so the
  controlled restart adds no new state. It costs about one to two minutes of
  capture at a safepoint; the terminal keeps its punches and live punches
  that cannot be stored go to ADD directly (`recover_live_event_after_storage_error`)
  as they already do on the wedged store.

### 4.3 The confirmed boot

`storage_repair_boot` sees `pending_verify` false, so 2.7.6's confirmed
rules apply with nothing changed:

1. Discovery and recovery (nothing open on P02).
2. Run 1: 2.7.5's repair, `DEFERRED` `full` again.
3. The round: the exposure lists the custody blocks as candidates; blocks are
   taken by tail gain, then gain, each with its verified redo record (the
   queue's verified cursor offset in it), until the erased pages reach the
   target. Confirmed cap 256 blocks, two rounds a boot, the 15 min deadline.
4. Remount; run 2: survey, normalization (the one index rewrite: the 51
   spans become readable), the hold, the check (deleting leaked pages), the
   restore comparison of every held byte, compaction, the finish and the
   probe: `REPAIRED`.
5. After `storage_init`, the glue reads the exception record (state
   `CONFIRMED`, this image's tag): it logs `STORAGE_REPAIR_EXCEPTION_ROUND`
   with the outcome and records `ROUND_DONE` (`REPAIRED`, `CLEAN` or
   `REPAIRED_WITH_GAPS`) or `ROUND_SHORT` and why.
6. The probe passes, `qs` verifies persistence, delivery resumes and
   `pending.jsonl` (about 2.5 MB) drains to ORDS. Boot health now passes the
   ordinary way; the journal is `RECONCILING`, so `confirm_or_report_rollback`
   returns at once and `acknowledge_pending_success` reports `SUCCEEDED`
   when source coverage is certified (section 7.2).

### 4.4 Bounds and backoff

- Per boot: 2.7.6's bounds (two rounds, three runs, 256 blocks on a confirmed
  image, the deadlines, one closed block per round).
- Controlled restarts per confirmation: at most three, counted in the record
  before each. The first follows confirmation. A second and a third only
  when the previous round boot left the store unhealthy for a reason a boot
  can change: `short` `time` or `cap` after `pages_freed` above 0, the
  round's remount failed, `store_blocked`, or the area `UNREADABLE` or
  `NO_MEMORY`; and only at least 1 h (second) or 6 h (third) after the
  previous controlled restart. When the wall clock is unknown now or was
  when the restart was counted (a network that blocks NTP), the same backoff
  runs on the boot's own clock instead of waiting forever: the cap of three
  still bounds the total, and a reset before the wait ends only repeats the
  wait (14.2 item 4).
- Never after `held_mismatch`, more gaps than before, an area `FOREIGN`,
  `ABSENT` or `OVERLAP`, the attempts guard (`REPORT_ONLY`), `short`
  `candidate` on the confirmed image (no custody block left to help) or a
  write that failed.
- Ordinary reboots (power cuts) run the repair as every boot does and are
  not counted. A repair that crashes, watchdogs or browns out every boot is
  stopped by 2.7.5's attempts guard (with 2.7.6's brownout rule).
- The record's crash counter (M7) counts a panic, watchdog or lockup
  wherever it happened, but a brownout only when it ended a boot inside the
  repair or its round - 2.7.6's D5 scope. A voltage dip during ordinary
  runtime at a flaky-power site says nothing about the exception's machinery
  and must not end the controlled-restart budget (14.1 item 4).

### 4.5 When the round cannot repair the store

When no further controlled restart is allowed and the store is still not
healthy, the device reports the deployment `FAILED` with `error_code`
`STORAGE_REPAIR_EXCEPTION_UNREPAIRED` (`RECONCILING` -> `FAILED` is an
existing transition; ADD then pauses the campaign), records `UNREPAIRED`
and clears its OTA journal. It keeps running the confirmed 2.7.7 (no image
can do better on that store: on P02 and 13FL the other slot holds 2.5.2 or
2.4.12, which have no repair),
stays connected, sends heartbeats and logs, and takes the next OTA ADD
offers. Without this, a `RECONCILING` deployment that can never pass boot
health would block every further OTA to that device (section 1.5).

### 4.6 Next boots

The record keeps `confirm_reason` `STORAGE_REPAIR_EXCEPTION` for the
heartbeat until a later deployment replaces it. Every later boot is an
ordinary confirmed 2.7.7 boot: 2.7.6's rules, rounds whenever a run would end
without room.

## 5. Failure modes after confirming

### 5.1 Power cuts at every write

| Cut | Next boot | Custody |
|---|---|---|
| before step 3.4 (record, report, re-check) | the bootloader rolls back to 2.5.2, exactly as 2.7.6 (`ROLLED_BACK`); 2.7.7's record is ignored by 2.5.2 and replaced by a later deployment | nothing written to the store beyond 2.7.5's run |
| inside `mark_app_valid` | IDF rewrites the active otadata entry: a cut leaves it valid (below) or unreadable, and the bootloader then takes the other entry, the old image, as in a rollback (above) | unchanged |
| after `mark_app_valid`, before the journal says `RECONCILING` | **2.7.6 would re-enter the confirmation loop on a `VALID` image, and a timeout there calls `esp_ota_mark_app_invalid_rollback_and_reboot()`**, which would hand a store that has had custody windows back to the old image. 2.7.7: when the journal says `READY_TO_BOOT`, the running partition is already `VALID` and the record says `CONFIRMING` or `CONFIRMED` for this deployment and image, the journal goes to `RECONCILING` and the loop is skipped (the same window exists after an ordinary confirmation in 2.7.6; that path is left as it is) | the confirmed boot of 4.3 |
| after `RECONCILING`, before or during the safepoint wait | the confirmed boot of 4.3 | the same |
| in the round's windows | 2.7.6 10.1: the same image completes the block from its record before any mount (confirmed records are redone) | byte-exact (harnessed: `reclaim-cuts` and the field model, section 9) |
| in run 2 (check, restore, compaction, probe) | 2.7.5's manifest and resume on the same image | byte-exact or exact gap evidence (2.7.6's `REVIEW_TO_END` sweeps: P02, confirmed, 3,620 and 3,626 cuts, every one byte-exact) |
| in the record writes of 4.3 step 5 or a retry's counter | the record is CRC-checked; a torn record reads as `CONFIRMED` with no retries used, or invalid (treated as `CONFIRMED`, `restarts` 3: no further controlled restart) | none |

### 5.2 The round does not finish

| Case | What happens | Then |
|---|---|---|
| a block will not verify (worn cell) | closed, its record a byte source, the round goes on (one close per round; 2.7.6 R2-1) | the next run puts its bytes back |
| a second block will not verify | the round stops | `ROUND_SHORT`; retry only if a later boot can help (4.4) |
| the area is `UNREADABLE`, `NO_MEMORY` | no round (`STORAGE_REPAIR_RECLAIM_UNAVAILABLE`), the store as 2.7.5 leaves it | retryable once per 4.4 |
| the area is `FOREIGN`, `ABSENT`, `OVERLAP` | no round | not retryable: `UNREPAIRED` (4.5); owner decision 2 of 2.7.6 |
| `held_mismatch` | rounds halt, held bytes put back | not retryable: `UNREPAIRED`; investigate |
| the remount after a round fails | the store unmounted this boot (`LOCAL_FAILURE`) | retryable; OTA still works (5.3) |
| `store_blocked` (a failed block's record neither a source nor journaled) | the VFS off this boot, the next boot decides from the block | retryable; OTA still works (5.3) |
| run 2 still `DEFERRED` (the estimate was wrong) | a second round in the same boot when rounds remain | retryable if `pages_freed` > 0 |

In every case custody holds by 2.7.6's rules (a kept page keeps its bytes at
its place, or is in a verified record that is redone or serves as a source)
and the device stays manageable.

### 5.3 Staying manageable

- **Heartbeats and logs** need only the WebSocket.
- **OTA** needs NVS, HTTPS, the OTA partition and, today, a directory read of
  the mounted store. 2.7.7: when the VFS is not mounted this boot,
  `legacy_evidence` uses the boot repair's survey instead: the private mount
  listed every object header, so the repair records whether any name begins
  with `/zktj` (`ZJ_DEVICE_BASENAME`). Absent there and no `zkt_journal` NVS
  namespace: no evidence. If the survey never ran (the private mount failed)
  the check still refuses, as today. 2.7.7 never creates journal files, so
  the boot's survey stays true for the boot.
- **`RECONCILING`** stops blocking OTA for good through 4.5.

## 6. The planner on a confirmed image, and sufficiency

### 6.1 The rules (2.7.6, unchanged)

A block is a candidate on a confirmed image when its magic is valid and
strong, no entry is mislabeled, something is dropped, it is not the block of
an open or source record, it holds no live header or index page, and every
kept page is `NONE` or `CUSTODY` (section 1.3). Dropped: lookup entry
`0x0000`, the page's own deleted flag set, named by no valid index page of
any object. Entry 0 stays while a later entry stays.

So on P02 the confirmed candidates are exactly the 67 custody blocks (no
`unconfirmed_blocks`), and the 1,817 excluded, 88 index and 68 weak-magic
blocks stay out on every image.

### 6.2 Sufficiency

**Analytic bound.** Every `OK` block drops at least one page (1.2), so
`custody_gain >= custody_blocks = 67 >= 34 = want`. The round takes
candidates until `gained >= want` and stops early only for:

- a cap: 256 blocks; at most 34 needed;
- time: `rx_may_retry` reserves 2.5 s a block plus the next check; 34 x 2.5 s
  = 85 s against a 15 min confirmed deadline;
- slots: 96 in the area, at most 34 records, done slots reusable;
- NVS: `srp_nvs_entries_needed` + the journal against 265 available;
- a candidate that no longer reads as planned (nothing else writes the
  store during the round) or names more than five queues (custody pages are
  of the five reclaim queues, the catalog and probes; only queues are
  listed);
- failures: one closed block lets the round go on, a second stops it
  (section 5.2).

The target already includes the work (one page), `spare_for_compaction` and
30 pages for SPIFFS's own writes, the same target with which 2.7.6's harness
ends `REPAIRED` on every P02 model.

**The field-matched model.** The prototype of 1.3 (model A, 62 custody
blocks, fewer than P02's 67), then a confirmed 2.7.7 boot (image tag
changed, as after a confirmation):

| | blocks reformatted | pages freed | kept | erased after | result | gaps | check passes | used |
|---|---|---|---|---|---|---|---|---|
| model A | 10 (all custody) | 36 | 114 | 36 | `REPAIRED` | 0 | 2 (6,686 leaked pages deleted) | 4,609,364 -> 2,930,927 |
| model B | 8 (all custody) | 36 | 84 | 36 | `REPAIRED` | 0 | 2 | 4,608,862 -> 2,930,425 |

Both pass `verify_reclaimed`: every block's magic and erase count, SPIFFS's
free-block count equal to the flash's, no written page under a free entry, no
open record, every model file byte-exact through plain SPIFFS, three free
blocks or more, clean tails, appends and a probe file, SPIFFS's GC moving
nothing a live file reads, and a clean next repair. The round needed 8 to 10
of 62 to 75 custody blocks. A cut sweep over that confirmed round, 52 cuts
(every seventh write, both tears), left custody byte-exact with no gap
record every time (section 9.2).

### 6.3 New report fields (diagnostics only; no rule changes)

`srr_reclaim_report_t` and `diagnostics.storage.repair.reclaim` gain:

| Field | Meaning |
|---|---|
| `want` | pages short when the round was considered (the exposure line's figure) |
| `custody_gain`, `unconfirmed_gain` | the sum of `plan.gain` over the custody blocks and over the blocks any image may take |
| `excluded_hidden` | excluded blocks whose first excluding page belongs to an object with a header that is not live or not mapped |
| `excluded_unwindowed` | ... to a live file never put in a window |
| `excluded_last_page` | ... is a reclaimable file's last data page |
| `excluded_index_flag` | ... says index in its own header under a live lookup entry |
| `excluded_busy` | `OK` blocks of an open or source record |
| `excluded_file`, `excluded_file_blocks` | the object (by its header's name, hidden ones too) that excludes the most blocks, and how many |

`keep_class` gains an out-parameter for the excluding reason and object;
`plan_candidates` adds them up. The exposure log line keeps its format and
gains `want` already printed there.

### 6.4 Other evidence the gate needs

- `add_connector_heartbeat_acked_ms()`: the heartbeat task keeps the
  `message_id` of its last heartbeat; the inbound `ack` handler, when an
  `ack` names it, stores the time. ADD already acknowledges every envelope.
- `storage_inventory_snapshot(si_report_t *)`: a copy of the finished scan.
- `led_status_fault_cause(LED_STATUS_LOCAL_FAILURE, LED_CAUSE_STORE_WRITE)`
  at the `zone_lite.c` sites where a store write, append, create or rename
  failed, and a count of such incidents beside `local_failures`; every other
  site keeps `led_status_fault()` (cause other). A contract test lists the
  store-write sites. S3 accepts `LOCAL_FAILURE` only when the two counts are
  equal.
- `storage_repair_exception_evidence()`: the boot's merged report fields the
  gate reads, and the survey's journal-name result (5.3).

## 7. ADD and the operator

### 7.1 What the device reports

| When | Report |
|---|---|
| every heartbeat of a 2.7.7 image | `ota.storage_exception`: `{state, reason, since_seconds, restarts, round_boots}`; states `NONE`, `EVALUATING`, `REFUSED`, `ELIGIBLE`, `CONFIRMING`, `CONFIRMED`, `ROUND_DONE`, `ROUND_SHORT`, `UNREPAIRED`; `ota.confirm_reason` `STORAGE_REPAIR_EXCEPTION` once confirmed that way; `ota.boot_health_blockers` (term codes) while unconfirmed |
| the gate's reason changes | log `STORAGE_REPAIR_EXCEPTION_REFUSED` (WARN, at most once a minute), `..._ELIGIBLE` (WARN, once) |
| before `mark_app_valid` | progress `BOOTED_PENDING`, `error_code` `STORAGE_REPAIR_EXCEPTION`, `error_message` the evidence ("DEFERRED full; 34 pages short; custody 67 blocks, N pages; 51 spans held (12801 B); probe ENOENT") |
| after it | progress `RECONCILING`, `error_code` `STORAGE_REPAIR_EXCEPTION`; log `STORAGE_REPAIR_EXCEPTION_CONFIRMED` (WARN) |
| the round boot | log `STORAGE_REPAIR_EXCEPTION_ROUND` (WARN `REPAIRED`, ERROR otherwise), the repair report as in 2.7.6 with `level` 3 and the custody blocks |
| unrepaired | progress `FAILED`, `error_code` `STORAGE_REPAIR_EXCEPTION_UNREPAIRED`, `error_message` the last outcome |
| covered | progress `SUCCEEDED` (as today, `error_code` empty) |

Reason codes: `NOT_PENDING`, `ROLE`, `NOT_OTA_IMAGE`, `ALREADY_UNREPAIRED`,
`REPAIR_RESULT`, `REPAIR_STAGE`, `REPAIR_GUARD`, `SHORT_REASON`,
`AREA_NOT_READY`, `RECLAIM_FAULT`, `CUSTODY_UNHELD`, `CUSTODY_LOST`,
`EVIDENCE_UNACKED`, `INVENTORY_PENDING`, `INVENTORY_UNEXPLAINED`,
`CUSTODY_SHORT`, `STORAGE_ERROR_CLASS`, `LED_CAUSE`, `STORE_NOT_MOUNTED`,
`OTA_PRECHECK`, `NVS_RECORD`, `ADD_DISCONNECTED`, `HEARTBEAT_UNACKED`,
`RECEIPT`, `HEALTH_<TERM>` (any other boot-health term), `STABILIZING`.

### 7.2 `BOOTED_PENDING` -> `SUCCEEDED`, and P02's coverage

ADD's transitions are unchanged: `BOOTED_PENDING` -> `RECONCILING` ->
`SUCCEEDED` (or `FAILED`). The `FirmwareEvent` rows keep `error_code`
`STORAGE_REPAIR_EXCEPTION` for `BOOTED_PENDING` and `RECONCILING`, so the
deployment's history shows "confirmed under the storage exception" after it
succeeds; the device's heartbeat shows `confirm_reason` too.

`SUCCEEDED` needs certified source coverage on the device. P02 has none: job
`f69322c1` has been `NEEDS_ATTENTION` `SOURCE_RANGE_LAYOUT_INVALID` at
ordinal 57,300 of 68,788 since 25 September, and the snapshot fix
(bb5a61a, on `main` since 8 October, so in 2.7.6 and 2.7.7; its own example
is P02's 68,788 count) never retries a held job by itself. So P02's
deployment stays `RECONCILING` after a good round, holding one of the fleet
reservation's two slots and blocking further OTA to P02, until the operator
acts:

1. Before the campaign: cancel any leftover `ACTIVE` or `PAUSED` campaign in
   P02's zone; read P02's 2.7.6 heartbeats (13:29 to 13:45 UTC on 21
   October) for `led` local-failure sources and `ota.last_error` (S3 and
   3.7).
2. Run the 2.7.7 campaign for P02 alone.
3. Watch `ota.storage_exception`: `EVALUATING` -> `ELIGIBLE` ->
   `CONFIRMED`; the `BOOTED_PENDING` and `RECONCILING` events with
   `STORAGE_REPAIR_EXCEPTION`; one restart; then the round boot's repair
   report (`level` 3, `REPAIRED`, custody blocks reformatted), `qs`
   `persistence_verified` true and the store healthy. `REFUSED` with a
   reason means the image will roll back at the budget's end: read the
   reason; nothing was confirmed.
4. Once P02 is `ONLINE` with healthy storage, retry `f69322c1` from its
   checkpoint (ADD's job action "retry": `NEEDS_ATTENTION` -> `QUEUED`). If it
   is held again, cancel it and request a new full-history reconciliation.
   When a job certifies coverage the device reports `SUCCEEDED`; then the
   HIL acceptance (`HIL_ACCEPTED`) as for every 2.7.x target.
5. `FAILED` `STORAGE_REPAIR_EXCEPTION_UNREPAIRED`: the device stays on 2.7.7
   and can take a fix; stop and report.

### 7.3 ADD changes

- `schemas.py`: `OtaHeartbeatPayload` gains `storage_exception` (a lenient
  object: every field optional, codes `UPPER_CODE`), `confirm_reason` and
  `boot_health_blockers`; `StorageRepairReclaim` gains the fields of 6.3.
  Unknown values never fail validation.
- `ota.py` and `storage_contract.py`: only the release plumbing (2.7.7 among
  the platform versions, its qualified predecessors, `platform_baselines`).
  No transition changes: the free-text `error_code` already carries the new
  reasons.
- Tests: the heartbeat fields; progress `BOOTED_PENDING` and `RECONCILING`
  with `STORAGE_REPAIR_EXCEPTION` recorded in the events, then `SUCCEEDED`
  or `FAILED` `STORAGE_REPAIR_EXCEPTION_UNREPAIRED` (campaign paused).

## 8. WebSocket and IPC stacks

**Data.** Every 2.7.3 to 2.7.5 device reports `websocket_task` minimum free
about 740 B of the component's default 4,096 B (`WEBSOCKET_TASK_STACK` in
esp_websocket_client 1.7.0; `start_websocket` sets no `.task_stack`), and
`ipc0` 436 to 728 B of 1,280 B on some.

**WebSocket.** The task runs the TLS reads and, on reconnect, the handshake,
and dispatches our `websocket_event` handler in its own context (the
component's event loop has no task), which logs through `ESP_LOGW`
(`vprintf` takes about 1.26 KB, measured for 2.7.6, section 12 there). 3,356 B
used leaves no room for that log on a deeper TLS path. 2.7.7 sets
`.task_stack = 6144` for the ZKT family (the Hikvision family keeps its
qualified profile): expected minimum free about 2,790 B.

**IPC.** `ipc0` and `ipc1` run IDF's cross-core calls (the flash-operation
stall, cross-core interrupt allocation and free). IDF 5.5.3 already raises
the default to 1,280 B on the ESP32-S3. The worst observed use, 844 B, leaves
436 B; an interrupt saves its context frame (about 0.16 KB on this core) and
spills register windows on the interrupted task's stack before switching to
the ISR stack, and an overflow there panics the device (on an unconfirmed
image, a rollback). 2.7.7 sets `CONFIG_ESP_IPC_TASK_STACK_SIZE=1792` in
`sdkconfig.zkt.defaults`: expected minimum free 948 to 1,240 B.

**Internal RAM.**

| | 2.7.6 | 2.7.7 | Delta |
|---|---|---|---|
| `websocket_task` stack | 4,096 B | 6,144 B | +2,048 B while the client runs; allocated by every `esp_websocket_client_start` (also on `restart_transport`) |
| `ipc0` + `ipc1` stacks | 2 x 1,280 B | 2 x 1,792 B | +1,024 B from startup |
| total | | | +3,072 B internal |

`reserve_delivery_worker_stacks()` (24,576 B + 16,384 B, contiguous) runs
before `add_connector_start()`, so only the IPC change precedes it: 1 KiB
less of P02's 102,400 B boot largest block. The WebSocket stack comes after
it and out of the runtime heap (P02's runtime largest block 63,488 B); each
reconnect needs about 6.5 KiB contiguous (stack and TCB). A stack in PSRAM
is not possible: the component creates its task with
`xTaskCreatePinnedToCore`.

**HIL must read:** `websocket_task` at least 2,048 B free, `ipc0` and `ipc1`
at least 768 B, `ZMD_STACK_RESERVE_OK` (never `..._FAILED`), no "Could not
start ADD WebSocket client" or "restart failed", and `internal.min_free` and
the largest internal block against 2.7.6 on the same device.

## 9. Harness and host tests

### 9.1 Pure logic (plain cc, ASan and UBSan, like `boot_confirm_host.c`)

`tests/firmware/storage_exception_host.c` with
`test_storage_exception_host.py`:

- the gate on P02's 21 October report and runtime evidence: `ELIGIBLE`;
- each term of section 3 flipped alone: refused with that term's reason;
  ADD, receipt, heartbeat and stability timing (119 s refused, 120 s
  eligible, a refused evaluation restarts the clock);
- the record codec: CRC, torn writes, stale deployment or image tag;
- the restart policy: the first restart, retries only for the retryable
  outcomes, 1 h and 6 h backoff, no restart without time, at most three;
  the unrepaired verdict;
- the VALID-image rule: `READY_TO_BOOT` with a valid running image and a
  matching record never enters the confirmation loop; without a matching
  record the 2.7.6 behaviour is kept.

### 9.2 The SPIFFS harness (ESP-IDF 5.5.3's SPIFFS, power cuts)

`reclaim-p02field` in `spiffs_repair_host.c`, run by
`check_spiffs_repair.py` (CI at 8 MiB):

- **The builder** writes P02's history interleaved (each iteration a pending
  chunk, `acked_uids.txt` IDs, outbox and command rows, rows of the file to
  be removed; quiet stretches with pending alone), applies 2.7.6's P02
  damage (51 damaged spans of one index page with valid copies, four strays,
  a stale header copy, a cursor past its file, no erased page), and stops
  the removal after flagging its header (E2). A second variant deletes the
  header and uses denser never-windowed files instead (E1).
- **Fit:** the released 2.7.5 reproduces P02's first report (as
  `reclaim-p02` does); 2.7.6 unconfirmed reproduces 21 October: `DEFERRED`
  `full` -10001, `short` `candidate`, `rounds` 0, 0 candidates, 0
  unconfirmed, custody 67 within 10%, index 88 within 10%, excluded 1,817
  within 2%, 68 weak, `want` 34 within 4.
- **The gate's storage terms** on that report: `ELIGIBLE` (fed through the
  pure module with ADD and OTA inputs true); with one custody block fewer
  than `want` (a variant that leaves every custody block one droppable
  page): `CUSTODY_SHORT`, and the boot ends as 2.7.6.
- **The confirmed boot:** `REPAIRED`, no gap, every reformatted block in the
  unconfirmed boot's custody set (no excluded, index or weak block touched),
  `erased_after` at least `want`, the 51 spans read through plain SPIFFS,
  `verify_reclaimed`.
- **Minimum gain:** a variant whose custody blocks each drop exactly one
  page (the analytic worst case): still `REPAIRED`.
- **Cuts:** the confirmed round cut at every write (`REVIEW_FULL`; CI every
  41st), both tears, same image: custody byte-exact, no gap record; and cuts
  to the end of the boot under `REVIEW_TO_END` on request.

The prototype that produced the numbers of 1.3 and 6.2 lived in the session's
scratch directory (it is not in the repository and is rebuilt in-tree by the
implementation). Its cut sweep over model A's confirmed round (through
2.7.6's `run_reclaim_cuts`, `AFTER_SAME`): 52 cuts, writes 119 to 299 of
the boot every seventh, both tears; every one custody byte-exact with no gap
record.

### 9.3 Contract tests (read the sources)

In `test_storage_repair_boot_contract.py` and `test_boot_confirm.py`:

- `esp_ota_mark_app_valid_cancel_rollback` is called only in the ordinary
  `ACCEPT` path and the exception path, the latter after the record, the
  receipted `BOOTED_PENDING` and the re-check;
- a `READY_TO_BOOT` journal on a `VALID` image with a matching exception
  record goes to `RECONCILING` before the confirmation loop, so the loop's
  `esp_ota_mark_app_invalid_rollback_and_reboot` cannot reach it;
- `ota_manager.c` and `zkt_ota_guard.c` call no store write (`fopen` for
  writing, `unlink`, `rename`, `SPIFFS_*`, queue appends); the only store
  read is the legacy-evidence listing, with the survey fallback;
- the ZKT WebSocket config sets `.task_stack` 6144; `sdkconfig.zkt.defaults`
  sets the IPC stack 1792;
- the store-write LED sites use the store-write cause, and no other site
  does;
- the exception's NVS key is outside every 2.7.5 and 2.7.6 layout
  (`srp_state_t`, `srp_manifest_t`, the gap and reset lists, the reclaim
  journal unchanged byte for byte).

### 9.4 Builds and the rest of CI

`idf.py build` in `espressif/idf:v5.5.3` for production, the direct-legacy
role, Hikvision (unchanged stack), recovery 2.6.27, the 2.7.0 writer and the
2.6.23 bridge; stack again with `-fcallgraph-info=su` for
`storage_repair_boot` (the new report fields and `keep_class`'s out
parameter must not deepen the 4,544 B realistic chain) and for the gate in
`ota_manager` (its 12 KiB task); the 8 backend shards with
`scripts/run_ci_tests.py --verify-plans`; ruff with CI's paths; the OpenAPI
and repository contracts; the four firmware jobs.

## 10. Files

| Area | Files | Change |
|---|---|---|
| New | `firmware/zone_lite/main/storage_exception.h`, `.c` | the gate (inputs -> verdict, reason), the record codec, the restart policy; no ESP-IDF |
| OTA | `ota_manager.c` | the exception branch in `confirm_or_report_rollback`; a receipt-checked `report_state` with a message; the safepoint restart; the VALID-image rule; `FAILED` `..._UNREPAIRED` and the journal clear; heartbeat `storage_exception`, `confirm_reason`, `boot_health_blockers` |
| ADD link | `add_connector.c`, `.h` | boot health split into named terms (same result); heartbeat acknowledgement time; ZKT `.task_stack` 6144 |
| Repair glue | `storage_repair.c`, `.h` | the evidence accessor; the survey's journal-name result; the round boot's record update and log; heartbeat fields |
| Engine | `storage_repair_core.c`, `.h` | `want`, `custody_gain`, `unconfirmed_gain`, the excluded breakdown (report only) |
| Inventory | `storage_inventory.c`, `.h` | `storage_inventory_snapshot` |
| LED | `led_status.c`, `.h` | the store-write cause and its count |
| App | `zone_lite.c` | the store-write LED sites |
| OTA guard | `zkt_ota_guard.c` | the unmounted-store fallback |
| Build | `main/CMakeLists.txt`, `sdkconfig.zkt.defaults` | the new source; `CONFIG_ESP_IPC_TASK_STACK_SIZE=1792` |
| ADD | `apps/add_backend/zk_add/schemas.py` | heartbeat fields |
| Tests | `tests/firmware/storage_exception_host.c`, `test_storage_exception_host.py` (new); `spiffs_repair_host.c`, `tools/check_spiffs_repair.py`; `test_storage_repair_boot_contract.py`, `test_boot_confirm.py`, `test_storage_repair_host.py`; `tests/unit/test_storage_repair_diagnostics.py`, `test_boot_confirm_telemetry.py`, a new `test_ota_storage_exception.py` | |
| Release (its own commit, as ab079f1) | `firmware/zone_lite/CMakeLists.txt`, `storage_upgrade.c`, `upgrade_guard.c`, `.h`; `ota.py`, `storage_contract.py`; `deploy/add/*.ps1`; `.github/workflows/add-ci.yml`, `firmware-hil-candidate.yml`; `tests/firmware/test_hil_publication.ps1`, `tests/unit/test_platform_27_release.py`, `test_storage_contract.py`, `test_storage_upgrade_host.py` | version 2.7.7; 2.7.6 as a qualified predecessor (its published application digest; the marker grows to 154 of 160 characters) |
| Docs | this design; `docs/zone-lite-2.7.7-hil.md` (the runbook, with section 7.2's operator steps) | |

Not touched: `reconciliation.py`, `attendance_*.py`, `device_health.py`
(section 1.5's facts are read, not changed); `boot_confirm.c` (the budget is
unchanged); the redo record format, the reclaim journal and every 2.7.5
NVS layout.

## 11. Open points and residual risks

**For the owner:**

1. **13FL and the hold - decide before its campaign, not after a rollback.**
   The gate requires `files_unheld_custody` 0, as the decision states. If
   13FL's custody (its 3.8 MB blocked queue plus pending) exceeds the hold
   budget, its first 2.7.7 boot reports `REFUSED` `CUSTODY_UNHELD` and rolls
   back - a full 900 s outage, a rollback to 2.4.12 and flash churn whose
   outcome is knowable beforehand from 13FL's heartbeat sizes. The round
   itself does not need the hold (2.7.6's "no holds at all" runs end
   `REPAIRED`); relaxing C1 to "every unheld custody file is a reclaim queue
   with no unreadable byte" would be a decision for its own round. If C1 is
   kept and the sizes do not fit, the runbook's 13FL section says what the
   expected refusal is and what is learned from it (14.2 item 5).
2. **The retry restarts** (4.4): three in all with 1 h and 6 h backoff, only
   after progress. One restart and no retries is narrower; the retries help
   only rare outcomes (time, cap, a remount, a blocked store).
3. **The IPC stack** (section 8): recommended on margin and cost (1 KiB);
   leaving 1,280 B is defensible since no IPC overflow has been reported.

**Residual risks:**

- After confirmation there is no way back to the pre-2.7.5 image (the OTA
  floor refuses it; 14.2 item 2 narrowed 13.1 item 1): a store the
  round cannot repair stays wedged on 2.7.7 until a later release, with
  punches going to ADD live or staying on the terminal. 4.5 keeps that
  device OTA-capable. A merely REFUSED attempt no longer floors the device:
  CLAIMED alone, with every slot of the discovered area erased, proves no
  record was ever completed, so 2.6.25/2.6.27 stay available to it.
- The gate trusts the boot's report and the inventory. The runtime does write
  to the store in between (the probe, outbox restores, compaction, cursors;
  section 13 item 9): the probe is held on boots that may use the exception,
  and every other program or erase of the store restarts the 120 s window.
- An ADD action that ends a `RECONCILING` deployment does not exist: a
  healthy exception device whose source coverage never certifies holds its
  reservation (and P06's location) until an operator gets coverage certified
  (section 13 item 5).
- 13FL: its 3.8 MB `blocked_identity.jsonl` holds about 15,000 unreadable
  spans. On a boot that may use the exception the inventory's budget now
  grows 20 ms per held span, capped at +300 s (14.2 item 5), so the
  scan is expected to finish; if the real per-span cost is worse than the
  model, an incomplete scan still refuses (`INVENTORY_UNEXPLAINED`) and the
  image rolls back.
- S3's store-write cause depends on classifying the `LOCAL_FAILURE` sites
  correctly; a site left as "other" makes the gate refuse (safe), a site
  wrongly marked store-write could let an unrelated fault pass. The contract
  test lists every site.
- The prototype explains the histogram with a hidden header (E2); the device
  may show E1 instead. Either way the confirmed rules take only the custody
  blocks, and the analytic bound does not depend on which.
- Until P02's coverage job is resolved its 2.7.7 deployment stays
  `RECONCILING` and holds a fleet reservation slot (7.2).
- The WebSocket stack adds 2 KiB to every reconnect's contiguous allocation;
  a device near its internal RAM floor could fail a reconnect that 2.7.6
  managed. HIL compares the largest internal block.
- Everything else of 2.7.6 section 15 (decisions 5 and 6, torn erases,
  waiting spans) is unchanged.
- None of this has run on a device.

## 12. Timeline of the unconfirmed boot (M6)

| t (P02, 21 October) | What |
|---|---|
| 0 | boot; the repair (2.7.5's run, about 21 s, then the exposure) |
| about +25 s | `storage_init` returns; the area test and the probe hold are decided; the 900 s budget starts |
| +60 s | 2.7.4's inventory starts (180 s budget, plus 20 ms per held span capped at +300 s on a boot whose probe is held: 13FL's ~15,000 spans); on P02 it finished at about +2 min |
| until then | the gate says `EVALUATING` `INVENTORY_PENDING` (unknown: it neither refuses nor opens the window) |
| inventory done | the window opens if every term holds; any refusal closes it, any program or erase of the store restarts it |
| window + 120 s | the sequence: the record `CONFIRMING`, the evidence log (10 s acknowledgement), the receipted `BOOTED_PENDING` (signed HTTPS, 15 s at most), the re-check; a sequence that does not finish is tried again at most every 30 s |
| earliest about +4 to +5 min | `esp_ota_mark_app_valid_cancel_rollback()`; never when less than 90 s of the budget is left before the sequence, or when the budget is spent at the re-check |
| then | the controlled restart at a capture safepoint (30 min at most, then regardless); the next boot runs the round |

## 13. Review round 1 (21 to 22 October 2026): resolutions

Two critiques: the first (14 defects, numbered 1 to 14 below) and a
device-lens one (H1 to H4, M1 to M7, L1, L2). Every point is listed; **A** is
accepted and implemented, **P** partly, **R** rejected, each with the reason.

### 13.1 The first critique

1. **A. The OTA floor.** `zj_ota_before_download` refuses every target below
   2.7.6 (`sx_version_redo_aware`) with `STORAGE_REDO_FLOOR` once the reclaim
   journal has any flag, round or block, any sector of the redo area is not
   erased (a foreign one too), the image confirmed under the exception, or
   the store stays unmounted (`storage_repair_ota_floor`). It is checked
   before the legacy evidence and before the unmounted-store survey
   fallback. ADD's `_redo_floor_exclusion` does the same from the heartbeat
   (`storage.repair.reclaim.ota_floor`, `ota.confirm_reason`, a confirmed
   `ota.storage_exception.state`), so ADD never offers 2.5.2, 2.6.25 or
   2.6.27 to such a device. CLAIMED is never cleared, so the floor is
   permanent once anything was written to the area (the critique's
   "simplest"). *Superseded in part by 14.2 item 2: CLAIMED alone,
   with every slot of a fully discovered area erased, no longer floors; and
   by 14.1 item 3: ADD latches the floor durably on the connector.*
2. **A. Decision 5 (c) on exception images.** When the exception's record
   says confirmed and the boot's last run leaves queue spans waiting for byte
   sources, the VFS stays off that boot (`STORAGE_REPAIR_EXCEPTION_WAITING`;
   outcome `WAITING`, retryable). Live punches go to ADD or stay on the
   terminal; OTA works through the survey fallback under the floor.
3. **A. The area test, and a bench run before P02.** On a boot the exception
   could use (the gate's repair terms hold with the test assumed to pass),
   the glue journals CLAIMED first and then programs, verifies, erases and
   re-verifies one free sector (`srx_area_test`); the gate requires
   `PASSED`. The harness cuts at each of its writes: the area stays `READY`,
   never `FOREIGN` (it would read `FOREIGN` without CLAIMED first). The
   runbook makes a bench ESP32-S3 with the in-tree P02 field store, power
   cuts included, a prerequisite of P02's campaign.
4. **A. The proof's model.** `reclaim-p02field` keeps the image tag and only
   flips `pending_verify`; the guard's state carries over (`REPORT_ONLY`, no
   retry); a manifest still active refuses (`MANIFEST`). The numbers of 1.3
   and 6.2 came from the deleted prototype with a changed tag; the in-tree
   model replaces them (CI at 8 MiB: 0 usable, about 67 custody, 1,816
   excluded, 68 weak magic, 38 pages short, then `REPAIRED`).
5. **P. A `RECONCILING` that never certifies.** An exception deployment that
   waits in `RECONCILING` posts its status with a checked receipt (when it
   changes, at most every 15 min) and clears its journal when ADD's receipt
   says the deployment ended (`FAILED`, `ROLLED_BACK`, `CANCELLED`,
   `SUPERSEDED`, `RELEASE_REVOKED`); `UNREPAIRED` and `UNHEALTHY` (H4) end it
   `FAILED` from the device. **Rejected: "allow `fetch_assignment` once
   `ROUND_DONE`".** ADD itself holds every new offer to a device whose own
   deployment is still active (`pending_offer_hold` counts the device's own
   reservation: `NATIONWIDE_LOCATION_BUSY`), so the poll would get nothing.
   A healthy P02 without certified coverage keeps the existing remedy (retry
   `f69322c1`, or cancel it and request a new full reconciliation); an ADD
   action that ends a `RECONCILING` deployment is an ADD change of its own
   (section 11).
6. **P. "OTA path proven".** Added: the capability ADD accepted on this
   boot, the passive slot (present, not the running one, an OTA slot, a
   predecessor whose exact digest the release knows), 16 KiB of largest
   internal block, and OTA polling without the WebSocket after 10 min out of
   ADD's reach on an exception image. **Rejected: a ranged, authenticated
   GET of an image header.** ADD issues a download grant only with an offered
   assignment and there is none while the device is `BOOTED_PENDING`; a
   probe-grant endpoint would be new ADD API. The same TLS stack and signed
   HTTPS run in the receipted `BOOTED_PENDING` and the capability report the
   gate requires. A restart after N failed WebSocket starts was not added
   (the decoupling covers the same failure).
7. **A. S2, S3 and a vanished custody file.** The probe and `qs` errors are
   judged as (operation, errno) pairs; every custody file the repair reported
   must be in the inventory with at least its size (`CUSTODY_MISSING`).
8. **A. The unreadable set.** The inventory records the unreadable spans'
   count, first, last and an order-free digest; the repair the same for the
   held spans; the gate requires equality and no byte lost otherwise.
9. **P. The runtime writes the wedged store.** The probe is held on boots
   that may use the exception (it fails at its capacity check with ENOSPC and
   writes nothing); the window's fingerprint is SPIFFS's used and total bytes
   **and every program and erase of the VFS's SPIFFS** (its HAL is wrapped at
   link time), re-checked immediately before `mark_app_valid`. The other
   writers (outbox restore and compaction, cursors, backups) are not
   suppressed: a write of theirs restarts the window, which is safe (a
   refusal rolls back as 2.7.6). The inventory is not re-run: with no store
   write since the window opened, its unreadable set cannot have changed by
   a write.
10. **A. A broken image that wedged its own store.** The first survey's
    erased pages must be short of the run's target (`ARRIVED_WITH_ROOM`).
11. **A. The hold on the round boot.** An exception image's runs take custody
    blocks only when every custody file was held (`custody_needs_hold`);
    otherwise `short` `hold`, nothing written, retryable (harnessed).
    Recommendation kept: C1 is not relaxed for 13FL.
12. **A. Durable evidence.** Before the image may confirm, ADD must store an
    ack-waited log `STORAGE_REPAIR_EXCEPTION_EVIDENCE` with a structured
    context (a `duplicate` acknowledgement does not count), and accept the
    `BOOTED_PENDING` with a receipt that echoes `error_code`; event details
    keep `error_message`.
13. **A. Never invalidate a valid image.** On the non-local path, for every
    image: `READY_TO_BOOT` on a `VALID` running partition goes straight to
    `RECONCILING`, and the loop's rollback runs only when the image is not
    valid. The bridge and writer local proofs are 2.7.0's design and are left
    as they are.
14. **P. Traps.** A dedicated restart wait with no progress reports, the
    restart counted in the record before the safepoint is claimed and an
    unconditional restart once claimed; `UNREPAIRED` and `UNHEALTHY` in a fixed
    order (record, receipted `FAILED`, journal cleared, record reported),
    resumed by the next boot; `CONFIRMING` marked `ABANDONED` before a
    rollback. **Rejected: quiescing the store's owners before the restart.**
    2.7.x's non-journal owners have no quiesce protocol; the restart is at a
    capture safepoint, as every 2.7.x OTA restart is, and is no worse than a
    power cut, which 2.7.5 and 2.7.6 are built and harnessed for.

### 13.2 The device-lens critique

- **H1, P.** A `LOCAL_FAILURE` incident now carries the errno captured at its
  failing call: ENOSPC counts as room; EIO on a read of a reclaim queue counts
  as a held read, with the queue and where the read started (the gate also
  requires that start at or before the last held span). Classified: the
  attendance appends and the live fallback after them, the blocked-to-pending
  transfer, the pending and outbox generation restores, the command inbox
  append, recovery and rewrite, the catalog stage, replace and recovery, the
  outbox compaction, settles and bulk append, the ORDS legacy read (queue and
  offset), the blocked and quarantine reads and settles, and the queue
  store's periodic verification (by its (operation, errno) pair). Every other
  site stays ordinary and the gate refuses on it. **Rejected: reading P02's
  2.7.6 heartbeats first.** ADD keeps only a device's latest heartbeat
  diagnostics; P02 has reported 2.5.2 since 13:45 on 21 October, so the
  13:29 to 13:45 LED sources are gone. The first 2.7.7 boot reports the
  gate's reason, the failing terms and the LED's first and last source, and
  a refusal costs only the rollback 2.7.6 already does.
- **H2, A.** (operation, errno) pairs; EIO and every read operation refuse.
- **H3, P.** The receipt carries `storage_exception_allowed` (release
  `AVAILABLE` or `HIL_ONLY`, campaign `ACTIVE`), and the gate requires it:
  pausing the campaign is ADD's veto. **Rejected: a per-campaign opt-in**
  (a schema migration; the P02 campaign targets P02 alone). Documented: a
  release an exception device runs is never revoked; 2.7.8's contract lists
  2.7.7 under the retry rule while 2.7.7 is `HIL_ONLY`.
- **H4, A.** `UNHEALTHY` after 30 min of failing storage terms with ADD
  reachable on a `ROUND_DONE` image; one exception target at a time, 13FL only
  after P02 is `SUCCEEDED` or `FAILED`; P06 (the same location) waits while
  P02's deployment is active.
- **M1, A.** As item 12.
- **M2, A.** None of the exception's reasons touches `s_last_error`
  (contract test); they ride in `ota.storage_exception` and on the
  deployment's message.
- **M3, A.** As item 14.
- **M4, A.** As item 13, independent of the exception's record.
- **M5, A.** The fallback comes before the queue store's lock; D4 is read
  once per boot (a busy store is asked again at most once a minute); the
  runbook runs a real OTA out of a confirmed-but-unrepaired 2.7.7, mounted
  and unmounted.
- **M6, A.** A busy lock is unknown (neither refuses nor advances the
  window); the sequence starts only with 90 s of budget left and re-checks
  the budget right before `mark_app_valid`; `CONFIRMING` is written once;
  section 12 has the timeline. The inventory's 180 s budget is unchanged
  (13FL's risk, section 11). *Superseded by 14.2 item 5: the budget
  now scales with the held spans on exception-candidate boots.*
- **M7, P.** The bench run of item 3; the record counts boots that a panic,
  watchdog or brownout ended after confirmation, reports them and stops
  retries. A crash loop of a confirmed image outside the repair is bounded
  only by the image itself, as for every confirmed image (residual).
- **L1, A.** 6 KiB is allocated by each client start (the first start and
  `restart_transport`), not each automatic reconnect; the old task's stack is
  freed by the idle task, so `restart_transport` can briefly need two
  blocks; judge the margin against the fleet's runtime minimum; the HIL
  criterion accepts `websocket_task` absent from the 16 closest-to-overflow
  tasks; `sdkconfig.zkt.defaults` applies to every ZKT role CI builds
  (production, direct, recovery, writer, bridge).
- **L2, A.** The per-object counters live in the run's arena; blocks with
  more than one reason count as `mixed`; `excluded_file` is sent only when
  printable and at most 32 characters, and ADD drops an unreadable one alone.

### 13.3 Owner decisions after the review

- **13FL and the hold:** C1 kept (both critiques recommend it).
- **IPC stack:** raised to 1,792 B (+1 KiB of internal RAM).
- **Retry restarts:** three in all, kept: with decision 5 (c), a round boot
  that leaves spans waiting is the likeliest retryable outcome.

## 14. Review round 2 (23 October 2026): resolutions

Two critiques of the implementation at 3de0f19: a custody-lens one (five
defects) and a device-lens one (six, two of them re-raises). Every point is
listed; **A** is accepted and implemented, **P** partly, **R** rejected, each
with the reason. The custody lens confirmed power loss at every confirmation
step, the valid-image rule, the window's store fingerprint, the hold rule and
the UNREPAIRED/UNHEALTHY ordering; the device lens confirmed the budget
arithmetic, the record staleness rules, the ADD plumbing end to end and the
stack change. Neither found a path that confirms a broken image or loses or
duplicates a readable unsettled byte.

### 14.1 The custody-lens critique (defects 1 to 5)

1. **A (= 14.2 item 6). The inventory's 16-entry cap could permanently
   refuse `CUSTODY_MISSING`.** See 14.2 item 6: priority slots for the
   repair's custody files, and the harness's cluttered variants.
2. **A (= 14.2 item 1). D2 specified but not implemented.** See 14.2 item 1.
3. **A. ADD's redo floor was heartbeat-fresh.** A device that journaled
   redo-area work and rolled back to 2.5.2 overwrote its diagnostics with
   heartbeats carrying no `ota_floor`, and ADD would offer 2.6.25/2.6.27
   again. ADD now latches the floor durably on the connector
   (`ota_floor_latched_at`, migration 20261023_0058): set by any heartbeat
   whose diagnostics show the floor and by the confirmation's own
   `RECONCILING` or ended-`FAILED` progress reports, never cleared by a
   later heartbeat. A `BOOTED_PENDING` exception report alone does not
   latch, matching the device's narrowed floor (14.2 item 2): a refused
   boot writes nothing the area self-test does not erase. **Rejected: the
   alternative "write the reachability argument into 13.1 and call the ADD
   check advisory".** The latch is one nullable column and makes the check
   real; an unwritten invariant ADD cannot see is what this defect is.
4. **P. A brownout anywhere after confirmation ended the restart budget.**
   A brownout now counts as the image's crash only when it ended a boot
   inside the repair or its round (2.7.6's D5 scope, read from the previous
   boot's RTC breadcrumb); a panic, watchdog or lockup still counts
   anywhere. **Rejected: "or not when the ended boot's round made
   progress".** Progress does not make a mid-round brownout harmless - the
   D5 rationale (an erase that pulls the supply down must not loop) applies
   exactly there, and the narrower scope already spares the flaky-power
   sites the defect names.
5. **A. A transient NVS failure on the round boot could end the deployment
   `UNREPAIRED` in the boot whose round `REPAIRED` the store.** Before
   concluding `UNREPAIRED` from a `CONFIRMED` (or `CONFIRMING`) record, the
   glue re-derives the outcome from this boot's own repair report
   (`storage_repair_exception_repair`, `sx_round_outcome`,
   `sx_record_after_boot`), stores it, and decides from that instead.

### 14.2 The device-lens critique (defects 1 to 6)

1. **A. D2 (heartbeat acknowledgement) is now code.** The heartbeat task
   keeps the `message_id` of the heartbeat it last sent; the inbound ack
   handler stamps the one ADD acknowledged (ADD answers every accepted
   message with at least its default ack); the gate refuses
   `HEARTBEAT_UNACKED` unless one was acknowledged within 45 s
   (`SX_HEARTBEAT_ACK_SECONDS`, three 15 s heartbeat periods). The sent id
   is cleared on transport loss. One simplification against the written
   D2, recorded here: the term is "a heartbeat was acknowledged", not "a
   heartbeat that carried `ELIGIBLE` was acknowledged" - the ack carries
   only the id, and every heartbeat of an evaluating boot carries
   `ota.storage_exception`, so the stored state is at most 45 s older than
   the acknowledgement.
2. **A. The permanent floor was latched before ADD's veto.** The floor is
   narrowed to what is actually unsafe: a journal flag beyond CLAIMED, any
   round or block, any redo slot not erased (foreign and torn writes
   included), a confirmed exception, or an unmounted store. CLAIMED alone,
   with every slot of a fully discovered area erased, proves no record was
   ever completed (a torn record leaves a non-erased slot; a boot that
   could not inspect the whole area keeps the floor), so a merely REFUSED
   attempt no longer bans 2.6.25/2.6.27, the images that recovered both
   Peshawar ESPs. **Rejected: keeping permanence with an owner sign-off in
   section 11** - the narrowing is provably custody-safe and keeps the
   fleet's one proven recovery remedy for the devices likeliest to need it.
3. **A. H4's "one exception target at a time" is now enforced in ADD**, at
   the receipt: `storage_exception_allowed` additionally requires that no
   other connector's active deployment carries `STORAGE_REPAIR_EXCEPTION`.
   Two candidates racing each other both refuse (safe; the operator pauses
   one campaign), and a second target can confirm only once the first's
   deployment ended.
4. **A. The restart policy no longer waits forever on an unknown clock.**
   When the wall clock is unknown now or was when the restart was counted
   (`restart_epoch` 0), `sx_next_step` runs the same 1 h / 6 h backoff on
   the boot's own clock (`uptime_s`). `SX_MAX_RESTARTS` bounds the total; a
   reset before the wait ends only repeats the wait. The host test that
   asserted the dead-end now asserts the bounded fallback. **Rejected:
   counting WAIT verdicts in the record toward a bound** - every counted
   WAIT is an NVS write on a device whose NVS the exception already leans
   on, and the uptime backoff needs none.
5. **P. 13FL's inventory budget.** On a boot that may use the exception
   (the area test passed and the probe is held), the inventory's budget is
   180 s plus 20 ms per span the boot repair holds, capped at +300 s -
   known before the scan starts, bounded well inside the 900 s budget.
   C1 versus 13FL's hold sizes is put to the owner as a pre-campaign
   decision (section 11 item 1), and the runbook's 13FL section now says
   what a refusal there means. **Rejected: closing a file's scan early when
   its unreadable span set already equals the repair's held set** - that
   duplicates C4's equality check inside the scan with a second
   implementation that could drift from the one the gate trusts; the scaled
   budget achieves the liveness without a second oracle.
6. **A. A small custody file is guaranteed a report slot.** The inventory's
   keep policy gains a middle tier: unreadable files first, then the
   repair's custody names (passed by the firmware as priority names), then
   the largest. The SPIFFS harness's cluttered variants build an 18-file
   store where 16 files are larger than the one-row `/add_corrupt.bak` and
   prove both directions: with the priority the gate's custody check
   passes; with 2.7.6's size-only policy it would refuse `CUSTODY_MISSING`.
   The P02 fit variant keeps P02's own six-file directory so the 8 MiB
   histogram assertions stay faithful. **Rejected: evaluating C5 against
   the scan's full name walk** - the walk's names live only inside the scan
   task; the report is the one structure the gate, the heartbeat and the
   operator all read, so the guarantee belongs there.
