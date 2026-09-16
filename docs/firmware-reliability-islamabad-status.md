# Firmware reliability implementation status

Work in progress. This document is not release approval or HIL acceptance.

Base: `55f049ab7292b80a771a37a882a2d042b1664021`.
Branch: `codex/firmware-reliability-islamabad-hil`.

## Current implementation

| Finding | Implemented locally | Remaining release requirements |
|---|---|---|
| 3 | Allocation-free queue scan with unknown/error state; verified EOF retirement | Fault matrix and integration with committed segmented checkpoints |
| 4 | Allocation-free JSON syntax check; retry parse/serialization allocation failures; preserve failed quarantine | Complete injected write/allocation failures and durable evidence transfer |
| 5 | Bounded live parser, sanitizer coverage including 13-byte frame | Established protocol hint for concatenated frames and source recovery integration |
| 6 | Explicit resource error; durable-outcome success allowlist; serializers reject incomplete JSON | Complete end-to-end allocation matrix and reconciliation assertions |
| 7 | Portable segmented queue and capacity adapter | Runtime integration, bounded ACK cache, auxiliary-file budgets, resumable blocked repair |
| 8 | 64 KiB segments; legacy ORDS streams from a checked persistent cursor without a second backlog copy | Integrate remaining queues and qualify all legacy backup combinations |
| 9 | Legacy ORDS uses bounded read/send/commit; at most 100 events or one ORDS request per slice | Sustained-load qualification and worker diagnostics |
| 10 | Checked task startup, buffer retry, supervisor and visible faults | Exhaustive worker-failure and liveness qualification |
| 11 | Preserve ambiguous blocked backup generations | Recover every legacy transition with explicit transaction generations |
| 12 | Require terminal serial and identity fingerprint for local historical repair | Terminal replacement/reused-ID integration tests and verified resolution path |
| 13 | Coherent versioned/checksummed runtime NVS blob, checked legacy queue commits and batch flush/sync/close | Complete persistence fault matrix and audit remaining call sites |
| 14 | Initial 4:1 live/background worker scheduling and capped priority hold | Separate receipts, per-queue retry, sustained-load fairness proof |
| 15 | Completed-with-exclusions wording; persistent local fault on delivery fallback | Optional diagnostics across firmware/backend/UI; operator-state tests |
| 16 | No production change | Version reservation, compatibility image, upgrade/rollback proofs and exact-target OTA |

The new segmented component is compiled but **not yet used by the runtime queues**.
Existing firmware version remains unchanged. Local build uses the CI setup password
and is unsigned; it is not a production artifact.

## Local verification

- ESP-IDF 5.5.3 Docker build passes; binary 0x120000 bytes, application partition
  0x280000 bytes, 55% free. Bootloader and partition definitions unchanged.
- Portable queue/parser C regression tests run under AddressSanitizer and
  UndefinedBehaviorSanitizer.
- Attendance serializers tested against the pinned ESP-IDF cJSON implementation,
  failing each allocation in turn; incomplete records are rejected. Included in CI.
- All 393 backend/unit, firmware and companion tests pass locally. Actual legacy
  drain orchestration is compiled into a host test covering allocation failure,
  receipt failure, checkpoint failure, restart, concurrent live append during
  network delivery, and bulk failure followed by single-record recovery.
  This does not substitute for exact-commit GitHub CI.

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
