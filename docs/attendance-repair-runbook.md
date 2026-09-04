# Attendance blocked-punch review and ORDS release runbook

This runbook governs the terminal-scoped **Attendance → Needs review** and **Attendance → Release history** workflow. It is a correction of effective identity attribution, not a rewrite of physical attendance history. **All events** continues to show the original capture disposition and adds the independently verified effective release state.

## Non-negotiable safety contract

The following fields are physical source facts and must never be changed or deleted by an employee repair:

- ADD attendance `event_uid` and Oracle `EVENT_UID`;
- source UID and source user ID;
- connector, terminal serial, and source device;
- device event timestamp, punch/raw-punch value, status, and source type;
- encrypted raw event, source manifest, source epoch, capture receipt, and source certificate.

Only the effective device-user association, employee display name, and CNIC may change. Oracle updates an existing row in place or inserts a missing row under the original event UID. ADD activates its materialized identity fields only after Oracle returns durable content proof. A job is complete only when the corrected raw row and the final employee/day projection are both verified and stale old-CNIC output is absent.

Membership and content proof remain separate:

- `ords_status` and its confirmation timestamp prove event-UID membership only;
- `identity_content_status`, `identity_content_confirmed_at`, and `identity_downstream_confirmed_at` prove corrected identity content and downstream convergence.

Ambiguous ownership, cross-device UID collisions, immutable-field mismatches, changed preconditions, invalid clock/event UID, and ORDS-rejected events remain locked. Identity-reuse events may proceed only through the v2 full-CNIC/name attestation path when current, frozen historical, UID/user-ID, CNIC, and ownership evidence all agree. There is no force/override path. Existing full-history reconciliation remains non-destructive; both legacy Oracle delete statements remain permanently gated by `WHERE 1 = 0`.

## Feature gates and limits

Both gates default to `false`:

```text
ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED=false
ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED=false
ADD_ATTENDANCE_REPAIR_LEGACY_ADMISSION_ENABLED=false
```

Execution cannot start unless preview is enabled and the ADD ORDS URL plus the dedicated
`ADD_ATTENDANCE_REPAIR_ORDS_USERNAME` / `ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD` credential are
configured. The generic `ADD_ORDS_USERNAME` / `ADD_ORDS_PASSWORD` connector credential is never
sent to an identity-repair route. The server enforces:

- one terminal and exactly one current employee per v2 release;
- legacy jobs already in progress retain their original 1–500 employee shape;
- no more than 250,000 frozen events;
- 100 Oracle items per transaction;
- two Oracle correction requests globally;
- 15-minute immutable preview validity;
- database leases and bounded exponential retry.

Prepare records the exact selected-event manifest, any explicit all-filtered omissions, the
candidate-set membership digest, filters, frozen target identity, and full source certificate.
Exact item/cohort membership is built by the repair worker inside a database savepoint, including
when source coverage is already current; a rejected large selection therefore cannot leave a
partial preview. The event cap applies to selected punches. Full source cohorts remain certified
without allowing unrelated terminal history to consume that cap.
Retryable Oracle preview failures (transport, 408, 429, and 5xx) back off durably before they
require operator attention.

Larger review sets must be split into Pakistan-time date ranges. The server converts each inclusive Pakistan date range to a UTC half-open interval. It does not truncate a selected source cohort or silently add a newly discovered punch to an existing selection.

## Production prerequisites

Do not enable preview or execution until all of these are true:

1. PostgreSQL and Oracle backups exist and their restore procedures have been tested.
2. The production Oracle raw table and its event-UID index have been inventoried.
3. There are no duplicate non-null event UIDs.
4. `EVENT_UID` has a single-column unique index.
5. Oracle uses `AL32UTF8`, which is required for byte-identical ADD/Oracle payload digests.
6. `SLIC_ZKT_RECOMPUTE_DAILY_FLAGS` is valid and has been tested for both old and new CNIC/day groups.
7. The real `DATASYNC` consumer is understood, including how it removes or updates an old-CNIC projection.
8. A site-specific idempotent `SLIC_ZKT_DOWNSTREAM_REPAIR` implementation and `SLIC_ZKT_REPAIR_DOWNSTREAM_STATUS` verification view have been reviewed and tested. The status view may return `T/T` only when the final projection is correct and stale old identity is absent.
9. The dedicated ADD Oracle username/password is distinct from connector and fleet credentials. Connector/fleet credentials must receive `401` from all identity-repair routes.
10. ADD, Oracle, and the downstream consumer have synchronized operational time.

