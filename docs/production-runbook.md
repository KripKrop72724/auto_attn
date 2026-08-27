# Production deployment, rollback, and operations

## Server contract

The production host is the Windows machine carrying the repository self-hosted Actions runner.
Docker publishes:

- dashboard: `0.0.0.0:8095` → `https://attendancedevices.slichealth.com`;
- API/device gateway: `0.0.0.0:8096` → `https://autoattn.slichealth.com`.

PostgreSQL and Redis remain on the private Compose bridge. The TLS reverse proxy must preserve
WebSocket upgrade headers and forward `X-Forwarded-Proto`. `deploy/add/Caddyfile.example` documents
the domain mapping.

## One-time runner preparation

From an elevated PowerShell on the server:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\add\grant-runner-docker-access.ps1
```

Confirm the runner service is running as `NT AUTHORITY\NETWORK SERVICE`, that this SID is a direct
member of `docker-users`, Docker Desktop/service is running, and the runner can execute
`docker info`. Restart the runner after group membership changes.

Create the production environment outside the checkout. Preferred path:

```text
C:\ProgramData\StateLife\AttendanceDeviceDashboard\config\add.env
```

Alternatively configure repository variable `ADD_ENV_FILE` with an absolute protected path, or
encrypted secret `ADD_ENV_FILE_CONTENT` for initial bootstrap. The deploy script persists the
validated copy under ProgramData with ACLs limited to SYSTEM, Administrators, and NETWORK SERVICE.
For an existing environment that predates automatic onboarding, configure the repository secret
`ADD_FLEET_ROOT_SECRET` separately. The runner injects only that value into the protected file and
preserves the existing PostgreSQL and PII secrets. Configure the `add-production` environment
secrets `ADD_ORDS_USERNAME` and `ADD_ORDS_PASSWORD`; every deployment injects those approved Oracle
credentials into the protected file so a stale host copy cannot silently restore an obsolete
verifier.
The optional `ADD_ADMIN_PASSWORD_HASH` repository secret similarly enforces the approved Argon2id
administrator verifier without placing the password in the workflow or checkout.
Zone Lite 2.5.0 COMM Key management additionally requires the `add-production` environment secret
`ADD_COMM_KEY_SECRET_FERNET_KEY`. Generate and escrow it separately from `ADD_PII_FERNET_KEY`; the
deployment rejects key reuse. Keep repository variables `ADD_COMM_KEY_MANAGEMENT_ENABLED` and
`ADD_COMM_KEY_REVEAL_ENABLED` false until the corresponding staged release gates are approved.

Required values are listed in `.env.add.example`. Generate unique random PostgreSQL, lookup, fleet,
and ORDS secrets; generate the Fernet key using `Fernet.generate_key()`; store only an Argon2id hash
for the administrator password. `ADD_ADMIN_COOKIE_SECURE` must remain true and the public device URL
must be `wss://autoattn.slichealth.com/device/v2/stream`. Single-quote env values—especially the
Argon2id hash—so Compose does not interpret dollar delimiters. The deployment script canonicalizes
the protected copy and rejects values that cannot be represented safely.

## CI and release gate

**ADD CI** is required before deployment. It enforces:

- repository consolidation and State Life palette contract;
- current-worktree Gitleaks scan;
- Python lint, 28+ backend/firmware contracts, PostgreSQL migration and drift check, package build;
- dashboard tests, TypeScript compile, and optimized build;
- encrypted-NVS ESP-IDF 5.5.3 build;
- Compose config, image build, migrations, independent UI/API health, and full-stack smoke test.

**Deploy ADD** is triggered only by successful main CI and deploys `workflow_run.head_sha`, never a
moving branch. A manual dispatch may name an exact SHA.

If repository visibility must temporarily be public for the self-hosted Actions billing/runner
arrangement, the release operator must use this fail-safe order:

1. delete obsolete Actions runs and artifacts;
2. record that the repository is private;
3. change visibility to public immediately before the tested push/dispatch;
4. wait for ADD CI, Deploy ADD, and public verification to finish;
5. restore private visibility in a `finally` operation even when CI/deploy fails;
6. query the repository API and record proof that `visibility=private`.

