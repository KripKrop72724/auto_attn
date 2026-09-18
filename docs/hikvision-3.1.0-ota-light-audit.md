# HIK Zone Lite 3.1.0

Hikvision and ZKT remain separate signed firmware families. The OTA candidate
workflow accepts an explicit family, verifies the matching compiled application
identity, and publishes `zone-lite-hikvision-3.1.0.bin` with release ID
`zone-lite-hikvision-3.1.0`. ADD labels the release **HIK Zone Lite 3.1.0**.
The first remote deployment is quarantined to the configured exact Hikvision MAC
using `ADD_FIRMWARE_HIKVISION_HIL_TARGET_MAC`, independently of the existing ZKT
canary. Unrestricted production promotion still requires the existing real
hardware qualification; a successful OTA alone does not satisfy those gates.

## Health and logs

An ESP heartbeat proves its ADD connection, not its terminal's availability.
Capture failure now supplies a specific ADD explanation and alert: configuration,
network/timeout, authentication, HTTP response, response size, changed source,
invalid response, or durable-storage failure. Recovery resolves that alert.
Fleet and device views show this distinction rather than raw `HIKVISION_POLL_2S`.
The two-second start-to-start polling target remains unchanged. Slow serialized
ISAPI requests can extend it; measured interval, request stage, HTTP status and
consecutive failures are visible. Operational logs include the stage, textual
reason, duration, source cursor and queue/storage state, without credentials or
employee data. The restored poll cursor is reported even before the terminal
becomes reachable.

## Automatic light history audit

Sixty seconds after boot, a separate low-rate audit pins a bounded retained
history range at or below the live poll cursor. It preserves at most 20 records
per page, starts at most one page every 30 seconds, and yields terminal access
between steps. Explicit ADD history assignments and profile operations have
priority. The audit waits for terminal recovery, ADD connectivity, and a source
queue below 40 records. It does not change the live poll cursor.

The audit stores its terminal/source binding, upper bound, inclusive anchor,
progress and completion time in encrypted NVS. Source queue persistence precedes
checkpoint commitment; interrupted writes safely replay through ADD's existing
observation/event deduplication. A changed anchor or corrupt checkpoint blocks
progress rather than claiming coverage. The audit resumes after reboot and
repeats six hours after a completed sweep. A large history can take days at this
rate; live polling provides prompt outage catch-up throughout. No audit can
recover history that the terminal has already deleted or overwritten.

`COMPLETE` means the bounded sweep entered durable local custody. ADD receipts
and Oracle outcomes remain separate. It is not a full-reconciliation coverage
certificate. Telemetry exposes state, saved cursor, preserved count and last
completion; blocked audits raise an ADD alert.

## OTA acceptance

Hikvision boot acceptance checks restored source custody/checkpoint, verified
storage, active/fresh poll/history/upload workers, ADD delivery and heartbeat
workers, and authenticated ADD connectivity. It does not read ZKT-specific
session/coverage fields. A disconnected external terminal remains a capture
fault; it does not make a healthy application roll back. The OTA journal reaches
success only after recovered local source observations have ADD durable
receipts and ADD acknowledges the checked version/digest/OTA-slot transitions.
These checks leave the existing ZKT acceptance path intact.

Remote evidence is recorded after the actual ADD browser campaign. A compile,
unit test, heartbeat version or release publication alone is not an OTA success.
