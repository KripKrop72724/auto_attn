# Zone Lite 2.3.0 release

Zone Lite 2.3.0 introduces the ADD-owned, resumable reconciliation protocol documented in [ADD-owned reconciliation](add-owned-reconciliation.md).

The release changes the reconciliation protocol and must not be promoted as a patch release. It preserves the existing OTA partition layout and does not repartition or erase ESP storage.

Acceptance requires:

- exact application version `2.3.0`, immutable application SHA-256, approved signing key, and the exact tested Git SHA;
- HIL proof of nonzero-offset ZKT reads, first-anchor and committed-boundary rejection, disconnect/restart resume, full-storage command execution, current append-tail recovery, and live punch delivery during a source job;
- ADD migration `20260806_0012`, no schema drift, feature flag enabled, readiness healthy, and the Reconciliation workspace available;
- sequential canary/zone campaigns in the order SLICTOWER, Karachi, Swat, Peshawar, stopping before the next zone on any failed acceptance gate;
- no forced reboot while a ZKT exclusive operation or temporary administrator lease is active.

The previous 2.2.53 release remains the rollback target until the 2.3.0 zone campaign has passed its observation window. A rollback does not delete ADD source manifests or certificates.
