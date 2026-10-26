# Zone Lite 2.7.7: ZKT platform HIL

2.7.7 is 2.7.6 plus one narrow way for a new image to confirm although its
storage health fails: the confirm-first storage exception (owner decision on
2.7.6's open decision 3, 21 October 2026: **(a) confirm first**). An image
whose store arrived wedged (the boot repair deferred for room only, every
custody byte held on flash, the planner's own count of droppable custody
pages covering the shortfall) may mark itself valid once ADD has stored the
evidence and allowed it, every other boot-health term passes, and nothing has
changed for 120 s. It then restarts once at a capture safepoint, and 2.7.6's
confirmed round, unchanged, reformats custody blocks in place on the next
boot. Every other storage failure rolls back exactly as 2.7.6. 2.7.7 also
raises the ADD WebSocket task's stack to 6 KiB and IDF's IPC tasks' to
1,792 B, accepts the bridge-era journal leftovers that stop
ZONE-SLICTOWER-3FL's OTA (section 4), and on `PARTITION_HASH_FAILED` names
the failing step, erases the written slot and restarts the download from
byte 0, which ADD lets the owner retry with one click (section 5; design
section 15). The design, the gate, its review and every rejected point are in
[zone-lite-2.7.7-confirm-first.md](zone-lite-2.7.7-confirm-first.md) (section
13 for the review). The [2.7.6 runbook](zone-lite-2.7.6-hil.md) still applies
to the repair's report and the round, and the
[2.7.1 runbook](zone-lite-2.7.1-hil.md) to publication, campaigns, acceptance
evidence and rollback.

## Why

Peshawar 02's first 2.7.6 boot (21 October, 13:29 UTC, from 2.5.2) found no
block an unconfirmed image may take: 0 candidates, 67 custody blocks, 88 with
an index page, 1,817 excluded, 68 with a weak magic; 34 pages short. The
round needs a confirmed image for custody blocks, so the boot ended
`DEFERRED` and the image rolled back to 2.5.2 at the 900 s budget. 2.7.7's
model of that store (`check_spiffs_repair.py reclaim-p02field`, CI at 8 MiB)
reproduces the histogram and shows the confirmed round freeing the room from
custody blocks alone, `REPAIRED`, every file byte-exact, through power cuts.

## Before any campaign

1. **Owner and review.** The owner's word on design section 11 and a green
   `main` with this branch merged. Publish like 2.7.6 (`HIL_ONLY`). Keep
   2.7.6 published: the release plumbing commit adds it as a qualified
   predecessor. **Never revoke 2.7.7 while a device runs it under the
   exception**: such a device cannot roll back, and ADD offers a fix only
   from a published predecessor (2.7.8's contract lists 2.7.7 under the retry
   rule while 2.7.7 is `HIL_ONLY`).
2. **The bench run (critiques 3 and M7), on a bench ESP32-S3 with secure boot
   like the fleet's, before P02:**
   - write P02's store as its 2.7.5 boot left it:
     `P02FIELD_IMAGE=/tmp/p02field.bin python tests/firmware/tools/check_spiffs_repair.py reclaim-p02field --kib=8192`
     in `espressif/idf:v5.5.3`, then flash it raw at 0x7A0000 (the 8 MiB
     `storage` partition) on a bench device running the signed 2.5.2;
   - run the 2.7.7 campaign for the bench device exactly as for P02 below and
     read the same evidence: `ELIGIBLE`, `CONFIRMED`, one restart, the round
     boot `REPAIRED`;
   - repeat from a fresh image with power cuts: during the unconfirmed
     boot's area test (the LED is in `STORAGE_REPAIR`), between
     `STORAGE_REPAIR_EXCEPTION_CONFIRMED` and the restart, and several times
     during the round boot (`STORAGE_REPAIR` again). Every boot after a cut
     must end with custody intact (`gaps` 0, `held_mismatch` 0) and never
     return to 2.5.2 after `CONFIRMED`;
   - **M5:** on a bench 2.7.7 confirmed under the exception whose store is
     still wedged (stop after the first restart, before the round fits, or
     use a store the round cannot repair), run a real OTA to a later test
     build, once with the store mounted and once unmounted
     (`STORAGE_REPAIR_EXCEPTION_WAITING`). Both must download and boot; an
     OTA to 2.5.2 must be refused (`STORAGE_REDO_FLOOR`).
3. **ADD.** The ADD deployment must include 2.7.7's ADD changes (the receipt's
   `storage_exception_allowed` and `error_code`): an older ADD makes every
   exception refuse with `RECEIPT` (safe: the image rolls back as 2.7.6).
