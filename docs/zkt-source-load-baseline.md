# Observed ZKT source workload

Authenticated `GET /api/v1/devices/{connector_id}/source-load-baseline` measures
the previous 30 complete Pakistan calendar days from canonical source ordinals.
This read-only diagnostic is an input to capacity investigation. It does not
authorize an OTA, establish seven-day capacity, or qualify a terminal profile.

The source scope binds both connector and terminal database ownership, confirmed
serial, current binding generation, active source epoch and committed cursor.
Missing or inconsistent scope returns unknown counts, not a zero-load result.
Only the current epoch contributes. Older epochs may contain useful history,
but joining them without continuity evidence could count recovered copies twice.

Every distinct canonical ordinal contributes independently, including equal
bytes and same-second punches. Delivery attempts, legacy event IDs, Oracle
outboxes and employee identities do not determine the count. The endpoint never
decrypts or returns raw records or attendance payloads. Responses are not cached.

Inventory and minute aggregates use one database statement snapshot. A second
metadata read detects a changed cursor, chain, terminal count, binding or epoch.
PostgreSQL statements have a three-second deadline and a 250 ms lock deadline;
timeouts roll back a savepoint and restore the caller's transaction settings.
Minute groups are bounded before application processing. The endpoint is a
manual diagnostic, not a fleet polling query.

Encoded terminal dates are interpreted using the shared ZKT calendar decoder.
Impossible calendar dates are counted as exceptions; they do not roll into the
next month. Missing raw evidence, missing ordinals, unusable timestamps/layouts,
uncertified custody and uncovered terminal counts remain separate reasons.
Daily zeros mean no usable observed records in that day, not proof of no punches.

`source_inventory_complete` describes the stored ordinal interval only.
`coherent_snapshot` describes measurement metadata stability only.
`baseline_complete` remains false and `qualification` is `NOT_ASSERTED` even
when both are true: independent profile qualification, terminal clock history
and closure of the requested calendar window are not yet available. The
measurement digest identifies the source snapshot and measured results; it is
not a qualification signature. Capacity promotion must retain these limitations.

Synthetic SQLite and PostgreSQL tests cover scope changes, missing evidence,
invalid dates, same-second multiplicity, old-epoch exclusion, authenticated
access and protected-data omission. PostgreSQL tests also exercise lock timeout
recovery, a concurrent manifest change and 200,000 canonical records. These
tests do not supply the real 30-day per-zone baseline or physical model evidence.
