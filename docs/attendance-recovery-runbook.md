# Attendance recovery runbook

Attendance recovery is append-only. The source manifest, Hikvision evidence,
and existing attendance rows are never edited or deleted by this workflow.
Recovery first freezes a server-side candidate digest, then requires a fresh
preview, an administrator step-up, and a typed confirmation. Each worker item
revalidates its state digest before it can touch the idempotent ORDS outbox.

## Rollout gates

Keep all three flags disabled during migration. Enable them in this order:

1. Set `ADD_ATTENDANCE_RECOVERY_PREVIEW_ENABLED=true` and keep execution and
   source correction disabled. Compare the read-only summary with the database
   inventory for one ZKT and one Hikvision terminal.
2. Set `ADD_ATTENDANCE_RECOVERY_EXECUTION_ENABLED=true` and restrict
   `ADD_ATTENDANCE_RECOVERY_ALLOWED_ZONES` to one canary zone. Use **Retry safe
   delivery**, **Rebuild missing outbox**, or **Recover stale in-flight** only
   after the preview counts match the intended scope.
3. Monitor job leases, retry success, duplicate event UIDs, unknown statuses,
   and the age of the oldest outbox row. Expand the zone allow-list only after
   the canary is stable.
4. Enable `ADD_ATTENDANCE_SOURCE_CORRECTION_ENABLED=true` only after the
   canary demonstrates that every derived event retains source digest,
   terminal/epoch lineage, operator reason, and an immutable correction row.

The recovery lease must be at least twice `ADD_ORDS_TIMEOUT_SECONDS`. The
production settings validator enforces this and prevents execution without
preview mode.

## Operator lanes

- **Safe delivery** contains missing outbox rows, pending events, retryable
  transport failures, and expired in-flight leases. Preview and approve a
  frozen batch; the worker queues each event through the existing one-row
  outbox contract.
- **Identity held** is read-only. Exact terminal serial, UID, user ID,
  snapshot revision, active state, and authoritative CNIC evidence must agree
  before a held event can be reclassified into the safe lane. Name similarity
  and a reused user ID are never sufficient.
- **Source correction** accepts only a single parseable identity with a
  timestamp-only defect. Enter an explicit timezone-aware timestamp, preview
  the row-level exclusions, and approve the derived event batch. The original
  ZKT manifest or Hikvision observation remains unchanged.
- **Permanent review** includes unknown states, invalid UIDs, source
  conflicts, missing terminal provenance, and ORDS payload rejection. Inspect
  evidence or mark it reviewed through the existing audited source-evidence
  controls; it is never automatically retried.

## API workflow

Use `GET /api/v2/attendance-recovery/summary` for counts, then POST a preview
with immutable filters. The response returns `candidate_digest`, an expiry,
and a server signature. Submit that exact digest, signature, expiry,
idempotency key, reason, password, and typed confirmation to create a durable
job. Poll the job and its items; pause, resume, cancel, or retry only with the
job digest and a confirmation such as `PAUSE <job_id>`.

Timestamp correction uses the matching source-correction preview and job
endpoints. Hikvision protected evidence can be inspected through the audited
source-correction evidence detail/reveal endpoints; reveal responses are
marked `no-store`.

## Safe production checks

Before enabling a larger zone scope, run the ADD backend and frontend tests,
OpenAPI contract check, migration smoke test, frontend build and bundle budget,
and the production deployment health probes. If a job reports state drift,
identity hold, or source review, leave the item preserved and create a new
preview after the underlying evidence has been reviewed.
