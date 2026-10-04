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
| Legacy probes hide journal worker and append failures | Separate owner, synchronous capture, ADD transport and retained legacy workers; bounded mailbox/catalog snapshots; append faults survive unrelated work; unknown authority is retained as recovery telemetry | Actual owner-thread recovery and capacity tests; pinned cJSON allocation faults; heartbeat, HIL and browser evidence tests | Implemented diagnostic component; migration counts, latency qualification and release acceptance remain open |
| Rejected evidence cannot be traced safely | Bounded rejection categories and envelope request IDs without copying protected payloads | `test_browser_reliability.py` | Implemented |
| Source timestamp/layout exceptions | Extracted 8/16/40-byte firmware and ADD fact decoders; explicit live layouts, calendar validation, strict count/layout agreement and bounded transport; six model selectors | Sanitized record/transport harnesses, 3,500 cross-language fact/clock vectors and ASan/UBSan | Partial: valid physical-model fixtures and actual exception root cause are unqualified |
| History encoding performs interpretation before raw custody | Exact 2.7.0 source rows emit raw evidence only when journal runtime permits writing; no legacy interpretation fallback through a hold | Production encoder with pinned cJSON, every allocation failure, invalid/zero raw bytes, missing roster and actual runtime gates | Encoder component only; receiver activation, bridge cutover and qualified interpretation remain open |
| Short socket reads can extend a terminal operation indefinitely | One absolute monotonic deadline for a command, prepared-buffer transfer or live frame, including interleaved preservation and ACK; per-call nonblocking I/O after readiness | Fragment trickles, repeated events, interrupted waits, readiness races, actual Unix/TCP sockets and both ESP-IDF builds | Implemented transport bound; terminal scheduling and physical-model qualification remain open |
| Historical attendance UID mistaken for current enrollment identity | Never supply a historical UID to current-roster matching; reject empty 40-byte text identities; preserve missing-reference source rows as `IDENTITY_UNRESOLVED`. Review notes cannot remove the identity hold or certify Oracle delivery | Synthetic empty/space-only fields, C/ADD rejection agreement, actual historical parser, baseline/tail replay and review-gate tests | Implemented guard; historical correction evidence and model qualification remain open |
| Dual delivery and same-second occurrence identity | Encrypted ADD observation/opaque receipts; exact-source occurrence aliases; canonical firmware encoding, strict typed receipt verification and bounded delivery worker | `test_zkt_custody.py`, independent C/Python vectors, socket dispatcher, actual-file delivery faults and PostgreSQL overlapping-socket tests | Partial: receiver disabled; capture activation and live/history semantic matching remain open |
| Delayed source replies can cross recovery epochs | Assignments, requests, committed ACKs and coverage carry the terminal source UUID; the bridge/writer rejects missing epochs, checks reply type and epoch, and refreshes coverage authority after reboot | Legacy and bound transactions, same-generation epoch recovery, lost replies, stale duplicate attempts, bounded parsers and allocation/sanitizer tests | Implemented wire component; model and occurrence qualification remain open |
| Legacy event IDs collapse distinct source occurrences | Custody inspection holds a shared attendance link when distinct canonical ordinals in one source epoch point to the same legacy attendance row; changed links and unverified attendance ownership remain explicit holds | Same-second/equal-byte source fixtures, retained acknowledged outboxes, binding/alias faults and independent later-record progress | Negative guard implemented; occurrence-based attendance creation and live/history matching remain open |
| Custody can outlive an untracked processing obligation | Migration `0044` adds per-packet work and immutable receipt links in the custody transaction; bounded assembly, fair inspection, revision-triggered holds and an authenticated status endpoint | Receipt/work rollback, fragment conflict/replay, per-record decryption failure isolation, bounded repair, PostgreSQL overlapping sockets/SKIP LOCKED and actual dump/restore | Implemented components; profile-qualified interpretation and Oracle creation remain open |
| Decoder retries and changes leave no inspectable interpretation history | Encrypted, versioned, append-only evidence processes at most 128 records per step; keeps record errors, alternate layouts and correction provenance separate from immutable source custody | Synthetic maximum packets, interrupted transactions, corrupted chains, decoder revisions, PostgreSQL concurrency, 200,000-row scheduling, migration retention and UI status tests | Derived-evidence component only; profile qualification, attendance creation and historical repair remain open |
| Source preservation depends on a premature interpretation | Epoch-bound `RAW_PRESERVED` ingress commits original bytes, a range receipt, cursor and processing obligation together; separate pending-interpretation counters and Oracle holds | SQLite/PostgreSQL transaction failure, lost reply, concurrent replay, byte/binding mutation, migration retention and browser assurance tests | Receiver component only; disabled pending qualified interpretation, occurrence matching and delivery integration |
| Competing writes, empty legacy scans, recovery faults | Verified-empty legacy cache; compact AES-GCM journal, reserved nonces, append-only segments, receipt-bound retirement and bounded journal storage task; raw capture hooks before live/interleaved ACK | Actual filesystem fault injection, independent crypto vectors, NVS port faults, concurrent owner and capture harnesses | Partial: gated startup implemented; ESP qualification and per-item legacy migration remain open |
| Runtime checkpoint writes compete with the storage task; a timed-out commit can reuse an old generation | Exact bridge/writer versions route runtime NVS through the owner, retain unfinished replies, allocate generations from actual NVS and verify committed readback | NVS failure and late-result injection, actual owner thread, corruption/capacity isolation and quiescence; gateway fallback refusal | Implemented component; legacy custody migration, corrupt-lease recovery and physical qualification remain open |
| Catalog replacement competes with capture and verifies entire files without yielding | Bridge/writer catalog mutations use the owner, token/offset-bound 512-byte producer chunks and 4 KiB transaction steps; retained timeout replies block conflicting use and stale RAM aliases | Actual owner-thread live interleaving, timeout/admission/allocation faults, interrupted transactions and recovery by the unchanged reader; invalidated-alias regression | Integrated in gated builds; legacy custody migration and physical latency qualification remain open |
| Slow repeated identity reads in backlog previews | Connector-locked preview batches reuse shared identity evidence and fetch outboxes once; release and delivery revalidate without the preview cache | Mixed-record proof equality/query bound; next-transaction identity conflict; existing 100,000-row responsiveness test | Implemented; no manual jobs created or approved |
| Oracle core-field check can miss changed zone/device/capture/clock/trust data or unfinished daily processing | Versioned full stored-raw projection and independently verified daily punch times; weaker responses and pending daily work cannot acknowledge delivery | SQLite/PostgreSQL replay/fencing regressions, an isolated Oracle compile/execution job with a 200,001-row synthetic retained set, and nonexecuting anonymous compilation against 19c table definitions | Implemented component only; isolated 19c execution, ORDS and business-policy qualification remain open; field intents inactive |
| Repeated unchanged identity holds | Roster-revision eligibility and six-hour bounded audit; existing manual identity gates preserved | Backlog/identity/force-release regressions; PostgreSQL concurrency and two 100,000-row repair tests | Deployed in `bd395cc`; non-roster evidence scheduling needs wider qualification |
| Incompatible rollback | Persisted reader proof binds the validated bridge image, OTA slot, terminal, epoch and layout; gated startup separates reading from writing; pre-erase OTA check preserves the certified bridge | Native/Linux sanitizer, actual ESP adapter and OTA/owner fault harnesses | Partial: install interlock implemented; operational rollback selection, complete migration and signed bridge qualification remain open |
| Rollback restores dual delivery for new punches | Irreversible ADD authority commits before sequence allocation; the validated bridge resumes raw journal capture after cutover; uncertain authority and disabled builds cannot fall back | Root/NVS commit/readback faults, actual owner, bridge boot/reboot, live dispatch and UI uncertainty checks | Implemented cutover component; legacy migration, full runtime telemetry and signed qualification remain open |
| OTA boot confirmation waits for ADD or an unused ESP Oracle worker; HTTP success can conceal a rejected state | Exact bridge/writer images validate local preservation, required workers and an authenticated terminal snapshot before contacting ADD; durable stages order reports and typed receipts verify actual deployment state and running image | Local outage, lost-reply, interrupted checkpoint, stale-worker, mark-valid failure, rejected-state and receipt allocation tests; legacy/Hikvision regression builds | Implemented local boot component; remote HIL, operational rollback and signed qualification remain open |
| OTA restart can race a live read or a timed-out caller's accepted write | A terminal-owner handoff blocks new sessions; the journal owner drains accepted work before acknowledging quiescence | Production gateway/OTA control flow, blocked threaded writes, abandoned callers, lock failures and Hikvision regression | Implemented component; migration evidence and physical qualification remain open |
| Command storage competes with attendance preservation | Exact bridge/writer images stream encrypted inbox work and bounded processed/cancelled-ID scans through the storage owner, retain uncertain replies, and serialize recovery | Production adapters, allocation/replay/malformed-line tests, short writes, failed sync/close, late completion and owner-task fairness | Command-storage component; legacy custody migration remains open |
| Retained segmented queue readers and checkpoint writers compete with capture | New images route queue operations through the storage task with fixed PSRAM buffers, 512-byte copies, absolute deadlines and retained replies | Real queue files, actual public entry points and owner thread; full/refused/interrupted operations, lost replies, stale tokens and maximum records | Segmented ownership component; receipt-bound migration and ESP headroom remain open |
| ADD flat-file producers and delivery still perform competing storage operations | New-image producers and delivery use copied owner requests; consumed-prefix recovery yields, all surviving generations drain, and verified-empty evidence is invalidated by writes | Actual adapters and family routing, real files, storage/NVS faults, lost replies, backup-restore failure, stale tokens and owner quiescence | ADD flat-file ownership implemented; migration evidence and ESP qualification remain open |
| A failed Oracle backlog restore can be mistaken for an empty queue | Require successful generation restoration before scanning or caching empty; propagate retirement/restore errors and keep failed boot restoration visible | Actual Oracle read/send/commit orchestration with failed rename/stat, preserved backup/temp files, retry and lock release; production boot failure checks | Recovery defect corrected; complete migration and field qualification remain open |
| Retained corrupt-file evidence bypasses the storage task | New-image quarantine reads and exact-receipt retirement use copied owner requests, existing NVS checkpoints and bounded prefix recovery | Actual production adapters and delivery slice, raw/binary/oversized tails, lost receipts/replies, NVS/read/close/stat/remove faults, empty cache and both family paths | Implemented evidence component; raw custody does not resolve identity or deliver attendance to Oracle |
| Legacy Oracle and identity-blocked files still compete with capture | New-image producers, readers and retirement use the storage task; initial recovery is deferred from boot and yields between prefix slices | Actual adapter and gateway/Oracle/blocked consumers, unchanged checkpoint reader, surviving generations, failed writes/restores/checkpoints, exact receipt ordering, allocation faults and family isolation | Storage-owner component; complete migration records, incident recovery and measured ESP/catch-up qualification remain open |
| Legacy read and retirement failures can be missing from shared health | Fixed per-queue read/append/retirement incidents, captured I/O errors, active versus recovered counts in ADD, and a HIL precondition | Actual queue adapters, shared health/probe/recovery functions, short reads and failed close/NVS/reclamation, partial retry, unrelated progress, ingestion and UI tests | Read faults recover only after a complete affected-queue retry; write/retirement proof and durable incident history remain open |
| Valid retained delivery can retire the original queue bytes without ADD custody | Bridge/writer ADD and Oracle drains require an exact raw-evidence receipt after the existing delivery or quarantine obligation, before checkpoint retirement | Actual ADD worker and Oracle consumers, real files/checkpoints, lost ADD replies, restart, generation failure, allocation faults and family isolation | Retained delivery component; a complete migration inventory, receipt-linked checkpoint certificate and ADD interpretation of retained blocked records remain open |
| Snapshot recovery can rewrite a retained identity-blocked original before custody | Bridge/writer snapshot callers defer to the existing raw-evidence delivery worker; only a committed ADD custody acknowledgement permits source retirement | Actual snapshot and delivery functions, a matching roster, terminal-session isolation, lost acknowledgements, failed checkpoints, stable replay identity, generation failure and legacy-family regression | Original-byte handoff component; ADD identity resolution and Oracle delivery remain separate, incomplete obligations |
| Maximum storage-task duration cannot establish capture p99 | Fixed-size capture-session histograms measure complete packet preservation and individual fragment submission-to-commit; ADD retains their counts and shows conservative p99 bounds separately from failures | Actual file-backed capture and queue waits, clock wrap, partial packets, counter exhaustion, allocation failures, heartbeat storage and stale/invalid UI evidence | Measurement component; per-occurrence latency, ESP performance, qualified windows and release acceptance remain open |
| Development journal and OTA scan use an unmounted directory | Journal runtime and OTA evidence scans share the actual `/storage` boot mount constant | Production boot initialization and runtime adapter compare journal/OTA/catalog/command paths to the captured VFS mount | Integration defect corrected; signed-device boot and physical qualification remain open |
| Identity lookup can expose a row before a later read or close fails | New-image catalog restoration, lookup and tombstone loading read copied owner chunks; lookup validates complete row counts and clears unresolved outputs | Actual consumer functions, every allocation site, late reads, changed revisions, extra/missing rows, truncated tails and legacy close failure | Implemented read component; ESP catalog latency and legacy custody migration stay open |
| Wall-clock changes or reused IDs can misdirect temporary administrator recovery | Boot-local monotonic expiry, identity-bound encrypted-NVS leases, a retained presence witness, exact uncertain-write replay and explicit evidence holds | Production grant/watchdog adapters and actual owner task with clock, reboot, changed identity, missing records, terminal and NVS fault injection | Implemented software component; physical fault, ESP latency, field recovery and full scheduling qualification remain open |
| New release can bypass legacy storage-contract validation | 2.6.16/2.7.0 registration rejects until reader/rollback validation is implemented | `test_storage_contract.py` | Guard implemented; release intentionally blocked |
| CI compiles a bridge that cannot resume capture after rollback | Compile the development 2.6.16 bridge with journal capture included; runtime authority and persisted reader proof still gate its use | Actual boot/runtime and platform tests cover both build flags, legacy authority before cutover and journal capture after rollback | Build configuration corrected; no signed artifact or field compatibility qualification |
| Nationwide capacity and promotion evidence | Fixed 17-device scope; 75% partition budget, doubled peak and seven-day calculation; wave/location/concurrency and evidence evaluators. Fourteen-day fleet observation begins after the last device qualification, not installation | `test_zkt270_qualification.py` | Partial: offline evaluators only; not an OTA authorization service |
| Concurrent requests can exceed the nationwide upgrade limits | PostgreSQL offer serialization and exact-inventory admission after bridge/writer registration; two reservations and one per physical location, retaining offline/paused/uncertain cancelled offers | Actual assignment transactions, overlapping PostgreSQL sessions, rollback, family and legacy/HIL regressions | Implemented negative admission guard; trusted qualification, wave enforcement and verified stop/recovery remain open |
| Replayed delivery can inflate workload estimates | Read-only 30-day source-ordinal counts, same-second multiplicity, strict current source scope, bounded query deadlines and explicit unknown/uncertified evidence | SQLite/PostgreSQL tests, concurrent change and 200,000-record measurement | Implemented measurement component; profile, clock/window closure and actual zone capacity qualification remain open |
| Backup file existence mistaken for restore proof | Restore pre-deployment dump into an isolated database; verify revision; clean up; retain backup digest and verification time | PowerShell failure/cleanup regressions and actual disposable PostgreSQL restore | Production deployment `37135387664` passed this gate on 3 October 2026 |

