# Saved attendance repair

Operators open **Attendance → Needs review → Repair attendance**, check all saved
history or one exact device, review the counts and device list, and start with
the administrator password. A saved link reopens the same run. Records remain
held when their source or employee evidence is uncertain. The existing guided
employee verification flow is available alongside these results.

## Preservation and decisions

- Checks only read attendance. They freeze exact connector, hardware and terminal
  identities and the greatest saved event ID per connector. New punches belong
  to a later check. No firmware change, terminal scan, employee deletion or Oracle
  schema change is part of this operation.
- Source UID, terminal serial, retained identity observations and verified CNIC
  evidence must agree. Current names or a CNIC in today's roster cannot prove an
  arbitrary historical punch. Source records that have been withdrawn, identity
  reuse, conflicting CNICs and revoked approvals stay held.
- Per-employee observation intervals start with an actual retained snapshot;
  migration does not invent older evidence. Partial snapshots, replacement,
  changed identity and nonconsecutive observations break continuity. Unrelated
  employees changing does not break an unchanged employee's interval.
- The approved item, prior identity state, protected identity projection, audit
  event and single existing outbox commit together. Event UIDs and raw source
  evidence remain unchanged. A lost commit replays the same database cursor.
- Identity proof is checked again at execution and before an ORDS claim. A request
  already in flight is observed without changing its payload or identity.
- `WAITING_ORACLE` is not success. A confirmed item requires the existing verified
  acknowledgment state **and** its saved Oracle confirmation timestamp.
- Pause/stop prevent new records from being added to delivery. Already queued
  attendance continues to Oracle and remains accounted for. Neither action
  deletes captured attendance.

## Bounded work and concurrency

Each independent worker tick scans or applies at most 100 rows. Per-device saved
cursors and rotation prevent one device monopolizing a run. Database row locks
fence workers; no lock or transaction is held during network calls. Approval is
actor-bound, signed and expires 15 minutes after the check finishes. Connector
locks reject overlapping safe/legacy operator repairs. CSRF and password checks
remain mandatory for changes.

The ORDS worker reserves a persistent 4:1 live/background service budget, including
batch size one. Background selection is independent of the live queue's SQL
limit, rotates between connectors, and retains per-record backoff. Existing
idempotent delivery, lost-reply verification and receipts are reused.

Automatic repair is a separate switch. Snapshot changes prompt a recheck after
a five-minute debounce; an hourly bounded sweep also catches new holds and missed
notifications. Unrepairable rows in automatic sweeps are counted and hashed in
the saved check, without duplicating one item per hold on every sweep. Original
attendance remains the detailed evidence; a full operator check lists every held
record. Automatic runs persist individual decisions for records they can repair.

A failure rolls back its batch and retains the cursor. Worker failures and ten
minutes without progress are visible; the UI also detects missing worker updates
independently. Browser disconnection does not cancel a server run. UI counts use
unique saved attendance, not joined manifest rows. Reconciliation percentages are
labelled as that check's scope and cannot round unfinished work up to 100%.

## Additive interfaces and migration

- `POST /api/v2/attendance-recovery/checks`: asynchronous, idempotent check.
- `GET /api/v2/attendance-recovery/checks`: paginated history.
- `GET /api/v2/attendance-recovery/checks/{id}`: saved state and signed approval.
- `POST /api/v2/attendance-recovery/jobs`: new `SAFE_REPAIR` request with check ID,
  signature and password; older request bodies remain supported.
- Existing job GET/items/control routes recognize safe repair runs. Items use
  cursor pagination. Old controls cannot change a new workflow's state machine.
- `GET /api/v2/attendance-recovery/coverage`: current unique saved attendance
  counts, optionally scoped by exact connector ID.

Migration `20260922_0034` adds observation history, per-device check tasks, append-only
decisions and a delivery fairness checkpoint. It supports fresh and upgraded
PostgreSQL installations. Downgrade preserves operational evidence. Roll back the
application with execution disabled; do not remove these tables or pending outbox
records. An old application may leave an observation gap, which the new reader
handles conservatively after a later upgrade.

