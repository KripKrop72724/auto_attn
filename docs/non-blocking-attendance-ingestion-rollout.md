# Non-blocking attendance ingestion rollout

## Safety invariant

ADD may acknowledge an attendance batch only after one database transaction has
committed its batch receipt, every per-input disposition, all accepted attendance
events, encrypted quarantine evidence, and all required Oracle outbox rows.
Semantic validation failures are durable `QUARANTINED` outcomes and never block a
later punch. Database, encryption, storage, or unknown internal failures are not
acknowledged and remain retryable.

## Backend deployment gate

1. Take and verify a restorable database backup.
2. Apply Alembic revision `20260824_0019` before starting the new application.
   The migration is additive and its evidence tables must not be downgraded.
3. Deploy all backend replicas in one controlled rolling window. Do not leave old
   all-or-nothing ingestion replicas serving device traffic after the new path is
   enabled.
4. Deploy the matching frontend after the API is healthy. Existing Zone Lite
   2.4.12 devices are compatible: they accept the normal ACK and ignore its new
   settlement fields.
5. Send a synthetic mixed batch in the production smoke scope: valid row,
   malformed row, valid row. Require one receipt with `accepted=2`,
   `quarantined=1`, two attendance rows, and both valid rows in the Oracle outbox.

Do not promote if an ACK can be observed before transaction commit, encrypted
evidence cannot be decrypted under step-up audit, or the same batch retry creates
a second receipt/event.

## Backend observation window

Observe for at least 30 minutes before starting firmware OTA:

- attendance acceptance continues while quarantine counts increase;
- `DEVICE_MESSAGE_REJECTED` for `attendance_batch` stops recurring;
- batch replay returns the original receipt and creates no duplicate event;
- acknowledgement latency, database errors, and websocket reconnects remain at
  baseline;
- Oracle backlog age and retry counts do not regress;
- no connector becomes `DEGRADED` merely because one row was quarantined.

If storage or database errors occur, confirm that the device retains the outbox
row and that no ACK was sent. Repair forward. A backend rollback reintroduces the
head-of-line defect, so use it only as a last-resort containment action and keep
the additive evidence tables intact.

## Zone Lite 2.4.13 OTA gate

Build and sign one immutable 2.4.13 artifact from the reviewed commit. Record its
SHA-256, signing key, secure-boot state, partition layout, build log, and source
commit. Never rebuild between cohorts.

Roll out the identical artifact in these stages:

1. simulator and firmware contract tests;
2. attached physical HIL device with forced malformed-row, ACK-loss, network-loss,
   reboot, and rollback exercises;
3. one OTA-capable canary device, preferably near the operations team;
4. one OTA-capable device per region;
5. 10 percent of eligible devices;
6. 25 percent, 50 percent, then 100 percent of eligible devices.

Hold each device stage for at least 30 minutes and each multi-device stage for at
least two normal punch cycles. Devices marked `LEGACY_MANUAL_UPDATE` are excluded
from OTA and require a separately scheduled, checksum-verified manual upgrade.

Automatically pause a campaign on any of these conditions:

- any rollback, boot-health failure, signature failure, or partition mismatch;
- any accepted punch missing from both ADD receipt evidence and the retained
  device outbox;
- acknowledgement failure rate above 1 percent for 10 minutes;
- connector reconnect or `DEGRADED` rate more than twice baseline;
- Oracle backlog age increasing for 15 minutes;
- corrupt-row preservation or outbox cursor advancement failure;
- quarantine rate above the pre-agreed data-quality threshold.

Rollback only the affected firmware cohort to the previously signed 2.4.12 image.
The backend non-blocking settlement path remains active during firmware rollback.

## Acceptance evidence

Close the rollout only when all eligible OTA devices report 2.4.13 and boot health,
all manual-only devices have an owned upgrade ticket, open quarantines have named
review owners, receipt/event/outbox counts reconcile, and the live regional view
shows no recurring attendance-batch rejection loop.
