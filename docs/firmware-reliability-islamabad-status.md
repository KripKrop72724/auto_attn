# Firmware reliability implementation status

Work in progress. This document is not release approval or HIL acceptance.

Base: `55f049ab7292b80a771a37a882a2d042b1664021`.
Branch: `codex/firmware-reliability-islamabad-hil`.

## Current implementation

| Finding | Implemented locally | Remaining release requirements |
|---|---|---|
| 3 | Allocation-free queue scan with unknown/error state; verified EOF retirement | Fault matrix and integration with committed segmented checkpoints |
| 4 | Allocation-free JSON syntax check; retry parse/serialization allocation failures; preserve failed quarantine | Complete remaining injected failures and legacy evidence-file draining |
| 5 | Bounded live parser, sanitizer coverage including 13-byte frame | Established protocol hint for concatenated frames and source recovery integration |
| 6 | Explicit resource error; durable-outcome success allowlist; complete-UID dedup with bounded probes; serializers reject incomplete JSON | Complete end-to-end allocation matrix and reconciliation assertions |
| 7 | Portable segmented queue; tested 60/55 pressure hysteresis, reserved admission and retirement cleanup | Runtime integration, bounded ACK cache, auxiliary-file budgets, blocked identity-resolution qualification |
| 8 | 64 KiB segments; legacy ADD and ORDS stream from checked persistent cursors without a second backlog copy | Drain old evidence files and qualify all interruption boundaries |
| 9 | Legacy ORDS uses bounded read/send/commit; at most 100 events or one ORDS request per slice | Sustained-load qualification and worker diagnostics |
| 10 | Checked task startup, buffer retry, supervisor and visible faults | Exhaustive worker-failure and liveness qualification |
| 11 | ADD/ORDS active, backup and temp files drain independently; preserve ambiguous blocked backup generations | Complete all interruption boundaries and malformed-tail recovery |
| 12 | Require terminal serial and identity fingerprint for local historical repair | Terminal replacement/reused-ID integration tests and verified resolution path |
| 13 | Coherent versioned/checksummed runtime NVS blob, checked legacy queue commits and batch flush/sync/close | Complete remaining call-site audit; segmented I/O and actual runtime NVS failure matrix exercised |
| 14 | Shared tested 4:1 scheduler, alternating bulk/proof service and per-lane backoff; ADD segmented compatibility reader | Activate separate receipt writes; prove direct-send and sustained-load fairness |
| 15 | Versioned optional diagnostics across firmware/backend/UI, stale/missing labels, durable fault recovery gating and completed-with-exclusions wording | Complete persistence/queue/reconciliation instrumentation and browser qualification |
| 16 | No production change | Version reservation, compatibility image, upgrade/rollback proofs and exact-target OTA |

The ADD worker can read segmented attendance/receipt queues for compatibility,
but **new segmented writes remain disabled**. The ORDS segmented compatibility
reader is also connected and tested across receipt failure/restart. Legacy and segmented
blocked readers transfer unresolved evidence with verified custody receipts. Complete
legacy migration and predecessor-image verification remain outstanding.
The ADD worker also reads the evidence lane with an exact custody receipt check.
Existing firmware version remains unchanged. Local build uses the CI setup password
and is unsigned; it is not a production artifact.

## Local verification

- ESP-IDF 5.5.3 Docker build passes; binary 0x120000 bytes, application partition
  0x280000 bytes, 55% free. Bootloader and partition definitions unchanged.
- Portable queue/parser C regression tests run under AddressSanitizer and
  UndefinedBehaviorSanitizer. Actual segmented queue I/O is faulted at open,
  seek, read, partial write, flush, sync, close, checkpoint and deletion calls;
  restart preserves previous durable records, including uncertain commits.
- Legacy appends reject an unfinished trailing row. Startup dedup ignores
  incomplete/oversized/malformed rows and compares full UIDs; a full cache
  cannot manufacture a duplicate.
