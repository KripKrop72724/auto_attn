# Zone Lite 2.6.24 and 2.6.25: one-shot Peshawar storage recovery

## Evidence prompting this release

On 8 October 2026 both Peshawar connectors (ZONE-PESHAWAR-02 and
ZONE-PESHAWAR-06) ran the signed 2.5.2 application with a full 8 MiB SPIFFS
partition:

- `IDENTITY_CATALOG_MEMORY_FALLBACK`: the verified ADD catalog stays in PSRAM.
- `DELETE_USER` fails with `IDENTITY_TOMBSTONE_PERSIST_FAILED`. 2.5.2 rewrites
  the whole encrypted catalog plus the tombstone into a temporary file before
  deleting, and that write cannot succeed.
- `ORDS_DRAIN_STORAGE_BACKPRESSURE`: the ESP's own ORDS outbox cannot compact.
- `LIVE_LOCAL_STORAGE_RECOVERED`: live punches reach ADD only through the direct
  acknowledgement fallback.

`BLOCKED_IDENTITY_RECOVERY_DEFERRED` reports a retained `blocked_identity.jsonl`
of 2,775,901 bytes on 02 and 4,279,392 bytes on 06. 2.5.2 never shrinks that
queue above 64 KiB, and `acked_uids.txt` is append-only.

The 2.6.15 HIL campaigns of 2 October booted on both devices, then failed
`BOOT_HEALTH_TIMEOUT` on the full partition and returned to 2.5.2. Every 2.6.x
and journal image requires verified persistence below the 75% storage budget
before boot confirmation. Neither a 2.6.x retry nor 2.7.0 can therefore stick on
these devices before space is recovered. Only remote access is available.

## Field result of 2.6.24 and the 2.6.25 change

ZONE-PESHAWAR-02 ran 2.6.24 on 8 October 2026. After 5,185 rows (about
1.84 MB) a read of `blocked_identity.jsonl` failed. The run stopped with
`STORAGE_RECOVERY_SOURCE_READ`, removed nothing and returned to 2.5.2. The
bytes past that point cannot be read by any image, 2.5.2 included, so 2.6.24
could never retire that file.

2.6.25 is the same role with one change. A region that the filesystem cannot
read is measured, reported to ADD, and skipped:

- Reads are unbuffered. After a failed read, the same bytes are re-read in
  32-byte pieces, each from a fresh open. An unreadable piece starts a region,
  which extends in 32-byte steps until a one-byte probe reads again or the file
  ends. An offset that cannot be reached by a seek also counts as unreadable.
- A scan pass measures every region before anything is sent. It logs
  `STORAGE_RECOVERY_SCAN` and, for the first 20 regions,
  `STORAGE_RECOVERY_UNREADABLE_REGION` with offset, length and errno.
- More than 16 KiB of unreadable bytes in one run is refused with
  `STORAGE_RECOVERY_UNREADABLE_LIMIT`, and nothing is sent or changed. The device
  owner approved this bound on 9 October 2026. It is about 45 rows.
- Within the bound, the transfer pass sends each region to ADD as its own
  durable record. Its record ID is `gap:<offset>:<length>`, reason `MALFORMED`,
  with a JSON body of type `storage_recovery_unreadable`. The readable fragments
  of a row cut by a region are sent as exact bytes, as `MALFORMED`.
- The transfer pass must find the same regions as the scan, or the run ends with
  `STORAGE_RECOVERY_SOURCE_CHANGED` before anything is removed.

The generation and record identities are unchanged from 2.6.24. Rows that 2.6.24
already moved into ADD custody replay the same receipts.

ZONE-PESHAWAR-06 completed 2.6.24 on 8 October 2026. All 11,959 rows reached ADD
custody. Storage use fell from 5,192,688 to 1,659,361 bytes, and the device
returned to 2.5.2. The removal and UID recording took about 13 minutes of SPIFFS
garbage collection; the image logs nothing during that step.

### Removing a file with an unreadable data page