## Required release evidence

Capture latency schema 1 uses thirteen inclusive, non-cumulative millisecond
buckets: 0, 1, 5, 10, 25, 50, 100, 250, 500, 1,000, 5,000, 15,000, and
the remaining uint32 range. Packet timing starts at entry to raw preservation
and ends after every fragment commits and temporary buffers are cleared.
Fragment timing includes the owner submission and reply wait. It excludes
terminal socket receipt before preservation, the capture runtime's entry lock,
ADD transport, interpretation and Oracle work. Failed or uncertain operations
do not become successful samples; their existing failure/timeout counters remain
independent. Partial packets can contribute committed fragments but no complete
packet sample. One millisecond is added to elapsed clock ticks to avoid
understating time lost to the millisecond clock's truncation. Counters freeze
visibly on exhaustion, and old capture snapshots
do not publish a fresh histogram. Boot identity plus capture restart count binds
each series; collectors must not join series or average percentiles. ADD's p99
is a bucket upper bound, limited by the actual measured maximum. It is not a
qualified per-attendance latency or a substitute for a timed workload test.

The [source-load diagnostic](zkt-source-load-baseline.md) measures observed
calendar-day and minute counts without using delivery attempts. It cannot turn
missing source history or an unqualified clock/profile into a complete baseline.
Its measurement remains separate from the seven-day capacity gate.

