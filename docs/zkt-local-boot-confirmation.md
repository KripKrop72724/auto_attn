# ZKT local boot confirmation

Only exact ZKT `2.6.16` and `2.7.0` images use the new local confirmation path.
The deployed older versions and Hikvision retain their existing policy. Local
confirmation establishes that the image can preserve attendance; it does not
certify complete source coverage, Oracle completion, remote HIL or production.

The local gate requires completed storage upgrade/recovery, current persistence
proof, the journal startup controller's reader/worker checks, and an authenticated
stable terminal snapshot with known counts. An empty enrollment is allowed.
The bridge additionally requires its retained legacy delivery workers and
buffers. The 2.7.0 writer uses the owner/capture/custody transport worker set and
does not require an ESP Oracle worker. LED state, identity resolution and ADD
connectivity cannot substitute for or invalidate these independent local proofs.
An unfinished local recovery obligation still prevents confirmation.

The OTA controller checks local health before contacting ADD. After ESP-IDF
marks the image valid, it commits `LOCAL_VALIDATED` in the existing atomic OTA
checkpoint. It then reports `BOOTED_PENDING`, commits `BOOT_REPORTED`, reports
`RECONCILING`, and commits that local state. Lost responses retry the same
transition. A failed checkpoint stops the next report; restart resumes its
last committed state. In particular, a failed ADD report after local validity
does not start another boot-health timeout or turn into an automatic rollback.
The source-coverage gate and checked final success path remain separate.

For these two versions, an HTTP success alone cannot acknowledge OTA progress.
ADD returns a versioned receipt after committing the transition, with the actual
deployment state, deployment ID, target version and application digest from its
release record. The firmware verifies the exact requested state and identity;
boot and reconciliation reports also require its measured running image digest.
A previously failed or cancelled deployment cannot acknowledge a later boot
report. Only `RECONCILING` may accept the same deployment's already committed
`SUCCEEDED` state, allowing recovery from a lost final reply or local clear.
Missing fields, malformed replies and allocation failure leave progress pending.
Legacy firmware and capability registration keep their existing HTTP contract.

The checkpoint layout/version is unchanged, with two additional state strings.
Both the paired reader bridge and writer must contain these readers before
release. Old images are not authorized rollback targets once the new writer is
active. The existing bootloader failure path remains for an image that cannot
establish local health inside the configured deadline; exact compatible reader
qualification and the separate operator rollback coordinator remain required.

Diagnostics retain the failed local step: unsupported image/family, storage
upgrade, journal recovery, unverified storage, telemetry lock, terminal session,
unknown terminal counts, legacy workers or ESP-IDF mark-valid failure. NVS errors
retain the OTA journal's actual operation category. Source/HIL acceptance must
still use current boot and image evidence.

Tests execute production C control flow and NVS adapters under sanitizers. They
cover an ADD outage beyond the boot deadline after local confirmation, lost
replies, each new checkpoint boundary, repeat mark-valid failure/recovery,
coherent checkpoint reload, worker/storage refusal and both firmware families.
The real HTTP acceptance path is tested with pinned cJSON under allocation
faults and sanitizers. Backend tests exercise the actual endpoint after both
failed and successful terminal states and verify that receipt image evidence
comes from the release, not an unaccepted request.
This software evidence does not prove physical power-cut recovery or flash
endurance; those tests remain NOT_PERFORMED.