4. **One exception target at a time (H4).** P02 first; 13FL only after P02's
   deployment is `SUCCEEDED` or `FAILED`. While P02's deployment is active
   it blocks new offers and campaigns only in P02's own zone; the fleet-wide
   and per-location limits are gone (owner, 23 October 2026), so Peshawar 06
   is not held back by it. ADD enforces H4 at the receipt (design
   14.2 item 3): while another connector's exception deployment is active,
   `storage_exception_allowed` is false and a second candidate refuses
   `RECEIPT`; two candidates racing each other both refuse until one
   campaign is paused.

## 1. Peshawar 02, from 2.5.2

Cancel any leftover `ACTIVE` or `PAUSED` campaign in P02's zone first, then
run the 2.7.7 campaign for P02 alone. **Pausing the campaign is the veto:**
while it is paused ADD's receipt withholds the exception and the image rolls
back as 2.7.6.

**The unconfirmed boot** (the deployment's message and the heartbeat's
`ota.storage_exception` show the gate):

1. The repair as on 21 October (about 21 s), `DEFERRED` `full`, then the
   area test: `STORAGE_REPAIR_EXCEPTION_AREA_TEST` `PASSED` and
   `reclaim.area_test` `PASSED`. `FAILED` or `NO_SLOT`: stop (the image rolls
   back).
2. `reclaim.want`, `custody_gain` (at least `want`), `custody_blocks`, the
   excluded breakdown (`excluded_hidden`, `excluded_unwindowed`,
   `excluded_mixed`, `excluded_file`): record them; they settle which of the
   design's two mechanisms excluded P02's blocks.
3. `ota.storage_exception.state` `EVALUATING` (`INVENTORY_PENDING`) until
   2.7.4's inventory finishes (about 2 min), then `ELIGIBLE`
   (`STABILIZING`) for 120 s, then the evidence log
   `STORAGE_REPAIR_EXCEPTION_EVIDENCE` (its context has the numbers), the
   deployment `BOOTED_PENDING` `STORAGE_REPAIR_EXCEPTION`, then
   `RECONCILING` `STORAGE_REPAIR_EXCEPTION` and the log
   `STORAGE_REPAIR_EXCEPTION_CONFIRMED`, about 4 to 5 min after boot.
   `ota.boot_health_blockers` lists only `QS_RECOVERY`, `QS_PERSISTENCE`,
   `QS_LAST_ERROR`, `QS_PROBE_ERROR` or `LED_LOCAL_FAILURE`.
