# Zone Lite 2.2.41 offline-safe OTA rollback hotfix

Zone Lite 2.2.41 preserves every resilience and identity correction from
2.2.40 and closes an additional fail-safe gap exposed by its quarantined SWAT
canary.

## Corrected invariant

An unconfirmed OTA image now starts its local 15-minute boot-confirmation and
rollback task immediately after the ESP network stack is initialized, before
the application blocks waiting for Wi-Fi association. If the candidate cannot
rejoin Wi-Fi, it can no longer wait indefinitely: the ESP invalidates the
unconfirmed partition and reboots into the previously valid image without
depending on ADD, the ZKT, Internet access, or a remote command.

Normal success remains fail closed. The candidate is marked valid only after
ADD acknowledges the candidate and the connector proves authenticated ZKT
stability, a complete identity catalog, and non-regressing user and attendance
counts. The change does not alter live capture, historical reconciliation,
setup-portal access, or LED semantics. Identity handling for the separate
40-byte record-layout defect is described below.

The SWAT canary also proved that the first 16-bit field of its 40-byte
attendance records is not in the terminal's current enrollment-UID namespace.
2.2.41 therefore treats a disagreeing record UID as preserved audit evidence,
not as an identity veto. The verified UID and fingerprint come from the
current complete user snapshot, and ADD continues to reject historical repair
outside the uninterrupted captured-at identity-continuity window. Eight-byte
UID-only records remain UID-authoritative, and a 40-byte UID that agrees with
the snapshot remains corroborating evidence.

## Rollout gate

The 2.2.40 SWAT campaign must not be promoted because its forced truth sweep
mapped zero of the current-day records after the UID namespace disagreement.
Publish 2.2.41 as a new immutable `HIL_ONLY` build targeted only to the exact
SWAT MAC. Promotion and sequential zone rollout remain subject to the full
gates in the OTA agent runbook and the 2.2.40 guarded resilience release notes.
