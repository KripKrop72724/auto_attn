# Hikvision Zone Lite qualification

Status: **NOT QUALIFIED — read-only hardware observations started 2026-09-17.**

See [the exact-device observations](hikvision-ds-k1t342efwx-v3.3.5-observations.md).
Authentication, identity, user enumeration and small serial-bounded history reads
have succeeded. Live delivery, CRUD and production acceptance remain unqualified.

The first target is DS-K1T342EFWX, photographed running V3.3.5 build 20220310.
This document and the read-only probe implement the first prerequisite of the
Hikvision plan. They do not introduce a working Hikvision ESP image, enable a
production device, or certify any firmware. Existing ZKT behavior is unchanged.

## Local setup

Run from the production repository with its Python environment. The probe uses
the existing `httpx` dependency; no vendor SDK or additional service is required.

The Mac must have a LAN/VPN route to the terminal. Wi-Fi on the same premises
alone does not prove reachability: guest isolation, VLANs, and routing may prevent
access. Use the exact IP shown in the terminal's network settings. Do not use a
subnet mask or gateway as the terminal address.

ISAPI uses the device's web/ISAPI account. An **ISUP registration key is not an
ISAPI login credential**. Do not substitute it or try alternate passwords.

Create a private config interactively; password input is hidden:

```sh
mkdir -p local-data/hikvision
.venv/bin/python -m zk_add.hikvision_probe --init-config local-data/hikvision/probe.json
```

The setup prompt requests the full device serial, not just a short serial suffix.
It refuses to overwrite an existing config. On POSIX systems the probe accepts
only an owner-readable/writable regular file, not a symlink. Keep this config under
the ignored `local-data` directory; never attach or commit it.

HTTPS verifies certificates using system trust or an explicitly supplied CA file.
For a self-signed device certificate, obtain and verify that certificate locally
and supply it as the trusted CA file. There is no `verify=false` option. HTTP with
Digest requires explicit trusted-LAN opt-in. The client ignores proxy environment
variables and will not follow redirects or switch to HTTP automatically.

## First read-only observation

```sh
.venv/bin/python -m zk_add.hikvision_probe \
  --config local-data/hikvision/probe.json \
  --output local-data/hikvision/observation-01.json \
  --stream-seconds 20 --page-size 20 --max-pages 2
```

The probe verifies the serial before reading users or events. It collects device
information, clock/capability responses, a bounded sample of users and history,
and live-stream observations while searches execute on a separate connection.
Only GET reads and POST searches are permitted. No person creation, editing,
deletion, enrollment, time setting, restart, or device configuration is performed.

The live window is bounded by the selected duration plus a transport read timeout;
an idle stream may end with `NETWORK_TIMEOUT`. A stream containing only heartbeats
does not establish live attendance support. Natural stream termination and partial
parts are reported. Concurrent requests are attempted, but their success alone
does not certify loss-free operation under load.

The report is owner-only and cannot overwrite a previous report. Names, employee
IDs, terminal serials, addresses, timestamps, unknown values, and sensitive fields
are replaced with aliases. Aliases match within one report, including employee
numbers in users and events, but not between reports. Event codes and source event
serial numbers remain available for inspection. Images are discarded incrementally.
Do not commit real-device reports without reviewing the sanitized contents.

Exit code 0 means a report was written for the expected device; **it does not mean
qualification passed**. Inspect every endpoint/search status. Exit code 2 indicates
a configuration/output failure or failure to verify terminal identity.

Pagination returns the actual returned count, rather than the requested page size.
Changing totals, repeated pages, session mismatches, premature completion, malformed
responses, and zero-progress pages are reported as failures. Exhausting the page
budget leaves `enumeration_complete=false`. Even a complete enumeration always has
`coverage_certified=false`: this probe cannot prove a frozen source boundary.

After the small sample succeeds, a read-only retained-history enumeration can use
`--max-pages 7500 --page-size 20`, bounded to 150,000 records. This can run for a long
time and compete with device traffic, so schedule it as a controlled hardware test.
Only a few sanitized samples and aggregate counts are retained in memory/report.
Do not interpret a stable total or serial gaps as proof of attendance completeness.

## Qualification matrix

All rows below start **NOT RUN**. Record model, full serial privately, exact firmware,
test date, sanitized evidence location, and result for each run. A newer model or
firmware does not inherit certification automatically.

| Gate | Evidence required |
| --- | --- |
| Device identity | Exact serial binding, model/firmware, clock, ISAPI authentication and transport behavior |
| Event semantics | Controlled successful/failed face, finger and card checks; final vs intermediate multi-factor events |
| Identity parity | Same source identifier for each controlled punch in live and history, including two distinct same-second punches |
| User identifiers | Leading zeros survive list/search; field limits and accepted string formats recorded |
| History coverage | Retained lower bound, fixed upper bound, ordering, search limits, count semantics, boundary anchors |
| Resume | Reconnect and expire a search while punches continue; prove no omitted or duplicated attendance |
| CRUD | Designated test employee only; create/readback, name update preserving rights/credentials, delete/poll/readback |
| Historical preservation | User edits/deletion do not remove or alter stored punches; employee-number reuse characterized |
| Resource behavior | Live stream stays healthy during searches and CRUD; bounded memory and reconnect behavior |
| Reset behavior | ESP/device restart and controlled source reset; stable event identity or explicit source-epoch evidence |
| Delivery | Independent ADD and ORDS outages, durable acknowledgements and identity holds |
| OTA | Signed family rejection and compatible rollback preserving evidence |
| Soak | 72 hours with controlled punches, recovery, reconciliation, CRUD and storage-pressure checks |

The read-only probe intentionally leaves every hardware acceptance gate `NOT_RUN`.
Its output is observation evidence, not an executable capability profile. There is
no flag to force qualification or override a failed mandatory feature.

Do not infer the successful event-code allowlist from generic ISAPI documentation.
Do not implement source identity using a search position or employee/time alone.
Do not enable Oracle release until live/history identity parity is demonstrated.

## Implementation handoff after qualification

1. Freeze the exact model/firmware profile from the verified evidence: endpoints,
   limits, source key, epoch rules, history strategy and successful event codes.
2. Add the dedicated ESP32-S3 build variant within `firmware/zone_lite`, reusing
   onboarding, queues, identity catalog and OTA. Add signed family enforcement.
3. Add backward-compatible vendor metadata, source protocol and ADD feature flag;
   leave all existing devices and releases explicitly ZKT.
4. Implement the live-to-ADD/ORDS slice with bounded multipart parsing and durable
   independent destination state, followed by gap recovery and full reconciliation.
5. Add verified profile commands and ADD-managed CNIC/shift mappings; preserve
   tombstones, historical identity and existing password-confirmed command controls.
6. Add capability-driven dashboard controls, run regression/HIL/soak tests and
   promote only the exact qualified device in a single-device pilot.

The terminal is now reachable and its ISAPI credentials are verified. The remaining
early gates, particularly new-punch stream delivery, must be evidenced before its
profile is certified. No production rollout is authorized by a successful probe
report alone.

## Development checks

```sh
.venv/bin/python -m pytest -q tests/unit/test_hikvision_probe.py
.venv/bin/python -m ruff check apps/add_backend/zk_add/hikvision_probe.py tests/unit/test_hikvision_probe.py
```

The tests cover transport allowlisting, Digest authentication, serial binding,
redaction, XML rejection, multipart fragmentation, skipped images, oversized parts,
pagination failures, leading-zero identifiers, protected output, and a streaming
150,000-event synthetic enumeration. Synthetic tests are not hardware evidence.
