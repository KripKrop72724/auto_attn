# Historical attendance delivery sweep

ADD now runs a restart-safe maintenance sweep for retained attendance events.
The sweep starts with the oldest unresolved event and processes bounded pages.
It creates a missing `add_ords_outbox` row, releases an event only after the
existing ZKT or Hikvision evidence proves its identity, and leaves invalid or
ambiguous events preserved for review.

The sweep never deletes or rewrites the physical attendance facts. It does not
reset an existing `FAILED_RETRYABLE` outbox, so the normal ORDS backoff remains
effective. `event_uid` remains the Oracle idempotency key.

Operational status is included in the existing overview `ords_delivery`
object:

- `missing_outbox` counts deliverable events (pending, retrying, in flight,
  firmware-receipt or membership checks) that have no durable delivery row,
  plus held events that already have a Force release or Direct ORDS decision.
  This is the only real delivery gap and should be zero.
  - The sweep rebuilds the row within one pass only for a `PENDING`,
    `FAILED_RETRYABLE` or `RETRYING` event that does not need manual approval.
    It skips any event with `manual_release_required`, with or without a
    release decision, so such an event stays missing until someone
    investigates it.
  - An `IN_FLIGHT`, `FIRMWARE_RECEIPT_*`, `ACKED_FIRMWARE` or `MEMBERSHIP_*`
    event without a row is never picked up by the sweep. Preview **Rebuild
    missing outbox** (`REBUILD_OUTBOX`) in attendance recovery once its rollout
    gates are enabled (see `attendance-recovery-runbook.md`); it too skips an
    event that needs approval and has none. Otherwise investigate how the row
    was lost.
  - `decided_held_without_outbox` is the part of `missing_outbox` whose stored
    status is still held although a release decision exists. Force release and
    Direct ORDS write the decision, the `PENDING` status and the outbox row in
    one transaction, and no code deletes outbox rows, so this is expected to be
    zero. Investigate any such event before any rebuild or release.
  - `without_outbox_by_status` names the statuses involved.
- `held_without_outbox` counts identity holds without a delivery row and
  without a release decision: `BLOCKED_IDENTITY`, `QUARANTINED_IDENTITY_REUSE`
  and `WAITING_FOR_SNAPSHOT`, the statuses the manual-approval guard, the sweep
  and Force release treat as held. `review_without_outbox` counts every other
  event without a row: the non-identity quarantines (invalid device time,
  invalid event UID, missing terminal provenance, ORDS rejection, source
  conflict) and any state ADD does not recognise. Both are held by design.
  - ZKT ingestion creates no Oracle work item for a punch without a CNIC or
    with an impossible device time, and a later identity-reuse quarantine of
    such a punch adds none.
  - Many held punches still have a placeholder row in the held status, and
    those are in none of these counts: every Hikvision and journal punch, a ZKT
    punch whose firmware receipt arrived with it, and the ZKT holds the sweep
    reached on 22-23 September 2026, before #188 made it skip punches that need
    approval.
  - A verified release (synced-CNIC proof, Force release or Direct ORDS)
    creates or reuses the delivery row; the sweep never does so for a punch
    that still needs approval.
- These counts describe ADD's queue, not Oracle. A legacy ZKT terminal can post
  a punch to ORDS itself and report a firmware receipt. If the receipt arrives
  with a punch that has no CNIC, ingestion writes a receipt outbox that the
  worker then blocks together with the punch, leaving a held placeholder row.
  If it arrives after the punch is held, ADD keeps the receipt as evidence and
  creates no row. So any held punch, with or without a row, may already be in
  Oracle under the identity the terminal sent.
  - To find held punches with a terminal receipt, join `add_oracle_receipts` on
    `attendance_event_id` for all held events, not only those without a row.
  - A missing receipt does not prove the punch is absent from Oracle. Only a
    read-only `raw-captures/check` by `event_uid` settles whether Oracle has it.
- `without_outbox_total` and `without_outbox_by_status` show the whole
  outbox-less population by stored delivery status. The attendance recovery
  summary reports the same `missing_outbox`, `held_without_outbox` and
  `review_without_outbox`.
- The recovery summary's tiles use the same identity-hold set, so
  `WAITING_FOR_SNAPSHOT` is "Identity held" on both. Its Source review tile
  counts every other unacknowledged state outside the safe and in-flight lanes,
  which includes firmware-receipt and membership checks still being verified.
  Such an event without a row is counted as missing as well, because the worker
  verifies only through an outbox row.
- `historical_sweep` reports state, last event ID, pages, scanned rows,
  repaired rows, created outboxes, unresolved rows, and quarantines.
  `counter_scope` is `CUMULATIVE_SINCE_STARTED_AT`: each counter is a running
  total since the sweep row was first created (`started_at`), never a count of
  distinct punches.
  - `scanned`: row visits. A held punch is visited again after its six-hour
    retry or whenever its terminal's roster snapshot revision changes, and is
    counted at every visit.
  - `repaired`: repair steps, not punches. One synced-CNIC release adds two
    (the release and the check of the `PENDING` row it creates), and a source
    identity recovery can likewise add two.
  - `outbox_created`: rows the sweep created for an event whose identity was
    already verified. It leaves out the row a synced-CNIC release creates and
    the placeholder rows written for quarantined or unresolved events.
  - `unresolved`: row visits that left the punch held or unrecovered.
  - `quarantined`: events the sweep quarantined for an invalid event UID or
    device time.

  A pass ends when a page finds no candidates: the sweep goes `IDLE`, resets
  `last_event_id` to 0 and starts the next pass on the following maintenance
  tick, so `IDLE` is brief while held punches keep becoming due. `RUNNING`
  means only that the last page found work; it does not mean the held punches
  are on their way to Oracle.

During rollout, apply migration `20260922_0032`, deploy the ADD application,
and observe the historical sweep counters before widening the production
drain. A row that remains unresolved is safe to review through the existing
identity/release workflow; there is no force-attribution path.
