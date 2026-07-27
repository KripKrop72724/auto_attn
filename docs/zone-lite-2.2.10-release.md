# Zone Lite 2.2.10 release record

Zone Lite 2.2.10 hardens authoritative Oracle receipt staging during large
terminal truth reconciliations.

## Production evidence

`ZONE-PESHAWAR-02` preserved all 728 users and 95,327 terminal attendance
records after its 2.2.9 OTA. Its first full truth retry then failed at receipt
staging after the terminal dump had remained resident for several minutes.
The device stayed online, live capture continued, and no terminal data was
deleted.

The receipt path still built a 100-item cJSON tree and then reparsed and
reprinted it to create the durable ADD outbox row. That transient object graph
used constrained heap while the multi-megabyte terminal dump was resident.
The high-level failure was reported as `ORACLE_RECEIPT_QUEUE_SATURATED`, even
though the durable outbox had drained before the truth cycle began.

## Fix

- Validate the confirmation path, timestamp, and every event UID before
  serialization.
- Collapse exact duplicate event UIDs within each bounded receipt batch.
- Construct the complete, bounded outbox record directly in an 8 KiB PSRAM
  buffer, with a normal-heap fallback.
- Enforce every write against `ADD_OUTBOX_LINE_BYTES`.
- Preserve the independent parse-and-validation pass in the outbox worker
  before transmission.
- Keep the existing progress-aware durable-outbox backpressure and
  acknowledgement rules unchanged.

## Rollout gate

1. Build and publish 2.2.10 as `HIL_ONLY`.
2. Update only `ZONE-SWAT-01`; require one boot, preserved counts, a completed
   firmware-forced full reconcile, clean logs, and a five-minute stable hold.
3. Promote the exact canary bytes.
4. Update `ZONE-PESHAWAR-02` and require its reconcile timestamp to advance
   past `2026-07-27T17:33:38Z`, with all 95,327 terminal records preserved and
   no receipt/truth saturation or reconcile failure.
5. Only then continue the remaining nationwide rollout one device at a time.
