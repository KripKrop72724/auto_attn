# Zone Lite 2.2.23 corrective release

## Purpose

Zone Lite 2.2.23 removes the remote ESP-to-Oracle heavy reconciliation call
from the normal ADD-connected production path. Complete, identity-safe truth
windows are first persisted to the firmware's fsync-backed ADD outbox. ADD then
uses its validated internal Oracle route, durable retry worker, and membership
checks to finish delivery.

Direct Oracle reconciliation remains a bounded fallback when ADD is disabled
or the durable ADD outbox cannot accept a window.

## Production acceptance gates

1. Publish the exact green main commit as `HIL_ONLY` for `ZONE-SWAT-01`.
2. Require boot/runtime health, certified ZKT connectivity, a complete stable
   identity snapshot, and preserved terminal counts.
3. Require a post-boot `IDENTITY_CATALOG_APPLIED` event followed by
   `ORACLE_RECONCILE_DELEGATED_TO_ADD`.
4. Page through the complete ADD attendance ledger for the canary and require:
   - ADD row count equals the terminal attendance count exactly;
   - the accepted pre-update count is preserved; and
   - every row is Oracle-confirmed (`ACKED` or `ACKED_CHECK`).
5. Do not promote or update another zone if any historical row remains identity
   blocked, quarantined, unconfirmed, or absent from ADD.

No attendance data is deleted by this release.
