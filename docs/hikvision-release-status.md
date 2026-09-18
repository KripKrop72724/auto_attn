# Hikvision pilot status

Status on 2026-09-18: **ADD deployed; signed 3.0.3 flashed to the authorized
LF-ZONE-BLD9-03 ESP; pilot delivery observed; full release qualification incomplete.**

## Current hardware evidence

- PRs 162–165 are merged. CI 35317698044 passed all five checks; production
  deployment 35318320760 and its public checks passed. Signing 35318318234
  published the 3.0.3 HIL_ONLY bundle. The ZKT firmware rollout remains disabled.
- Application-only flash preserved provisioning and storage. Secure Boot verified
  the new application signature; ADD reports the expected hardware identity and
  firmware. The terminal is reachable and the durable polling cursor reached 173664.
- Three recent-tail attendance rows have ADD custody and Oracle confirmation.
  This is recovery/poll delivery evidence, not a measured fresh-punch latency result.
- The source queue drained with no reported read/write failures. After the serial
  capture reboot, persistence proof awaits a new durable write; the previous
  durability alarm remains latched rather than being manually cleared.
- Profile publication and full reconciliation have not yet passed. Hardware timing
  exposed starvation: checking the predecessor and new range in separate searches
  can consume the whole five-second interval. Version 3.0.4 combines the committed
  anchor and new records in one ordered search, verifying the anchor before any
  new record is admitted. It is pending signed hardware verification.
- ESP profile create/edit/delete dispatch and the 72-hour soak remain incomplete.
  The capability profile remains NOT_QUALIFIED; no AVAILABLE promotion is claimed.
- Historical CNIC-correction preview/execution remain temporarily disabled by
  operator authorization. Normal Oracle attendance delivery remains enabled.

The following development notes describe earlier milestones; statements that no
merge, deployment or flash occurred refer to those earlier milestones only.

Installation: `LF-ZONE-BLD9-03` (zone ID and device name), ESP32-S3 and serial-bound
DS-K1T342EFWX V3.3.5 build 220310. Protected installation data remains outside
version control in `local-data/hikvision/`.

## Updated operator requirements

The operator explicitly accepted five-second polling instead of event streaming
for this release. They also specified the existing ZKT identity convention:
`name-CNIC` permits Oracle delivery. A historical event with a plain name uses
the exact employee number in a complete verified terminal-profile snapshot;
missing mappings and observed employee-number reuse remain held.
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
On September 18 the operator authorized preparing the combined change for production
review while keeping the Islamabad ZKT firmware rollout disabled. PR 162 now targets
`main` and includes this dependency. PR 161 remains draft. This is preparation of
the combined source/backend change, not acceptance of the unfinished hardware gates.
Firmware publication/promotion workflows remain manual; none was dispatched.

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

## Historical profile identity correction

The retained 19,320-record diagnostic contains no CNIC-encoded event names,
including its 3,641 face-code records. The name-CNIC rule therefore also resolves
against the exact employee number in ADD's verified complete profile snapshot.
Leading zeros remain significant, and names are never matched by similarity.
Observed identifier reuse, conflicting identities, missing profiles and incomplete
snapshots remain held. A bounded worker releases previously unmapped, unattempted
outbox rows when verified profiles arrive; non-null attendance identities stay
pinned. Snapshot references and profile versions are retained in attendance
provenance without changing event UIDs.

## September 18 reconnection and combined review

The Mac is back on the terminal LAN. A new authenticated identity read verified the
configured terminal serial, DS-K1T342EFWX model and V3.3.5 firmware. The connected
ESP was independently read through `/dev/cu.usbmodem1101` and matches the intended
`ac:27:6e:a4:e9:74` ESP32-S3. No flash or eFuse write was performed.

GitHub Actions run 35246202822 passed repository-contract, backend, frontend,
firmware and containers for commit 605e60c. The combined PR is mergeable against
main. Follow-up fixes separate Hikvision string employee-number allocation from
ZKT's 16-bit UID, retain used/deleted identifiers, allow 32-digit overrides end
to end, and reject oversized/non-ASCII command identifiers before truncation.
Hikvision cannot acquire ZKT write certification from heartbeat metadata.

The read-only retained-history diagnostic resumed from its durable checkpoint;
this remains qualification evidence, not an ADD production reconciliation job.
The production release still requires complete verified CRUD dispatch, signed
factory qualification and physical ADD/Oracle delivery evidence. Preparing the
combined PR does not waive those requirements or enable a ZKT firmware rollout.

Combined-review follow-up validation: 721 backend, firmware-host and companion
tests pass (one existing Starlette deprecation warning). Both Hikvision and ZKT
ESP-IDF builds pass. These are local build/regression results, not hardware
acceptance or signed production artifacts.
