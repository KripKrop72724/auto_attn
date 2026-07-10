# Attendance Device Dashboard (ADD)

ADD is the command, control, and surveillance surface for ESP32–ZKT pairs. The browser UI is
published on port `8095`; the API and outbound ESP32 control gateway are published on port `8096`.
The connector initiates every internet connection, so no inbound branch firewall rule is required.

## Production model

- PostgreSQL is the durable source for devices, encrypted identities, attendance, commands,
  temporary administrator leases, telemetry, alerts, and the hash-chained audit ledger.
- Redis is reserved for horizontally scaled realtime fan-out and rate limits. A single API worker
  is used until that fan-out adapter is enabled, preventing split-brain WebSocket ownership.
- The ESP32 keeps live attendance registration open but does not perform HTTP uploads on the ZKT
  socket task. ORDS delivery drains from a durable local outbox on a separate task.
- Reconciliation starts only after a two-minute recovery stability window. Every 15 minutes the
  ESP compares the terminal's attendance-count delta with live events already captured. A matching
  delta completes as a light sync; a mismatch, counter reset, first boot, or six-hour truth window
  triggers the full table read. This retains gap detection without repeatedly pulling tens of
  thousands of rows from older models.
- ORDS and ADD have independent flash-backed outboxes. ADD attendance is removed only after an
  application-level WebSocket acknowledgement, so either destination may be unavailable without
  losing the other delivery path. Backend reconnect also republishes a complete user snapshot.
- Terminal restarts are scheduled at 02:00, 12:00, and 22:00 Pakistan time. An active enrollment
  lease blocks restart.

## Intermittent and older terminals

Connector health and terminal health are independent. A live ESP32 can report a ZKT terminal as
`SUSPECT`, `RETRY_WAIT`, `FLAPPING`, `RECOVERING`, or `ONLINE`. One failed probe does not create a
hard outage. Repeated transitions enter a five-minute jittered quiet period; full subnet discovery
is limited to once per 15 minutes. The last authenticated IP opens the live session directly, so a
healthy steady-state boot needs one authenticated connection rather than a probe followed by a
second connection. The ESP32 is not rebooted because a terminal is unavailable.

After recovery, live capture is restored before any full reconciliation. Three consecutive healthy
observations and a two-minute stable session clear the flapping alert. The dashboard retains the
transition history and shows the next retry time.

## Enrollment safety

Only a certified 72-byte ZKT user record is writable. Legacy 28-byte records have eight-character
names and cannot preserve the required CNIC suffix, so they remain read-only. A temporary elevation:

1. requires the dashboard administrator password;
2. verifies the user is currently regular and present;
3. writes privilege `14` and verifies the ZKT acknowledgement;
4. records the 10-minute deadline in ESP32 NVS;
5. writes privilege `0` at expiry even if ADD is unreachable; and
6. retries and raises a critical overdue alert if the ZKT itself is powered off.

## Deployment

Create a root `.env.add` from `.env.add.example`, then run:

```sh
./deploy/add/deploy.sh
```

On the Windows production runner, use `deploy/add/deploy.ps1`. The runner service needs Docker
named-pipe access once; from an Administrator PowerShell on that server run:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\add\grant-runner-docker-access.ps1
```

The script verifies that the Actions service uses `NETWORK SERVICE`, grants that service account
Docker named-pipe access, starts the Docker service when needed, and restarts the Actions runner so
the new token membership takes effect. Windows does not permit nesting the runner-created local
group inside the machine-local `docker-users` group. On a shared server, prefer reconfiguring the
runner under a dedicated service account and add only that account to `docker-users`.

The self-hosted GitHub runner first checks the repository variable `ADD_ENV_FILE`, then its default
`$HOME/.config/auto-attn/add.env` path, and finally the encrypted repository secret
`ADD_ENV_FILE_CONTENT`. The temporary workspace copy is deleted after every deployment attempt.
Public DNS/TLS should reverse proxy the two domains as shown in `deploy/add/Caddyfile.example`.
The production routes are expected to terminate TLS before forwarding
`attendancedevices.slichealth.com` to `127.0.0.1:8095` and `autoattn.slichealth.com` to
`127.0.0.1:8096`. Preserve WebSocket upgrade headers and do not expose PostgreSQL or Redis.

Back up PostgreSQL with `deploy/add/backup.sh`; schedule it outside the application container and
copy the encrypted output off-host.
