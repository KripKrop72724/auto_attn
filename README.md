# ZKTeco Attendance Fraud-Monitoring POC

[![CI](https://github.com/KripKrop72724/auto_attn/actions/workflows/ci.yml/badge.svg)](https://github.com/KripKrop72724/auto_attn/actions/workflows/ci.yml)
[![Shipping CD](https://github.com/KripKrop72724/auto_attn/actions/workflows/cd.yml/badge.svg)](https://github.com/KripKrop72724/auto_attn/actions/workflows/cd.yml)

This repository contains a greenfield Python POC with two runnable products:

- **Zone Agent**: local Windows/branch service that talks to ZKTeco devices over LAN, stores data in SQLite first, monitors device and PC clock tampering, records outages, and syncs queued records to head office.
- **Head Office**: central FastAPI server and dashboard that receives zone data, revalidates trust status, and reports attendance, outages, clock checks, and incidents.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
zk-head-office --host 127.0.0.1 --port 8080
zk-zone-agent --host 127.0.0.1 --port 7860
```

Then open:

- Zone Agent: <http://127.0.0.1:7860>
- Head Office: <http://127.0.0.1:8080>

Create a zone token from **Head Office → Zones**, then enter it once in the Zone Agent setup screen. Production sync is pinned to `https://head-office-production.up.railway.app`; localhost URLs are only allowed when `ZK_ZONE_ALLOW_DEV_HEAD_OFFICE_URLS=true`.

## Zone Device Discovery

The Zone Agent automatically scans detected private LAN subnets for TCP `4370` and persists candidates on the Scan page.

Saved devices start local monitoring immediately after Comm Key validation, even when the zone is not registered with head office yet. In that state the agent uses a local unregistered identity, captures live attendance, runs clock/fraud checks, records outages, and queues sync payloads. Once setup succeeds and a zone token is issued, pending local records are associated with the registered zone and uploaded.

For devices or firmware that do not reliably emit SDK live events, the worker also performs a monitored `get_attendance()` reconciliation during the 5-second clock-check loop. Newly appearing records are stored as `LIVE_POLL`, classified with the same live trust rules, and shown on the Live Attendance page through a 3-second local polling feed.

Comm Key brute force is available for owned devices but disabled by default:

```bash
ZK_ZONE_BRUTEFORCE_ENABLED=true zk-zone-agent --host 127.0.0.1 --port 7860
```

The brute-force flow is opt-in per candidate, requires local operator confirmation, refuses public IPs, and refuses already configured live devices unless explicitly allowed through the API.

## Shipping

The repository ships through GitHub Actions:

- `CI` runs lint, tests, and Python package build across Ubuntu/Windows and Python 3.11/3.12.
- `Shipping CD` builds Python distributions on Ubuntu and the Windows Zone Agent installer on `windows-latest`.
- Tag pushes matching `v*` publish a GitHub Release with the Python package and Windows installer artifacts.

To cut a release:

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Important Notes

- The zone agent stores mutable local state under `local-data/zone-agent` by default on non-Windows systems and under `C:\ProgramData\ZKZoneAgent` on Windows.
- `pyzk` is used through an adapter (`ZKClient`) so tests can run with fake devices and hardware access remains isolated.
- `pyzk` is GPL-2.0; review licensing before commercial redistribution.
- SQLite is intentional for branch/zone local state. Head Office production runs on Railway PostgreSQL.
