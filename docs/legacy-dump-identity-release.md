# Releasing held legacy dump punches

Firmware without certified source coverage (2.4.x, 2.5.x, 2.6.x, and 2.7.x when
coverage is lost) reads the terminal's whole attendance log after a reconnect
(`DUMP_RECONNECT`) or a restart (`DUMP_STARTUP`). It uploads those rows within
seconds, before its next 15-minute user snapshot. ADD's ingest gate therefore
holds every such row as Unknown Identity (`BLOCKED_IDENTITY` /
`BLOCKED_PROVENANCE`). Before this change nothing could release a dump row
that had no saved terminal record, so holds waited for a full-history
baseline.

## The proof (`AUTHENTICATED_RECONCILE_BATCH`)

A held dump row with no saved terminal record from any connector passes only
when all of these hold:

- it belongs to the same connector and ZKT device (never Hikvision), and its
  terminal serial equals both the confirmed and the current serial;
- its clock quality is OK, and ADD received it within 10 minutes of the
  firmware reading it;
- it carries a 64-character event UID, the enrolled UID and the 64-character
  identity fingerprint;
- one retained, unrevoked identity interval for that terminal user ID, UID and
  fingerprint strictly covers the punch time (no tolerance; whole-roster
  continuity never counts); and
- that interval belongs to the current device user, and its CNIC is the
  user's current CNIC. If HR edited the CNIC after the punch, the row stays
  held: the fingerprint does not change with the CNIC.

Any saved terminal record decides instead of this proof. PostgreSQL enforces
the same rule in `add_auto_reconciled_cnic_verified` (migration
`20261022_0056_dump_release`, chained after `20261022_0055`). The migration
only replaces that function: no table is altered, scanned, locked or
backfilled. A punch that fails any check stays held.

Only three paths use this proof: the automatic release at a roster snapshot
(below), the owner's zone release (below), and the pre-send recheck of rows
they released. Ingestion, the delivery sweep, safe repair, chunk processing
and the identity-evidence report do not.

## Settings

Set them in the server's `.env.add` (commented defaults are in
`.env.add.example`) and restart ADD.

| Environment variable | Default | Meaning |
|---|---|---|
| `ADD_ATTENDANCE_DUMP_IDENTITY_RELEASE_ENABLED` | `true` | **Kill switch.** `false` withdraws the proof everywhere: no automatic release, running owner releases pause (also the ones only following punches to Oracle), no new owner release can be approved, and the pre-send check holds again every row it released that Oracle has not acknowledged. |
| `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_ZONES` | empty | Zone IDs for automatic release, comma-separated; `*` for every zone. **Empty means automatic release is off everywhere.** |
| `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_HOURS` | `72` | Window for automatic release; both the punch and ADD's hold of it must be newer. `0` turns automatic release off. |
| `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_MAX_SNAPSHOT_GAP_MINUTES` | `60` | ADD's complete roster snapshots just before and just after the punch must be at most this far apart. |
| `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_BATCH` | `100` | Automatic releases per connector per snapshot (re-releases have their own equal cap). |
| `ADD_ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASE_HOURLY_CAP` | `200` | Automatic releases per connector in any hour (re-releases separately). 2.6.x sends a snapshot on every reconnect, so the per-snapshot cap alone does not bound the rate. |
| `ADD_ATTENDANCE_IDENTITY_BACKLOG_BATCH_SIZE` | `25` | Punches the owner release checks per worker transaction. |
| `ADD_ATTENDANCE_IDENTITY_BACKLOG_IN_FLIGHT_LIMIT` | `100` | The owner release queues no more while this many of its punches wait for Oracle. |
| `ADD_ATTENDANCE_CHUNK_HISTORY_RELEASE_LIMIT` | `25` | Held full-history, dump and 15-minute rows a source chunk may release in its own transaction (see below). |

Setting `..._AUTO_RELEASE_HOURS=0` or emptying `..._AUTO_RELEASE_ZONES` stops
only automatic release. To stop everything that uses the proof, including
running owner releases and the pre-send acceptance, set
`ADD_ATTENDANCE_DUMP_IDENTITY_RELEASE_ENABLED=false`.

## Automatic release (recent punches, enabled zones)

At each complete, stable roster snapshot of a terminal in a listed zone, ADD
releases held dump rows that pass the proof when:

- the punch and ADD's receipt of it are both within the window; and
- ADD's complete roster snapshots just before and just after the punch are at
  most the gap apart. A dump that uploads punches from an outage (for
  example an ESP recovering from a storage fault) or old punches for the
  first time therefore waits for the owner, even though the identity
  interval continued across the outage.