ESP-IDF 5.5.3 SPIFFS removes a file from its last page backwards. It first marks
the index header deleted, so the name disappears at once. If a data page fails
its header check, the removal stops there but still returns success. Pages after
the bad page are freed. Pages before it stay allocated until a SPIFFS filesystem
check reclaims them, and Zone Lite never runs that check.

After a 2.6.25 run that skipped a region, compare `STORAGE_RECOVERY_INVENTORY_AFTER`
with the use before the run. Expect a drop of roughly the retired file size less
the recorded UIDs (65 bytes each). A much smaller drop means the start of the file
is still allocated. Reclaiming it needs a separately approved filesystem check,
because that check also repairs or drops pages of any other damaged file.

## What the image does

2.6.25 is a separately built ZKT role
(`-D PROJECT_VER=2.6.25 -D ZONE_LITE_STORAGE_RECOVERY=ON`). Both 2.6.24 and
2.6.25 are reserved for this role. It is never the operating firmware.

1. It boots from its OTA slot in `ESP_OTA_IMG_PENDING_VERIFY` and never calls
   `esp_ota_mark_app_valid_cancel_rollback()`.
2. Before changing anything, it requires all of:
   - the exact connector ID and Wi-Fi MAC of one of the two targets;
   - the signed 2.5.2 application (`4b4aa069…`) in the other OTA slot, in state `VALID`;
   - a deployment journal selecting exactly this version.

   A stale `journal_v1` cannot hide the legacy journal that 2.5.2 wrote.
3. It mounts the existing partition without formatting. It starts no terminal
   session, capture, delivery worker, queue owner or command executor. ADD
   commands receive `RETRYING`/`STORAGE_RECOVERY_ACTIVE` and are offered again to
   2.5.2. Catalog and reconcile messages are ignored.
4. It logs a storage inventory (`STORAGE_RECOVERY_INVENTORY_BEFORE`): partition
   use and the size of every file.
5. It streams `blocked_identity.jsonl` and any retained `blocked_recovery.bak/.tmp`
   generation in order:
   - Each non-empty row is sent as exact bytes through the existing
     `queue_evidence` custody path: queue `blocked_legacy`, reason
     `LEGACY_RECOVERY` or `MALFORMED`.
   - Each row must be acknowledged with its matching durable ADD receipt before
     the next row is sent.
   - Generation and record identities derive from file size, offset, length and
     CRC, so a repeated run replays the same receipts.
   - 2.6.25: unreadable regions are skipped and reported as described above.
6. Only after every readable row and every unreadable region is acknowledged,
   and the source sizes are unchanged:
   - it resets the 2.6.x `legacy_queues/blocked` cursor to offset zero, as
     `lq_reclaim` does;
   - it removes those files;
   - it appends their event UIDs to `acked_uids.txt` in the 2.5.2 format, so
     2.5.2 does not queue the same punches again.
7. It logs the outcome and a second inventory, reports deployment state `FAILED`
   with the outcome code, and calls `esp_ota_mark_app_invalid_rollback_and_reboot()`.
   The device returns to 2.5.2.

Any refusal, interruption, power loss or stop leaves every legacy file
unchanged, apart from receipts already held by ADD. The bootloader returns to
2.5.2 for any reset of the pending image. `pending.jsonl`, the ADD outboxes, the
catalog and the command inbox are never touched.

| Outcome code | Meaning |
| --- | --- |
| `STORAGE_RECOVERY_COMPLETE` | Every row receipted, sources retired, UIDs recorded |
| `STORAGE_RECOVERY_SEEN_PARTIAL` | Sources retired; some UIDs could not be recorded (rows may be queued again by 2.5.2, never lost) |
| `STORAGE_RECOVERY_RETIRE_PARTIAL` | Every row receipted; a removal failed |
| `STORAGE_RECOVERY_NOTHING_TO_DO` | No retained blocked queue |
| `STORAGE_RECOVERY_STOPPED`, `…_ADD_UNAVAILABLE`, `…_TIMEOUT` | Stopped before retirement; files unchanged |
| `STORAGE_RECOVERY_SOURCE_CHANGED`, `…_ROW_TOO_LARGE`, `…_PREPARE_RETIRE` | Refused before retirement; files unchanged |
| `STORAGE_RECOVERY_UNREADABLE_LIMIT` | 2.6.25: more than 16 KiB unreadable; nothing sent or changed |
| `STORAGE_RECOVERY_SOURCE_READ` | The source could not be opened (2.6.24: any read failure); files unchanged |
| `STORAGE_RECOVERY_TARGET_MISMATCH`, `…_ROLLBACK_IMAGE`, `…_ROLLBACK_STATE`, `…_NOT_PENDING_VERIFY`, `…_NOT_AUTHORIZED` | Preconditions failed; nothing changed |

