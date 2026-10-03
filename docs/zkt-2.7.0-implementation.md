# ZKT 2.7.0 implementation register

Baseline: `6b8bf0732df2e9c26db376fe7835132baffd4c4f`. Scope: the 17 active ZKT
connectors in the approved nationwide plan. Lahore 03 is a spare; Hikvision
behavior remains a regression requirement, not part of this rollout.

This register records implementation evidence. It is not a release certificate.
No signed 2.6.16 bridge or 2.7.0 candidate is qualified by the existence of code.
Physical power interruption and endurance qualification: **NOT_PERFORMED**.

The separate local nationwide status document records all 17 devices, open
Wave D prerequisites and a scoped read-only ADD classification. Live operational
counts are kept outside this public repository.

## Issue, change and verification

| Issue | Change | Verification | Status |
|---|---|---|---|
| Fresh APIs with stale screens | Canonical browser topics, reconnect/overflow resync, 30-second polling, focus refresh, shared device snapshots and old-response rejection | `test_browser_reliability.py`, `realtime.test.tsx`, existing drawer/App tests | Implemented; local tests passed; not deployed |
| Storage and worker failures conflated | Reported durability no longer derives from LED state; diagnostics v2 keeps probe failures, boot/sample identity and runtime obligations. ZKT persistence probes retry with backoff and clear only their own incident after a complete filesystem/NVS proof | `test_runtime_contract.py`, storage fault injection, ingestion and HIL tests; both ESP-IDF family builds passed | Partial: probe recovery implemented; queue/legacy incident recovery and boot gates remain open |
| Rejected evidence cannot be traced safely | Bounded rejection categories and envelope request IDs without copying protected payloads | `test_browser_reliability.py` | Implemented |
| Source timestamp/layout exceptions | Extracted 8/16/40-byte decoder; strict count/layout agreement; bounded range and session/length checks; six model selectors | Sanitized record and fragmented/coalesced transport harnesses with ASan/UBSan | Partial: valid physical-model fixtures and actual exception root cause are unqualified |
| Dual delivery and same-second occurrence identity | Versioned encrypted ADD observation receipts; exact-source occurrence aliases; replay returns receipt even after later transport sequence | `test_zkt_custody.py`, PostgreSQL overlapping-socket test; additive migration/restore | Partial: receiver disabled; journal transport and live/history semantic matching remain open |
| Competing writes, empty legacy scans, recovery faults | Verified-empty cache with producer invalidation for legacy queues and ADD outboxes | Blocked/legacy drain and admission fault harnesses | Partial: empty scans fixed; storage owner, journal and recovery state machine remain open |
| Repeated unchanged identity holds | Roster-revision eligibility and six-hour bounded audit; existing manual identity gates preserved | Backlog/identity/force-release regressions; PostgreSQL concurrency and two 100,000-row repair tests | Implemented; not deployed; non-roster evidence scheduling needs wider qualification |
| Incompatible rollback | Bridge readers, exact predecessor manifests, persisted compatibility proof | Pending | Open |
| New release can bypass legacy storage-contract validation | 2.6.16/2.7.0 registration rejects until reader/rollback validation is implemented | `test_storage_contract.py` | Guard implemented; release intentionally blocked |
| Nationwide capacity and promotion evidence | Fixed 17-device scope; 75% partition budget, doubled peak and seven-day calculation; wave/location/concurrency and evidence evaluators. Fourteen-day fleet observation begins after the last device qualification, not installation | `test_zkt270_qualification.py` | Partial: offline evaluators only; not an OTA authorization service |
| Backup file existence mistaken for restore proof | Restore pre-deployment dump into an isolated database; verify revision; clean up; retain backup digest and verification time | PowerShell failure/cleanup regressions and actual disposable PostgreSQL restore | Implemented; production backup has not been taken |

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

ZKT persistence-probe errors are independent of attendance-write and queue faults.
Retries back off from two to sixty seconds, rerun both stores' proof, and never
clear an unrelated storage error. Successful recovery clears the active probe
error and consecutive count; the total failure count remains visible until reboot.
Hikvision retains its prior recovery policy. No LED reset substitutes for a
successful storage check.

