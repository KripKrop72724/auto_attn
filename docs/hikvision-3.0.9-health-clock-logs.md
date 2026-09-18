# Hikvision 3.0.9 health, clocks and operations logs

Hikvision defaults to a two-second **start-to-start target** from 3.0.9 onward.
The reported interval is authoritative; the legacy provisioned `poll5-pilot-v1`
profile identifier remains unchanged to preserve device binding and approved
profile operations. ADD continues accepting five-second telemetry from older
firmware. Signing/qualification accepts evidence explicitly measured at either
supported cadence; this change does not fabricate qualification results.

The terminal request mutex still serializes ISAPI operations. Every poll grants
at most one background page. Profile/history requests consume that slot, and
history releases the mutex before waiting for ADD's durable receipt. Polling
continues during reconciliation; slow device requests can extend the target.
Heartbeat reports poll count, measured last interval, and accepted history-page
count so simultaneous progress can be checked on hardware.

A clean Hikvision boot now verifies a dedicated SPIFFS probe using write, flush,
fsync, reopen, exact readback, and removal, then verifies an encrypted NVS blob
through commit, close and reopen. No queue records, attendance, provisioning or
source cursors are replaced. Failed queue recovery or a latched storage error
cannot be cleared by this probe. It runs only before workers on boot, avoiding
periodic flash writes. Successful proof allows the existing ADD durability
recovery rules to resolve an old fault even when no fresh punch arrives.
Green requires successful polling, an empty known source queue, ADD connectivity,
verified storage, and fresh running ADD/source delivery workers.

The serial and ADD operation logs receive polling state/periodic summaries,
clock sampling, and history-page receipt outcomes without employee data or
credentials. The terminal clock is read once per minute through authenticated
`/ISAPI/System/time`; it is never set. Offset-bearing ISO timestamps are parsed
strictly, and slow (>2s), failed or stale samples remain unverified. Heartbeats
show the sample and drift; missing samples clear stale live-clock displays.

New recent polling/stream observations carry the contemporaneous clock sample
inside their durable upload envelope. ADD stores that proof in attendance
provenance, classifies <=120-second drift as OK and larger drift as DRIFTED.
Samples older than 120 seconds, timezone-assumed events, retained-history events
and events remote from the sample remain UNKNOWN. Replays preserve existing
clock evidence; historical timestamps and clock labels are never backfilled
from today's sample.

Validation before publication: 765 backend/firmware-host/companion tests;
80 focused regression tests after cadence telemetry; 109 frontend tests plus
14 focused tests including the new cadence rendering case; frontend build;
ESP-IDF 5.5.3 Hikvision build; lint and repository/OpenAPI contracts. Hardware
flash and live deployment evidence are recorded separately after publication.
