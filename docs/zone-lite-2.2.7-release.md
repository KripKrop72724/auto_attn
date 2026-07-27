# Zone Lite 2.2.7 release record

Zone Lite 2.2.7 makes OTA completion durable when the device boots the target
firmware but ADD does not receive the first `SUCCEEDED` progress message.

## Failure observed

During the sequential 2.2.6 rollout, Peshawar-02 booted 2.2.6 and preserved its
complete terminal snapshot. The first `SUCCEEDED` acknowledgement was not
accepted by ADD. Firmware cleared its local OTA journal anyway, so ADD retained
the deployment as active and offered the same release again.

## Corrective contract

- Firmware keeps a `RECONCILING` journal until ADD acknowledges `SUCCEEDED`.
- A pending success acknowledgement is retried before another assignment is
  requested.
- Firmware treats an assignment for its already-running target version as an
  acknowledgement-recovery operation and never downloads it again.
- ADD recognizes an active deployment whose connector already reports the
  target version, records `SUCCEEDED`, and returns no download assignment.
- Both plain application versions (`2.2.7`) and connector versions
  (`zone-lite-2.2.7`) are normalized for the server-side guard.

## Rollout gates

1. Build and test the exact main commit with ESP-IDF 5.5.3.
2. Publish 2.2.7 as a signed `HIL_ONLY` release for `ZONE-SWAT-01`.
3. Prove SWAT reaches stable `LIVE_CAPTURE`, then promote the same bytes.
4. Update each remaining OTA-ready zone sequentially.
5. Confirm every campaign is terminal, every connector is stable, and Oracle
   membership checks contain no missing or duplicate attendance events.
