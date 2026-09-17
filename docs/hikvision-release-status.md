# Hikvision 3.0.0 development status

Status on 2026-09-17: **development integration implemented in part; not deployed,
not flashed, and not certified for production**.

Installation: `LF-ZONE-BLD9-03` (zone ID and device name), ESP32-S3 and serial-bound
DS-K1T342EFWX V3.3.5 build 220310. Protected installation data remains outside
version control in `local-data/hikvision/`.

## Updated operator requirements

The operator explicitly accepted five-second polling instead of event streaming
for this release. They also specified the existing ZKT identity convention:
`name-CNIC` permits Oracle delivery; names without a valid CNIC remain held.
`name-S-CNIC` preserves the existing shift-worker convention. Neither names nor
CNICs enter the deterministic event UID. A conflicting CNIC for an already
received source event is quarantined for controlled identity repair.

The stream issue is no longer the release blocker for this polling mode. Signed
qualification supports `capture_mode=poll`, interval 5, and polling-specific gates
instead of the two streaming gates. Other release gates have not been waived.

## Implemented development components

- Dedicated Hikvision CMake family/application descriptor, version 3.0.0;
  existing ZKT remains 2.5.2. Signed firmware/factory selection rejects cross-family
  images. Protected NVS fields include a required stable source epoch.
- A five-second ESP polling worker, source-serial seeks, previous-record checks,
  durable local evidence before encrypted NVS checkpoint advancement, bounded
  queue custody upload and hash-bound ADD receipts. Initial installation captures
  the recent tail while explicitly leaving full retained history outstanding.
- ADD-owned serial-source reconciliation jobs, serialized request worker,
  fresh search sessions, fixed upper cutoff, page receipts, two matching scans,
  retained-boundary revalidation, pause/resume and separate Oracle assurance.
  The existing fleet scan-slot and Oracle backlog limits apply. Empty-history
  certification requires two empty observations. This is not ZKT ordinal evidence.
- Raw evidence and normalized attendance/Oracle outbox work commit together.
  ADD custody ACKs do not retire the independent Oracle work. Event classification
  requires an ADD-owned, serial/epoch-bound policy. New policies default disabled;
  configuring them requires administrator step-up and an audit entry.
- Strict name-CNIC parsing, leading-zero employee identifiers, unknown-identity
  holds, same-source identity conflict quarantine and no fabricated IN/OUT.
- Dashboard polling health, queue depth, last successful check and source serial;
  ZKT-only COMM Key and restart controls are hidden on Hikvision devices.
- Physical provisioning now selects a firmware family end to end, validates Hikvision
  LAN/credential/binding fields, forwards them to encrypted NVS, and rejects
  cross-family artifacts in both the worker and USB companion. The dashboard
  provides a Hikvision installation form.
- Verified low-level user CRUD helpers, bounded paginated profile reads and multipart parsing exist.
  Profile snapshots now use two matching paginated scans, encrypted ADD staging
  and atomic publication into the existing user workspace. Explicit user refresh
  commands use the same path; interrupted reads do not imply deletion.
  Verified create/edit/delete command dispatch remains incomplete.

## Hardware observations

Ten empty checks on five-second start intervals took 0.95–1.50 seconds per read.
A subsequent five-minute polling diagnostic captured employee 111 face events
173527 and 173529 in a scheduled check. This proves the host-side ISAPI polling
path, not end-to-end ESP latency or Oracle acceptance. Current terminal profiles:
33 total, 30 with recognized name-CNIC encoding. The remaining three are not
assigned guessed identities.

The earlier real-device full-history audit has durably captured 19,320 records
from its frozen 143,497-record scope. It remains a partial diagnostic scan, not a
complete retained-history certificate. The new ADD-owned scan protocol has
synthetic and transaction/replay tests; it has not completed a production job.

## Remaining before release

- Exact-device ESP polling, restart/power-loss recovery, contention, heap/storage
  pressure, signed OTA rollback, end-to-end ADD/Oracle receipts and full source job.
- Complete verified CRUD dispatch, profile qualification,
  capability-driven user controls, and hardware validation of the provisioning path.
- Final hardware qualification and the required 72-hour soak.

## Concrete release dependency

The working base is `codex/firmware-reliability-islamabad-hil`, associated with
[draft PR 161](https://github.com/KripKrop72724/auto_attn/pull/161). Its durable queue
foundation is not present in production `main`. That draft explicitly records
outstanding hardware/recovery/soak work; its green CI does not satisfy those gates.
The Hikvision change must not silently promote the entire unrelated HIL rollout.

The existing factory signing workflow requires an exact green commit already on
`main`. No signing/provisioning workflow, merge, production deployment, eFuse change
or ESP flash has been performed for Hikvision. A quarantined development candidate
is not an AVAILABLE production release.

See [hardware observations](hikvision-ds-k1t342efwx-v3.3.5-observations.md) and
[qualification procedure](hikvision-qualification.md).

## CI correction and latest verification

The fresh PostgreSQL migration failure was caused by historical revision 0001
creating current model metadata before the additive Hikvision revisions ran.
Revisions 0027–0029 now inspect existing columns/tables consistently with the
repository's bootstrap convention. Fresh PostgreSQL upgrade and Alembic schema
checks pass; regression tests cover both bootstrap and existing-install shapes.
GitHub run 35242434114 passed all five checks for commit bfd34dc.

Subsequent provisioning integration passes local regression (711 tests before the
additional protected-worker test, which also passes), 106 frontend tests, frontend
build and scoped lint. The connected ESP was reidentified at USB port
`/dev/cu.usbmodem1101` with the expected MAC; no flash or eFuse write was performed.
The serial-bound Hikvision terminal remains reachable. Full verified CRUD dispatch,
physical end-to-end delivery/recovery, retained-history job completion and 72-hour
soak remain release gates. Green CI alone does not prove those results.

Profile integration follow-up: 715 backend/firmware-host/companion tests pass,
including snapshot interruption, duplicate pages, changed inventories and two-pass
empty snapshots. PostgreSQL upgrade through revision 0030 and schema comparison
pass. The Hikvision ESP image builds with the periodic/manual profile reader;
profile requests take precedence over full history while polling retains priority.
These are build/host results, not a claim of flashed hardware acceptance.
