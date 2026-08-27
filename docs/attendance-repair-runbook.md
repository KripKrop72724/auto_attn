# Employee attendance repair and resync runbook

This runbook governs the terminal-scoped **Reconciliation → Employee repair** workflow. It is a correction of effective identity attribution, not a rewrite of physical attendance history.

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

Ambiguous ownership, UID reuse, cross-device UID collisions, immutable-field mismatches, or changed preconditions are `NEEDS_REVIEW`. There is no force/override path. Existing full-history reconciliation remains non-destructive; both legacy Oracle delete statements remain permanently gated by `WHERE 1 = 0`.

## Feature gates and limits

Both gates default to `false`:

```text
ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED=false
ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED=false
```

Execution cannot start unless preview is enabled and the ADD ORDS URL and credentials are configured. The server enforces:

- one terminal per job;
- 1–500 current employees;
- no more than 250,000 frozen events;
- 100 Oracle items per transaction;
- two Oracle correction requests globally;
- 15-minute immutable preview validity;
- database leases and bounded exponential retry.

Prepare only records the durable request and frozen target identity. Exact cohort/event
membership is built by the repair worker inside a database savepoint, including when source
coverage is already current; a rejected large cohort therefore cannot leave a partial preview.
Retryable Oracle preview failures (transport, 408, 429, and 5xx) back off durably before they
require operator attention.

Larger histories must be split into Pakistan-time date ranges. The server converts each inclusive Pakistan date range to a UTC half-open interval. It does not truncate a cohort.

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
2. Run the normal transactional deployment. Migration `20260827_0021` is additive and creates the durable job, target, cohort, item, identity-revision, Oracle-receipt, hash-ledger, audit-chain-head, Oracle-slot, and worker-heartbeat records.
3. Verify `/health/ready`, sign-in, CSRF protection, password step-up, and the employee-repair tab while the gate is dark.
4. Confirm the repair worker heartbeat is updating and that no new Oracle operation can be claimed while execution is disabled.
5. Set preview enabled and execution disabled. Restart through the normal deployment process; do not edit a running container.

Never use Alembic downgrade to roll back a production database containing repair jobs or identity revisions. Restore the paired PostgreSQL backup instead.

## Preview-only validation

Use one certified terminal with a complete stable user snapshot.

1. Query a known employee with no intended correction.
2. Confirm the UI shows only masked CNIC and masked source identifiers.
3. Confirm candidate cohorts show exact device-user, source UID/user ID, evidence class, first/last time, count, and source evidence.
4. If coverage is stale or incomplete, confirm ADD creates one existing full-device reconciliation dependency. The ZKT protocol cannot scan one employee; the dependency may ingest missing punches for other employees.
5. After certification, confirm ADD rebuilds the final employee preview and that newly discovered events are included.
6. Compare frozen event count, cohort digest, source certificate, and Oracle classifications with independent read-only queries.
7. Wait beyond 15 minutes and confirm approval is rejected as expired.
8. Change one target, source cohort, event, or snapshot and confirm approval is rejected as drift.
9. Confirm no Oracle repair receipt, ADD identity revision, or raw-row change was created.

Source freshness gates the start of membership freeze. If a large read-only Oracle classification
outlasts that age window, elapsed time alone does not invalidate it; approval still requires the
same source certificate, terminal cursor/parity, identity snapshot, exact cohorts, and immutable
event facts.

## Controlled execution rollout

Enable execution only after preview validation and an approved change window.

1. One synthetic employee/event.
2. One known real correction on one terminal.
3. A job with 10 employees.
4. A job with 50 employees.
5. Open the existing 500-employee limit only after every prior gate passes.

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

1. Open **Reconciliation → Employee repair** and select one terminal.
2. Check preview and execution gates, worker health, Oracle capabilities, stable snapshot, and source certification.
3. Select up to 500 eligible current employees. An unresolved duplicate-CNIC employee is not eligible.
4. Choose all dates or an inclusive Pakistan date range.
5. Build candidates. Current lineage is included; historical aliases require explicit selection. Do not select a cohort based only on name, CNIC suffix, or user ID similarity.
6. Freeze the preview. If ADD must reconcile the full device first, wait for the dependency and review the rebuilt final preview.
7. Review exclusions, Oracle classifications, affected events/days, and the preview digest.
8. Enter a non-PII audited reason, complete administrator password step-up, and type the exact confirmation phrase.
9. Monitor the durable per-employee and per-event ledger through Oracle check/apply/verify, ADD activation, and downstream verification.
10. Download the evidence JSON after completion and verify its certificate.

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
- review items;
- retrying items and unknown Oracle outcomes;
- downstream wait count and oldest downstream lag;
- stale item leases and leased Oracle slots;
- durable worker heartbeat state/timestamps/error code.

Alert on:

- missing/stale repair worker heartbeat;
- any unknown outcome older than the Oracle timeout plus lease period;
- downstream lag outside the approved service objective;
- stale leases;
- increasing review or retry-exhaustion backlog;
- `ATTENDANCE_REPAIR_NEEDS_ATTENTION`;
- Oracle authentication/capability loss.

Alerts, logs, realtime browser events, audit rows, and job metadata must contain IDs, states, counts, and digests only—never plaintext CNIC or protected employee name. HTTP response bodies from ORDS must not be logged.

## Outage and rollback rules

During an Oracle outage, general ADD and live source preservation remain available. Repair operations wait durably. Disable `ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED` to stop new Oracle mutations; leave the worker running so known receipts, ADD activation, and downstream verification can forward-complete.

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
