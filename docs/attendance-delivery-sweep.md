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
  firmware-receipt or membership checks) that have no durable delivery row.
  This is the only real delivery gap and should be zero. The sweep rebuilds
  the row only for `PENDING`, `FAILED_RETRYABLE` and `RETRYING` events, within
  one pass. An `IN_FLIGHT`, `FIRMWARE_RECEIPT_*`, `ACKED_FIRMWARE` or
  `MEMBERSHIP_*` event without a row is never picked up by the sweep: preview
  **Rebuild missing outbox** (`REBUILD_OUTBOX`) in attendance recovery once its
  rollout gates are enabled (see `attendance-recovery-runbook.md`), or
  investigate how the row was lost. `without_outbox_by_status` names the
  statuses involved.
- `held_without_outbox` counts identity holds without a delivery row:
  `BLOCKED_IDENTITY`, `QUARANTINED_IDENTITY_REUSE` and `WAITING_FOR_SNAPSHOT`,
  the statuses the manual-approval guard, the sweep and Force release treat as
  held. `review_without_outbox` counts every other event without a row: the
  non-identity quarantines (invalid device time, invalid event UID, missing
  terminal provenance, ORDS rejection, source conflict) and any state ADD does
  not recognise. Both are held by design: ZKT terminal ingestion creates no
  Oracle work item for a held or quarantined punch, while Hikvision and journal
  ingestion keep a placeholder row in the held status. A verified release
  (synced-CNIC proof, Force release or Direct ORDS) creates the delivery row;
  the sweep never does so for a punch that still needs approval.
- These counts describe ADD's queue, not Oracle. A legacy ZKT terminal can
  post a punch to ORDS itself. When its firmware receipt reaches ADD for a held
  punch without a release decision, ADD keeps the receipt as evidence but
  creates no outbox and leaves the punch held. A held punch without an outbox
  can therefore already be in Oracle under the identity the terminal sent;
  check `add_oracle_receipts` by `event_uid` before reading the held count as
  "not in Oracle".
- `without_outbox_total` and `without_outbox_by_status` show the whole
  outbox-less population by stored delivery status. The attendance recovery
  summary reports the same three numbers.
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