The workflow intentionally does not grant itself repository-administration permission.

## Transactional deployment

`deploy/add/deploy.ps1` performs the following without printing environment values:

1. verifies checkout SHA and required production settings;
2. makes a read-only authenticated `raw-captures/check` request from the ADD host;
3. protects `C:\ProgramData\StateLife\AttendanceDeviceDashboard` ACLs;
4. backs up the previous protected env and tags both current application images;
5. starts durable dependencies and writes a binary-safe PostgreSQL custom-format dump;
6. validates Compose, pulls bases, and builds the exact checkout;
7. starts the stack and waits for Compose health, API readiness, and independent UI health;
8. repeats the authenticated membership probe inside the running API container, using the exact
   environment visible to the worker;
9. records commit, Alembic revisions, image IDs, env hash, and backup path—never secrets;
10. keeps fourteen days of local backups.

An Ubuntu runner then verifies TLS and both public domains. A deployment is reported successful only
when both authenticated Oracle gates and the local/public application checks pass.

## Automatic rollback

If build, migration, startup, or local health fails, the script stops the new UI/API. If Alembic
changed, it terminates application sessions, recreates the database, and restores the pre-deploy
dump. It retags the prior UI/API images, starts them with `--no-build`, and checks readiness. The
workflow still reports failure, making the rollback visible.

If no prior complete image set exists, a failed first deployment is stopped rather than pretending
to be rolled back. Manual recovery uses the dump and release metadata in:

```text
C:\ProgramData\StateLife\AttendanceDeviceDashboard\backups
C:\ProgramData\StateLife\AttendanceDeviceDashboard\releases
```

Never run Alembic downgrade for the protected-data contract revision; restore its paired backup.

## Routine verification

After every release, verify:

- the expected Zone Lite firmware version is reported after any required physical flash;
- the ADD-reported firmware version exactly matches the ESP-IDF serial boot descriptor;
- any required full truth cycle ends with ORDS `status=200 ok=true`, durable ADD reconcile enqueue,
  zero blocked/skipped identities, and `complete=true`;
- a complete user refresh has populated raw-record fingerprints before editing a legacy user whose
  displayed user ID contains replacement question marks;
- the target UID, terminal user count, and terminal attendance count are unchanged before retrying
  a failed legacy-record edit.

Employee attendance repair has independent dark-launch gates and an Oracle/downstream deployment
ceremony. Both gates remain false by default. Do not enable execution based on a successful generic
ORDS membership probe: first complete the inventory, unique-event-UID, ADD-only authentication,
operation replay, daily-flag, downstream stale-identity-removal, and controlled 1/10/50 employee
gates in the [Employee attendance repair and resync runbook](attendance-repair-runbook.md).

```text
http://127.0.0.1:8095/health/ui
http://127.0.0.1:8096/health/ready
https://attendancedevices.slichealth.com/health/ui
https://autoattn.slichealth.com/health/ready
```

Then sign in, select a live connector, confirm live log movement, ZKT time sampling, user snapshot,
attendance filters, alert rendering, and next restart. Do not perform a user write until the device
is certified writable and the snapshot is complete.

The Fleet view exposes the backend ORDS backlog plus retrying, identity-blocked, and quarantined
counts. Delivery is claimed in small bounded batches with limited concurrency. Payload-level `400`,
`413`, and `422` responses are preserved as quarantined events so one poison row cannot block later
attendance; transport, authorization, endpoint, throttling, and server failures remain durably
retryable. ADD stores only a redacted failure category and HTTP status in its alert details, never an
ORDS response body or CNIC. Identity-blocked counts come from the immutable attendance ledger because
those rows intentionally do not enter the ORDS outbox until identity is repaired. The database permits
only one open alert per connector and condition code; repeated observations update that alert rather
than flooding the operations queue.