At most the per-snapshot cap and the hourly cap per connector are released.
Each snapshot that releases rows writes one
`ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED` audit event listing them. A row
that was released before and that delivery held again (because a newer
snapshot arrived before Oracle confirmed it) is released again under its own
cap, without the window or gap limit. A row an administrator took into a
manual or forced send is never released on this proof.

A punch newer than the latest snapshot cannot pass yet: the next snapshot
that still shows the same identity extends the interval over it. The
service helper `dump_release_outlook` reports this as
`awaiting_next_snapshot`, and `release_path` says whether a snapshot or only
the owner can release it. It is meant for the identity-evidence report owned
by `fix/attendance-release-safety`; this change leaves that endpoint as it is.
Before sending such a punch with Direct ORDS, check whether the next snapshot
will release it.

## Owner release (everything else)

1. Attendance → Needs review → Older recovery holds. Pick the zone and choose
   **Check zone** (off-peak for large zones; one check page runs at a time).
   ADD reads the zone's held recovery punches page by page and shows, per
   terminal, capture type and hold age:
   - **Held**: held dump punches never released before;
   - **Can be released**: the dump proof passes now;
   - **Next user list**: another proof passes; the next roster snapshot
     releases them, not this action.
   The check changes nothing. Punches some proof released earlier that were
   then held again (by delivery or a guard) are shown separately and are not
   part of the check or of any release from it.
2. Choose the groups, then **Release N punches**, give a reason, type
   `RELEASE <zone id>` and enter the administrator password.
3. The approval freezes exactly the punches of the chosen groups, up to the
   highest punch ID of the check, that are held and never released at that
   moment (their ID runs and digest are kept on the run and in the approval
   audit). It is refused if more are held than the check counted. The worker
   releases at most the approved number of them, in batches of
   `..._BACKLOG_BATCH_SIZE`, re-checking every punch with the same proof and
   database guard. A frozen punch that another path released, or that left
   the hold, before the run reaches it is passed and counted as "no longer
   held". No other punch is ever included.
4. If a chosen terminal's user list is partial or unstable, its serial awaits
   confirmation, or a new user list arrives while a batch runs, the release
   waits and checks those punches again; it never passes them. A long wait
   shows on the run.
5. Every released punch is followed until Oracle acknowledges it. If delivery
   holds it again because a newer snapshot arrived first, it is checked again
   with the same proof at each newer snapshot, up to three failed checks.
6. **Stop** (password) releases nothing more. Punches already released stay
   released and are still followed to Oracle; the run shows "Stopped:
   following released punches to Oracle" until they are confirmed.

The approval, each batch (with its event IDs), the stop and the completion
are audited (`ATTENDANCE_IDENTITY_BACKLOG_RELEASE_*`). The same actions are
available at `GET /api/v2/attendance-identity-backlog/preview` and
`/api/v2/attendance-identity-backlog/releases`. ADD has one administrator
account, so "owner" means the password step-up plus the typed confirmation.

## Chunk-time release

A full-history or tail chunk that links a held row to its saved terminal
record releases it in the same transaction with the saved-record proof (never
the dump proof). Current-reconcile rows are always released there, as
before. Other terminal reconcile rows, including legacy dump rows the chunk
found already present, are released there only up to
`ADD_ATTENDANCE_CHUNK_HISTORY_RELEASE_LIMIT` per chunk, because the device
waits for the chunk's acknowledgement; the next roster snapshot releases the
rest.

## Captured CNIC on firmware before 2.6

2.4.12 and 2.5.2 fill a dump or reconcile record's CNIC from the terminal's
user table when they read the log, not when the employee punched (2.6.x
clears it). If the user ID changed hands in between, that captured CNIC is
the new holder's, and the synced-CNIC release used to send the punch under
it. A non-live punch is now released on its captured CNIC only when no
unrevoked retained interval shows a different, known CNIC for that terminal
user ID at punch time. The same migration applies this to
`add_auto_synced_cnic_verified`, and the pre-send check holds such a punch
again if an earlier build released it. Live punches carry the punch-time
CNIC and are unchanged; so are punches with no retained interval.

## What stays held

- Rows with no retained interval covering the punch: new-hire first punches,
  punches before ADD observed the terminal, and punches across a partial
  roster.
- Rows whose user has no valid CNIC (for example A.Saleem), or whose CNIC
  changed after the punch (for example Toheed's punches before 19 October).
- Rows ADD received more than 10 minutes after the firmware read them.
- Rows with a mismatched UID, fingerprint or record UID, or a saved record
  from another connector.
- Pre-2.6 dump and reconcile rows whose captured CNIC is not the CNIC a
  retained interval shows for that user ID at punch time.
