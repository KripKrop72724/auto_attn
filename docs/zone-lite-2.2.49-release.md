# Zone Lite 2.2.49 measured retry-lease hotfix

## Production evidence

The 2.2.48 Tower-3 canary remained `ONLINE`, `LIVE_CAPTURE`, `OTA_READY`, and
`CERTIFIED` and correctly replaced the false `FULL_RECONCILE_FAILED` event with
`FULL_RECONCILE_DEFERRED_ORDS_GATE`. However, its live ZKT session took as long
as 96 seconds to return to the scheduled retry. The original 30-second priority
lease expired first, allowing the large ORDS drain to reacquire the gate and
leaving authoritative truth stale.

## Change

- The ORDS-gate priority lease is now a bounded three minutes.
- The five-second in-flight-drain grace period and ten-second scheduled retry
  remain unchanged.
- The measured 96-second retry cadence is covered with margin, so a subsequent
  ORDS transaction cannot start before the gateway gets another truth attempt.
- If the ZKT gateway disappears, the lease still expires automatically; ADD and
  ORDS queues remain durable and live capture is never deleted or bypassed.
- Gate contention remains informational. Real ZKT, identity, snapshot, and
  Oracle failures retain the existing fail-closed behavior.

## Acceptance gate

Re-run the immutable SWAT canary, then update Tower-3 only. Accept Tower-3 only
after a `FULL_RECONCILE_DEFERRED_ORDS_GATE` is followed by daily
`ORACLE_RECONCILE_DELEGATED_TO_ADD` truth windows, with no later
`FULL_RECONCILE_FAILED`, and the serial, 175 users, and at least 49,395 punches
remain preserved. Do not advance to Tower-13 or Peshawar before that evidence.
