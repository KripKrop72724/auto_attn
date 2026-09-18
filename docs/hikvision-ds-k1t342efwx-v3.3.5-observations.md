# DS-K1T342EFWX V3.3.5 — hardware observations

Observation date: 2026-09-17. Status: **NOT QUALIFIED FOR PRODUCTION**.

These observations are from the photographed terminal, verified by its device
identity. They are not claims about other terminals or firmware. Credentials and
person data are intentionally excluded; private reports remain in ignored,
owner-only `local-data/hikvision/` files.

## Verified read-only behavior

| Check | Observation |
| --- | --- |
| Identity | DS-K1T342EFWX, V3.3.5, build 220310; matches photographed terminal |
| Authentication | `admin` account accepted with HTTP Digest; supplied corrected password verified |
| Transport | HTTP available; HTTPS certificate not trusted by the test Mac; diagnostic HTTP opt-in explicit |
| Network | DHCP address changed; MAC/serial binding required, IP alone is insufficient |
| Device and access capabilities | All original capability reads succeeded |
| Users | Initially 32 profiles; a later complete read returned 33 after the operator added a profile externally |
| User capability limits | Employee number 1–32; name 0–128; page maximum 30 (reported limits, character/byte semantics not yet verified) |
| User operations | Disposable profile create, name edit and targeted delete verified by readback; details below |
| Historical records | Initial total 143,444 access-control records; not an attendance count |
| Initial source boundary | First observed serial 30001, last observed serial 173444; new records arrived subsequently |
| Event capability limits | Search page maximum 30; source serial maximum 3,000,000,000 |
| Bounded source query | `beginSerialNo` and `endSerialNo` accepted; returned records respected the tested bounds |
| Repeatability | Two fresh searches each over 30001–30090 and 100001–100090 returned identical raw-record digests |
| Clock | One sample was approximately 92 seconds behind the Mac; device clock was not modified |

## Open live-delivery issue

`GET /ISAPI/Event/notification/alertStream` opens successfully and supplies JSON
access-control events in multipart messages. Its initial messages replay retained
history, explicitly marked `currentEvent=false`.

The first 180-second observation received 600 replayed events. A subsequent
observation over five minutes received more than 1,000 replayed events and no
events marked current. During this investigation, new events were readable from
history. An operator reported a successful face check; a recent history record
with major 5, minor 75 was present. This establishes a candidate correlation, not
a measured live-delivery latency or proof of that employee's identity.

A second operator-reported face test and externally added employee profile were
checked with fresh, serial-bound device reads. The new employee was present in
the complete user snapshot, and two distinct history records for that employee
carried major 5/minor 75. Their source serials were 173477 and 173479. Employee
details are kept only in the protected local report. During the concurrent stream
observation, received messages continued to carry `currentEvent=false`; the new
records had not arrived in the observed portion. This verifies profile visibility
and historical face-event retrieval, not connector-driven CRUD or live latency.
The completed second observation lasted 161 seconds: 626 replay records,
serials 32486–33111, zero current records, and neither of the two new test records.
The listener was closed after saving sanitized evidence.

The terminal advertises `subscribeEventCap`. A second concurrent subscription was
rejected with `deployExceedMax`. After closing our existing listener, an isolated
`POST /ISAPI/Event/notification/subscribeEvent` session with heartbeat 5,
channelMode all and eventMode all succeeded, but its initial events also replayed
history. These transient subscriptions did not modify device configuration.

Do not assume that receiving stream messages proves live attendance works. Keep
replay/current/unknown counters separate, retain current samples independently of
the first replay samples, and inspect alternate XML envelopes during controlled
punch tests. Do not relabel historical polling as certified live streaming.

