# Implementation Notes

## Current ZKT Device POC Profile

- Label: `Main Gate MB20`
- IP: `192.168.110.137`
- Port: `4370`
- Comm Key: `1979`
- Serial: `ADZV211860253`
- Device Name: `MB20/0`
- Platform: `ZLM60_TFT`

Use the zone local UI at `/devices` to add this device. Registration is not required for
local capture: the worker starts with a local unregistered identity, records attendance,
clock checks, outages, and fraud incidents, then syncs the queued rows after setup issues
a real zone token.

## Local Data Flow

1. Device worker receives live capture or dump record.
2. If SDK live events are not emitted, the worker reconciles `get_attendance()` during
   the 5-second clock loop and stores newly discovered records as `LIVE_POLL`.
3. Attendance processor generates deterministic `event_uid`.
4. Record is inserted into local SQLite.
5. Audit ledger appends a chained hash row.
6. Sync queue receives a canonical JSON payload.
7. Sync worker uploads batches and keeps local rows after ACK.

## Trusted Time Flow

1. Head office `/api/time` anchors trusted UTC.
2. Agent stores server UTC and `time.monotonic_ns()`.
3. During internet outage, trusted time advances from monotonic elapsed time.
4. Windows wall-clock movement is compared against monotonic elapsed time and creates `ZONE_PC_CLOCK_TAMPER` when it jumps over 15 seconds.

## Production Head Office

- Railway project: `zk-head-office-prod`
- Railway service: `head-office`
- Canonical branch sync URL: `https://head-office-production.up.railway.app`
- Existing branch installs must either upgrade to a build with that default or set `ZK_ZONE_PRODUCTION_HEAD_OFFICE_URL=https://head-office-production.up.railway.app` before setup/reset.