- The capacity adapter reserves space inside a 75% absolute ceiling, stops
  historical admission at 60%, resumes below 55%, and admits only reserved
  operations at 70%. Tests cover those thresholds and nearly full storage.
  Integration across legacy/auxiliary files and verified recovery is unfinished.
- Attendance serializers tested against the pinned ESP-IDF cJSON implementation,
  failing each allocation in turn; incomplete records are rejected. Included in CI.
- All 436 backend/unit, firmware and companion tests pass locally. Actual legacy
  drain orchestration is compiled into a host test covering allocation failure,
  receipt failure, checkpoint failure, restart, concurrent live append during
  network delivery, and bulk failure followed by single-record recovery.
  This does not substitute for exact-commit GitHub CI.
- Draft PR #161 at initial commit `83df4fd36575cbb8ae8ebc15840f41a3d7cdaaa2`
  passed every GitHub check, including containers. Subsequent changes require
  another exact-commit run.
- The diagnostics migration was tested against isolated PostgreSQL 16, including
  downgrade/upgrade and schema-drift checks. The frontend passed 94 tests, its
  production build and bundle budget after diagnostics changes. Ordered HIL UI
  changes passed 98 frontend tests and another production build.

## Durable queue evidence and runtime checkpoints

ADD stores original queue bytes and provenance encrypted, with a unique custody
identity and digest. Retries return the same receipt; changed bytes/provenance or
wrong ownership are rejected. Acknowledgements occur after commit. Custody is
explicitly `PRESERVED_UNRESOLVED`, not an attendance identity resolution. Admin
listing omits raw data; reveal requires CSRF, step-up and an audit entry.

The firmware verifies exact evidence receipt identity before retiring malformed
ADD/ORDS rows. Embedded nulls cannot hide trailing bytes. Queue generations use
a checked persistent storage instance. Evidence serialization is tested against
actual cJSON with every allocation failed in turn. PostgreSQL migration upgrade,
downgrade and schema checks pass. Legacy blocked files now use bounded persistent reads, with independently preserved
backup/temp generations. A sanitizer test drains 10,003 records across restart, failed
checkpoint and concurrent append without copying the backlog; truncated tails remain
preserved as faults. Old quarantine files still need bounded draining integration.

Runtime NVS failures restore the last committed source cursor in memory, invalidate
completion telemetry and require recovery. The actual writer is host-tested with
open/set/commit failures, including an uncertain commit, invalid state and generation
exhaustion. Remaining persistence callers still require audit.

## Ordered HIL rollout controls

Locally implemented: additive ordered exact-target markers, configured allowlist,
connector/MAC/confirmed terminal matching, preview digest binding to target order
and hashes, assignment and download revalidation, paused/revoked grant rejection,
and next-target selection based on same-candidate acceptance evidence. Legacy
Quetta single-MAC scope remains supported. The protected candidate workflow now
requires exact-commit green checks and accepts either target format. Publication
is tested with PowerShell in an isolated fixture, including immutable scope and
legacy compatibility. The fixture is not a real release.

The acceptance-recording endpoint/validator, scoped maintenance commands,
predecessor capability validation and production evidence are still outstanding.
No HIL acceptance evidence has been inserted into production.

## Unperformed qualification and deployment

No PR merge, ADD deployment, protected signing, firmware publication, production
maintenance command or device update has been performed by this implementation.
The 24-hour integration soak, 30-minute 10 events/second load test, browser gates,
upgrade/rollback qualification and both production smoke tests remain outstanding.
Physical power-cut and long-term flash endurance testing are not performed.

Implement ADD support before publishing firmware. Preserve the Quetta diagnostic
release/campaign. Extend ordered HIL scope end-to-end before targeting Islamabad.
Proposed 2.5.4 and 2.6.0 versions must be checked for availability before use.

## Exact production scope

1. ZONE-SLICTOWER-13FL: MAC `e0:72:a1:d6:3c:7c`, terminal `PGB1261200077`,
   connector `4567587c-29ee-4e59-92a4-6c36650a84aa`.
