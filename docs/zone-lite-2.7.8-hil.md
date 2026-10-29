# Zone Lite 2.7.8: ZKT platform HIL

2.7.8 is 2.7.7 with one fix to the confirm-first storage exception's gate
and better reasons when it refuses. Everything else, the
[2.7.7 runbook](zone-lite-2.7.7-hil.md) included (P02's two boots, 13FL,
3FL by USB, Swat and Quetta, rollback), applies unchanged with 2.7.8 for
2.7.7. The design is still
[zone-lite-2.7.7-confirm-first.md](zone-lite-2.7.7-confirm-first.md).

## Why

**Peshawar 02 (26 October, first 2.7.7 boot)** refused
`INVENTORY_UNEXPLAINED` although every field it reported matched the held
set: `pending.jsonl` 51 unreadable spans = the repair's 51 blocked spans,
offsets 493,968 to 523,335, `span_digest` 3,631,770,592 = `blocked_digest`,
12,801 bytes = 51 x 251, `first_error` 5. The term that failed was the one
the heartbeat did not carry: 2.7.7 required every failed span read to be
`EIO`. ESP-IDF's VFS maps `SPIFFS_ERR_DELETED` and `SPIFFS_ERR_NOT_FOUND`
to `ENOENT` (everything else to `EIO`), and a span whose index entry points
at a deleted page (an interrupted GC move, as the harness models P02's
damage) reads `ENOENT`; by elimination that is what P02 met. The SPIFFS
harness read every failure as `EIO`, so it never met
the refusal. Read the way ESP-IDF reads them, the harness's model of P02
now gives 51 `ENOENT` spans (one variant 23 `EIO` and 28 `ENOENT`).