The Historical identity backlog separates deleted-user evidence from exact orphaned event cohorts.
An orphaned cohort is operator-actionable only when every preserved row has the same valid terminal
user ID, no linked device-user record, and either the same non-empty UID or one exact normalized
terminal name for legacy rows that predate UID capture. Its SHA-256 group token versions the complete
event membership and status. Saving authoritative HR
evidence requires an exact service-number match, compatible name, typed confirmation, audit reason,
and administrator re-authentication; any concurrent change makes the request stale. ADD creates only
a deleted identity tombstone and requeues the preserved rows. It never creates or changes a live
terminal user. Missing UIDs without one stable name, missing names without a UID, multiple names,
linked identities, malformed user IDs, duplicate active CNIC claims, and identity reuse remain
fail-closed. At ingestion, neither a current user nor a tombstone may supply a
CNIC unless both the terminal user ID and UID match exactly; user-ID reuse alone is never accepted.
When an older blocked row already carries one durable `device_user_id` that still points to an
active, conflict-free user with missing CNIC, ADD labels it `ACTIVE_USER_ENRICHMENT` and routes the
operator to the normal certified user-edit command. A verified terminal write then enriches every
row linked to that exact device-user record and requeues it; no historical alias is created.

Firmware-reported Oracle receipts are hints, not final delivery proof. ADD places every such event in
`FIRMWARE_RECEIPT_UNVERIFIED`, checks up to 500 immutable event UIDs at a time through
`raw-captures/check`, and promotes only Oracle-present rows to `ACKED_CHECK`. A missing UID is
automatically returned to `PENDING`, its provisional confirmation fields are cleared, and the
preserved event is delivered through the normal idempotent outbox. A membership-check outage leaves
the row visibly unverified with bounded backoff. Late firmware receipts cannot downgrade an
`ACKED`/`ACKED_CHECK` row. This audit also consumes legacy `ACKED_FIRMWARE` rows after deployment, so
historic false acknowledgements become self-repairing as soon as the Oracle route is available.
ADD also rechecks every independently confirmed UID at least once per day. A transient recheck
failure retains the last known proof while exposing a retrying membership state; if Oracle reports
the UID missing, ADD clears the stale confirmation and requeues its immutable event automatically.

`raw-captures/check`, `raw-captures`, and `raw-captures/reconcile` must accept the same approved ADD
credential. The canonical `SLIC_ZKT_TRUTH_API` stores only uppercase SHA-256 password digests. If a
legacy single-verifier Oracle deployment returns `200` from `check` but `401` from package-backed
endpoints, run `deploy/add/oracle/20260728_unify_raw_capture_auth.sql` while authenticated as the
owning schema. If the package is already the valid non-destructive dual-verifier version but the
approved ADD credential receives `401` from `raw-captures`, run
`deploy/add/oracle/20260728_allow_add_auth_non_destructive.sql` after replacing its two ADD
placeholders locally. That migration preserves both installed verifiers, adds a third SHA-256-only
ADD verifier, refuses unexpected package shapes, validates compilation, and restores the original
package body automatically on failure. Re-running the same substituted migration is idempotent.
If `raw-captures` is still a large legacy inline handler rather than a package wrapper, run
`deploy/add/oracle/20260728_route_live_capture_through_package.sql`. It backs up the exact handler,
requires the known legacy insert/delete/hash shape, replaces only its ORDS source with the
non-destructive `post_live` wrapper, verifies the installed metadata, and restores the original
handler automatically on failure. The migration performs no attendance table DML. Afterward, verify
membership and package-backed delivery with the same approved credential, then allow the preserved
outbox to drain. Do not start OTA while package-backed delivery returns `401`.

If package-backed live inserts return `ORA-00001` for
`UK_HR_RAW_ATTN_ONE_CHECKIN` or `UK_HR_RAW_ATTN_ONE_CHECKOUT`, run
`deploy/add/oracle/20260728_recompute_live_daily_flags_non_destructive.sql`. The package migration
inserts new normal punches with false/false flags, locks only affected employee-days, clears their
prior flags, then recomputes the earliest check-in and latest check-out across the complete stored
day in the same transaction. It never deletes attendance. Compilation or source-invariant failure
restores the exact prior package body. Verify with a known missing preserved event, then require the
ADD retry count to drain to zero before OTA.

