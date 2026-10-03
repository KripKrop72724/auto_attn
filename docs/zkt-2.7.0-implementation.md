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
| Fresh APIs with stale screens | Canonical browser topics, reconnect/overflow resync, 30-second polling, focus refresh, shared device snapshots and old-response rejection | `test_browser_reliability.py`, `realtime.test.tsx`, existing drawer/App tests | Deployed in `bd395cc`; authenticated fleet API returns snapshot identity |
| Storage and worker failures conflated | Reported durability no longer derives from LED state; diagnostics v2 keeps probe failures, boot/sample identity and runtime obligations. ZKT persistence probes retry with backoff and clear only their own incident after a complete filesystem/NVS proof | `test_runtime_contract.py`, storage fault injection, ingestion and HIL tests; both ESP-IDF family builds passed | Partial: probe recovery implemented; queue/legacy incident recovery and boot gates remain open |
| Rejected evidence cannot be traced safely | Bounded rejection categories and envelope request IDs without copying protected payloads | `test_browser_reliability.py` | Implemented |
| Source timestamp/layout exceptions | Extracted 8/16/40-byte firmware and ADD fact decoders; explicit live layouts, calendar validation, strict count/layout agreement and bounded transport; six model selectors | Sanitized record/transport harnesses, 3,500 cross-language fact/clock vectors and ASan/UBSan | Partial: valid physical-model fixtures and actual exception root cause are unqualified |
| Dual delivery and same-second occurrence identity | Encrypted ADD observation/opaque receipts; exact-source occurrence aliases; canonical firmware encoding, strict typed receipt verification and bounded delivery worker | `test_zkt_custody.py`, independent C/Python vectors, socket dispatcher, actual-file delivery faults and PostgreSQL overlapping-socket tests | Partial: receiver disabled; capture activation and live/history semantic matching remain open |
| Custody can outlive an untracked processing obligation | Migration `0044` adds per-packet work and immutable receipt links in the custody transaction; bounded assembly, fair inspection, revision-triggered holds and an authenticated status endpoint | Receipt/work rollback, fragment conflict/replay, per-record decryption failure isolation, bounded repair, PostgreSQL overlapping sockets/SKIP LOCKED and actual dump/restore | Implemented components; profile-qualified interpretation and Oracle creation remain open |
| Competing writes, empty legacy scans, recovery faults | Verified-empty legacy cache; compact AES-GCM journal, reserved nonces, append-only segments, receipt-bound retirement and bounded journal storage task; raw capture hooks before live/interleaved ACK | Actual filesystem fault injection, independent crypto vectors, NVS port faults, concurrent owner and capture harnesses | Partial: capture/delivery startup gated; catalog and legacy handoff remain open |
| Repeated unchanged identity holds | Roster-revision eligibility and six-hour bounded audit; existing manual identity gates preserved | Backlog/identity/force-release regressions; PostgreSQL concurrency and two 100,000-row repair tests | Deployed in `bd395cc`; non-roster evidence scheduling needs wider qualification |
| Incompatible rollback | Bridge readers, exact predecessor manifests, persisted compatibility proof | Pending | Open |
| New release can bypass legacy storage-contract validation | 2.6.16/2.7.0 registration rejects until reader/rollback validation is implemented | `test_storage_contract.py` | Guard implemented; release intentionally blocked |
| Nationwide capacity and promotion evidence | Fixed 17-device scope; 75% partition budget, doubled peak and seven-day calculation; wave/location/concurrency and evidence evaluators. Fourteen-day fleet observation begins after the last device qualification, not installation | `test_zkt270_qualification.py` | Partial: offline evaluators only; not an OTA authorization service |
| Backup file existence mistaken for restore proof | Restore pre-deployment dump into an isolated database; verify revision; clean up; retain backup digest and verification time | PowerShell failure/cleanup regressions and actual disposable PostgreSQL restore | Production deployment `37135387664` passed this gate on 3 October 2026 |

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

The firmware adapter sends one immutable item and checks a typed committed
receipt for its exact identity/digest. Corrupt journal extents use explicit
`JOURNAL_EXCEPTION` custody. Decimal strings preserve 64-bit counters through
cJSON. The storage owner recomputes the payload before retirement, rejecting a
receipt for different bytes. The delivery task is implemented but startup and
capture activation remain gated; no signed candidate is implied by these checks.

The delivery worker submits one owner operation at a time and releases all
storage resources before an ADD exchange. Five-second owner deadlines release
only the caller's reply; accepted storage work still finishes. Missing custody
ACKs retry the same immutable bytes with bounded jitter. A receipt remains in
RAM until the owner commits retirement; uncertain retirement is recovered from
the durable checkpoint. Reclamation removes at most one settled segment per
step, including while ADD is offline. Phase/progress snapshots remain readable
while a network exchange is blocked. The current ACK wait is fifteen seconds,
with separately bounded connector mutex and socket-send waits.

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

Migration `0044` adds derived processing obligations without changing immutable
receipts. Receipt creation and its work link share one commit; a work-storage
failure emits no custody ACK. A fragmented packet has one work row and retains
every received extent, including conflicts. New evidence revisions wake a
group, while an unchanged replay cannot reschedule a hold. Missing fragments,
unqualified profiles, source associations, conflicting evidence and unavailable
decryption each have a separate state, reason and responsible component.

The bounded inspector locks the connector before its work rows, matching the
ingestion lock order, and skips connectors currently owned by another worker.
Per-connector quotas prevent one large backlog from consuming the inspection
batch within the 17-connector scope. It stops at profile qualification and
creates no attendance or Oracle rows. The maintenance schedule is not yet a
qualified throughput path for the proposed burst envelope.