The vendor defines minor `0x4b` (decimal 75) as face authentication passed and
`0x26` (decimal 38) as fingerprint comparison passed. The final qualified mapping
still requires controlled positive/negative and multi-factor checks.
[Official event definitions](https://open.hikvision.com/hardware/structures/NET_DVR_ACS_ALARM_INFO.html)

## Recovery and certification limits

- A later bounded search returned HTTP 401; a fresh authenticated client succeeded.
  Capture reproducible recovery evidence before assigning a cause or adding retries.
- No full 143,444-record traversal or frozen-boundary certificate was produced.
- A separately authorized disposable regular-user profile was created, renamed,
  and deleted. Leading zeros survived readback; the name-only update preserved
  every other returned profile field. Deletion was verified by absence. Existing
  employees were not modified. No credentials were enrolled on the disposable
  profile, so deletion consequences for biometrics remain unqualified.
- Temporary HTTP notification slot 2 and ISUP enablement experiments were
  restored and verified. ISUP restoration required the original key, which is
  omitted by the device's GET response. Clock and terminal firmware were unchanged.
- No ADD/Oracle attendance was released by these diagnostics.
- Face/fingerprint parity, source epoch/reset semantics, retention rollover,
  interrupted full scans, profile lifecycle, dual delivery, ESP memory/OTA and
  the 72-hour soak remain open.

The Python source-normalization helpers have synthetic regression tests for
live/replay/history key parity, immutable-fact conflicts, same-second distinct
punches, leading zeros, timezone handling, and explicit event-code qualification.
They are development helpers, not a deployed ADD ingestion path or ESP firmware.

## Additional live diagnostics

The operator's subsequent face punches were visible as history serials 173482,
173484 and 173485. A five-minute HTTP notification experiment received no POST
at the configured Mac listener. The listener checked a single path and
Content-Length bodies; this does not establish that all notification formats are
unsupported. The temporary, previously unused destination was removed and its
disabled/empty state verified.

Disabling the unused ISUP destination briefly did not bypass the stream backlog:
126 messages, serials 33113–33238, all non-current. Original ISUP enablement and
destination were restored and read back successfully. No live fix is established.

The vendor's [2026 release notes](https://assets.hikvision.com/prd/normal/all/files/202606/releasenote%5CDS-K1T342_Series_MinMoe_Terminal_Release_Note_V4.48.40_build260522.pdf)
list this exact model and newer ISAPI/platform changes. They do not specifically
identify a fix for the observed replay behavior or establish a safe direct upgrade
path from this installed build. No terminal upgrade has been performed.

## ESP development status

### Parser, restart and resumable source observations

Direct HTTP notification slot 1 produced multipart/form-data with an `event_log`
part containing JSON but no Content-Type header. The previous parser discarded
that metadata. Both diagnostic and embedded parsers now accept that exact untyped
part while still skipping explicitly typed image parts. Fragmented-body regression
tests cover the fix. Historical notifications were decoded successfully; this is
not yet proof of a current punch arriving through direct notification.

The subsequent five-minute direct-notification test used the corrected parser
from process startup. The operator confirmed another face punch; history showed
employee 111 face-success records 173522 and 173524. The listener decoded 153
notifications (serial range 31930–32583), with neither controlled record received.
These notifications omitted currentEvent; their source serials establish that
they are older than the test punches. The unused slot 1 destination was removed
in cleanup and its original empty URL/zero address/zero port read back successfully.
This tested direct route also failed the current-punch acceptance check.

A filtered POST subscription using all advertised minor-filter fields was accepted,
but observed messages still replayed history. Correcting the parser also did not
make the observed GET subscription deliver the controlled current face events.

One controlled restart was performed after closing our stream and history clients.
Readback verified unchanged terminal identity/firmware, all 33 user profiles by
complete snapshot hash, and oldest retained source serial 30001. Source serials
remained monotonic. Post-restart face events 173517 and 173519 were readable from
history but absent from the bounded GET stream observation. This is evidence of
failure in that tested route, not proof that every possible ISAPI configuration
fails or that a firmware upgrade will fix it.

The resumed serial-seek audit committed another 3,000 records in 131.38 seconds,
bringing its partial durable total to 19,320. Each page used a fresh search ID and
position zero with an incremented lower serial bound and fixed upper bound 173497.
No complete scan, repeated-pass comparison or coverage certificate exists yet.

The official model page now lists V4.48.40 build 260629. A safe upgrade sequence
from this installed build and a fix for the observed behavior have not been
established. No upgrade has been performed.
[Official model downloads](https://pro-av.hikvision.com/mena-en/products/Access-Control-Products/Face-Recognition-Terminals/Value-Series/ds-k1t342efwx/)

### Embedded build

The connected ESP32-S3 was inspected without flashing or changing eFuses. A
dedicated `zone_lite_hikvision` development image, version 3.0.0, builds with IDF
5.5.3 in the existing partition layout: 0x110000 bytes, 57% application-partition
space remaining. Runtime heap and reliability are not established by this build.

Bounded parsing, source-custody upload, family checks and history/CRUD helpers
exist in the working tree. The 150,000-record synthetic history test passes.
Full reconciliation jobs, command dispatch, identity workflows, independent
attendance delivery, dashboard/provisioning integration and hardware qualification
are still incomplete. This image is not a production release.

The operator assigned zone ID and device name `LF-ZONE-BLD9-03`. Wi-Fi and terminal
credentials remain only in protected local configuration; they are not compiled
into the image. Signed provisioning through the existing ADD environment remains
pending.

## Operator-approved polling mode

After the direct-notification tests, the operator accepted a five-second polling
mode for this release. Ten initial empty checks took 0.95–1.50 seconds per request.
A subsequent five-minute diagnostic maintained scheduled checks and returned the
operator's new face records 173527 and 173529. The read that captured these records
took 3.398 seconds; this is request duration, not measured punch-to-Oracle latency.
All subsequent empty polls left the source serial unchanged. No notification
configuration was modified by the polling tests.

The operator also selected the existing ZKT name-CNIC rule for Oracle identity.
A complete read found 33 terminal profiles, 30 with recognized CNIC suffixes.
Profile names, CNICs and credentials are omitted from this report. This rule
supersedes the earlier requirement for a separate identity mapping before every
Oracle release. Missing encodings and contradictory source identities remain held.
