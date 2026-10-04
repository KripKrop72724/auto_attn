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

The checkpoint layout/version is unchanged, with additional state strings.
Both the paired reader bridge and writer must contain these readers before
release. Old images are not authorized rollback targets once the new writer is
active. A 2.7.0 local-health timeout now enters the bounded failed-boot
coordinator. It blocks new terminal sessions, waits for the current session's
cleanup, drains accepted owner work and commits/read-backs `FAILED_BOOT_INTENT`
through the reserved owner control slot. Full ordinary reply slots and caller
timeouts cannot cancel accepted work or manufacture a successful handoff.

Before invalidating the failed writer, the ESP adapter verifies its actual
digest, two-slot partition layout, secure boot, encrypted NVS, current reader
proof and the exact retained, VALID 2.6.16 bridge. It uses ESP-IDF's non-rebooting
rollback operation and reads back the selected bridge before allowing restart.
An uncertain result retains the intent; retry checks actual boot selection
again. The bridge stays VALID instead of becoming another trial boot merely
because the terminal is unavailable. These checks use the pinned ESP-IDF 5.5.3
implementation; signed-device and physical interruption qualification remain
required. An unqualified bridge-to-legacy/factory predecessor remains an
explicit recovery hold. Legacy images and Hikvision retain their existing path.

After returning to the bridge, journal startup/recovery must complete before
the original writer deployment is reported as `ROLLED_BACK`. ADD verifies the
immediately preceding successful bridge deployment, its image/slot evidence,
and any received writer failure evidence. Only a `BOOT_HEALTH_TIMEOUT` failure
of the same latest 2.7.0 attempt may refine `FAILED` to `ROLLED_BACK`; other
terminal outcomes remain immutable. The original failure event is retained and
the campaign stays paused. Concurrent reports share one committed recovery
event. ADD's receipt binds the stored bridge digest, separately from the failed
writer artifact. Commit failures, lost responses and failed local cleanup retain
the intent for retry; neither HTTP success nor a requested state clears it.
If the bridge returns before a failure intent commits, the same recovered-reader
and previous-image checks apply. It reports `PREVIOUS_FIRMWARE_OBSERVED` without
claiming a bootloader, watchdog or physical reset cause. Missing accepted bridge
or complete deployment evidence keeps the report pending for investigation.

Diagnostics retain the failed local step: unsupported image/family, storage
upgrade, journal recovery, unverified storage, telemetry lock, terminal session,
unknown terminal counts, legacy workers or ESP-IDF mark-valid failure. NVS errors
retain the OTA journal's actual operation category. Source/HIL acceptance must
still use current boot and image evidence.

Tests execute production C control flow and NVS adapters under sanitizers. They
cover an ADD outage beyond the boot deadline after local confirmation, lost
replies, each new checkpoint boundary, repeat mark-valid failure/recovery,
coherent checkpoint reload, worker/storage refusal and both firmware families.
Failed-boot tests cover a stopped terminal session, occupied ordinary reply
slots, NVS uncertainty, unqualified rollback evidence, changed boot selection,
bounded deadlines, recovered-reader startup and lost recovery acknowledgements.
SQLite and PostgreSQL endpoint tests cover changed image/slot/attempt evidence,
concurrent replies and commit failure without acknowledging recovery.
The real HTTP acceptance path is tested with pinned cJSON under allocation
faults and sanitizers. Backend tests exercise the actual endpoint after both
failed and successful terminal states and verify that receipt image evidence
comes from the release, not an unaccepted request.
This software evidence does not prove physical power-cut recovery or flash
endurance; those tests remain NOT_PERFORMED.
