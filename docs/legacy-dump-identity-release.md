# Releasing held legacy dump punches

Firmware without certified source coverage (2.4.x, 2.5.x, 2.6.x, and 2.7.x when
coverage is lost) reads the terminal's whole attendance log after a reconnect
(`DUMP_RECONNECT`) or a restart (`DUMP_STARTUP`). It uploads those rows within
seconds, before its next 15-minute user snapshot. ADD's ingest gate therefore
holds every such row as Unknown Identity (`BLOCKED_IDENTITY` /
`BLOCKED_PROVENANCE`).

## The proof

A held dump row with no saved terminal record from any connector is released
only when all of these hold:

- it belongs to the same connector and ZKT device, and its terminal serial
  equals both the confirmed and the current serial;
- ADD received it within 10 minutes of the firmware reading it;
- it carries a valid event UID, the enrolled UID and the 64-character
  identity fingerprint;
- one retained, unrevoked identity interval for that terminal user ID, UID and
  fingerprint covers the punch time, from consecutive snapshots (whole-roster
  continuity never counts); and
- that interval belongs to the current device user, and its CNIC is the
  user's current CNIC. If HR edited the CNIC after the punch, the row stays
  held: the fingerprint does not change with the CNIC.

PostgreSQL enforces the same rule in `add_auto_reconciled_cnic_verified`
(migration `20261021_0054`). A punch that fails any check stays held for
manual review.

## Automatic release (new holds)

At each roster snapshot, ADD releases rows that pass the proof and that it
held within `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_HOURS` (default 72), at
most `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_BATCH` (default 200) per
connector per snapshot. Each snapshot that releases rows writes one
`ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED` audit event listing them. Set the
hours to 0 to turn automatic release off. The delivery sweep and ingestion
never use this proof.

## Owner release (older holds)

Older holds are historical backlog. They are never released automatically.

1. Attendance → Needs review → Older recovery holds. Pick the zone and choose
   **Check zone**. ADD reads the zone's held recovery punches page by page and
   shows, by capture type and hold age, how many the proof would release. The
   check changes nothing.
2. Choose **Release N punches**, give a reason, type `RELEASE <zone id>` and
   enter the administrator password.
3. The worker releases them in batches of
   `ADD_ATTENDANCE_IDENTITY_BACKLOG_BATCH_SIZE` (default 200), re-checking every
   punch with the same proof and database guard. Punches added after the
   approval are not included. **Stop** (password) ends the run. Punches
   already released stay released.

The approval, each batch (with its event IDs), the stop and the completion
are audited (`ATTENDANCE_IDENTITY_BACKLOG_RELEASE_*`). The same actions are
available at `GET /api/v2/attendance-identity-backlog/preview` and
`/api/v2/attendance-identity-backlog/releases`.

## Chunk-time release

A full-history or tail chunk that links a held row to its saved terminal
record now releases it in the same transaction, for every terminal reconcile
source, including legacy dump rows the chunk found already present. Before,
this happened only for current reconciliation rows; the others waited for
the next snapshot.
