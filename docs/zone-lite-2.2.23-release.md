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

## Historical identity clearance

Before canary acceptance or nationwide promotion, open the selected terminal's
**Historical identity backlog** in ADD. The read-only report separates:

- events attributed to one exact deleted terminal user;
- identity-reuse or duplicate-CNIC conflicts that require separate review; and
- unassigned events that cannot be safely attributed.

Only rows marked `HR_DIRECTORY_EVIDENCE` may use the evidence form. The operator
must copy the CNIC, employee ID, service number, employee name, and zone from an
authoritative HR record. ADD requires an exact service-number match, a compatible
normalized name, the selected row version, an audit reason, typed confirmation,
and administrator re-authentication. Alphanumeric service numbers are supported,
but approximate or inferred identity matching is not.

A successful resolution preserves the attendance rows, binds the verified
identity to the deleted terminal user and its tombstone, and requeues those rows
for Oracle delivery and independent membership confirmation. It does not delete
or merge a terminal user, fingerprint, UID, or attendance event.

Promotion remains forbidden until the report is empty, ADD count equals the
terminal count, every ADD event has Oracle confirmation, and the permitted
Oracle raw-capture checks independently agree.
