# Zone Lite 2.2.1

Zone Lite 2.2.1 is the first remote-update candidate for the OTA-capable fleet.

## Changes

- Increase the signed ADD OTA HTTP transport buffer to 4096 bytes.
- Wait for ADD connectivity before reporting OTA capability.
- Retry capability reporting until ADD accepts it.
- Defer assignment polling until capability reporting succeeds.

These changes prevent a large ADD response header or a transient connection during startup from leaving an otherwise capable connector labeled for manual firmware updates.

## Rollout safety

This release is application-only. It must not update the bootloader, partition table, encrypted NVS, OTA metadata, identity queue, or storage partition.

Before publication or campaign creation:

1. Normal CI must pass for the exact `main` commit.
2. A physical HIL gate must pass for that exact commit and include real power removal during download and first boot.
3. The protected signing workflow must publish an immutable signed `2.2.1` package.
4. ADD must verify the release manifest and image hash while the OTA feature flag remains disabled.
5. Campaigns must run one zone at a time and stop on any failure, rollback, stale progress, identity regression, or attendance regression.

The intended campaign order is:

1. `ZONE-PESHAWAR-02`
2. `ZONE-PESHAWAR-06`
3. `ZONE-SWAT-01`

After every device update, confirm reconnect, first-boot acceptance, `OTA_READY`, ZKT capture continuity, identity reconciliation, queue recovery, and an observed post-update attendance delivery before continuing.
