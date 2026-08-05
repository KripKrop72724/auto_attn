# Zone Lite 2.2.51 atomic OTA/ZKT safepoint hotfix

## Incident

The one-device SWAT 2.2.50 canary preserved its exact terminal serial, 35 users,
and 26,714 punches, but its OTA reboot overlapped an already prepared ZKT
attendance truth read. The ESP restarted safely, while the terminal retained the
abandoned prepared-read state. Subsequent truth sessions therefore returned an
implausible all-invalid timestamp snapshot and were correctly rejected without
replacing Oracle truth.

A controlled ZKT protocol restart cleared the terminal-side prepared buffer and
the immutable 2.2.50 release remained quarantined. It was never promoted for
nationwide use.

## Change

- Add an atomic OTA restart claim shared by the OTA task and the ZKT gateway.
- Let the signed image download and validate without pausing live punch capture.
- After the image is ready, wait until the authenticated terminal is online and
  the gateway is in live-capture/online idle state before claiming reboot.
- Prevent startup identity reads, queued ADD commands, administrator-lease
  enforcement, identity verification, time sampling, reconciliation, and
  scheduled ZKT restarts from beginning after the OTA claim.
- Preserve the 2.2.50 bounded large-terminal truth allocation and fresh-session
  retry changes without altering identity, punch, outbox, or ZKT data.

## Production acceptance

1. Build, sign, and publish the exact main-branch 2.2.51 artifact as HIL-only.
2. Cancel the rejected SWAT 2.2.50 campaign without canary acceptance.
3. Update only SWAT and require exact serial `AEXH232260005`, 35 users, no
   attendance-count regression from 26,714, a stable identity snapshot, and a
   fresh successful delegated truth cycle with no later failure.
4. Promote the immutable 2.2.51 artifact only after the SWAT gate passes.
5. Update only `ZONE-PESHAWAR-02`; require serial `CJH9211060009`, at least 728
   users, no attendance-count regression from 96,628, bounded-memory telemetry,
   a fresh successful delegated truth cycle, and no later reconcile failure.
6. Keep `ZONE-PESHAWAR-06` and all other devices paused until Peshawar-02 passes
   every acceptance condition.