Every deployment records credential-free DNS, TCP 443, and `OPTIONS` reachability from the Windows
runner host, the resolved Oracle destination addresses, the runner's public egress address, `OPTIONS`
reachability from the running API container, and boolean proxy-presence signals. The probe never sends
credentials or prints a response body. It is warning-only because a route outage must not roll back an
otherwise healthy ADD release. A degraded route remains visible as `ORDS_DELIVERY_FAILED`; queued
attendance is retained and retried with bounded backoff while network routing is repaired.

The earlier authenticated membership probes are different: they send the configured credentials
only to the canonical internal Oracle endpoint, submit one synthetic event UID, perform no table
DML, validate the bounded response contract, and never print a credential or response body. An HTTP
authorization failure is release-blocking and triggers the normal transactional rollback.

Interpret deployment probe results in this order:

- `DNS probe=OK` with `TCP 443 probe=ERROR_TIMEOUT` and both proxy booleans `False` means the
  production host or its upstream firewall/ACL is blocking outbound TCP 443. Permit the Windows host
  and Docker NAT network to reach `eclaim2.slichealth.com:443`, then rerun the deployment probe. Do not
  rotate ORDS credentials for this condition.
- `selected route=OK` with zero Windows ORDS block candidates and working general public egress
  narrows that timeout to the upstream outbound route. A nonzero candidate count is only a local
  firewall lead because application- and service-scoped block rules can share `Any:443`; inspect the
  matching rule locally without publishing rule names or network topology in CI logs.
- If both internal host and container probes return `HTTP_200` for `local.slichealth.com` while the
  public host times out, ADD uses the validated internal hostname. Keep remotely deployed Zone Lite
  devices on the public hostname unless their own route evidence requires otherwise. The deployment
  script canonicalizes the protected ADD environment to this internal endpoint so an older environment
  file cannot silently restore the unreachable public route.
- A successful host probe with a failed container probe points to Docker NAT or container-specific
  egress policy.
- HTTP `401`/`403` means transport is working and authentication should be checked without logging
  credentials or response bodies.
- HTTP `400`/`413`/`422` applies to individual payloads; ADD quarantines those rows and continues with
  newer attendance.

## Alert response

| Alert/state | Immediate action | Do not do |
| --- | --- | --- |
| ESP offline | Check branch power/internet; retain queued commands | Do not repeatedly restart ZKT |
| ZKT flapping/retry wait | Observe next retry and last good time; check LAN/power | Do not probe port 4370 manually in a loop |
| Duplicate serial quarantine | Find duplicate physical/config mapping; keep both read-only | Do not override write block |
| Duplicate user CNIC | Open Identity Review. If HR confirms one employee has multiple terminal records, use the audited same-employee resolution; otherwise correct the wrong CNIC on the specific user | Do not infer equality from a masked suffix, incomplete punch history, or name similarity alone; never delete punches |
| Partial/truncated snapshot | Wait for stable complete snapshot; inspect logs/memory | Do not create/edit/delete users |
| Admin revoke overdue | Keep command active; restore terminal reachability; verify privilege 0 | Do not grant a second lease |
| ORDS backlog | Check the configured ADD ORDS route/TLS/auth; preserve flash queue | Do not erase connector storage |
| Command retrying | Read pre/postcondition and ZKT state; allow bounded retry | Do not issue duplicates |
| Terminal clock drift | Confirm ESP SNTP, then controlled terminal correction | Do not alter stored punches |

## Twenty-four-hour observation gate

For the first production connector and after protocol changes, retain a 24-hour observation record:

- ESP online percentage and reconnect count;
- ZKT state transitions, flap quiet periods, and connection attempts per hour;
- live punches versus terminal count delta and reconcile outcome;
- ADD/ORDS outbox high-water marks and oldest age;
- command/lease results and any overdue revoke;
- heap low-water mark, resets, watchdogs, and flash journal recovery;
- scheduled restart slot completion exactly once per slot;
- public UI/API availability and database backup success.

Acceptance requires no lost/duplicate attendance, no tight retry loop, no unexplained ESP reset, no
unrevoked privilege, and bounded recovery after each simulated/observed outage.
