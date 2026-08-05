# Zone Lite 2.2.44

## Production correction

Tower 13 proved the 2.2.43 authoritative truth path under a full preservation
partition: ADD acknowledged every Aug 3, Aug 4, and Aug 5 batch and no queue was
deleted. It also exposed a separate live-capture invariant. A ZKT live packet
was counted as observed even when its redundant pending/blocked file append
failed, so a later light reconcile could incorrectly match the terminal
counter while the operator LED remained in local failure.

2.2.44 changes only that exceptional storage-pressure path:

- normal live persistence and outbox delivery are unchanged;
- if a live pending/blocked append or storage-lock acquisition fails, firmware
  attempts one bounded, authenticated ADD delivery for the idempotent event;
- the live record counts toward the next ZKT counter comparison only after
  local durability or an explicit ADD acknowledgement;
- a missing acknowledgement deliberately leaves the event uncounted, producing
  a counter mismatch that forces authoritative ZKT truth repair;
- an acknowledged recovery clears the stale local-failure LED latch; and
- a complete acknowledged truth recovery also clears that latch.

The ZKT remains the durable source of truth throughout the exceptional direct
delivery. Pending, blocked-identity, and ADD outbox files are never truncated or
deleted.

## Rollout gate

Publish as HIL-only, prove on SWAT, then repeat the single-zone Tower 13 pressure
gate. Expansion remains prohibited until Tower 13 is steadily
`ONLINE/LIVE_CAPTURE/OTA_READY/CERTIFIED`, reports no `ESP LOCAL FAILURE`,
preserves or increases its pre-release punch/user counts, emits acknowledged
live-storage recovery when pressure recurs, and completes subsequent light/full
reconciles without a durability gap.
