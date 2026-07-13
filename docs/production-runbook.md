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
preserves the existing PostgreSQL, PII, administrator, and ORDS secrets.
The optional `ADD_ADMIN_PASSWORD_HASH` repository secret similarly enforces the approved Argon2id
administrator verifier without placing the password in the workflow or checkout.

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
2. protects `C:\ProgramData\StateLife\AttendanceDeviceDashboard` ACLs;
3. backs up the previous protected env and tags both current application images;
4. starts durable dependencies and writes a binary-safe PostgreSQL custom-format dump;
5. validates Compose, pulls bases, and builds the exact checkout;
6. starts the stack and waits for Compose health, API readiness, and independent UI health;
7. records commit, Alembic revisions, image IDs, env hash, and backup path—never secrets;
8. keeps fourteen days of local backups.

An Ubuntu runner then verifies TLS and both public domains. A deployment is reported successful only
when local and public checks pass.

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

```text
http://127.0.0.1:8095/health/ui
http://127.0.0.1:8096/health/ready
https://attendancedevices.slichealth.com/health/ui
https://autoattn.slichealth.com/health/ready
```

Then sign in, select a live connector, confirm live log movement, ZKT time sampling, user snapshot,
attendance filters, alert rendering, and next restart. Do not perform a user write until the device
is certified writable and the snapshot is complete.

## Alert response

| Alert/state | Immediate action | Do not do |
| --- | --- | --- |
| ESP offline | Check branch power/internet; retain queued commands | Do not repeatedly restart ZKT |
| ZKT flapping/retry wait | Observe next retry and last good time; check LAN/power | Do not probe port 4370 manually in a loop |
| Duplicate serial quarantine | Find duplicate physical/config mapping; keep both read-only | Do not override write block |
| Partial/truncated snapshot | Wait for stable complete snapshot; inspect logs/memory | Do not create/edit/delete users |
| Admin revoke overdue | Keep command active; restore terminal reachability; verify privilege 0 | Do not grant a second lease |
| ORDS backlog | Check public ORDS/TLS/auth; preserve flash queue | Do not erase connector storage |
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