2. Active ZONE-SLICTOWER-3FL: MAC `a4:cb:8f:d4:66:64`, terminal `PGB1261200074`,
   connector `2ca9a4c2-5ae4-4330-8d14-840223672897`.

Install the compatibility reader before activating segmented writes. Verify the
previous OTA slot can recover candidate data. Run each 15-minute observation only
after installation, migration and initial reconciliation finish. Require qualifying
legitimate attendance, a scoped 30-second ADD interruption, a controlled ESP reboot,
fresh telemetry, source continuity and durable delivery evidence. Record INCOMPLETE
if evidence is missing; elapsed time is insufficient. Hold the second target until
the first passes. The final release remains HIL_ONLY; no nationwide promotion.


## OTA completion evidence

Removed the server shortcut that completed an active OTA deployment from the
heartbeat version string alone. Six regression cases cover every active phase;
only the checked device progress path may complete a deployment. This is not HIL
acceptance. Firmware journal/reconciliation recovery and predecessor verification
still require implementation and qualification.


OTA progress and capability builders now reject missing image evidence and failed
JSON fields. The production functions run against ESP-IDF cJSON with every allocation
failed in turn under sanitizers; no incomplete message is sent. Frontend validation
now passes 100 tests, including completed-with-exclusions and explicitly cancelled
wording; build and bundle budgets also pass. ESP-IDF 5.5.3 rebuild passes.


## Shared legacy capacity admission

Legacy pending/blocked capture and ADD outboxes now share the same measured
filesystem budget as segmented writes. Oracle receipt writes use the recovery
reserve. Historical materialization stops making local writes after a failed
admission, while the complete source scan remains available for acknowledged
server recovery. Legacy capture commits each record and releases the capture
lock between records rather than holding it for a full historical scan.

Actual admission/append code is tested under allocation-free capacity measurement,
60/55 hysteresis, reserved-space exhaustion, and open/write/flush/sync/close faults.
Every admitted local operation releases the budget lock on failure. ADD acceptance
no longer clears a local storage fault. Outbox envelopes reject every failed cJSON
field insertion; sanitizer tests use the ESP-IDF cJSON implementation.

436 backend/unit, firmware and companion tests and the ESP-IDF build pass locally.
Auxiliary catalog/command writers, verified recovery gating, and activation of
segmented producers still require completion. This checkpoint is not release approval.


## OTA journal and installation ordering

OTA now stores a versioned, checksummed NVS checkpoint and reads validated legacy
journals only when the new checkpoint is absent. Failed/uncertain writes stop the
operation, restore the last committed in-memory state, and require checked reload.
The READY_TO_BOOT checkpoint commits before OTA finish can select a new slot;
invalid image digests restore the running slot. Interrupted downloads abort their
transport handle. Same-version assignment alone cannot manufacture completion.

OTA success waits for certified source coverage instead of a fixed delay and retries
lost server transition acknowledgements without clearing the journal. Actual C host
tests inject NVS, transport, descriptor, image verification, boot-selection and
completion failures. These tests do not certify a physical upgrade or rollback.
The predecessor compatibility guard is still required before segmented activation.


## Compatibility guard, worker recovery, and fragmented legacy recovery

The candidate writer now requires a checksummed compatibility capability and the
matching secure-boot predecessor image in the other OTA slot. The compatibility
mode continues legacy writes and reads every segmented lane. Host tests run the
actual ESP adapter with NVS, wrong-version, wrong-slot and digest failures. Both
compile modes have built with ESP-IDF 5.5.3, but no physical upgrade has occurred.
The signing script now verifies a compiled storage-contract marker before key access;
the signed manifest binds read/write formats and the compatibility version. ADD
checks fresh healthy compatibility telemetry and matching successful boot evidence
at preview, assignment and download. Production execution remains unperformed.

Delivery task startup uses retained handles and three attempts per rolling ten
minutes. A healthy capture task survives a delivery-task startup failure. The
acknowledgement lock gives a waiting background worker a turn before direct
senders can reacquire it; the actual wrapper has a 100,000-attempt host regression.