4. `REFUSED` with a reason means the image rolls back at the budget's end
   with nothing confirmed. Read the reason (design section 13 and
   `storage_exception.h`): `LED_CAUSE` names an incident that was not a room
   refusal (`storage.local_failure_source` and `local_failure_last_source`
   say which site); `STORE_CHANGED` that something kept writing the store;
   `RECEIPT` that ADD withheld it; `HEARTBEAT_UNACKED` that ADD has not
   acknowledged a heartbeat for 45 s (D2: the operator's view would be
   stale - check the WebSocket path and ADD's logs). Stop and report.
5. **The floor after a refusal** (design 14.2 item 2): a device that only
   REFUSED and rolled back is *not* floored - its journal holds CLAIMED
   alone and the area test left every slot erased - so ADD may still offer
   2.6.25/2.6.27 if a recovery becomes necessary. Confirmation (step 3's
   `RECONCILING`) floors it durably in ADD (`ota_floor_latched_at`, design
   14.1 item 3), even across later rollback heartbeats.
6. `STORAGE_REPAIR_EXCEPTION_RESTART`, then the ESP restarts at a capture
   safepoint (at most 30 min later).

**The round boot:**

1. `reclaim.trigger` `full`, `level` 3, `reclaim.blocks` above 0 (the model
   took about 10 custody blocks), `erased_after` at least `want`, no `short`
   (`hold` means a custody file was not held: retryable; record the hold
   numbers), `failed`, `held_mismatch`, `records_open` and `reported` 0.
2. The last run `REPAIRED` with 0 gaps, `check.deleted_live_files` 0;
   `STORAGE_REPAIR_EXCEPTION_ROUND` `OK`, `ota.storage_exception.state`
   `ROUND_DONE`, `ota.confirm_reason` `STORAGE_REPAIR_EXCEPTION`.
3. The probe passes, `qs` verifies persistence, the inventory finds nothing
   unreadable, and `pending.jsonl` (about 2.5 MB) drains to ORDS.
4. The deployment stays `RECONCILING` until source coverage is certified:
   retry `f69322c1` from its checkpoint; if it is held again, cancel it and
   request a new full-history reconciliation. Then `SUCCEEDED`, then the 2.7.1
   acceptance evidence.
5. `FAILED` `STORAGE_REPAIR_EXCEPTION_UNREPAIRED` (the round could not repair
   the store within three bounded restarts) or `..._UNHEALTHY` (repaired, but
   the storage terms still failed for 30 min): the device stays on 2.7.7,
   connected and OTA-capable (under the floor: 2.7.6 or later only), and the
   campaign is paused. Stop and report.

**Stacks** (both boots): `websocket_task` at least 2,048 B free or absent
from the 16 tasks closest to overflow; `ipc0` and `ipc1` at least 768 B;
`ota_manager` (12 KiB) at least 1,024 B free on the unconfirmed boot (the
exception's sequence adds about 350 B to its deepest path, a TLS handshake
under a receipted progress report: measured with `-fstack-usage`);
`ZMD_STACK_RESERVE_OK`, never `..._FAILED`; no "Could not start ADD WebSocket
client"; `internal.min_free` and the largest internal block against 2.7.6 on
the same device (P02's 2.7.6 runtime largest: 63,488 B; 2.7.7 needs about
3 KiB more).

## 2. SLICTOWER 13FL, from 2.4.12

Only after P02 has ended, and only after the owner has decided C1 against
13FL's sizes (design section 11 item 1): if its 3.8 MB blocked queue plus
pending exceed the hold budget (about 4.3 MB), the first boot's refusal
(`CUSTODY_UNHELD`) is knowable in advance and the campaign should not run
until C1 is relaxed for its own round or the queue is drained another way.
Its first 2.7.7 boot says on its own report whether the exception can apply:
`files_unheld_custody` must be 0 (the hold is kept: owner decision), `gaps`
0, and the inventory must finish over the 3.8 MB `blocked_identity.jsonl` -
its budget on an exception-candidate boot is 180 s plus 20 ms per held span,
capped at +300 s (design 14.2 item 5), about 480 s for 13FL's ~15,000 spans.
Every custody file the repair reports, however small, keeps a slot in the
inventory's report (design 14.2 item 6), so a cluttered directory alone no
longer refuses `CUSTODY_MISSING`. Anything else refuses and rolls back as
2.7.6, with the reason in the heartbeat; an expected first-boot refusal
still teaches the real store's histogram, hold sizes and span count from
the heartbeat's report.

## 3. Healthy devices, in place

On a healthy store 2.7.7 reports what 2.7.6 did plus `reclaim.ota_floor`
(false on a device whose redo area was never written) and confirms the
ordinary way; `ota.storage_exception` is absent.

## 4. SLICTOWER 3FL, from 2.7.2, by USB

3FL's 2.7.2 refuses every OTA with `JOURNAL_LEGACY_EVIDENCE_PRESENT` (the
2.6.18-2.6.21 bridges left a `zkt_journal` namespace and possibly `zktj*`
segments); only its *running* image's guard decides, so 2.7.7 cannot arrive
over the air. Once 2.7.7 runs, its guard accepts exactly the bridge-era
leftover (design 15.1) and later releases arrive by OTA again. The USB
install writes **only** the signed 2.7.7 application to the inactive slot and
**one** 4 KiB boot-selection sector. Never erase flash, never write NVS
(0x11000), the storage partition, the partition table, the bootloader or
eFuses. 2.7.2 stays in its slot for rollback.

**Before (on ADD, the day of the visit):**

1. Outside punch hours (after the evening shift or a holiday), with the
   terminal idle.
2. 3FL connected, `storage.durability` `HEALTHY`, every queue empty: the
   heartbeat's `queues` all 0 records (live, bulk, ORDS pending, blocked
   identity, quarantines), no `pending` reconciliation chunk, the terminal's
   records reconciled. If anything is queued, wait for it to drain.
3. Note the running slot (`ota.running_partition`, `ota_1` on 12 October)
   and its digest (2.7.2: `e908496e...`). The inactive slot is the other one:
   `ota_0` at `0x2A0000` or `ota_1` at `0x520000` (2.5 MiB each).
4. Cancel 3FL's failed 2.7.6 campaign. Download the published signed
   `zone-lite-2.7.7` image from ADD and check its SHA-256 against the
   release's `image_sha256` and its size against `image_size`.

**At the ESP (native USB; keep the site supply connected too):**

1. Read-only first: `esptool.py --chip esp32s3 -p PORT get_security_info`.
   Expect Secure Boot on and no flash encryption, like the fleet. If it
   reports Secure Download Mode, ROM flash reads and MD5 checks are
   unavailable: skip the dumps below (nothing they would protect is written).
   Otherwise, optionally, read-only dumps for the record:
   `read_flash 0x8000 0x1000 pt.bin`, `read_flash 0x17000 0x2000 otadata.bin`,
   `read_flash 0x11000 0x6000 nvs.bin`.
