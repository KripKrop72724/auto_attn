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

- `missing_outbox` counts retained events without a durable delivery row;
- `historical_sweep` reports state, last event ID, pages, scanned rows,
  repaired rows, created outboxes, unresolved rows, and quarantines.

During rollout, apply migration `20260922_0032`, deploy the ADD application,
and observe the historical sweep counters before widening the production
drain. A row that remains unresolved is safe to review through the existing
identity/release workflow; there is no force-attribution path.