Legacy partial/oversized rows now transfer bounded raw fragments into durable ADD
custody. Versioned checkpoints distinguish continuation bytes from attendance;
ordinary delivery cannot settle fragments. Interrupted custody/checkpoint writes
replay exact bytes. Tests include oversized rows, reboot between fragments, valid
looking suffixes, failed custody and concurrent append. Transport envelope fields
also reject every cJSON allocation failure in the ESP-IDF sanitizer matrix.

Storage LED faults remain latched across timer expiry and unrelated network status.
Verified storage recovery and comprehensive telemetry are still unfinished; this
change does not assert recovered health or authorize publication.

ADD inspection on 2026-09-16 found production 2.5.2 and the existing Quetta-only HIL
2.5.3. Islamabad firmware has not been published or installed. No soak or production
acceptance result is claimed by this checkpoint.


The recovery/worker checkpoint passed 444 local backend, firmware and companion
tests plus ESP-IDF 5.5.3. Seven additional signed-contract/predecessor tests reject
missing acceptance, wrong hashes/partitions, stale telemetry and revoked predecessors.
The publication fixture verifies legacy signing compatibility and rejects missing,
ambiguous or wrong-mode compiled storage markers. These fixture packages are not
production artifacts. The final exact-commit CI and release qualification remain
required.

The expanded checkpoint passes 451 tests locally and the firmware build. Queue
initialization now retries missing local resources without requiring a reboot;
segmented admission revalidates the durable storage instance before creating data.


## Command replacement and auxiliary capacity

Command inbox replacements now use checked NVS generations and keep the old file
until the replacement is committed. Recovery tests cover each checkpoint boundary,
uncertain commits, rename/delete failures, shorter/empty replacements, and ambiguous
legacy generations. Inbox scans distinguish errors from absence. Failed allocation,
decryption, reads, writes or close cannot silently remove a row. Queue-full or
allocation-limited boot replay remains retryable, without scheduling duplicates.
The active command inbox is limited to 64 KiB and shares recovery admission; larger
legacy command journals remain preserved and need a separate bounded migration.

Catalog streams share historical admission, are capped at 2 MiB, and sync each bounded
record. Metadata insertions and restore-close results are checked. Real cJSON fault
and measured 60/55/70 capacity tests exercise the production writer. Catalog replacement
generation migration, processed/cancelled-command retention and HIL command deduplication
are still outstanding. These changes do not close all auxiliary-persistence findings.

The command checkpoint passes 452 local tests and the ESP-IDF build. GitHub's earlier
8ce0279 firmware/frontend jobs passed, but the backend job exposed a missing POSIX
feature flag in the storage-upgrade host harness. That test flag is corrected and both
upgrade modes pass inside Linux/ESP-IDF. This is not a green release-qualification claim.

### Historical identity and immutable release follow-up (2026-09-16)

- Historical ingestion now requires an explicit terminal serial, confirmed binding, exact enrollment UID/fingerprint, and a stable snapshot continuity interval covering the event time. Missing/conflicting evidence is durably accepted as `BLOCKED_PROVENANCE`, without a person/CNIC assignment. Automatic current-snapshot/tombstone repair cannot overwrite this state.
- Firmware history parsing preserves event UIDs and source identifiers but cannot copy a current CNIC into direct historical ORDS delivery. Legacy historical ORDS rows and terminal-namespace mismatches transfer raw evidence to ADD as unresolved custody before their source checkpoint advances. ADD may resolve new historical attendance using its stored continuity evidence.
- Legacy quarantine files now drain in bounded fragments, including binary, partial and oversized rows, with exact durable custody before retirement.
- Release-store synchronization rejects changed signed manifests or artifact identities for an existing release ID, including otherwise valid signatures.
- Actual cJSON allocation fault tests now include ORDS normalization and permanent-rejection serialization. Local ESP-IDF 5.5.3 compatibility-mode build passed (unsigned development artifact only). Production signing/publication, HIL run controls, complete recovery diagnostics, and the full qualification campaign remain incomplete; this is not HIL acceptance evidence.

