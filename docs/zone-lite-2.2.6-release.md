# Zone Lite 2.2.6 release record

## Purpose

Zone Lite 2.2.6 fixes a large-history reconciliation failure observed on
`ZONE-SLICTOWER-3FL`. Oracle accepted the terminal's authoritative truth, but
the connector's bounded 4 MiB ADD outbox filled while the same history was
being queued for the dashboard. That left ADD delivery receipts behind the
bulk replay and caused the six-hour truth cycle to repeat as failed.

## Changes

- Oracle confirmation receipts are durably queued before the larger,
  idempotent ADD attendance replay for each bounded day window.
- The ADD reconcile outbox now applies bounded backpressure while acknowledged
  rows drain. It compacts acknowledged prefixes and resumes the same truth
  cycle instead of immediately failing at the fixed storage ceiling.
- Live attendance retains its separate priority outbox and is not blocked by
  historical repair.
- ADD verifies retry membership in batches of 100 with bounded concurrency of
  8. Existing Oracle rows converge to `ACKED_CHECK` promptly without resending
  them.

## Safety invariants

- The application-only OTA does not modify the bootloader, partition table,
  encrypted NVS, OTA metadata, identity catalog, or attendance storage files.
- Backpressure is bounded to two minutes per append attempt. If ADD is
  unavailable, terminal history and the existing durable outbox remain intact
  and the next reconcile cycle retries idempotently.
- The firmware never removes a terminal attendance record.
- Oracle and ADD continue to deduplicate by the immutable event UID.

## Production acceptance

1. Build and test the exact `main` commit.
2. Publish immutable signed version `2.2.6`.
3. Update `ZONE-SWAT-01` first and verify stable live capture, a complete
   firmware-forced truth pass, and zero Oracle duplicates.
4. Update one remaining `OTA_READY` zone at a time.
5. For every zone, verify `ONLINE`, `CERTIFIED`, complete user snapshot, normal
   live capture, successful truth reconciliation, a drained ADD retry queue,
   and Oracle total equal to Oracle distinct-event count.
6. For the reported SLICTOWER-3FL employee, require all 48 ADD rows to show an
   Oracle confirmation and independently verify all 48 immutable event UIDs
   through the Oracle membership API.
