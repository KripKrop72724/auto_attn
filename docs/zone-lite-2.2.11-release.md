# Zone Lite 2.2.11 release record

Zone Lite 2.2.11 removes a redundant per-event receipt burst from authoritative
terminal truth reconciliation.

## Production evidence

`ZONE-PESHAWAR-02` preserved all 728 users and 95,327 attendance records after
its 2.2.10 OTA. Oracle accepted the firmware-forced truth window and the full
ADD attendance stream was durably staged, but the cycle still ended with
`ORACLE_RECEIPT_QUEUE_SATURATED`.

The remaining failure was architectural rather than a malformed receipt or
terminal data loss. A 95,327-record history requires roughly 953 receipt
batches. Their serialized event UIDs alone exceed the bounded 4 MiB ADD bulk
outbox when production temporarily outruns acknowledgement. Those receipts
duplicate ADD's durable delivery and Oracle membership-check worker.

## Fix

- Keep durable per-event Oracle receipts for live and bounded bulk delivery,
  where they retire the firmware ORDS outbox safely.
- Stop producing per-event `FIRMWARE_RECONCILE` receipts for a complete
  terminal history.
- Preserve the entire reconciled attendance stream in ADD's durable bulk
  outbox.
- Let ADD deliver every reconciled event idempotently and independently
  confirm existing Oracle membership through `raw-captures/check`.
- Emit an `ORACLE_RECONCILE_ACCEPTED` checkpoint for each accepted truth
  window.
- Complete a full reconcile only when the terminal dump, Oracle truth request,
  and durable ADD truth staging all succeed.

This keeps the actual attendance recovery paths fail-closed while removing a
bounded-storage dependency that did not add data durability.

## Rollout gate

1. Publish 2.2.11 as `HIL_ONLY` and update only `ZONE-SWAT-01`.
2. Require preserved counts, a completed firmware-forced full reconcile,
   clean logs, and a five-minute stable hold.
3. Promote the exact canary bytes.
4. Update `ZONE-PESHAWAR-02` and require its reconcile timestamp to advance
   past `2026-07-27T17:33:38Z`, with at least 95,327 punches preserved and no
   truth/outbox saturation or reconcile failure.
5. Continue the remaining devices one at a time only after that gate passes.