ADD backup restore, additive migration and rollback-reader verification precede
new writers. Candidate signing, a seven-day automated soak, each field device's
ordinary punch traces over two working days, wave observation and the final
fourteen-day fleet observation remain separate gates. No historical alert,
component test, short smoke run or healthy network connection can substitute for
these gates. Blocked devices remain in the denominator.

Historical 8-byte records contain an attendance UID without an independently
supported user reference. They remain raw source evidence with an identity hold;
the current roster is not proof of historical ownership. The same hold applies
to a 40-byte record with an empty or space-only user field. Valid 16/40-byte
references keep their source identifiers and existing event UID construction,
with the attendance UID retained separately for audit. Current snapshot evidence
still requires ADD's historical continuity checks. Previously accepted records,
raw bytes and Oracle keys are not rewritten by this guard. Corrections require
separate derived evidence; a source review note alone cannot clear this hold.
Source rows and their canonical chain inputs are constructed together: an
allocation failure returns failure with neither row appended. Nine pinned
cJSON harnesses cover the source encoder, source wire contract, OTA progress
receipts and existing serializers.

ADD also checks older firmware's incoming source claims before ingesting or
recovering attendance. An 8-byte UID-only record, or a 40-byte record whose
user field is empty, cannot acquire an employee assignment from its submitted
nested event. Migration `0045` retains that original interpretation encrypted,
with its declared disposition and the guard version, separately from the raw
bytes and derived `IDENTITY_UNRESOLVED` custody state. Both baseline and tail
transactions continue their source chains while preserving this hold. Other
valid rows in the batch can proceed; replay cannot release an existing event
through a held claim. Existing accepted manifests and Oracle keys are unchanged.
Recovery epochs and additive database rollback retain the encrypted claim.
The audited source reveal shows the submitted interpretation separately from
the custody result. This is a negative identity check, not profile qualification.
Clock correction cannot use an 8-byte historical UID as employee identity.