2. Write the application to the **inactive** slot only:
   `esptool.py --chip esp32s3 -p PORT --after no_reset write_flash <0x2A0000|0x520000> zone-lite-2.7.7.bin`
   (`--no-stub` if the tool asks; in Secure Download Mode esptool cannot
   verify by MD5 - the bootloader's Secure Boot check does on boot). If this
   write fails, stop: selection is unchanged and 2.7.2 still boots.
3. Select it. Build the 4 KiB first otadata sector: one 32-byte entry, then
   `0xFF`. The entry: `ota_seq` (LE u32), 20 bytes `0xFF`, `ota_state` 2
   (`ESP_OTA_IMG_VALID`, LE u32), `crc` = CRC-32 of the 4 `ota_seq` bytes
   seeded `0xFFFFFFFF` (as IDF's `otatool.py`). The slot is
   `(ota_seq - 1) % 2`: use `ota_seq` `0x00100001` for `ota_0`, `0x00100002`
   for `ota_1` - far above any sequence 3FL has used, so this entry wins over
   the other sector's:

   ```
   python3 -c "import struct,zlib,sys; s=int(sys.argv[1],0); e=struct.pack('<I',s)+b'\xff'*20+struct.pack('<II',2,zlib.crc32(struct.pack('<I',s),0xFFFFFFFF)); open('otadata0.bin','wb').write(e+b'\xff'*(4096-len(e)))" 0x00100001
   esptool.py --chip esp32s3 -p PORT write_flash 0x17000 otadata0.bin
   ```

   State `VALID`, not `NEW`: with no OTA journal 2.7.7 would never confirm a
   `NEW` image and the next reset would return to 2.7.2. Do not use
   `otatool.py switch_ota_partition` here: it keeps the target sector's old
   `ota_state`, which on 3FL may be an `ABORTED` entry from the 2.7.1 attempt.
4. Reset (`esptool.py ... --after hard_reset` or the board's reset). If the
   image failed Secure Boot verification the bootloader falls back to the
   other slot (2.7.2): read the console and stop. A `VALID` entry has no
   automatic rollback: if 2.7.7 boots but resets repeatedly or does not
   reach ADD within 10 minutes, capture the USB console and apply the USB
   rollback below only if the console shows no `STORAGE_REPAIR_RECLAIM ...
   blocks reformatted in place` or `STORAGE_REPAIR_RECLAIM_REDONE` line
   (a confirmed image may run 2.7.6's round at boot; 3FL's store is
   `HEALTHY` beforehand, so none is expected). If a round ran, stop and
   escalate: 2.7.2 cannot read the redo area.

**After (on ADD):**

1. 3FL reports `zone-lite-2.7.7` on the written slot with the release's
   application digest; the boot repair `CLEAN`; `reclaim.ota_floor` false.
2. `storage.journal_evidence`: `state` `ACCEPTED` (`reason`
   `BRIDGE_LEFTOVERS`, `authority` `LEGACY`, `files`/`bytes`, `unreadable` 0)
   or `NONE`. `REFUSED` names why (`ADD_AUTHORITY`, `NAMESPACE_KEYS`,
   `FILE_UNREADABLE`, ...): 3FL then keeps refusing OTA exactly as before -
   stop and report; nothing was deleted.
3. Queues drain, the terminal reconnects, punches flow; ADD shows 3FL
   `ONLINE`.

**Rollback by USB** (only while `reclaim.ota_floor` is false - never once the
redo area holds records, which 2.7.2 cannot read): the same otadata write
with the next sequence for 2.7.2's slot (`0x00100002` after `0x00100001`, or
`0x00100003` after `0x00100002`).

## 5. Swat 01 and Quetta 01: PARTITION_HASH_FAILED

On 26 October both (2.7.3) downloaded all of 2.7.6 and the written slot did
not verify. With 2.7.7's ADD deployed, each `FAILED` `PARTITION_HASH_FAILED`
deployment offers **Retry same target**: one click makes a fresh deployment,
which the ESP downloads from byte 0 (a new deployment never resumes). A
second failure of the same release on the same ESP stops retries until its
power and flash are checked. Quetta-01 also had 104 panics in 113 boots
(the last a `StoreProhibited` in the `ota_manager` task on 2.7.3): read its
boot reports before retrying it, and prefer Swat-01 first. Once a device runs
2.7.7, the same failure says which step failed (`OTA_IMAGE_UNVERIFIED`:
`HEADER`, `READ`, `DIGEST` or `UNREPRODUCED`, whether the bytes on flash equal
the release) in the log and on the `FAILED` report, and its slot is erased.

## Rollback expectations

Before `mark_app_valid` every refusal, timeout or reset rolls back to the
previous image exactly as 2.7.6 (`ROLLED_BACK` or `FAILED`
`BOOT_HEALTH_TIMEOUT`, now with the gate's last reason in the message), and
the store is byte for byte as 2.7.6's unconfirmed boot leaves it (harnessed).
After it there is no way back: the image is valid, a reset is a round boot,
and the OTA floor refuses every image that cannot read the redo area.