The checked-in [`20260827_downstream_adapter_contract.sql`](../deploy/add/oracle/20260827_downstream_adapter_contract.sql) implements the verified production chain from `HR_RAW_ATTN_CAPTURE_EVENTS` to `HR_EMPLOYEE_ATTENDANCE`. Installation is additive and does not call a repair procedure. Runtime repair remains fail-closed for leave, payroll, override, manual, ambiguous, or otherwise protected attendance days.

## Oracle deployment ceremony

Run all scripts from a protected local copy. Never commit substituted credentials or spool output containing protected production structure.

1. Take an Oracle backup and retain the current package source and ORDS metadata.
2. Run [`20260827_identity_repair_preflight.sql`](../deploy/add/oracle/20260827_identity_repair_preflight.sql) with spooling enabled.
3. Review every object and scheduler job reported as referencing `DATASYNC` or `HR_RAW_ATTN_CAPTURE_EVENTS`.
4. Deploy and test the site-specific downstream adapter and status view.
5. In a protected local copy of [`20260827_identity_repair_contract.sql`](../deploy/add/oracle/20260827_identity_repair_contract.sql), replace only:
   - `REPLACE_WITH_ADD_API_USERNAME`;
   - `REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX`.
6. Deploy the package contract. It validates package compilation and unreplaced placeholders. On failure it restores the previously valid package, or drops a failed first installation.
7. In **APEX → SQL Workshop → RESTful Services**, open the existing `raw_attendance_capture` module and define or verify these four ADD-only resource templates and PL/SQL handlers:
   - `GET raw-captures/identity-repairs/capabilities`;
   - `POST raw-captures/identity-repairs/check`;
   - `POST raw-captures/identity-repairs`;
   - `POST raw-captures/identity-repairs/status`.
   Their handler sources must call only `SLIC_ZKT_IDENTITY_REPAIR_API.GET_CAPABILITIES`, `POST_CHECK(:body_text)`, `POST_REPAIR(:body_text)`, and `POST_STATUS(:body_text)` respectively. The checked-in contract's ORDS block represents the same RESTful Services metadata for guarded scripted deployments.
8. Require capabilities to report contract version `1`, ADD-only authentication, content preconditions, operation replay, raw verification, downstream verification, old-identity absence verification, and a batch limit of 100.
9. Confirm a connector credential and fleet credential cannot call any route.
10. Exercise same-operation/same-payload replay and same-operation/different-payload rejection in a non-production table or approved synthetic record.

The correction endpoint intentionally commits before serializing its response. If the response is lost, ADD queries status by operation ID and forward-completes from the durable Oracle receipt.

## ADD deployment

1. Keep both feature gates disabled.
2. Run the normal transactional deployment. Migration `20260827_0021` creates the durable repair engine; additive migration `20260904_0023` adds exact selections, workflow versioning, source/selection manifests, selected cohort counts, reuse attestations, and release-queue indexes.
3. Verify `/health/ready`, sign-in, CSRF protection, password step-up, and all three Attendance views while the gate is dark.
4. Confirm the repair worker heartbeat is updating and that no new Oracle operation can be claimed while execution is disabled.
5. Set preview enabled and execution disabled. Restart through the normal deployment process; do not edit a running container.

Store the dedicated identity-repair username and password as `add-production` GitHub environment
secrets named `ADD_ATTENDANCE_REPAIR_ORDS_USERNAME` and
`ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD`. The deployment refuses preview when either is missing, when
the pair matches the connector credential, or when the authenticated capability probe is not ready.

Never use Alembic downgrade to roll back a production database containing repair jobs, releases, attestations, or identity revisions. Restore the paired PostgreSQL and Oracle backups instead.

