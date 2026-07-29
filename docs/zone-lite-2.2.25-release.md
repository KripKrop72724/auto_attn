# Zone Lite 2.2.25 corrective release

## Purpose

Zone Lite 2.2.25 prevents zero or implausible ZKT record timestamps from being
classified as attendance. A zero ZKT timestamp can otherwise be formatted as a
January 2001 sentinel and acquire a false daily check-in in Oracle.

The protection is layered:

1. firmware excludes the malformed terminal record before generating an event
   UID or placing it in either durable attendance outbox;
2. ADD preserves any malformed event received from an older connector as
   `QUARANTINED_INVALID_DEVICE_TIME`, raises an alert, and never places it in
   the normal Oracle outbox; and
3. Oracle preserves direct-firmware anomalies as `SUSPECT_DEVICE_TIME` with
   neutral attendance flags while keeping valid historical suspect-clock rows
   in the normal daily ranking.

The Oracle migration is non-destructive and exact-shape guarded. The two proven
`ZONE-SLICTOWER-3FL` sentinel rows remain present with their original event IDs
and timestamps; only their trust classification and derived attendance flags
are corrected, with `DATASYNC=0`.

## Production acceptance gates

1. Publish the exact green main commit as `HIL_ONLY` for `ZONE-SWAT-01`.
2. Confirm SWAT's user count and terminal attendance count are identical before
   and after OTA.
3. Confirm normal live and reconnect attendance is Oracle-confirmed after boot.
4. Confirm `ATTENDANCE_TIMESTAMP_QUARANTINED` does not appear for valid terminal
   rows and that no 2001 sentinel reaches ADD or normal Oracle attendance.
5. Confirm all five Oracle package/procedure objects remain valid, the BULK
   stage is empty, and the authenticated membership endpoint remains healthy.
6. Promote nationwide only after SWAT remains stable and the existing
   identity-blocked backlog has an authoritative, non-guessed resolution.

No attendance row is deleted by this release.