The transfer is bounded to eight hours and the whole run to nine. While the image
runs, the terminal keeps its punches. 2.5.2 reconciles them after it returns. Blocked rows held in ADD custody are not attendance approval: their
employees still need CNIC linkage before Oracle delivery. Linking those
identities is also what prevents the queue from growing again.

## Release constraints

- Signed and published only as `HIL_ONLY`, release channel
  `EXPERIMENTAL_HIL_ONLY`, for exactly `deploy/add/hil-targets-storage-recovery.json`.
  ADD keeps the published 2.6.24 release valid next to 2.6.25.
  It can never be promoted. `minimum_bootstrap_version` is 2.5.2, and the signed
  storage contract admits only the 2.5.2 application digest.
- Each target can start independently, in either order. Nationwide admission
  still allows one PESHAWAR install at a time.
- A deployment past `VERIFYING` is never offered or downloaded again. A lost
  terminal report cannot reinstall the image.
- The firmware, ADD (`zk_add/storage_recovery.py`) and the scope file pin the
  same two identities; `tests/unit/test_storage_recovery_release.py` compares them.

## Procedure

1. Merge only after green CI on the exact main SHA; ADD deploys automatically.
   Publication checks the deployed backend admits the contract, so ADD must
   be deployed first.
2. In ADD, cancel the zone's PAUSED campaign (2.6.15, or a finished 2.6.24
   run). A zone with an ACTIVE or PAUSED campaign refuses a new one.
3. Confirm each connector reports 2.5.2, digest `4b4aa069…`, an OTA partition,
   is online, and its terminal binding is CONFIRMED.
4. Dispatch `firmware-hil-candidate.yml` with:
   - family `zkt`, version `2.6.25`, empty `device_mac`;
   - `targets_json` equal to the scope file.
5. Start one zone alone: preflight, then create the campaign with typed
   confirmation `2.6.25`.
6. Watch the device logs for:
   - `STORAGE_RECOVERY_STARTED`, then `STORAGE_RECOVERY_INVENTORY_BEFORE`;
   - `STORAGE_RECOVERY_SCAN`, and any `STORAGE_RECOVERY_UNREADABLE_REGION`;
   - `STORAGE_RECOVERY_PROGRESS` every 500 rows, and `STORAGE_RECOVERY_CUSTODY_COMPLETE`;
   - the outcome line, then `STORAGE_RECOVERY_INVENTORY_AFTER`.

   The device then returns to 2.5.2, and ADD shows the campaign PAUSED with the
   outcome code.
7. Confirm:
   - 2.5.2 is back online with digest `4b4aa069…`;
   - live capture has resumed;
   - `IDENTITY_CATALOG_MEMORY_FALLBACK` has stopped after the next catalog delivery;
   - queue evidence shows the transferred `blocked_legacy` receipts.

   Then retry the user deletion.
8. Cancel the PAUSED recovery campaign once its deployment is terminal. Repeat
   steps 5–7 for the other zone if it still needs recovery.

Never cancel or revoke a recovery campaign while its deployment is OFFERED or
DOWNLOADING. Nationwide admission treats a cancelled offer as uncertain until a
later successful installation, and this image never succeeds.

Physical power-loss and flash-endurance qualification remain `NOT_PERFORMED`.
This release is a field recovery, not a qualification of 2.6.x or 2.7.0.