**SLICTOWER 13FL** refused `AREA_TEST`, but the boot runs the redo area's
test only when every other repair term holds; its real terms were
`CUSTODY_LOST` (one gap, 71 bytes), `CUSTODY_BLOCKS` (774 custody blocks,
over the round's cap of 256) and unexplained spans. **13FL stays refused**:
the owner has not decided its gap or its block count, and 2.7.8 relaxes
neither.

## What changed

1. **The held spans** (`storage_exception.c` `explain()`): a span read that
   failed with `EIO` or `ENOENT` is part of the held set; any other errno
   still refuses, and so do a failed open or stat and a file that ends
   before its size (`other_unreadable`). The first read's errno must be one
   the spans counted. The set checks are unchanged: count, first, last,
   digest and byte bounds.
2. **The LED's held reads** (`led_status.c`): `ENOENT` counts as a read of a
   held span only when it came from a seek or read of the opened reclaim
   queue (`legacy_queue_t.io_read`), never from its open (a missing file),
   its close or the quarantine owner's task. `EIO` is as in 2.7.7.
3. **Reasons.** An area test that did not run reports the repair term that
   kept it from running (`AREA_TEST` only when none did); the gate still
   never passes without a test that `PASSED`. The decision is still
   `sx_evaluate`'s first failing term; the new fields are diagnostics.

## What the new fields show

| Where | Field | Meaning |
|---|---|---|
| `ota.storage_exception` | `terms` | Every term failing at the last evaluation, bit *i* = reason *i* (`sx_reason_name` order). ADD names them in `failing_terms`; the device drawer shows them as "Storage exception failing terms". |
| `ota.storage_exception` | `detail`, `detail_file` | For `INVENTORY_UNEXPLAINED`: which condition failed and in which file: `SPAN_ERRNO`, `FIRST_ERRNO`, `OTHER_UNREADABLE`, `SPAN_COUNT`, `FIRST_OFFSET`, `LAST_OFFSET`, `DIGEST`, `BYTES`, `NOT_HELD`, `HELD_READABLE`, `UNMATCHED`, `INCOMPLETE`, `SCAN_ERROR`, `FILE_COUNT`. |
| `storage.inventory.entries[]` | `span_errors_enoent`, `span_errors_other` | Span reads that failed with `ENOENT`, and with any errno that is neither `EIO` nor `ENOENT`. |
| `storage.inventory.entries[]` | `span_other_error`, `span_other_offset` | The first such errno and its offset (only when `span_errors_other` > 0). |
| `storage.repair.reclaim` | `area_test` `NOT_RUN`, `area_skip` | The area test did not run on a boot whose repair deferred for room, and the term that kept it (also the log `STORAGE_REPAIR_EXCEPTION_AREA_TEST` "...not run: CUSTODY_LOST"). |

The deployment's progress message and the rollback report now read, for
example, `storage exception REFUSED: INVENTORY_UNEXPLAINED SPAN_ERRNO`, and
the `STORAGE_REPAIR_EXCEPTION_REFUSED` log adds the file and the terms mask;
`STORAGE_FILE_UNREADABLE` adds "51 spans, 51 ENOENT, 0 other".

## Before any campaign

Publish like 2.7.7 (`HIL_ONLY`, an exact prefix of
`deploy/add/hil-targets-zkt-270.json`). **Keep 2.7.7 published**: 2.7.8
accepts it as a qualified rollback predecessor (application
`6d9ee63dd7e5917c83a3ada14685405c0081073d593a7901487eb2a3fd2c3bc1`, ADD's
published `zone-lite-2.7.7`, HIL run 40326022451) beside 2.4.12, 2.5.2,
2.6.6 to 2.6.15 and 2.7.2 to 2.7.6. The compiled marker ends
`,2.7.5,2.7.6,2.7.7` and is exactly the 160 characters ADD's heartbeat field
keeps: a release that adds another predecessor must widen that field first.

## Peshawar 02 retest, from 2.5.2

Run the 2.7.7 runbook's section 1 with 2.7.8. On the unconfirmed boot,
after the inventory finishes (about 2 min):

- `storage.inventory.entries[pending.jsonl]`: 51 `unreadable_spans`,
  `span_errors_enoent` + the `EIO` reads = 51, `span_errors_other` 0, and the
  same offsets and digest as on 26 October.
- `ota.storage_exception` `ELIGIBLE` (`STABILIZING`) for 120 s, then the
  evidence, `BOOTED_PENDING` and `RECONCILING` `STORAGE_REPAIR_EXCEPTION`
  as in 2.7.7.
- `REFUSED`: read `reason`, `detail`, `detail_file` and `failing_terms`
  and stop. `INVENTORY_UNEXPLAINED` with `SPAN_ERRNO` means a span read
  failed with another errno (`span_other_error`/`span_other_offset` name
  it); anything else is a term 2.7.7's design covers. The image rolls back
  at the budget's end as before.

The round boot, the stuck reconciliation job and the stacks: as in 2.7.7.

## SLICTOWER 13FL

Do not run 2.7.8 on 13FL for the exception until the owner decides its gap
and its block count; its heartbeat would now say `CUSTODY_LOST` with
`failing_terms` `AREA_TEST`, `CUSTODY_LOST`, `CUSTODY_BLOCKS` (and
`INVENTORY_UNEXPLAINED` if a span still fails another way), and
`reclaim.area_skip` `CUSTODY_LOST`. That is the evidence the decision
needs; it changes nothing on the store.

## Production promotion

2.7.8 was proven on the field fleet, not on the bench gate. By 29 Oct 2026,
eight ESPs had run it from 26–27 Oct with no rollback:

- Peshawar 02, Faisalabad 01, Karachi, Lahore 01, Multan 01, Peshawar 06 and
  BLD5 01 were all online, OTA_READY and on HEALTHY storage.
- Peshawar 02 was storage-repaired by it.
- The only failures were three interrupted downloads on Faisalabad 02's slow
  link. The running image was untouched each time.

`firmware-fleet-promote.yml` (Actions → "Zone Lite fleet-proven ZKT promotion",
inputs: the release commit and version) promotes it only after re-checking
that evidence on the ADD host. It checks:

- The exact HIL_ONLY release identity.
- No rollback.
- No failure after a transfer.
- At least `minimum_field_devices` ESPs running the exact application digest,
  online with HEALTHY storage, `minimum_soak_hours` after success.

It then runs `promote-firmware.ps1` on the exact published bytes and confirms
that ADD re-reads them as AVAILABLE.

AVAILABLE does not widen who can be offered the image. ADD's exact-identity
nationwide guard (`zkt_ota_admission.pending_offer_hold`) still limits ZKT
offers to the 17 signed fleet targets, and the K50 bridge keeps its own rule.