## Preview-only validation

Use one certified terminal with a complete stable user snapshot.

1. Confirm **All events** remains chronological and keeps `ords_status` unchanged while showing a separate effective release state.
2. Confirm only `BLOCKED_IDENTITY` and `QUARANTINED_IDENTITY_REUSE` appear in **Needs review**. Invalid-time, invalid-event-UID, ambiguous, non-OK-clock, and ORDS-rejected punches must stay locked.
3. Query a known active employee and confirm the UI shows only masked CNIC and masked source identifiers. A missing-CNIC employee must remain visible and disabled, with **Add CNIC** linking to Users.
4. Confirm the punch table starts with nothing selected. Exercise explicit selection and all-filtered selection with exclusions across multiple pages; newly arriving punches must not enter either frozen selection.
5. Compare the candidate membership, selected and omitted manifests, source certificate, full-cohort certificate, event count, affected employee-days, and Oracle classifications with independent read-only queries.
6. Confirm unsafe Oracle classifications are explained and excluded while safe `MATCH`, `MISSING`, and `MISMATCH` items can proceed. A fully unsafe job must end **Completed with attention** without an Oracle mutation.
7. For reuse, confirm wrong CNIC, wrong current/historical name, competing UID/user-ID/CNIC owner, unstable snapshot, and unresolved duplicate CNIC all reject approval without storing plaintext proof.
8. Wait beyond 15 minutes and confirm approval is rejected as expired. Change one target, candidate membership, source cohort, event, or snapshot and confirm approval is rejected as drift.
9. Confirm no Oracle repair receipt, ADD identity revision, or raw-row change was created during preview-only validation.

Source freshness gates the start of membership freeze. If a large read-only Oracle classification
outlasts that age window, elapsed time alone does not invalidate it; approval still requires the
same source certificate, terminal cursor/parity, identity snapshot, exact cohorts, and immutable
event facts.

## Controlled execution rollout

Enable execution only after preview validation and an approved change window.

1. One synthetic punch.
2. One known real blocked punch on one terminal.
3. One release with 10 punches for one employee.
4. One release with 50 punches for one employee.
5. Widen use only after every prior gate passes; keep the exact 250,000 selected-punch ceiling.

For each stage, retain the evidence JSON and independently require:

- unchanged physical event UID, timestamp, source user ID, raw punch, and terminal serial;
- no duplicate event UID and no unexplained raw-row count variance;
- corrected Oracle name/CNIC content under the same event UID;
- `DATASYNC=0` handled by the known consumer or dedicated downstream procedure;
- old and new CNIC/day check-in and check-out flags recomputed correctly;
- final downstream employee/day projection correct;
- no stale old-CNIC projection;
- ADD effective identity revision active only after Oracle content proof;
- evidence certificate reports `valid=true`.

Stop immediately on any immutable change, content mismatch, cross-device collision, stale downstream identity, stuck lease, package/capability/authentication failure, unexplained count difference, audit-chain failure, or PII in logs/alerts/browser events.

## Operator procedure

1. Open **Attendance → Needs review** and find one employee/terminal group.
2. Check its precise lock/eligibility reason, stable current snapshot, saved CNIC, source certification, feature gates, and worker health.
3. Open the employee, apply optional Pakistan date/status/punch/source filters, and explicitly select individual punches or choose **Select all eligible matching filters**. Selection never crosses employee or terminal boundaries.
4. Prepare the release and review selected, safe, unsafe/excluded, ordinary, reuse, omitted-by-operator, and affected employee-day counts. Review every unsafe explanation.
5. If safe reuse is included, re-enter the full CNIC and authoritative employee name exactly. These values are verified transiently and never logged or stored in plaintext.
6. Enter a 10–500 character reason, current administrator password, and the exact server-generated confirmation phrase containing the preview digest prefix.
7. After approval, monitor **Attendance → Release history** through Oracle check/apply/verify, ADD activation, and downstream verification. Use pause/resume/cancel/retry only with an audited reason and password step-up.
8. Download the evidence JSON after completion and verify its repair ledger and certificate.

