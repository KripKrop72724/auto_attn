# Manual attendance force release

Policy: `manual-current-terminal-v1`. ADD only; no firmware change.

## Operator workflow

Open **Attendance → Needs review → Force release attendance**. Select exact
devices (serial and hardware identity are shown), or explicitly select **All
Pakistan**. Spares are excluded. Dates default to all saved dates. **Sync and
check** saves a durable run and the upper attendance boundary for each device.
Later punches are outside that run.

Review ready, blocked, already confirmed and unavailable totals. Only ready
punches can be approved. Provide one reason and your administrator password.
Approval uses the current matching terminal employee and CNIC for older punches;
it does not invent historical identity proof. The review expires after 15 minutes.

ADD refreshes each ready terminal again, skips changed matches and prepares at
most 100 records per transaction. A run survives navigation and server restart.
Pause/Stop prevent further preparation; committed deliveries continue to be
accounted for. Resume refreshes terminals again. Stop never deletes attendance.

**Forced** records a committed administrator decision, separately from delivery.
**ACKED_CHECK** means Oracle content verification matched the approved identity
and source facts. Sending a request, HTTP success, a duplicate response or UID
membership alone never produces confirmation. The Forced pill opens the decision,
reason, sync evidence and saved run. Lists do not expose unmasked CNICs.

## Custody and identity boundaries

- Both ADD and the fresh terminal row must contain the same valid CNIC. Names
  alone are insufficient. Legitimate UID-less formats need recognized protocol
  evidence; contradictory UIDs are blocked.
- Terminal serial, connector and hardware identity are frozen and rechecked.
  Unverified bindings, identity reuse, unknown/deleted users, conflicting capture
  evidence, damaged IDs/data/timestamps and unresolved conflicts remain blocked.
- Historical conflicts use the exact terminal serial as well as the connector.
  Evidence explicitly belonging to a replaced terminal cannot block a valid
  punch from the current terminal. Conflicts with unknown terminal provenance
  remain blocked. Empty optional UIDs never link unrelated employees.
- Pre-sync CNIC hashes and identity fingerprints are saved before requesting a
  new list. A refresh cannot erase a prior conflict. New capture-time CNIC hashes
  are stored independently of effective identity enrichment.
- Existing held rows receive a sticky manual-approval marker. ORM and deferred
  PostgreSQL constraints prevent automatic reopening, including older application
  images. Normal valid new attendance and existing explicit approvals continue.
- Snapshot enrichment, tombstone, identity-backlog and Hikvision held-record
  release are retired. Prior repair history remains readable; old start/approve
  APIs return 410. Automatic configuration and scheduled approval are removed.
- Per-record decisions, protected prior values, frozen payload, audit and outbox
  entry commit together. Original event UID, time, source facts and raw evidence
  remain unchanged. Global scheduler/device locks serialize overlapping runs;
  durable idempotency keys recover duplicate submissions and lost responses.

## Freshness and delivery

At most two manual terminal refreshes run concurrently; each has a 10-minute
deadline. Success requires a successful new REFRESH_USERS command plus a newer,
complete, stable, committed snapshot for the same terminal and connector boot.
Receipt/observation timestamps must agree with server-recorded command progress.
Each deliberate refresh has its own request key; retries reuse that key.
Preparation refreshes again after five minutes or a changed user-list hash.

Firmware lacks an exact request-to-snapshot identifier. This ADD-only policy
therefore uses command progress, receipt times, revisions and boot identity.
Ambiguous freshness excludes the device. Legacy capture information that was
never retained cannot be reconstructed; no missing evidence is represented as
historical proof.

For ZKT WebSocket refreshes, ADD also preserves the authenticated command-start,
committed-roster and command-success message order in the existing command event
ledger. All three must belong to the same boot, occur within the request's server
time window, and have increasing sequences. The roster's capture time must fall
inside that command's **device-clock** interval. This permits a consistently
skewed ESP clock without accepting old cached bytes: reused snapshot IDs, missing
boundaries, out-of-order messages, a clock jump, a changed boot, or an uncommitted
roster cannot supply this proof. Without this complete evidence, the original
strict clock freshness check applies. This does not relax attendance timestamp,
identity or Oracle confirmation rules, and requires no firmware change.

