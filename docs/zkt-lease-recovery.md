# ZKT administrator lease recovery

The 2.6.16 bridge and 2.7.0 writer read a separate encrypted-NVS `lease_v2`
record. It retains the authenticated terminal serial, enrollment UID, terminal
identity fingerprint, expiry and generation. The existing `runtime_v1` ABI is
unchanged. A damaged source checkpoint cannot erase a valid lease obligation.

Only the storage task writes lease evidence, under its local storage lock.
The terminal session submits one bounded request and retains its ticket across
timeouts. No terminal mutation occurs while that lock is held. Each commit is
read back in full before it can authorize elevation or retire an obligation.
An uncertain commit is resolved by replaying its exact facts before considering
another generation. Successful terminal revocation still requires a committed
inactive lease before the watchdog can clear its obligation.

An independent presence witness must also commit before the first elevation is
acknowledged. Afterward, a missing lease record is an evidence failure rather
than a new empty store. A cut between the record and witness writes preserves
the proposed active obligation; exact replay completes the witness. Corrupt
records and witnesses are retained, never formatted or silently overwritten.
CRC validation detects changed data; it is not a substitute for encrypted NVS
or physical fault qualification.

The first v2 lease requires an intact inactive legacy runtime checkpoint. An
active legacy lease has no durable identity fingerprint and therefore requires
independent resolution before bridge qualification. Its current UID is not
adopted as identity evidence. A bridge can recover v2 leases after rollback but
cannot create them while an older image might remain its rollback target. New
grants require the qualified journal writer's compatible-reader gate.

After reboot, a retained active lease is due for local revocation without ADD
or a reconstructed wall-clock timer. The gateway refreshes the enrollment and
checks both terminal and identity before any privilege write, and verifies the
same identity after writing. Reused or missing enrollment IDs, changed terminal
bindings and unreadable lease evidence create an explicit review hold. Live
capture and read-only roster refresh remain available; enrollment mutations,
automatic credential changes and scheduled restarts are held. An unrelated
revocation command cannot clear another user's lease. The leased enrollment
cannot be changed or deleted before its obligation is resolved.

Host tests execute the production storage port, actual owner task and gateway
adapters with NVS write/commit/readback failures, lost responses, retained
timeouts, record disappearance, source-checkpoint corruption, reboot, missing
or reused users and changed terminals. Address and undefined-behavior sanitizers
remain enabled. These are software component tests; ESP timing, interrupted
physical writes and field/rollback qualification remain open. No signed image,
campaign or full HIL verdict is produced by this component.