ZKT commands, prepared-buffer transfers and ordinary live frames each retain
their existing 90-second allowance as an absolute monotonic deadline. Short
reads, short writes, interrupted waits and interleaved live events cannot reset
that allowance. An expired transfer fails and its partial bytes are not parsed
or acknowledged. A fully preserved interleaved packet can remain in custody
even when the subsequent ACK deadline expires. An initial prepare command and
its following data transfer are separate bounded phases; this does not claim
that an entire historical scan fits in 90 seconds. On allocation failure the
caller abandons the session instead of draining an untrusted body. This fixes
an operation-bound defect, not a demonstrated cause of the field's malformed
source records.

## Browser event contract

Wire envelopes map to canonical `device`, `users`, `attendance`, `command` and
`reconciliation` topics in `zk_add/realtime.py`. Other existing named topics are
unchanged. Cursors combine a server-generation UUID and monotonically increasing
sequence. Every connection and subscriber overflow requests `resync`; periodic
named keepalives measure transport liveness only. Clients also fetch snapshots
every 30 seconds and on focus/visibility recovery. Those checks also replace
permanently closed browser streams after at least 30 seconds since the last
attempt, or open/connecting streams after at least 60 seconds without activity.
Replacement attempts stay at least 30 seconds apart, do not
refresh the last-activity evidence, and fence callbacks from older connections.
Polling continues if EventSource is unavailable or cannot be constructed.
`snapshot_at` orders shared
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

A bounded [occurrence match planner](zkt-occurrence-matching.md) now compares
complete decoded fact sets without selecting arbitrary same-second pairs.
Its 2,048-record windows preserve unbound occurrences, immutable observation
identities and prior one-to-one links. The planner requires independently proved
profile/coverage inputs and returns proposals only; database integration,
qualified profile evidence and canonical attendance creation remain open.
All 101 targeted matcher/decoder tests pass, including saturated ambiguity
buckets and seeded mixed populations. No model is qualified by these fixtures.

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
creates no attendance or Oracle rows. A separate, single-thread inspector now
runs outside general maintenance and Oracle dispatch. Its maximum 100-group
batch has a 250 ms budget checked between groups, fair connector rotation and
short PostgreSQL statement/lock deadlines (2 seconds / 250 ms). Work already in
progress finishes before its thread can be replaced, including during repeated
cancellation. Constructor failure and an exited worker retry with bounded
backoff; attempts and actual thread starts are separate counters.

