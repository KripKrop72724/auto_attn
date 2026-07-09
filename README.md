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

- Zone Agent: <http://localhost:7860>
- Head Office: <http://127.0.0.1:8080>

Create a zone token from **Head Office → Zones**, then enter it once in the Zone Agent setup screen. Production sync is pinned to `https://head-office-production.up.railway.app`; localhost URLs are only allowed when `ZK_ZONE_ALLOW_DEV_HEAD_OFFICE_URLS=true`.

On Windows, open the Zone Agent through `http://localhost:7860` before enrolling Windows Hello admin unlock. PIN and fingerprint unlocks are WebAuthn credentials scoped to `localhost`; the app does not need or store Windows, Outlook, or Microsoft account passwords.

## Zone Device Discovery

The Zone Agent automatically scans detected private LAN subnets for TCP `4370` and persists candidates on the Scan page.

Saved devices start local monitoring immediately after Comm Key validation, even when the zone is not registered with head office yet. In that state the agent uses a local unregistered identity, captures live attendance, runs clock/fraud checks, records outages, and queues sync payloads. Once setup succeeds and a zone token is issued, pending local records are associated with the registered zone and uploaded.

For devices or firmware that do not reliably emit SDK live events, the worker also performs a monitored `get_attendance()` reconciliation during the 5-second clock-check loop. Newly appearing records are stored as `LIVE_POLL`, classified with the same live trust rules, and shown on the Live Attendance page through a 3-second local polling feed.

Comm Key brute force is available for owned devices but disabled by default:

```bash
ZK_ZONE_BRUTEFORCE_ENABLED=true zk-zone-agent --host 127.0.0.1 --port 7860
```

The brute-force flow is opt-in per candidate, requires local operator confirmation, refuses public IPs, and refuses already configured live devices unless explicitly allowed through the API.

## State Life HR Enrollment App

The Windows shipping workflow also builds `StateLifeHREnrollment.exe`, a 32-bit portable native app
for **State Life Insurance Corporation** HR enrollment. It scans local ZKTeco port `4370`
candidates, finds or creates regular employee users, and triggers fingerprint enrollment on the
selected machine. The app does not expose Zone Agent admin tools or device controls. The HR EXE is
intentionally 32-bit because ZKTeco `zkemkeeper.dll` deployments are commonly 32-bit COM components.

The HR app uses comm key `1979` by default. IT can override it by creating:

```text
C:\ProgramData\State Life Insurance Corporation\HR Enrollment\secrets\comm_key.txt
```

The file must contain only the numeric comm key.

When an ESP32 Zone Lite unit is permanently attached to the same ZKT device, pause or power off the
ESP32 for the duration of HR fingerprint enrollment. The ESP32 keeps a long-lived ZKT SDK session
open for live capture, and many ZKT devices only handle one SDK client reliably at a time. If
enrollment times out after the finger was saved, the HR app reconnects and verifies the template
before reporting failure.

Network scan follows the candidate-first behavior used by the older dump tool: any local host with
TCP `4370` open is shown, even if the SDK validation step is busy or inconclusive. Unvalidated
candidates are validated when selected actions run, using TCP first and then UDP.

Some uFace/TFT devices can accept the SDK enrollment command but keep the screen on a loading state
instead of returning the registration events expected by `pyzk`. The HR app now drives the remote
fingerprint event loop itself, waits for the three accepted finger taps, clears stale
capture/enrollment state before and after enrollment, reconnects with a short settling retry window
to verify whether the template was saved, and retries the enrollment once with the alternate ZKT
protocol when the first protocol did not save the selected finger.

For uFace devices where remote fingerprint enrollment reaches the device screen but never returns
fingerprint events to the SDK, HR can use **Enroll Face** directly. On Windows PCs that have the
official ZKTeco `zkemkeeper.dll` COM SDK registered, the app uses the official `StartEnrollEx`
face path and then verifies whether the face count increased. The app does not use pyzk face index
`111` as a fallback on uFace devices because that firmware can open a stuck remote fingerprint
screen instead of the face enrollment workflow.

If **Enroll Face** reports that `zkemkeeper.dll` is not registered, install the official ZKTeco
Standalone SDK on the HR Windows PC. Alternatively, place the full official SDK DLL folder beside
`StateLifeHREnrollment.exe` and run the EXE once as Administrator so it can register the COM class.
For custom builds, place the official 32-bit SDK DLL folder under
`installer\windows\vendor`, or set `ZKEMKEEPER_DLL=C:\path\to\sdk\zkemkeeper.dll`
before running `installer\windows\build_hr.ps1`; the build bundles the full DLL folder
next to the portable EXE payload. The required DLL set is `commpro.dll`, `comms.dll`,
`plcommpro.dll`, `plcomms.dll`, `plrscagent.dll`, `plrscomm.dll`, `pltcpcomm.dll`,
`rscagent.dll`, `rscomm.dll`, `tcpcomm.dll`, `usbcomm.dll`, `zkemkeeper.dll`, and
`zkemsdk.dll`. Use 32-bit Python for that build because the official SDK DLLs are 32-bit.

GitHub Actions cannot store the SDK DLL set as a normal repository secret because the payload is
larger than the Actions secret size limit. For shipping CD, upload a private release asset named
`zkteco-sdk.zip` to the `zkteco-sdk` tag, or set repository secret `ZKEMKEEPER_SDK_URL` to a
private zip URL that contains the SDK DLLs. The workflow also honors repository variable
`ZKEMKEEPER_DLL_SHA256` to pin the expected `zkemkeeper.dll` hash.

## Shipping

The repository ships through GitHub Actions:

- `CI` runs lint, tests, and Python package build across Ubuntu/Windows and Python 3.11/3.12.
- `Shipping CD` builds Python distributions on Ubuntu, the Windows Zone Agent installer, and the State Life HR Enrollment portable EXE on `windows-latest`.
- Tag pushes matching `v*` publish a GitHub Release with the Python package and Windows installer artifacts.

To cut a release:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Important Notes

- The zone agent stores mutable local state under `local-data/zone-agent` by default on non-Windows systems and under `C:\ProgramData\ZKZoneAgent` on Windows.
- `pyzk` is used through an adapter (`ZKClient`) so tests can run with fake devices and hardware access remains isolated.
- `pyzk` is GPL-2.0; review licensing before commercial redistribution.
- SQLite is intentional for branch/zone local state. Head Office production runs on Railway PostgreSQL.
- Head Office timeline filters and timestamp display use `ZK_HEAD_DISPLAY_TIMEZONE`, defaulting to `Asia/Karachi`; Zone Agent UI uses the configured zone timezone.
