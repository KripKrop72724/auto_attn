# Zone Lite 2.2.22 corrective release

## Purpose

This release closes the two failures exposed by the nationwide 2.2.21 rollout:

- a reproducible pre-confirmation boot rollback on `ZONE-PESHAWAR-02`; and
- Oracle HTTP 500 responses when reconciliation temporarily assigned a second
  daily check-in/check-out before recomputing the complete stored day.

## Firmware changes

- OTA first-boot confirmation now remains pending for up to 15 minutes and is
  accepted only after:
  - ADD acknowledged the pending boot;
  - the encrypted identity catalog was received and persisted;
  - the ZKT session reached `ONLINE`; and
  - verified user and attendance counts were observed.
- Pending boots publish durable `WAITING_FOR_RUNTIME_HEALTH`,
  `RUNTIME_HEALTHY`, and `BOOT_HEALTH_TIMEOUT` evidence before any rollback.
- Secure-boot capability is read from hardware instead of being asserted.
- The running application SHA-256 is reported to ADD.
- The bounded fragmented ADD message limit is aligned with the server at
  512 KiB, and up to 4,096 verified historical identity rows are accepted.

## ADD and Oracle changes

- Firmware campaign responses expose per-device progress events, byte counts,
  attempts, and the latest error code so boot failures are diagnosable after a
  protected rollback.
- Device responses expose the reported running image digest and signing-key
  identifier.
- Oracle authoritative reconcile inserts neutral daily flags and invokes the
  same deterministic, non-destructive whole-day flag helper used by live and
  bulk ingestion.
- The Oracle migration automatically restores the exact original package body
  if any precondition or compilation check fails. It performs no attendance
  table DML.

## Acceptance gates

1. Repository, backend, frontend, and firmware contract tests pass.
2. Oracle package and helper are `VALID` with no compilation errors.
3. A complete reconcile window returns HTTP 200/201 and `success=true` without
   any delete.
4. SWAT HIL boots the exact signed image, reaches `RUNTIME_HEALTHY`, preserves
   users and attendance, and returns to `LIVE_CAPTURE`.
5. Peshawar-02 is the first production OTA after promotion. It must reach
   `RUNTIME_HEALTHY`, preserve at least 95,470 punches and 728 users, and stay
   online before any other device is updated.
6. Every remaining OTA-ready device is updated and verified individually.
