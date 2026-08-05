# Zone Lite 2.2.48 truth/ORDS arbitration hotfix

## Incident

`ZONE-SLICTOWER-3FL` remained live on 2.2.46, but every forced truth pass was
reported as `FULL_RECONCILE_FAILED` exactly 75 seconds after it began. The ZKT
was not failing: a storage-full background ORDS rewrite held the shared outbox
gate, and the foreground truth pass exhausted the old five-times-HTTP timeout
before it ever issued `CMD_ATTLOG_RRQ`.

## Change

- A due authoritative truth pass reserves short, expiring priority over the
  background ORDS drain.
- An in-flight drain gets five seconds to release the gate. If it is still
  busy, live capture continues and truth retries after ten seconds.
- The reservation expires after thirty seconds if the ZKT session disappears,
  so ORDS delivery cannot be starved by a dead gateway.
- Gate contention is reported as `FULL_RECONCILE_DEFERRED_ORDS_GATE`, not as a
  ZKT read failure, and the operator log is rate limited.
- Real ZKT transport, snapshot, identity, and Oracle failures retain their
  existing fail-closed behavior and fresh-session retry limits.

## Production acceptance

After the exact SWAT canary and immutable promotion, update Tower-3 before any
Peshawar device. Accept only if Tower-3 remains `ONLINE`, `LIVE_CAPTURE`,
`OTA_READY`, and `CERTIFIED`; preserves serial `ADZV211860253`, 175 users, and
at least 49,394 punches; clears `ESP_LOCAL_FAILURE`; and completes a full truth
pass without a later `FULL_RECONCILE_FAILED` event.
