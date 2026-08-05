# Zone Lite 2.2.45

## Production objective

Keep the 2.2.44 fail-safe live-capture and local-storage recovery behavior while
bounding telemetry during a large authoritative ZKT truth scan.

## Change

Some older terminals contain many zero or implausible timestamp rows. The live
path must still report an invalid punch immediately, but a bulk reconciliation
must not send one ADD log request for every legacy row.

2.2.45 validates timestamps before building bulk events, counts invalid rows,
and emits one `ATTENDANCE_TIMESTAMP_QUARANTINE_SUMMARY` after releasing both
storage gates. The terminal truth is unchanged and no preserved queue is
deleted or truncated.

If more than half of a supposedly stable dump has implausible timestamps, the
snapshot is now rejected before any authoritative window is sent and retried
on a fresh authenticated ZKT session with recovery-sized chunks.

When the preservation partition has no room for a catalog staging file, the
complete validated ADD alias catalog can be activated from a bounded PSRAM
stage. Flash persistence remains best-effort and is retried on reconnect; a
power loss fails closed to the encrypted on-flash catalog rather than exposing
or guessing identity data.

## Invariants

- Live capture, CNIC resolution, event identity, and Oracle truth membership
  rules are unchanged.
- A live invalid terminal timestamp still emits the existing immediate
  `ATTENDANCE_TIMESTAMP_QUARANTINED` event.
- Bulk scans still exclude every zero or implausible timestamp; only duplicate
  telemetry is collapsed.
- A mostly implausible dump can never be accepted as empty authoritative
  truth.
- Identity catalog refresh remains available under flash pressure without
  deleting attendance queues.
- The 2.2.44 direct ADD acknowledgement fallback and explicit local-failure
  recovery remain intact.
