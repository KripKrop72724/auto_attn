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
  This is the only real delivery gap and should be zero.
- `held_without_outbox` counts identity-held events without a delivery row and
  `review_without_outbox` counts quarantined or unknown-state events without
  one. Both are held by design: ZKT terminal ingestion creates no Oracle work
  item for a held or quarantined punch, while Hikvision and journal ingestion
  keep a placeholder row in the held status. A verified release (synced-CNIC
  proof, Force release or Direct ORDS) creates the delivery row; the sweep
  never does so for a punch that still needs approval.
- `without_outbox_total` and `without_outbox_by_status` show the whole
  outbox-less population by stored delivery status. The attendance recovery
  summary reports the same three numbers.
- `historical_sweep` reports state, last event ID, pages, scanned rows,
  repaired rows, created outboxes, unresolved rows, and quarantines.
  `counter_scope` is `CUMULATIVE_ROW_VISITS`: the counters add every row visit
  since `started_at`, so a held punch re-checked many times is counted many
  times. The sweep keeps re-checking held punches after six hours or a roster
  revision change, so `RUNNING` means only that its last page found work; it
  does not mean the held punches are on their way to Oracle.

During rollout, apply migration `20260922_0032`, deploy the ADD application,
and observe the historical sweep counters before widening the production
drain. A row that remains unresolved is safe to review through the existing
identity/release workflow; there is no force-attribution path.