Cancellation before the first Oracle request cancels the draft. After execution starts, cancellation affects only untouched `ORACLE_APPLY` items. Oracle-committed or unknown-outcome operations always forward-complete; the system never attempts an unsafe identity rollback.

## State and recovery guide

| State/error | Meaning | Operator action |
| --- | --- | --- |
| `PREPARING_SOURCE` | A full-device source dependency or Oracle preview classification is running | Wait; inspect the linked reconciliation if held |
| `AWAITING_APPROVAL` | Immutable preview is valid for 15 minutes | Review and approve, or let it expire |
| `WAITING_ORACLE` / `ORACLE_VERIFY` | An operation may have committed and its response is unknown | Restore ORDS; do not edit Oracle rows or create a replacement job |
| `WAITING_DOWNSTREAM` | Raw content is verified; final employee/day projection is pending | Inspect the downstream consumer/adapter; do not mark complete manually |
| `PAUSED` | New untouched work is paused | Oracle-committed work may still forward-complete |
| `NEEDS_ATTENTION` | Job-level capability/auth/source issue or retryable held work | Correct the cause, then use **Retry safe work** |
| `COMPLETED_WITH_ATTENTION` | Safe items completed; one or more events remained unchanged/review-only | Export evidence; investigate exclusions independently |
| `IMMUTABLE_*`, `CROSS_DEVICE_UID_COLLISION`, `CONTENT_PRECONDITION_MISMATCH` | Safety proof failed | Do not retry as an override; inspect source/Oracle ownership |
| `ORDS_AUTHENTICATION_FAILED` / `ORDS_CAPABILITY_MISSING` | Contract cannot be trusted | Disable new execution, repair package/auth, then replay by operation ID |
| `RETRY_EXHAUSTED` | Automatic bounded retry stopped | Confirm the external condition is fixed, then retry safe work |

Successful employees remain complete when another employee needs review.

## Monitoring and alerts

The authenticated overview, repair preflight, and repair job-list responses expose PII-free worker telemetry:

- active jobs and oldest job age;
- oldest v2 blocked-punch queue age plus preparing, awaiting-approval, and execution age;
- review items;
- v2 Oracle exclusions grouped by code and identity-reuse approval failures grouped by code;
- retrying items and unknown Oracle outcomes;
- retry-exhausted v2 jobs;
- downstream wait count and oldest downstream lag;
- stale item leases and leased Oracle slots;
- durable worker heartbeat state/timestamps/error code.

Alert on:

- missing/stale repair worker heartbeat;
- any unknown outcome older than the Oracle timeout plus lease period;
- downstream lag outside the approved service objective;
- stale leases;
- increasing review or retry-exhaustion backlog;
- increasing blocked-punch queue age or reuse-attribution failure rate;
- `ATTENDANCE_REPAIR_NEEDS_ATTENTION`;
- Oracle authentication/capability loss.

Alerts, logs, realtime browser events, audit rows, and job metadata must contain IDs, states, counts, and digests only—never plaintext CNIC or protected employee name. HTTP response bodies from ORDS must not be logged.

## Outage and rollback rules

During an Oracle outage, general ADD and live source preservation remain available. Release operations wait durably. Disable `ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED` to stop admission of new Oracle mutations; leave the worker running so operations that may already have committed, known receipts, ADD activation, and downstream verification can forward-complete.

Application rollback:

1. Disable execution first.
2. Allow or inspect all `ORACLE_VERIFY`, `ADD_ACTIVATE`, and `DOWNSTREAM_VERIFY` items.
3. Roll back application images through the normal transactional deployment only after no old binary could abandon forward-completion.
4. Preserve all repair tables and evidence.

Oracle rollback:

1. Stop new execution in ADD.
2. Resolve every in-flight operation by ID.
3. Restore the previously captured package and ORDS metadata if required.
4. Do not reverse committed identity corrections by changing punch facts or deleting rows. Any further identity change must be a new audited repair revision.

The Oracle receipt table and ADD evidence ledger are retained. Their presence is evidence, not a reason to delete history.
