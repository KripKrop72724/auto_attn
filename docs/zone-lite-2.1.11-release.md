# Zone Lite 2.1.11 release record

## Outcome

Zone Lite 2.1.11 removes the six-hour same-count identity blind spot. Every live packet is retained by
the terminal, acknowledged, and then enriched only after two consecutive complete user-table reads
produce the same state hash. Background verification runs every 30 seconds so edits made through ADD,
ZKTeco software, pyzk, or the terminal are represented in ADD without relying on a user-count change.

## Safety properties

- Event UID, device time, punch facts, attendance history, deduplication, and both durable outboxes are unchanged.
- Missing or conflicting identity is never guessed. Unverified events stay blocked or waiting for a snapshot.
- Verified snapshot changes repair only unacknowledged events with matching device-user evidence.
- UID, device-user, or terminal-fingerprint reuse is quarantined.
- A CNIC removed from the terminal is also removed from ADD instead of leaving stale identity behind.
- Firmware blocked rows without a permanent rejection reason are repaired crash-safely and returned to pending ORDS delivery.

## Rollout

Deploy migration `20260721_0007` and the ADD application before flashing connectors. Keep
`ADD_IDENTITY_SNAPSHOT_GATE_ENABLED=false` during mixed-version operation. Application-only flash one
ESP, complete the hardware acceptance sequence, then flash the second ESP. Enable the gate only after
every connector reports Zone Lite 2.1.11 and has published a stable snapshot revision.

The 60-second repair target assumes a reachable terminal, ESP, ADD, and ORDS service. Outages preserve
events durably and replay them after recovery; they do not weaken identity validation.

## Rollback

Use the previous application partition and disable the ADD identity gate. Do not erase NVS, SPIFFS,
attendance, users, fingerprints, deduplication state, or outboxes. The additive provenance migration is
retained during rollback and can be ignored safely by the previous application release.
