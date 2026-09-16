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
- All 435 backend/unit, firmware and companion tests pass locally. Actual legacy
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