Runtime evidence has a distinct process-instance UUID, sampling time, active
operation age, committed inspection totals, lock-wait state, failure categories
and last useful progress. A pending transaction or stuck dispatcher is visible
as stalled, while an idle loop cannot imply record or Oracle completion.
Round-robin state advances only after commit. Failed ticks roll back and retain
their work obligations; blocked connectors are skipped without holding up other
sites. These budgets and tests do not yet qualify the proposed load/latency
envelope or the profile-dependent attendance interpretation path.

Authenticated `GET /api/v1/devices/{connector_id}/zkt-custody` exposes paginated
work states and missing-work detection, never raw bytes or employee identity.
Receipts predating the work contract have a bounded idempotent backfill helper;
an orphan receipt blocks writer activation until repaired. This is a backend
status interface. The ZKT device overview also presents these work states,
responsible teams and recent reasons without raw bytes or employee identity.
Its 30-second refresh, request deadline and per-snapshot evidence age are
independent of stream liveness. Unknown/failed/old/wrong-device responses do
not become zero counts, and a source association never becomes an Oracle pass.

The ADD fact decoder supports explicit 8/16/40-byte historical and
12/32/36/52-byte live layouts. It checks record boundaries and calendar dates,
preserves the original encoded clock, and converts Pakistan local time to UTC.
The historical attendance UID never becomes a current enrollment identity.
Identical same-second records remain separate facts. A packet with multiple
valid allowed interpretations remains ambiguous. Model labels only select
profiles; they do not grant qualification. This module is not yet connected to
attendance creation, and it does not authenticate the packet checksum.

For model diagnosis, authenticated
`GET /api/v1/devices/{connector_id}/source-evidence` lists at most 50 retained
records per page without raw bytes or employee identity. A record's `reveal`
POST requires CSRF, password confirmation and an audit commit before returning
its bounded protected source bytes. It checks both ownership keys, stored
length and digest. Valid records and exceptions are available without changing
original dispositions or delivery state. Current model metadata is labelled
separately from the unrecorded capture-time model; any associated attendance is
labelled as prior interpretation, not independent ground truth. Protected
samples must remain outside this public repository.

Source records that arrive before their canonical manifest retain a bounded
30-second association retry. Exact source bytes, terminal confirmation, epoch
and ordinal must agree before an alias is created. A savepoint isolates a
derived source conflict so other items can still commit custody. The original
receipt stays immutable; a changed terminal binding or conflicting source
gets a visible hold. Missing source references remain explicit holds.
`SOURCE_ASSOCIATED` does not create attendance, resolve identity or claim Oracle
completion. Revision-driven source wakeups and throughput qualification remain
future work; the inspector's runtime cadence is not a latency guarantee.

The source inspector also checks the retained attendance link. Distinct
canonical ordinals in the same epoch cannot use one legacy attendance row as
proof of independent delivery, even if that row already has an Oracle receipt.
Both custody receipts and occurrence aliases remain intact; the processing
obligation becomes `HELD_OCCURRENCE` with a reconciliation owner. A changed
alias-to-manifest link or unverified attendance ownership has its own reason.
Later valid observations continue. The guard never assigns an employee, creates
a replacement attendance UID, changes an Oracle key or resends an old outbox.
Recovery copies in another epoch and noncanonical evidence do not alone trigger
this same-epoch collision check. It is a negative check at inspection time,
not a positive delivery certificate or completed historical ambiguity audit.

Journal retirement corruption has an automatic replay path. The owner first
preserves the exact damaged checkpoint in a synchronized opaque segment, then
commits a cursor that replays all retained segments under unchanged identities.
The original checkpoint evidence needs its own ADD receipt before reclamation.
Unavailable reads, damaged root keys/counters and exhausted recovery capacity
still block recovery. Encryption identity and nonce allocation never reset.

## Verification recorded during implementation

- Absolute socket deadlines: 13 targeted transport/socket/contract tests pass
  under native Clang and Linux GCC ASan/UBSan. Actual Unix and loopback TCP
  sockets cover delayed partial reads and full send buffers. The Linux run
  leaves sockets blocking and verifies per-call nonblocking flags; Darwin's
  fixture uses nonblocking sockets because its send-buffer wait does not honor
  `MSG_DONTWAIT`. Injected libc ports are installed after fortified declarations
  so faults cannot accidentally call a real socket. All 190 firmware regressions
  and both unsigned ESP-IDF family builds pass.
- Legacy occurrence links: 142 custody/runtime/occurrence and reconciliation regressions
  passed, including retained Oracle acknowledgements on a shared legacy UID,
  distinct attendance rows with equal same-second facts, replay, recovery
  copies and changed source ownership. All 18 custody-panel tests, the production
  frontend build and bundle budget passed.
- OTA reader interlock: native Clang and Linux GCC sanitizers cover proof
  corruption, actual slot/image/security checks, stale delivery evidence,
  refused/late owner checks and repeated failed abandonment. The actual
  installer refuses fresh and resumed downloads before opening transport when
  the bridge would be overwritten. Orphaned legacy state and failed/bounded
  directory reads are held. Both unsigned ESP-IDF family builds pass.
