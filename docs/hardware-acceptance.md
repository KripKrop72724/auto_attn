# Zone Lite + ZKT hardware acceptance

This procedure is the release gate for each new terminal model/profile and the first production
connector. Use a controlled test employee identity; never experiment on an employee's real record.

## Preconditions

- ADD CI and production deployment/public checks are green for one exact SHA.
- The ESP32-S3 USB serial port is stable and its Wi-Fi MAC is recorded.
- ZKT serial/model/firmware, user count, attendance count, device time, and current IP are recorded.
- A fresh terminal backup/export exists according to local operations policy.
- The production `.env.add` fleet root matches the fleet root used by the provisioner.
- Explicit approval has been obtained before any irreversible eFuse burn for the exact MAC.

## Provision and boot proof

1. Inspect `espefuse summary` and retain a redacted report containing MAC, chip revision, key purpose,
   writeability, flash size, and secure-NVS decision—never raw keys.
2. Build the same commit that passed CI.
3. Run the secure provisioner. On first use, pass the exact approved MAC to
   `--confirm-efuse-burn-for`; on a proven prior Zone Lite device use
   `--trust-existing-derived-hmac` only with matching fleet records.
4. Require the provisioner's encrypted-NVS flash readback hash to match.
5. Capture serial boot logs until Wi-Fi, signed onboarding, TLS WebSocket, ZKT authentication, live
   event registration, and heartbeat succeed.
6. Confirm the connector appears automatically in ADD; there must be no registration action.

## Read-only baseline

Before any mutation, prove:

- reported ZKT serial exactly matches the recorded terminal;
- only one connector claims the serial;
- user-record size is observed consistently and snapshot is complete;
- user and attendance counts match the machine;
- ZKT time and sampled-at time update in ADD;
- filtered attendance and masked user data render correctly;
- live logs continue without browser refresh;
- idle connection attempts remain bounded—no rapid connect/disconnect cycle.

A 72-byte record may progress to writable after stable observations. A 28-byte, unknown, duplicate,
or partial profile must remain visibly read-only.

## Controlled user lifecycle

Record attendance count `A0`. Using a synthetic device user ID and valid test CNIC:

1. Create a regular user from ADD and verify exact name preview, UID/user-ID, privilege `0`, CNIC
   mask, and row version after the ESP reread.
2. Edit name, CNIC, and shift-worker state; verify the terminal and refreshed ADD snapshot agree.
3. Repeat the same idempotency key/replay command; verify no duplicate user and one terminal write.
4. Attempt a stale row-version update; require a safe conflict and zero terminal change.
5. Disconnect the ZKT during a write. Require `WAITING_FOR_ZKT`/`RETRYING`, bounded reconnect, then
   exactly one verified completion after recovery.
6. Delete the synthetic identity. Require the local encrypted tombstone, terminal absence, ADD
   deleted state, and attendance count exactly `A0`.
7. Search attendance for any test punch created before deletion; it must remain present and linked
   to the tombstoned identity.

Do not pass the gate if user deletion can invoke attendance-clear commands or if count changes.

## Administrator lease

1. Recreate/select the controlled regular user and record privilege `0`.
2. Grant a ten-minute lease with password step-up; verify privilege `14` and persisted absolute
   expiry in connector logs/ADD.
3. Confirm a restart request is blocked while the lease is active.
4. Disconnect ADD/internet but leave ZKT reachable; require local privilege `0` at expiry.
5. Repeat with ZKT offline across expiry. Require a durable overdue alert and bounded retry; restore
   the ZKT and require a verified privilege `0` without another operator command.
6. Power-cycle the ESP during the lease and prove the persisted deadline still revokes on time.

## Intermittent-terminal and anti-hammer test

Cycle only the controlled ZKT network path: online, brief offline, brief online, repeated offline.
Observe for at least one flap window.

- One failed exchange enters suspect/retry, not a tight loop.
- Repeated transitions enter `FLAPPING` and a jittered five-minute quiet period.
- Full discovery is no more frequent than fifteen minutes.
- The ESP remains online to ADD and reports next retry/live logs.
- No user write runs during flapping/recovery.
- After reachability returns, live capture registers first; three healthy observations and two stable
  minutes precede reconcile/write recovery.
- Reconcile is light at fifteen minutes unless counts prove a gap.
- Connection exits unregister and close; the terminal UI remains responsive throughout.

Capture attempts/hour and terminal response time before/after. A model that becomes unresponsive
under this bounded load stays read-only and requires model-specific review.

## Attendance and restart proof

- Produce controlled punches during steady state, ADD outage, ORDS outage, ZKT flap, and recovery.
- Verify deterministic IDs yield exactly one ADD row and one ORDS result per physical punch.
- Power-cycle ESP with queued events and prove flash replay/acknowledgement.
- Force a count mismatch and prove live capture resumes before bounded truth reconcile.
- Trigger an ADD restart command and verify one protocol restart, state transition, recovery, and no
  attendance deletion.
- Exercise each scheduled slot in a time-controlled test or injected clock build; require one restart
  per 02:00/12:00/22:00 Pakistan slot and no repeat after ESP reboot.

## Final evidence and sign-off

Retain commit SHA, firmware version/hash, ESP MAC, eFuse purpose result, ZKT serial/profile,
provisioning readback result, ADD connector ID, command IDs, pre/post counts, screenshots with masked
PII, bounded serial logs, outage timeline, and 24-hour observation metrics.

Pass only when every destructive-path invariant, local revoke, intermittent recovery, public domain,
and attendance durability check succeeds. Remove the synthetic user after preserving its punches and
tombstone evidence.
