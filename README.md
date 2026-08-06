<p align="center">
  <img src="apps/add_frontend/public/state-life-logo.png" width="120" alt="State Life Insurance Corporation logo">
</p>

# State Life Attendance Device Dashboard + Zone Lite

[![ADD CI](https://github.com/KripKrop72724/auto_attn/actions/workflows/add-ci.yml/badge.svg)](https://github.com/KripKrop72724/auto_attn/actions/workflows/add-ci.yml)
[![Deploy ADD](https://github.com/KripKrop72724/auto_attn/actions/workflows/add-deploy.yml/badge.svg)](https://github.com/KripKrop72724/auto_attn/actions/workflows/add-deploy.yml)

This repository contains exactly two tightly coupled production products:

- **Zone Lite** — ESP32-S3 firmware that owns one ZKT terminal connection, captures live punches,
  reconciles gaps, survives intermittent terminal availability, and executes protected commands.
- **Attendance Device Dashboard (ADD)** — the State Life control plane for fleet health, live logs,
  attendance, alerts, device time, restarts, and rich user administration through Zone Lite.

There is no manual connector-registration screen. A provisioned ESP initiates a signed onboarding
request and appears automatically. The browser never connects directly to a ZKT terminal.

## Production endpoints

| Surface | Public endpoint | Host binding |
| --- | --- | --- |
| State Life dashboard | `https://attendancedevices.slichealth.com` | `0.0.0.0:8095` |
| ADD API and ESP gateway | `https://autoattn.slichealth.com` | `0.0.0.0:8096` |
| PostgreSQL and Redis | private Compose network only | no host port |

The initial administrator username is `StateHealthAdmin`. Its password exists only as an Argon2id
hash in the protected server environment; plaintext credentials are never committed.

## What is implemented

- Fleet overview with live ESP/ZKT state, serial-style logs, alerts, device clock, drift, sync state,
  next restart, and operator-triggered restart.
- Device-scoped user search, creation, name/CNIC/shift editing, deletion, and role display.
- Reversible, password-confirmed same-employee resolution for legacy duplicate-CNIC terminal records;
  no terminal user, fingerprint template, or historical punch is merged or rewritten.
- Ten-minute administrator leases with password step-up and ESP-enforced automatic revocation.
- Deleting a user removes only the ZKT identity. Attendance rows remain immutable and are checked
  before the command can report success.
- Certified 72-byte ZKT user records are writable; ambiguous, truncated, duplicate-serial, and
  older 28-byte records fail closed.
- Live punch capture has priority over reconciliation. Durable flash outboxes independently deliver
  to ADD and ORDS, and a 15-minute light reconcile closes event gaps without repeatedly dumping the
  terminal.
- Older terminals use bounded backoff, jitter, a two-minute recovery stability gate, and a
  five-minute quiet period after repeated flaps. The ESP never reboots merely because the ZKT is
  temporarily unavailable.
- Preventive terminal restarts run at 02:00, 12:00, and 22:00 Pakistan time, are persisted by slot,
  and are blocked while an administrator lease is active.
- Per-MAC onboarding secrets, signed nonces, rotating connector tokens, encrypted backend fields,
  encrypted ESP NVS, encrypted command journals, masked audits, and duplicate-terminal quarantine.
- State Life branding based only on `#0094DA`, white, and neutral shades, including supplied logo,
  favicons, maskable icons, and install manifest.

## Current Zone Lite release

The current firmware candidate is **Zone Lite 2.3.0**. It moves request-based start-of-time
reconciliation checkpoints and evidence to ADD, reads only bounded terminal ranges, resumes from the
last ADD-committed ordinal, and replaces certified legacy full scans with lightweight append-tail
audits. ADD commands no longer depend on free ESP flash, and preservation storage is never
auto-formatted. Identity-blocked attendance remains fail-closed without changing event UIDs or
deleting terminal/Oracle history.
Heartbeat and onboarding versions are derived from the built application descriptor so ADD always
reports the actual image version.

See [the ADD-owned reconciliation runbook](docs/add-owned-reconciliation.md) and
[the remote firmware update runbook](docs/zone-lite-remote-firmware-updates.md) for bootstrap, release,
campaign, rollback, and recovery procedures. See [the Zone Lite 2.1.11 release record](docs/zone-lite-2.1.11-release.md) for identity-snapshot upgrade notes,
validation evidence, and the required `complete=true` acceptance markers.

## Repository layout

```text
apps/add_backend/       FastAPI control plane, migrations, command worker
apps/add_frontend/      React dashboard and State Life assets
firmware/zone_lite/     ESP-IDF firmware and secure provisioner
deploy/add/             Windows/Linux deployment, backup, proxy, Oracle contract
docs/                   Architecture, security, operations, hardware acceptance
tests/                  Backend and firmware safety contracts
```

`scripts/check_repository_contract.py` makes this product boundary a CI invariant.

## Local validation

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check apps/add_backend tests scripts firmware/zone_lite/tools/provision_zone_lite.py
pytest -q tests/unit tests/firmware

cd apps/add_frontend
corepack enable
pnpm install --frozen-lockfile
pnpm test
pnpm run build
```

For a disposable full-stack smoke test, create random non-production keys with
`scripts/generate_ci_env.py .env.add`, run Compose, and destroy the volumes afterwards. Never use
that generator to create production credentials.

## Production deployment and provisioning

Production deployment is automatic only after **ADD CI** succeeds. The self-hosted Windows runner
checks out the exact tested SHA, reads a protected `.env.add`, backs up PostgreSQL/config/images,
deploys Compose, checks both local ports, and then an external runner verifies both TLS domains.

ESP provisioning derives independent onboarding and encrypted-NVS keys from the protected fleet
root. First provisioning may require a one-time, irreversible HMAC eFuse burn and therefore always
requires explicit approval for the exact detected MAC address. A normal firmware flash is not
equivalent to that approval.

Use these runbooks before operating production:

- [Architecture and behavior](docs/attendance-device-dashboard.md)
- [Zone Lite secure automatic onboarding](docs/zone-lite-secure-auto-onboarding.md)
- [Security model](docs/security-model.md)
- [Production deployment and rollback](docs/production-runbook.md)
- [ESP/ZKT hardware acceptance](docs/hardware-acceptance.md)
- [Zone Lite build and provisioning](firmware/zone_lite/README.md)
- [ADD-owned reconciliation](docs/add-owned-reconciliation.md)
- [Zone Lite remote firmware updates](docs/zone-lite-remote-firmware-updates.md)
- [Zone Lite OTA operator and AI-agent runbook](docs/zone-lite-ota-agent-runbook.md)
- [Zone Lite signing-key custody](docs/zone-lite-signing-key-custody.md)
- [Zone Lite 2.1.11 identity release record](docs/zone-lite-2.1.11-release.md)

Never commit `.env.add`, provisioning JSON, generated NVS images, terminal
credentials, ORDS credentials, Wi-Fi credentials, or fleet-root material.