Manual delivery retains the 4:1 live/background allocation and rotates devices.
Network requests run outside database transactions. Slow manual content checks
do not delay receipt commits for live claims from the same batch. Authentication,
contract and prolonged retry failures appear as attention-needed while the
approved payload remains saved; retries use capped backoff.

The existing protected read-only `raw-captures/identity-repairs/check` contract
checks UID, terminal, user, timestamp, raw-punch facts, employee name and CNIC.
Missing records are sent through normal ORDS with the original UID, then checked
again. Lost replies are verified before replay. Conflicting Oracle content is
held for review and is never overwritten by this workflow. If the protected
interface is unavailable, records stay pending.

## Rollout and recovery

Deploy through GitHub's existing production workflow after the complete ADD CI
passes on the exact commit. Use these fail-closed settings:

| Setting | Initial qualification value |
| --- | --- |
| `ADD_ATTENDANCE_FORCE_RELEASE_PREVIEW_ENABLED` | `true` |
| `ADD_ATTENDANCE_FORCE_RELEASE_EXECUTION_ENABLED` | `false` until canary approval is ready |
| `ADD_ATTENDANCE_FORCE_RELEASE_ALLOWED_CONNECTORS` | `2ca9a4c2-5ae4-4330-8d14-840223672897` |
| `ADD_ATTENDANCE_FORCE_RELEASE_ALLOWED_FAMILIES` | `zkt` |

Canary: active SLICTOWER 3FL, hardware `a4:cb:8f:d4:66:64`, terminal
`PGB1261200074`. The similarly named spare is not a substitute. Prepare a small
date-bounded check, inspect exclusions, then let the administrator approve using
their password in ADD. Record run/audit IDs and actual matching Oracle receipts.
Test a larger bulk run after the small run passes. Qualify a healthy Hikvision
device before adding `hikvision` to the allowed families. Unavailable devices
remain excluded. National manual enablement follows acceptance; there is no
automatic-repair activation.

Migration `20260923_0035` is additive. Application rollback retains this schema,
approvals, pending outbox and audit evidence; it must not restore a pre-deployment
database dump over accepted attendance. The rollback compose file disables all
repair execution/automatic switches and starts the older API without attempting
to downgrade or resolve the newer Alembic revision. Database delivery guards stay
installed. Capture continues, while the rollback overlay pauses **all ADD Oracle
delivery** by clearing its base URL. Legacy workers cannot safely retry the new
frozen approval payloads. Both ordinary and manual outbox records remain saved;
restoring the qualified image with its normal configuration resumes delivery.
Firmware delivery is outside this ADD-only rollback. An unknown schema revision
stops automatic rollback for investigation instead of discarding data.

## Qualification evidence

Automated tests compile/run real backend transactions and transport handling:

| Concern | Regression coverage |
| --- | --- |
| Identity, CNIC, UID-less formats, provenance and damaged punches | `test_attendance_force_release.py` |
| New command plus complete snapshot; stale/partial/reboot/changed matches | same suite |
| No automatic reopening; approved payload and true content acknowledgements | same suite and existing backend/Hikvision regressions |
| Lost Oracle response, HTTP success/duplicate without proof, conflicting content | transport tests in same suite |
| Pause/resume/stop, expiry, frozen boundaries, idempotency and old controls | same suite |
| Real concurrent approvals, transaction rollback, old SQL guards, populated migration/rollback | `test_attendance_force_postgres.py` |
| 100,000 held records, bounded batches, responsive login and 4:1 live service | PostgreSQL load test in same suite |
| UI exclusions, password/reason, saved runs, Forced audit and old history | frontend unit tests and responsive browser suite |

GitHub CI additionally runs repository contracts, secret scanning, backend,
firmware and companion tests, migration/drift checks, frontend bundle budgets,
browser accessibility checks and the ESP-IDF build. Production acceptance is a
separate evidence step; passing software tests does not assert that a real
administrator-approved release or healthy Hikvision qualification has occurred.
