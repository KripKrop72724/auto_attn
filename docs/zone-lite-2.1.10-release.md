# Zone Lite 2.1.10 release record

## Outcome

Zone Lite 2.1.10 is the current production firmware release. It hardens full attendance truth
reconciliation on ESP32-S3 connectors attached to older ZKT/MB40 terminals with large flash-backed
attendance tables. It does not change employee identity policy, terminal credentials, provisioning
secrets, or destructive command behavior.

## What changed

- ZKT authenticated reads allow 90 seconds for slow `CMD_PREPARE_DATA` delivery and use the native
  TCP maximum chunk size, `0xffc0`, to reduce terminal flash requests.
- Every failure after `CMD_READ_WITH_BUFFER` preparation issues `CMD_FREE_DATA`, including allocation,
  partial-stream, and downstream-abort paths. A failed cycle cannot leave prepared terminal state for
  the next session.
- Full reconciliation acquires an ORDS outbox gate before downloading the multi-megabyte ZKT table.
  It releases that gate and the dump before downstream JSON/TLS work.
- Current-month reconcile events are stored in a bounded 5,000-row PSRAM array. ORDS JSON rows are
  generated transiently instead of retaining thousands of individual strings beside the dump.
- ORDS HTTPS requests share one transport mutex, preventing simultaneous truth and background outbox
  TLS sessions from exhausting internal memory.
- ADD truth is serialized and durably appended in groups of 32 batches. The full table and serialized
  ADD payload set are never resident together.
- Recoverable truth pressure uses the truth-repair LED state rather than latching a fatal local fault.
- The ESP-IDF main-task stack is 8 KiB, which safely rebuilds large persisted event-UID indexes during
  boot.
- Heartbeat and onboarding firmware versions come from `esp_app_get_description()`, keeping ADD and
  the flashed image in agreement.

## Completion contract

A full cycle is successful only when all of the following are true:

1. the refreshed ZKT attendance count equals the parsed dump record count;
2. the complete prepared buffer is read and freed;
3. ORDS returns `status=200 ok=true` with no invalid rows;
4. every ADD truth batch is durably appended; and
5. the final reconcile log ends with `complete=true`.

The connector does not advance its successful full-reconcile checkpoint after a partial dump or
failed downstream truth delivery. Live punch capture remains prioritized and deterministic event IDs
make retries idempotent.

## Hardware validation evidence

The production verification cycle used an ESP32-S3 with HMAC-backed encrypted NVS and an MB40-class
terminal. The observed acceptance evidence was:

- clean v2.1.10 boot with 4,540 persisted event UIDs restored;
- automatic Wi-Fi, SNTP, ADD WebSocket, and authenticated ZKT recovery;
- zero ADD outbox rows restored before the cycle;
- complete user snapshot with 728 terminal users;
- complete 3,778,804-byte attendance buffer containing 94,470 records;
- 2,826 current-month events and 2,718 CNIC-valid ORDS truth rows;
- ORDS `status=200 ok=true`, `invalid=0`, `deleted=0`, and 41 corrected rows;
- 2,826 ADD events durably appended in 283 batches;
- final result `blocked=0`, `skipped=0`, `truth=2718`, `complete=true`; and
- automatic ADD transport recovery followed by a successful user snapshot.

The verification record intentionally excludes Wi-Fi passwords, Comm Keys, ORDS credentials,
connector tokens, fleet roots, plaintext CNICs, and site provisioning JSON.

## Validation commands

```bash
.venv-codex/bin/python -m pytest tests/firmware/test_zone_lite_contract.py -q

. /path/to/esp-idf-v5.5.3/export.sh
cd firmware/zone_lite
idf.py build
```

The release gate requires all firmware contract tests, a successful ESP-IDF build, flash hash
verification, and the live completion contract above. A successful compile alone is insufficient.

## Upgrade and rollback

Use the secure provisioner for a new device. For an already provisioned device with a verified HMAC
eFuse and encrypted NVS, an application-only flash preserves configuration, event UIDs, and durable
outboxes. Never erase NVS or storage merely to clear a reconcile failure.

Before rollback, preserve serial logs and confirm ADD/ORDS outboxes are drained or recoverable. A
rollback must not weaken encrypted-NVS, eFuse, identity-precondition, attendance-retention, or
automatic-onboarding guarantees. Re-run the complete hardware acceptance procedure after any
protocol or storage rollback.