- Journal startup: native Clang and Linux GCC sanitizer tests cover bootstrap,
  proof deadlines, retries across clock wrap, checkpoint recovery, binding holds,
  stale/stalled evidence and all encrypted-NVS/writer-build combinations of the
  actual ESP adapter. The firmware run passed 186 regressions; after updating
  its startup-loop stub, all nine final startup/OTA/runtime tests passed.
  The expanded diagnostics serializer passed every pinned-cJSON allocation
  failure. Unsigned ZKT gated-writer and Hikvision ESP-IDF builds passed.
  Backend/schema/HIL tests: 258 passed. UI: 15 preservation-health tests and
  all eight browser/viewport cases passed; TypeScript, production build and
  bundle budgets passed. Desktop and narrow mobile screenshots were inspected.

- ADD historical-claim guard: 175 focused reconciliation, identity, evidence and
  repair tests passed, followed by all 13 dedicated guard tests including source
  epoch recovery. A real PostgreSQL migration test verifies old rows are not
  reclassified and new claim evidence survives additive rollback/re-upgrade.
  A separate actual database dump/restore preserved encrypted raw bytes and
  submitted claims; Alembic schema drift checks passed. The full unit run passed
  1,335 tests with 31 environment-dependent skips; the PostgreSQL migration test
  was also run explicitly against PostgreSQL and passed.

- Historical identity guard: 181 targeted firmware/decoder/reconciliation tests
  passed, then 61 reconciliation tests passed after bounding the unfinished-scan
  identity query. Full local regression: 1,500 passed, 29 environment-dependent
  skips; a separate real PostgreSQL run passed all 26 concurrency/large-backlog
  tests. Frontend: 153 tests passed, with the final eight reconciliation tests
  rerun after badge wording changed. Existing browser cases: 82 passed with 14
  viewport skips; the new identity hold case passed on all eight browser/viewport
  targets. Both unsigned ESP-IDF family builds and all seven cJSON harnesses passed.

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

- The subsequent full local run found a timeout in the existing 100,000-row
  manual-preview scan (1,449 other tests passed). Shared reads were repeated
  for every punch. After restricting reuse to each preview transaction, all
  81 focused policy tests and 19 PostgreSQL tests passed. The unchanged large
  test completed in 55.33 seconds, including responsive login and priority for
  live delivery. Execution still revalidates without the preview cache.

- Full regression after custody work, ADD decoding and preview performance
  changes (`db1f09d`): 1,509 passed in 292.19 seconds. Both 100,000-row
  PostgreSQL tests passed with their original deadlines.
- Audited source-evidence access: 68 focused tests passed, including session,
  CSRF, password, scope, audit, ciphertext/digest integrity and unchanged
  original dispositions. The protected endpoint is not a qualification grant.

- Late source association: 39 focused custody/packet tests and three PostgreSQL
  custody/concurrency/savepoint tests passed. Distinct same-byte ordinals retain
  separate aliases, replay keeps its receipt, changed confirmation cannot bind,
  and a conflicting item cannot prevent the next receipt from committing.
- Checkpoint recovery: 223 firmware/custody regressions passed, with retirement
  byte corruption and interrupted archive/reset operations under native and
  Linux ASan/UBSan. Four owner/admission tests include automatic recovery after
  a capacity refusal. Both family builds and ten independent wire/receipt
  vectors passed. These remain software tests, not physical power-cut proof.
- Independent custody runtime: 1,323 unit tests passed (30 environment-specific
  skips), plus four PostgreSQL custody tests covering row-lock isolation,
  enforced statement timeout, rollback and transaction-local settings. Rotation
  tests cover saturated fleets of 16 and 17 connectors and query-time deadline
  exhaustion. Cancellation and startup-failure tests distinguish attempts from
  actual starts and prohibit overlapping transaction owners. All 17 custody
  panel tests and eight primary-route browser cases passed; the production
  frontend build and asset budget checks also passed.
- Reader compatibility: native Clang and Linux GCC sanitizer harnesses cover
  every proof-byte corruption and torn prefix, uncertain commit/readback,
  generation exhaustion, partition/security/image changes and writer refusal.
  An independent Python fixture fixes the byte contract between artifacts.
  The owner harness verifies stale transport cannot open the writer and reads/
  settlement still work while writing is blocked. Both ESP-IDF family builds
  pass; these development builds remain unsigned and are not release artifacts.

## Work that still blocks the requested release

Derived interpretation now retains encrypted evidence in bounded steps, with
source offsets, alternate layouts, error categories and links to prior decoder
versions. The targeted regression set passed 140 tests in total, including the
separately rerun 200,000-row PostgreSQL scheduling fixture; three cases are
inapplicable to SQLite. A fresh database migration, actual PostgreSQL dump/restore,
additive downgrade/re-upgrade and schema check retained original and derived
ciphertext. The custody UI passed 28 interaction tests and all eight browser and
viewport cases; production build and bundle budgets passed. These results do
not qualify source interpretation or authorize historical attendance repair.

