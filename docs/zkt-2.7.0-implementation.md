# ZKT 2.7.0 implementation register

Baseline: `6b8bf0732df2e9c26db376fe7835132baffd4c4f`. Scope: the 17 active ZKT
connectors in the approved nationwide plan. Lahore 03 is a spare; Hikvision
behavior remains a regression requirement, not part of this rollout.

This register records implementation evidence. It is not a release certificate.
No signed 2.6.16 bridge or 2.7.0 candidate is qualified by the existence of code.
Physical power interruption and endurance qualification: **NOT_PERFORMED**.

## Issue, change and verification

| Issue | Change | Verification | Status |
|---|---|---|---|
| Fresh APIs with stale screens | Canonical browser topics, reconnect/overflow resync, 30-second polling, focus refresh, shared device snapshots and old-response rejection | `test_browser_reliability.py`, `realtime.test.tsx`, existing drawer/App tests | Implemented; local tests in progress |
| Storage and worker failures conflated | Durability no longer derives from LED state; diagnostics v2 keeps probe failures, boot/sample identity and runtime obligations | `test_runtime_contract.py`, ingestion and HIL tests; ESP build required | Implemented; hardware unqualified |
| Rejected evidence cannot be traced safely | Bounded rejection categories and envelope request IDs without copying protected payloads | `test_browser_reliability.py` | Implemented |
| Source timestamp/layout exceptions | Explicit decoder and framing qualification | Pending | Open |
| Dual delivery and same-second occurrence identity | ADD custody, aliases and destination receipts | Pending | Open |
| Competing writes, empty legacy scans, recovery faults | Single storage owner and compact journal | Pending | Open |
| Repeated unchanged identity holds | Evidence revision scheduling and bounded audit | Pending | Open |
| Incompatible rollback | Bridge readers, exact predecessor manifests, persisted compatibility proof | Pending | Open |
| Nationwide capacity and promotion evidence | Per-device capacity, wave and qualification gates | Pending | Open |

## Required release evidence

ADD backup restore, additive migration and rollback-reader verification precede
new writers. Candidate signing, a seven-day automated soak, each field device's
ordinary punch traces over two working days, wave observation and the final
fourteen-day fleet observation remain separate gates. No historical alert,
component test, short smoke run or healthy network connection can substitute for
these gates. Blocked devices remain in the denominator.

## Browser event contract

Wire envelopes map to canonical `device`, `users`, `attendance`, `command` and
`reconciliation` topics in `zk_add/realtime.py`. Other existing named topics are
unchanged. Cursors combine a server-generation UUID and monotonically increasing
sequence. Every connection and subscriber overflow requests `resync`; periodic
named keepalives measure transport liveness only. Clients also fetch snapshots
every 30 seconds and on focus/visibility recovery. `snapshot_at` orders shared
fleet/detail snapshots; `firmware_diagnostics_at` is telemetry receipt time,
`sampled_at` is device sampling time, and `boot_id` binds health to a boot.

The event hub remains process-local. Deploy one ADD web worker until a shared
event and connector transport is implemented; multiple workers are not qualified.