The event hub remains process-local. Deploy one ADD web worker until a shared
event and connector transport is implemented; multiple workers are not qualified.

## Custody receiver contract

`zkt_observation_batch` schema 1 carries at most 64 bounded raw observations.
Capture identity is SHA-256 of canonical JSON
`["zkt-observation-v1", terminal_serial, capture_epoch, capture_sequence]`.
The sequence belongs to a persisted capture epoch and must never be reused.
The receiver preserves an encrypted immutable payload and per-item receipt.
Malformed or conflicting observations receive preserved-exception dispositions;
infrastructure failures roll back and emit no receipt. A changed payload cannot
replace the original. Replays keep the original receipt and cannot rewind a
newer boot's telemetry identity. Oracle completion is never asserted by custody.

Exact source references bind only against committed canonical manifests with
matching connector, terminal, source epoch, ordinal and raw digest. The alias
keeps any existing attendance ID/Oracle key unchanged. Identical raw bytes at
different ordinals have different occurrence IDs. This is **not** the completed
live/history matcher: live-frame bytes differ from history bytes, so guessed
ordinals and timestamp-only associations are rejected. No new employee or
attendance record is inferred by this receiver.

Migration `0043` adds `zkt_custody_enabled=false` for every connector. There is
no operator activation endpoint in this stage. Do not set the field in
production until the journal reader, delivery adapter, occurrence matching and
bridge/rollback gates are complete. Rollback migrations retain custody evidence.

## Verification recorded on 3 October 2026

- Full local backend/firmware/companion regression run: 1,388 passed, 27 skipped.
  Skips require specific
  environments; they are not qualification passes.
- Frontend: 141 full-suite tests passed, including missing/wrong-boot evidence and
  active-versus-historical probe errors; TypeScript, production build and bundle
  budget passed. Browser matrix: 82 passed, 14 intentionally skipped by viewport.
- Subsequent storage/protocol changes: all 199 firmware and targeted runtime/HIL
  tests passed, followed by 33 qualification/HIL/runtime tests. A simulated
  multi-gigabyte terminal response fails without draining an untrusted body.
- ESP-IDF 5.5.3: ZKT and Hikvision development images compiled. Images are
  unsigned and retain their existing version identities; neither is a 2.7.0 candidate.
- PostgreSQL: additive migrations and schema check passed. Existing concurrency
  and two 100,000-row backlog tests passed. New overlapping custody sockets
  returned one committed receipt.
- Actual disposable PostgreSQL dump/restore preserved encrypted raw bytes and
  receipt IDs through additive downgrade/upgrade. This proves the local test
  procedure, not a current production backup.
- PowerShell restore gate tests cover successful restore, restore failure,
  revision mismatch, generated database identity and cleanup.
- CI now runs all five pinned-cJSON allocation/persistence harnesses; the prior
  command passed four script names as arguments to the first Python process.
  Dormant harness extraction/stubs were repaired and all five passed locally.

## Work that still blocks the requested release

1. Durable raw capture before acknowledging interleaved live events; the current
   prepared-read path still relies on terminal-tail recovery and is unqualified.
2. Single storage owner, compact authenticated journal, nonce reservation,
   interrupted-write recovery, incremental reclamation and bounded runtime tasks.
3. ADD-owned delivery for new records, preserved legacy migration checkpoints,
   one-to-one live/history matching, decoder correction provenance and automatic
   recovery of parser-affected history. No force-send or invented identity is allowed.
4. Signed 2.6.16 reader bridge and signed 2.7.0 candidate with exact predecessors,
   factory paths, compatible rollback slot and reproducible build records.
5. Trusted qualification collector and enforcement in OTA assignment. The scope
   and capacity evaluators cannot be substituted for measured evidence.
6. Thirty-day zone baselines, all-model fixtures, fleet burst/load/catch-up and
   complete fault matrix, seven-day automated soak and source-to-Oracle traces.
7. Production backup/restore gate, staged backend deployment, all four approved
   field waves and fourteen-day fleet observation. Karachi and other prerequisite
   failures remain in the denominator.

**Nationwide remote HIL: INCOMPLETE. Production qualification: INCOMPLETE.**
No connector has been upgraded or accepted by this implementation work.
