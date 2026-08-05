# Zone Lite 2.2.46

## Production objective

Keep live capture and ADD truth recovery healthy when a device's preservation
partition is too full to create an ORDS pending-outbox rewrite file.

## Change

The ORDS uploader now treats `ENOSPC` while creating, writing, or committing
its temporary rewrite as storage backpressure. The original pending outbox is
left unchanged, delivery retries back off for 60 seconds, and one rate-limited
`ORDS_DRAIN_STORAGE_BACKPRESSURE` warning is reported instead of repeatedly
re-latching `ESP_LOCAL_FAILURE` every two seconds.

Every temporary rewrite write is checked. A rewritten outbox is promoted only
after the complete file passes `fflush` and `fsync`; any partial or unreadable
temporary file is deleted while the original authoritative outbox remains in
place. Non-capacity I/O failures continue to fail closed as local faults.

## Invariants

- No attendance, blocked-identity, receipt, or preservation queue is deleted
  to manufacture free space.
- Live ZKT event capture, direct ADD acknowledgement, truth membership, CNIC
  resolution, and Wi-Fi provisioning behavior are unchanged.
- Successful idempotent Oracle deliveries can be retried safely if a rewrite
  cannot be committed.
- A real non-capacity storage error remains visible as `ESP_LOCAL_FAILURE`.
- The 2.2.45 timestamp prevalidation and bounded PSRAM catalog fallback remain
  intact.
