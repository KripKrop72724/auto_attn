# Zone Lite 2.2.9 release record

Zone Lite 2.2.9 makes authoritative Oracle receipt batches valid when a
terminal history contains repeated copies of the same physical punch.

## Failure addressed

The 2.2.8 Peshawar-02 regression run passed the former two-minute outbox
failure point and completed its large authoritative Oracle request. The next
step attempted to persist Oracle-confirmation receipts in batches of at most
100 event UIDs. A terminal can retain duplicate rows that deliberately map to
the same deterministic event UID, while the durable ADD receipt schema
requires every UID inside one message to be unique. The validator therefore
rejected that message before it reached the outbox, and the higher-level code
reported the generic `ORACLE_RECEIPT_QUEUE_SATURATED` error.

## Durable behavior

- Every receipt message validates all supplied event UIDs and collapses exact
  duplicates before durable append.
- The first occurrence remains in the message. Repeats across separate
  messages remain safe because ADD receipt ingestion is idempotent by event
  UID.
- The receipt payload remains bounded to 100 supplied UIDs and always contains
  at least one unique UID.
- Zone Lite 2.2.8's progress-aware outbox deadline, prior-backlog deferral,
  live-capture priority, and durable OTA journal protections are unchanged.

## Rollout gates

1. Build and sign the exact green main commit with ESP-IDF 5.5.3.
2. Publish 2.2.9 as `HIL_ONLY` and update only `ZONE-SWAT-01`.
3. Require one SWAT boot, a successful full reconcile, preserved terminal
   counts, and no new error or queue-saturation log.
4. Promote the exact canary-tested bytes.
5. Retry `ZONE-PESHAWAR-02` and require its 95,327-record truth cycle to finish
   without `ORACLE_RECEIPT_QUEUE_SATURATED` or `FULL_RECONCILE_FAILED`.
6. Update the remaining OTA_READY devices sequentially only after the
   Peshawar-02 regression gate passes.
