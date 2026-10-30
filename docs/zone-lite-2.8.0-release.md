# Zone Lite 2.8.0: secure-pull ZKT terminals

2.8.0 is 2.7.8 plus automatic support for ZKTeco terminals that use the
"secure pull" TCP protocol on port 4370. Everything else (storage repair,
the confirm-first exception, rollback, campaigns) is 2.7.8's, and the
[2.7.8 runbook](zone-lite-2.7.8-hil.md) applies unchanged.

## Why

A SenseFace M2F-LR ("ZKT Eco", platform `ZAM70_TFT`, firmware
`Ver 6.60 Apr 28 2025`) never worked with Zone Lite: discovery logged
"answered TCP but failed ZKT auth". Its desk unit (serial `UFS2253100068`)
showed why on 29 October 2026: it answers `CMD_CONNECT` with **6001**, a
challenge to an RSA/AES handshake, instead of 2000 or 2005, and 2.7.8 treats
every code but those as an authentication failure. The vendor SDK and field
notes list SenseFace 2A, SpeedFace and MB10-VL firmware with the same
challenge; SpeedFace V3L, MB560-VL and MB20-VL are likely the same generation.

## What changed

1. **`zkt_secure.c`** (pure C over mbedTLS; handshake, DMC keystream and
   `CMD_AUTH_EXT` key ported from the MIT-licensed
   [fandbsoft/zkteco4730-java](https://github.com/fandbsoft/zkteco4730-java)):
   - 10063 exchanges RSA public keys (the ESP's 2048-bit PKCS#1 PEM in a DMC
     record: `AA BB type len16 body crc32`, CRC seeded `0xFFEEBBAA`, XOR-ed
     with the vendor's 2033-byte keystream, base64);
   - 10064 sends the client secret RSA-encrypted to the terminal's key and
     decrypts the terminal's secret; the AES-256 key is the text
     `"c^s.(s<<8)^c.(c<<8)^s"`, zero-padded to 32 bytes;
   - 10065 confirms; from then on every frame both ways is
     `50 50 83 7C len32 AES-256-CBC(AB BB len32 zk-bytes crc32 pad)` with a
     zero IV (a zero CRC from the terminal is accepted, as the SDK does);
   - authentication tries `CMD_AUTH` with the native-order comm key, then
     `CMD_AUTH_EXT` (1106: HMAC-SHA256 of the comm key's decimal text, then
     `B3 F6 00 01`); 2032 (auth lock) stops; then `OPTIONS_WRQ SDKBuild=1`.
2. **`zone_lite.c`**: `CMD_CONNECT` answering 6001 runs the handshake in
   `zk_connect_and_auth_with_key_diagnosed`, so discovery, the gateway
   session, the comm-key probe and the maintenance restart all use it.
   `zk_context_t` carries the session's AES key; `zk_send_command`,
   `zk_recv_data_stream`, `zk_send_ack_only` and the live loop seal and
   open frames through it. **A legacy terminal (2000/2005) gets byte-identical
   frames to 2.7.8**: every new branch is behind `ctx->secure`, which a
   zeroed context and every new `CMD_CONNECT` clear.
3. **The ESP's RSA identity** is generated once per boot on the first 6001,
   under a mutex, with an RNG callback that yields every 20 ms so the idle
   tasks (and the task watchdog) keep running; later sessions reuse it.
4. **Clock**: a secure terminal applies `SET_TIME` 1-3 s after acknowledging
   it, so a secure session re-reads the clock up to four more times, one
   second apart, before `ZKT_TIME_SYNC_READBACK_FAILED`. Legacy sessions are
   unchanged.

After the handshake the desk unit matched every 2.7.8 assumption: 72-byte
users, 40-byte attendance (the UID field is the record number, as on the
G3), `0xFFC0` buffered reads with PREPARE/DATA/ACK, a second bulk read in
one session, 36-byte live punches within a second, an 8.5-minute idle
session, `USER_WRQ`/`REFRESHDATA`/`DELETE_USER`, restart (port back in 42 s).

## New ADD log codes

| Code | Level | Meaning |
|---|---|---|
| `ZKT_SECURE_SESSION` | INFO | Once per boot: the terminal uses secure pull and the AES session is up. |
| `ZKT_SECURE_KEY_READY` / `ZKT_SECURE_KEY_FAILED` | INFO / ERROR | The ESP's RSA key, with how long generation took. |
| `ZKT_SECURE_HANDSHAKE_FAILED` | WARN | The stage (public key exchange, session key exchange, server secret, confirm, CMD_AUTH, CMD_AUTH_EXT, authentication), the terminal's code and the module status. A wrong comm key shows as `authentication (code=2001)` or similar. |

The comm-key probe reports a failed secure authentication as
`AUTH_REJECTED` (a code arrived) or `AUTH_NO_RESPONSE`.

## Release plumbing

- 2.8.0 accepts **2.7.8** (application
  `b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e`, ADD's
  published `zone-lite-2.7.8`, AVAILABLE since 29 October) as a qualified
  rollback predecessor beside 2.7.2 to 2.7.7: device guard, compiled marker,
  ADD's `platform_baselines("2.8.0")`, the signing contract and CI.
- The compiled marker is now **166 characters**; ADD's heartbeat field
  (`storage.upgrade_contract`) was 160. **ADD must be deployed with the
  wider field before any device runs 2.8.0**, or its heartbeats are refused
  and it shows OFFLINE. Merging the release deploys ADD first; publish the
  firmware only after that deploy succeeds.
- Publication is as for 2.7.x: `firmware-hil-candidate.yml` publishes
  `zone-lite-2.8.0` HIL_ONLY to the signed 17-target nationwide scope, the
  owner runs campaigns, and `firmware-fleet-promote.yml` promotes it to
  AVAILABLE on field evidence; new boards then get it from
  `firmware-key-bootstrap.yml` (`production_release` 2.8.0, 2.7.8 or 2.5.2
  in ota_1).

## Offers without a confirmed terminal

Owner decision (30 October 2026): a production (AVAILABLE) image never
waits on terminal verification. An ESP behind a secure-pull terminal cannot
log in on 2.7.x, so it never reports a serial or model and its binding can
never be confirmed. `zkt_ota_admission.unconfirmed_terminal_offer` admits
any exact ESP whose terminal is not confirmed (never identified, not yet
confirmed, or confirmed from a label and never observed) for AVAILABLE
images: one of the 17 on its own connector and MAC, or a ZKT ESP added
later. HIL_ONLY images keep the 17 with their confirmed terminals, a
terminal an administrator confirmed still needs a qualified model, and the
K50 keeps its reviewed path. Gilgit (`ac:27:6e:a4:61:68`,
`LF-ZONE-GILGIT-01`), on 2.7.8 behind a SenseFace, is offered 2.8.0 as soon
as 2.8.0 is AVAILABLE.

## Verification

- The C module, built against ESP-IDF 5.5.3's mbedTLS 3.6.5, completed the
  handshake with the real SenseFace (`AUTH` 2001, `AUTH_EXT` 2000, serial
  read, counts, `EXIT`), with both 2048- and 1024-bit client keys, and
  Zone Lite's exact `CMD_CONNECT` bytes get 6001.
- `tests/firmware/tools/check_secure_pull.py` (CI, in `espressif/idf:v5.5.3`,
  ASan/UBSan): keystream digest, CRC, key derivation, both comm keys, the
  sealed frame's layout, a terminal-style frame (AA magic, zero CRC), a
  tampered frame refused, and a full simulated handshake with PKCS#1 and
  SubjectPublicKeyInfo terminal keys.
- **Not verified on an ESP**: no ESP32-S3 has run 2.8.0 against a secure
  terminal, and RSA-2048 key generation time on the S3 is unmeasured
  (seconds; once per boot). The legacy path is unchanged by construction,
  not by a field run.

## Rollout

1. Merge; wait for the ADD deploy to finish.
2. Dispatch the HIL candidate build for 2.8.0 at the merge commit.
3. Campaign a legacy-terminal device first if possible (it must behave as
   2.7.8), then the SenseFace site's new board.
4. Promote with `firmware-fleet-promote.yml` once devices succeed.

Rollback: an unconfirmed 2.8.0 rolls back to 2.7.8 as every 2.7.x does.