Authenticated `GET /api/v1/devices/{connector_id}/zkt-custody` exposes paginated
work states and missing-work detection, never raw bytes or employee identity.
Receipts predating the work contract have a bounded idempotent backfill helper;
an orphan receipt blocks writer activation until repaired. This is a backend
status interface; the dashboard presentation still needs integration.

The ADD fact decoder supports explicit 8/16/40-byte historical and
12/32/36/52-byte live layouts. It checks record boundaries and calendar dates,
preserves the original encoded clock, and converts Pakistan local time to UTC.
The historical attendance UID never becomes a current enrollment identity.
Identical same-second records remain separate facts. A packet with multiple
valid allowed interpretations remains ambiguous. Model labels only select
profiles; they do not grant qualification. This module is not yet connected to
attendance creation, and it does not authenticate the packet checksum.

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
- Journal/custody integration: 195 firmware and backend tests passed; both family
  builds, independent crypto/wire vectors and all five allocation harnesses
  passed. A sixth harness now checks the actual socket custody-ACK dispatcher
  under delayed, duplicate, mismatched and allocation-failure conditions.
- Raw capture integration: 208 firmware/custody/packet tests passed; native and
  Linux sanitizer harnesses retained accepted writes after caller timeout and
  refused ACK for partial captures. Normal ZKT, gated journal-writer and
  Hikvision ESP-IDF development builds passed. Independent C/Python wire and
  pinned-mbedTLS vectors include whole packets, fragments and out-of-civil-range
  capture clocks. CI now compiles the gated writer separately and does not
  publish that integration image.
- Custody processing: 34 targeted tests and two real PostgreSQL locking tests
  passed. Additive migration/schema checks and a real dump/restore preserved
  encrypted observations, receipt IDs and work links through downgrade/upgrade.

- ADD fact decoder: 57 tests passed, including 3,500 synthetic comparisons
  against production C source/live decoders and the firmware clock under
  ASan/UBSan. Tests cover invalid dates, UTC boundaries, ambiguous live layouts,
  preserved historical UID fields and repeated same-second records. These are
  codec tests, not physical-model qualification.

## Work that still blocks the requested release

1. Qualify and activate durable raw capture before acknowledging live events.
   The ordinary, command-response and prepared-read hooks are implemented under
   a disabled writer build switch. Partial packet fragments remain holds.
2. Activate the journal storage/delivery tasks through the compatible-reader
   gate and wire live capture; transfer catalog and
   legacy storage operations to the same owner. Qualify actual ESP latency,
   resource headroom, corrupt-checkpoint recovery and remaining runtime tasks.
3. ADD-owned delivery for new records, preserved legacy migration checkpoints,
   one-to-one live/history matching, decoder correction provenance and automatic
   recovery of parser-affected history. No force-send or invented identity is allowed.
4. Signed 2.6.16 reader bridge and signed 2.7.0 candidate with exact predecessors,
   factory paths, compatible rollback slot and reproducible build records.
5. Trusted qualification collector and enforcement in OTA assignment. The scope
   and capacity evaluators cannot be substituted for measured evidence.
6. Thirty-day zone baselines, all-model fixtures, fleet burst/load/catch-up and
   complete fault matrix, seven-day automated soak and source-to-Oracle traces.
7. Repeat the backup/restore gate for subsequent backend deployments, then all
   four approved field waves and fourteen-day fleet observation. Karachi and
   other prerequisite failures remain in the denominator.

**Nationwide remote HIL: INCOMPLETE. Production qualification: INCOMPLETE.**
No connector has been upgraded or accepted by this implementation work.

## Journal component implementation

The journal byte format and failure behavior are documented in
[`zkt-journal-v1.md`](zkt-journal-v1.md). Journal code compiles into the ZKT family
only. There is no call to `zj_owner_start` from the running firmware yet, no
writer activation, and no change to the published firmware version.

The owner copies requests into eight bounded slots, reserves three slots for
capture/retirement, and limits priority bursts so delivery reads can progress.
Its mailbox mutex never spans filesystem/NVS work. A timed-out caller cannot
free an in-flight request; abandoning a reply does not cancel accepted capture.
Only a completed durable write returns a capture sequence. The task reports
operation start, progress, occupancy, saturation and separate NVS/filesystem
errors. It never deletes or restarts another task that might own a lock.

Host tests use actual files and injected short writes, open/read/seek/sync/close
failures, interrupted rotation, malformed tails, corrupted records, uncertain
retirement and uncertain nonce reservations. The key-state harness verifies
terminal binding, orphan refusal, corruption detection and exact readback.
The concurrent owner harness demonstrates timeout safety and delivery at full
capacity. These are software tests, not physical flash qualification.

The delivery harness uses actual journal files and the bounded owner mailbox.
It checks lost responses after simulated ADD commit, a zero receipt proof,
uncertain retirement followed by reopening storage, disconnection between read
and send, owner stalls across monotonic-clock wrap, failed reply abandonment,
and bounded saturation. Valid retries preserve the exact wire payload; empty
polling performs no filesystem scan. Native Clang and Linux GCC sanitizer runs
passed. ESP task startup remains disabled until compatibility proof exists.

The CI failure caused by the transport harness's ambiguous C indentation was
fixed without disabling compiler warnings or tests. PR #260 and its main-branch
run passed all six CI jobs. Production deployment `37135387664` passed the
backup restore gate, migration and origin/public health checks for `bd395cc`.
An authenticated post-deployment fleet query confirmed snapshot identities.
Journal development and firmware qualification remain separate.