## Production rollout

All new feature flags default to **false**, and the protected GitHub deployment
workflow passes them through explicitly:

| Setting | Check-only | Canary | Nationwide |
| --- | --- | --- | --- |
| `ADD_ATTENDANCE_SAFE_REPAIR_PREVIEW_ENABLED` | true | true | true |
| `ADD_ATTENDANCE_SAFE_REPAIR_EXECUTION_ENABLED` | false | true | true |
| `ADD_ATTENDANCE_SAFE_REPAIR_AUTOMATIC_ENABLED` | false | false | true only after acceptance |
| `ADD_ATTENDANCE_SAFE_REPAIR_ALLOWED_CONNECTORS` | empty | exact canary connector IDs | empty after approval |

1. Merge only the exact commit whose required CI passes; deploy using ADD's
   existing GitHub Actions workflow and rollback process.
2. Enable checks nationally with execution and automatic repair off. Record the
   check IDs, device identities, count categories, observed times and release SHA.
3. Restrict execution to the verified active SLICTOWER 3FL connector, BLD5-01 and
   one confirmed Hikvision connector. Match connector ID, hardware ID and terminal
   serial; never select a duplicate display name alone. Obtain fresh checks after
   each scope/evidence change.
4. For each canary, record check/run IDs and before/after counts; exercise pause,
   resume, browser reconnect and worker restart. Verify Oracle receipt evidence,
   unchanged raw event UIDs, no new logical duplicates, continuing live capture,
   held records preserved, and clear reasons for unresolved records.
5. Expand the exact connector allowlist, then enable nationwide execution and
   automatic checks only after canary acceptance. If delivery or identity behavior
   regresses, disable execution/automatic repair, preserve queued attendance, and
   inspect the saved decisions before retrying.

Production canary acceptance is a separate operational receipt. Passing software
tests or a completed check does not establish that a production repair ran.

If check requests fail, the **ADD read-only repair diagnostics** GitHub workflow
reports bounded database wait/progress counters, container resource use and
allowlisted HTTP/exception counts. It runs only from `main` on the protected
production runner. It does not change repair flags, attendance, sessions or
delivery state, and never publishes raw logs, payloads or employee identities.

Device WebSocket transactions, bootstrap/catalog reads and rejection handling run
in worker threads with their own database sessions. HTTP device mutations and
signed-request database verification also run outside the event loop. Only plain
committed response/event payloads return to the asynchronous transport. Connector
row locks serialize overlapping socket sequence checks. Failed commits emit no
acknowledgement or browser success. Liveness and thread-pool health protection
remain enabled; diagnostics include fixed restart/lag markers and health-probe
exit codes without exposing probe output.

After deploying a responsiveness fix, compare container start time/restart count
across multiple health-check intervals and confirm login/session responses through
the public proxy. A single successful readiness probe is insufficient to establish
recovery from a restart loop.

## Qualification

`test_safe_attendance_repair.py` covers read-only checks, identity conflicts and
source withdrawal, wrong serial/UID/fingerprint/CNIC, transaction rollback,
receipt-only completion, stop/pause semantics, 5,002-row scans, API session/CSRF/
password requirements, Hikvision preservation, fairness with batch size one and
bounded automatic evidence storage.

`test_safe_attendance_repair_postgres.py` uses a disposable PostgreSQL schema for
concurrent admin approvals and 100,000 historical holds alongside live delivery,
reopening a database session after every checkpoint. CI runs it against its
PostgreSQL service; local runs use `ADD_SAFE_REPAIR_TEST_DATABASE_URL`.

Frontend tests cover check/approval, saved links, queued versus confirmed states,
expired approval and execution gating. Playwright exercises the full flow at
320–1600px and in Chromium, Firefox and WebKit, with keyboard and accessibility
checks. Repository tests, schema drift checks, frontend build/bundle limits and
both firmware builds remain release gates.