### Catalog transaction and memory follow-up (2026-09-16)

- Catalog activation now uses a canonical commit file and checked NVS transaction generations. Interrupted prepare/commit/retirement operations recover through the shared file transaction component; ambiguous legacy backups remain preserved. A dedicated catalog mutex serializes replacement, tombstone read/modify/write, and lookups without holding a transport or live-storage mutex.
- Pre-transport recovery checks only metadata and idle legacy replacement. Content verification for a prepared generation runs after transport startup. Uncertain canonical commit files are not removed by producer-stage cleanup.
- Tombstone updates no longer replace unreadable, truncated, or allocation-failed catalogs with an empty catalog. Reused user IDs retain separate enrollment UIDs. Every cJSON allocation in the actual update path is tested against ESP-IDF cJSON; failed close also prevents persistence.
- Encrypted storage wrappers check plaintext allocation and SHA derivation failures before decryption. Host tests inject allocator and crypto failures under sanitizers (crypto primitives are test doubles in that test; it is not cryptographic qualification).
- Firmware suite: 140 tests passed. Actual catalog activation restart matrix and actual-cJSON tombstone matrix passed. ESP-IDF 5.5.3 build passed, unsigned 0x130000-byte application with 52% partition headroom. Production deployment/qualification remains outstanding.

A successful flash catalog replacement now invalidates an older volatile alias
catalog. Actual persistence/allocation tests verify that failed replacements keep
the old cache authoritative and successful replacements switch lookups to the new
committed flash generation.

### Cross-release HIL sequencing (2026-09-16)

Compatibility target 2 is now held until target 1 has successful hardening-candidate
acceptance with the same exact ordered scope, source SHA, artifact digest and
application digest, and a successful deployment. Missing, incomplete, failed,
revoked, mismatched-scope and wrong-hash evidence are rejected. This applies through
the shared target selector used by preview, assignment and download authorization.
The 35 HIL-scope/storage-contract tests pass. The acceptance writer and production
validator are still pending; no acceptance is inferred from this unit test.

### Temporary-admin persistence audit (2026-09-16)

The runtime audit found another ignored checkpoint result in temporary-admin
handling. Elevation now follows a tested lease guard: persist the bounded revocation
obligation, elevate and reread, then persist the verified deadline before success.
An uncertain elevation or failed final checkpoint triggers verified revocation;
failed revocation retains the previously durable watchdog obligation. Clearing a
lease restores the RAM obligation if its checkpoint fails. Watchdog revocation
rereads terminal state rather than relying on an older user snapshot. Another UID's
active obligation cannot be overwritten by a new grant, and replay cannot extend
an existing deadline. Actual C state-machine and firmware-adapter host tests cover
failed/uncertain persistence, failed verification/revocation, clock and expiry
boundaries, invalid UIDs, and retained obligations. No production administrator
permission was changed during these tests.

### Allocation-safe queue diagnostics (2026-09-16)

Diagnostics now distinguish capacity admission rejection from actual write failure,
report the measured admission reserve and operation, and expose known/unknown depth
for all six segmented lanes. JSON allocation failure omits the incomplete diagnostic
object and releases every acquired lock. Actual ESP-IDF cJSON allocation injection
passed, the firmware suite passed 142 tests, and the ESP-IDF 5.5.3 unsigned development
build passed. Verified recovery/healthy reporting and release qualification remain
outstanding; no production firmware was published by this checkpoint.

### Light reconciliation commit ordering (2026-09-16)

Light reconciliation now resets its live-event evidence and reports success only
after the runtime checkpoint commits. Actual firmware adapter tests and the actual
NVS runtime writer cover failed open, set, commit, and uncertain commit: the previous
authoritative count is restored, live evidence is retained, and recovery is required.
The targeted sanitizer tests and ESP-IDF 5.5.3 development build pass.
