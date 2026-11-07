# Zone Lite 2.8.3: 4096 terminal users (FSDMAIN)

2.8.3 is 2.8.2 ([2.8.2 release](zone-lite-2.8.2-release.md)) with one change:
the ESP keeps up to 4096 terminal users (`MAX_USERS` in `zone_lite.c`, 2048
before). Everything else is 2.8.2's.

## Why

The uFace800 terminals at LF-ZONE-FSDMAIN hold 2,500 users. With a 2048-user
table every user snapshot is incomplete (`USER_SNAPSHOT_TRUNCATED`),
`zk_enforce_credential_policy` fails, and the ESP disconnects and reconnects
again and again. The ESPs cannot stay connected long enough to acknowledge
`PIN_TERMINAL_SERIAL`, so their terminal bindings stay `PENDING_DEVICE_ACK`.

| Zone | ESP hardware ID | Terminal serial |
|---|---|---|
| LF-ZONE-FSDMAIN-01 | `30:30:f9:17:87:2c` | `CKPG231060360` |
| LF-ZONE-FSDMAIN-02 | `8c:bf:ea:17:95:d0` | `AF4C204260255` |
| LF-ZONE-FSDMAIN-03 | `30:30:f9:1f:90:10` | `AF4C225060506` |

All three ESPs run the AVAILABLE 2.8.1 (application `21238fa3...`) in
`ota_0`. None of them is one of the nationwide 17.

## What changed

- Firmware: `MAX_USERS 4096`, version 2.8.3. Two user tables are held at once
  while a snapshot refreshes, both in PSRAM.
- Storage contract (owner decision, option A): exactly 2.8.2's rollback
  predecessors (2.4.12 ... 2.8.1) and its 178-character marker. 2.8.2 is not a
  predecessor, so an ESP on 2.8.2 does not take 2.8.3 in place.
- Scope: HIL_ONLY to an exact ordered prefix of the FSDMAIN pins (at most
  three; `deploy/add/hil-pins-2.8.3-fsdmain.json`,
  `deploy/add/firmware-2-8-3-hil-scope.ps1`,
  `apps/add_backend/zk_add/zkt283_fsdmain.py`), or of the nationwide 17.
- Binding exception (owner-approved, this scope only): an FSDMAIN target also
  matches while its terminal binding is `PENDING_DEVICE_ACK`. Its serial,
  confirmed serial and expected serial must still all be the pinned serial,
  and `SERIAL_CONFIRMATION_REQUIRED` is still refused. Campaign preview and
  creation, the fleet offer guard, the offer and the download grant apply it.
  Every other release and scope still requires `CONFIRMED`.

## Rollout

1. Deploy ADD with PR #457 (`DEVICE_MESSAGE_MAX_LENGTH` 2 MiB) first, so a
   2,500-user snapshot is accepted.
2. All three serials were confirmed in ADD on 7 Nov 2026 (bindings read
   `PENDING_DEVICE_ACK`). `PIN_TERMINAL_SERIAL` expires after 10 minutes and
   ADD does not resend it; if the ESP reports it EXPIRED or FAILED the binding
   becomes `SERIAL_CONFIRMATION_REQUIRED`, which the exception refuses.
3. Each FSDMAIN ESP is in its own zone (LF-ZONE-FSDMAIN-01, -02, -03); a
   campaign needs exactly one eligible connector in its zone.
4. Publish 2.8.3 HIL_ONLY to the FSDMAIN prefix with `Zone Lite quarantined
   HIL candidate`. The `windows-publication` CI job must pass on the commit
   (the PowerShell publication tests are not run elsewhere).
5. Run one-zone campaigns: FSDMAIN-01, then -02, then -03. Right before each
   campaign check the binding reads `PENDING_DEVICE_ACK` (or `CONFIRMED`); if
   it reads `SERIAL_CONFIRMATION_REQUIRED`, confirm the serial again first.
6. Once 2.8.3 is confirmed and the ESP stays connected: if its pin command
   shows EXPIRED, an admin uses "Confirm serial" again (the expired-pin reissue)
   to send a fresh `PIN_TERMINAL_SERIAL`. The binding must read `CONFIRMED`
   before any HIL run, acceptance or promotion soak.
7. During HIL on FSDMAIN-01 record free PSRAM (heap telemetry) and the log line
   `ADD user snapshot users=... bytes=...`; the snapshot must be below #457's
   2 MiB limit. Two 4096-user tables (about 1.7 MB each) are live during a
   refresh, plus the snapshot JSON.