OTA installation checks the exact downloaded length and expected ESP application
digest before `esp_https_ota_finish` can select the new boot slot. Aligned complete
writes leave no encrypted-flash tail for finalization; IDF still verifies the
signature before selection. Resume checkpoints are whole erase sectors, with
old partial checkpoints rewound and EOF checkpoints re-reading the last sector.
Fault tests exercise both firmware families, all 4,095 offsets inside a sector,
short/long downloads, failed hashing, signature failure and NVS checkpoints.
The pre-erase journal interlock now permits only the attested bridge-to-writer
install edge, rechecking actual reader identity through the storage owner.
Running 2.7.0 cannot overwrite its rollback bridge. This also prevents using
ordinary reinstallation as a rollback operation; selecting an already-verified
reader without erasing it still needs an explicit operational path. Legacy
firmware refuses direct 2.7.0 installation or retained journal evidence.
Physical power-loss qualification remains a separate release requirement.

1. Qualify and activate durable raw capture before acknowledging live events.
   The ordinary, command-response and prepared-read hooks are implemented under
   a disabled writer build switch. Partial packet fragments remain holds.
2. Qualify the implemented journal startup/compatible-reader gate and live
   capture and the implemented storage-owner handoff on actual ESP devices. Qualify actual ESP latency,
   resource headroom, checkpoint recovery on actual ESP hardware and remaining runtime tasks.
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

The exact bridge/writer's retained ADD and Oracle drains now preserve original
queue bytes in ADD before retiring a successfully delivered row. Queue names,
generation and extent identities use the existing evidence protocol; a lost
reply or failed local checkpoint replays the same object. Accepted attendance
batches and verified Oracle responses remain separate obligations, so raw custody
alone cannot claim delivery or resolve identity. No filesystem or terminal lock
crosses the additional custody exchange. Older ZKT and Hikvision images retain
their existing behavior. This adds one custody exchange for successfully delivered
retained rows and needs catch-up qualification. It does not reconstruct already
retired predecessor rows or certify complete migration. These evidence records retain the existing
`PRESERVED_UNRESOLVED` disposition; downstream status is checked independently.

Bridge/writer snapshot processing leaves retained identity-blocked originals
untouched, even when the current roster matches. The existing delivery worker
transfers those exact bytes to ADD before committing their retirement. A lost
acknowledgement or failed retirement checkpoint retries the same evidence identity.
The terminal-session caller performs no additional storage or network work and
reports zero local identity repairs. ADD identity resolution and downstream
delivery for these preserved blocked records still require implementation and
qualification; this change does not turn raw custody into an identity decision.

ADD-owned Oracle delivery now has a separate inactive component described in
[`zkt-oracle-delivery.md`](zkt-oracle-delivery.md). It freezes encrypted payloads,
retains the route across feature rollback, fences stale claims and commits a
scoped Oracle content receipt atomically with completion. New intents require
contract 2's stored raw projection and separate daily punch-time proof
(`ORACLE_RAW_DAY_TIMES_V2`). The independent read-only Oracle package is supplied
for review and synthetic CI; it is not installed by ADD deployment. Production
19c, ORDS, downstream business-policy and live-delivery qualification remain open.
No existing record is registered or rerouted by this component. Qualified
canonical occurrence creation and backend rollback compatibility are required
before activation.

The journal byte format and failure behavior are documented in
[`zkt-journal-v1.md`](zkt-journal-v1.md). Journal code compiles into the ZKT family
only. The app task now calls a gated startup controller for exact 2.6.16/2.7.0
identities. Current development images retain their old versions, so this
does not activate journal workers or change the published firmware version.

The owner copies requests into eight bounded slots, reserves three slots for
capture/retirement, and limits priority bursts so delivery reads can progress.
Its mailbox mutex never spans filesystem/NVS work. A timed-out caller cannot
free an in-flight request; abandoning a reply does not cancel accepted capture.
Only a completed durable write returns a capture sequence. The task reports
operation start, progress, occupancy, saturation and separate NVS/filesystem
errors. It never deletes or restarts another task that might own a lock.

Exact 2.6.16/2.7.0 command inboxes use this owner for encrypted line reads,
replacement and the existing `file_tx/commands` checkpoint. Requests copy at
most 512 bytes; transaction hashes advance in bounded steps between live
writes. The 64 KiB inbox limit remains enforced. An incomplete producer never
becomes a recovery intent, and an orphan legacy filtered `.tmp` cannot be
promoted without a committed transaction. A timed-out activation retains its
ticket and invalidates the restored-inbox snapshot when collected. Replay
checks preserve command IDs; queue refusal remains retryable. ADD remains the
durable command authority. These changes do not migrate legacy attendance or
qualify ESP timing or flash.

The same owner handles `processed_commands.txt` and `add_cancelled.txt` for
exact bridge/writer images. Each scan reads at most 4 KiB and closes its handle
before yielding. The two 64 KiB caches retain every existing ID; saturation
causes an explicit refusal, never automatic eviction. An append is acknowledged
only after write, flush, sync and close. A replay of a retained ID syncs it again
before claiming durable completion, covering a previous lost/failed sync result.
Partial tails and read failures hold execution. One retained client ticket per
cache survives caller timeout; collecting it never answers a request for another
ID. Existing legacy/Hikvision paths and cache formats remain unchanged.

New-image catalog restoration, identity lookup and tombstone loading also read
512-byte copied chunks from the owner. No caller retains a catalog file handle
while waiting; revision checks and one 30-second stream deadline reject mixed
or stalled reads. Missing storage is distinguished from a verified missing
catalog. Both new and legacy readers reject incomplete lines and incomplete
row counts; identity results are cleared if any later row, read or close fails.
The valid RAM catalog remains authoritative, including when a requested alias
is absent. On a disk fallback, full validation may cost more than an early
matching-row return; actual ESP latency and memory headroom require qualification.

