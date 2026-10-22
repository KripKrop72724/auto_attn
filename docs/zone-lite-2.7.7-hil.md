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
1,792 B. The design, the gate, its review and every rejected point are in
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
   it holds one of the two nationwide reservations and its location
   (Peshawar): Peshawar 06 waits.

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
   `RECEIPT` that ADD withheld it. Stop and report.
5. `STORAGE_REPAIR_EXCEPTION_RESTART`, then the ESP restarts at a capture
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

Only after P02 has ended. Its first 2.7.7 boot says on its own report whether
the exception can apply: `files_unheld_custody` must be 0 (the hold is kept:
owner decision), `gaps` 0, and the inventory must finish in its 180 s budget
over the 3.8 MB `blocked_identity.jsonl`. Otherwise it refuses and rolls back
as 2.7.6, with the reason in the heartbeat.

## 3. Healthy devices, in place

On a healthy store 2.7.7 reports what 2.7.6 did plus `reclaim.ota_floor`
(false on a device whose redo area was never written) and confirms the
ordinary way; `ota.storage_exception` is absent.

## Rollback expectations

Before `mark_app_valid` every refusal, timeout or reset rolls back to the
previous image exactly as 2.7.6 (`ROLLED_BACK` or `FAILED`
`BOOT_HEALTH_TIMEOUT`, now with the gate's last reason in the message), and
the store is byte for byte as 2.7.6's unconfirmed boot leaves it (harnessed).
After it there is no way back: the image is valid, a reset is a round boot,
and the OTA floor refuses every image that cannot read the redo area.