Journal appends additionally require an owner-executed reader compatibility
check. The new encrypted-NVS capability binds the exact validated 2.6.16 bridge
image, OTA slot, terminal, key epoch and partition layout. The ESP adapter reads
those image/security facts locally and refuses a factory or unconfirmed
rollback image. Proof writes commit and read back; damaged/missing evidence
does not open the writer. The bridge can attest reading capability without
gaining writer permission. Recovery invalidates cached permission. The startup
controller requires secure boot, encrypted NVS, a provisioned terminal binding,
verified local storage and an OTA slot. It starts receipt delivery during
checkpoint recovery, then checks compatibility before permitting capture.
Successful starts are counted separately from attempts; stale or stalled
workers are held without task deletion. The pre-erase install interlock preserves
the certified bridge. These components do not qualify an artifact, complete
legacy migration or provide the operational compatible-rollback selection path.

Both development images include journal capture code. The 2.6.16 bridge initially
keeps the persisted legacy authority; including that code does not authorize a
writer cutover. After a verified cutover and compatible rollback, the bridge
must resume preservation under the persisted ADD authority. A bridge compiled
with `ZONE_LITE_JOURNAL_WRITES=OFF` intentionally fails boot and reader-proof
checks, so it cannot satisfy this role. Earlier successful compilation and image
descriptor checks for that disabled configuration were not compatibility
qualification. CI now builds the capture-capable bridge, while the host matrices
continue to test refusal of capture-disabled builds. Signing, security and
persisted-proof requirements remain unchanged.

The storage task also owns the three retained corrupt-file generations for
exact bridge/writer images. Their copied queue domain permits reads and
custody-backed retirement only. The delivery task obtains an exact ADD
`queue_evidence` receipt before sending a retirement token; it releases all
filesystem locks before the network call. Existing file names, checkpoint
keys and raw byte order are preserved. A consumed prefix is checked in at most
8 KiB per request, so another lane can progress during recovery. Verified empty
files are cached because these retained generations have no producer. Errors
and uncertain results preserve the source or replay already receipted evidence.
This transfers unresolved evidence; it does not classify it as valid attendance,
resolve identity, or establish Oracle delivery. Complete legacy migration
remains open.

Oracle and identity-blocked flat files now use the same copied owner transport
in exact bridge/writer images. A dedicated adapter retains the original paths,
`legacy_queues` keys and checkpoint ABI, streams surviving active/backup/temp
generations separately, and retries failed restoration before trusting empty.
Boot defers these reads and does not scan entire files to warm a volatile UID
cache; immutable legacy event IDs remain in every retained row and replay.
Runtime backlog checks read conservative RAM evidence instead of scanning a
file. Appends invalidate that evidence before submitting work, including when
admission or the reply is uncertain.

The new-image legacy Oracle reader processes one copied row per operation. It
retires only after Oracle acknowledgement plus its preserved ADD receipt, an
exact raw-evidence receipt, or durable transfer to the blocked queue. The worker
uses a 100 ms interval only after a successful retirement; outages and refusals
retain the existing two-second retry cadence. This bounds work between owner
turns but does not establish field throughput or catch-up qualification. New
journal observations still use ADD authority; the Oracle reader is for retained
legacy rows. Blocked identity recovery keeps its existing exact terminal and
identity-fingerprint checks, preserves the original event UID, and cannot retire
before the destination append succeeds. Historical, changed, rejected and
unresolved identities remain held or transferred as raw evidence. These
components do not complete per-item migration evidence or authorize a release.

The seven retained flat-file queues report separate read, append and retirement
incidents while the storage owner holds its local lock. A successful complete
peek, including required generation restoration and checkpoint validation,
resolves only that queue's read incident. Partial prefix verification,
contention, capacity refusal and another queue's progress cannot resolve it.
The full segmented recovery and persistence checks must still run again before
the device claims verified storage. Failed reads during retirement are reported
as I/O failures; they no longer masquerade as a stale receipt token. The first
captured read/seek error survives cleanup errors.

Active read, append and retirement counts are distinct from read recoveries
since boot. ADD retains these optional fields, displays the affected queue and
operation, and prevents an active incident from producing a healthy UI or HIL
verdict. An optional catalog refusal cannot replace an existing error's
operation label. Legacy firmware and Hikvision do not enter the new reporting
path. This incident inventory is bounded and boot-local. Append and retirement
incidents deliberately remain latched: a later successful operation is not
proof of the earlier record's custody. Per-item recovery evidence, persisted
incident history and release qualification are still required.

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
passed. New capture remains disabled until compatible-reader proof exists.

The CI failure caused by the transport harness's ambiguous C indentation was
fixed without disabling compiler warnings or tests. PR #260 and its main-branch
run passed all six CI jobs. Production deployment `37135387664` passed the
backup restore gate, migration and origin/public health checks for `bd395cc`.
An authenticated post-deployment fleet query confirmed snapshot identities.
Journal development and firmware qualification remain separate.

Identity-bound administrator lease persistence and its recovery limits are described in [`zkt-lease-recovery.md`](zkt-lease-recovery.md). Active legacy leases require independent resolution before a bridge can be qualified.
