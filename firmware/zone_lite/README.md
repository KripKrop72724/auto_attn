# Zone Lite ESP32-S3 firmware

Zone Lite is the sole ZKT connector for ADD. It maintains live attendance capture, durable ADD/ORDS
delivery, device time/health telemetry, safe user commands, automatic onboarding, temporary admin
revocation, intermittent-terminal recovery, and three daily maintenance restarts.

## Production behavior

- Opens one serialized ZKT session and always unregisters/exits/closes it on teardown.
- Restores live attendance before reconcile after an outage.
- Runs a light count reconcile every 15 minutes and a bounded truth read only when needed.
- Treats repeated availability transitions as flapping, waits with bounded jitter, and never scans a
  subnet more than once per 15 minutes.
- Persists the last reconciled attendance count and six-hour truth timestamp in encrypted NVS, so an
  intermittent reconnect performs a light count check instead of repeatedly dumping full history.
- Keeps ADD and ORDS flash outboxes independent; an outage at one destination does not block the
  other or the live ZKT socket.
- Arbitrates ORDS outbox work before a full ZKT dump, then releases the multi-megabyte terminal
  buffer before building or sending downstream truth payloads.
- Gives a due authoritative truth read a bounded three-minute priority lease over background ORDS
  rewrites. If a storage-full rewrite is already in flight, live capture continues and truth
  retries after ten seconds instead of blocking for 75 seconds or falsely reporting a ZKT read
  failure. The lease covers the measured 96-second busy-session retry cadence with margin and still
  expires if the gateway disappears.
- Reads large MB40 buffers in native `0xffc0`-byte chunks with a 90-second authenticated I/O
  deadline and sends `CMD_FREE_DATA` on every post-prepare exit path. A failed authoritative
  attendance read closes the possibly desynchronized protocol session and retries twice on fresh
  authenticated sessions with bounded `0x4000`-byte recovery chunks; these intentional refreshes
  do not count as terminal flapping.
- Measures the largest current-month daily window before allocating truth working memory, retains
  the 5,000-event fail-closed ceiling, serializes ORDS rows one at a time, and durably commits ADD
  truth in groups of 32 batches. This prevents a 90,000-record ZKT dump from coexisting with an
  unnecessary fixed 5,000-event array and reports bounded memory evidence to ADD for large terminals.
- Reconciles the current month every six hours and walks retained terminal history one month at a
  time from the oldest discovered punch. The encrypted-NVS cursor survives reboot; blocked identity
  windows are skipped for the remainder of the sweep, reported to ADD, and retried after 24 hours.
- Requires a stable before/dump/after attendance count and complete identity map before Oracle can
  apply an authoritative window. Oracle API v2 rejects partial truth before any delete.
- Sends every authoritative Oracle month as independently attested daily windows with a dedicated
  45-second HTTPS deadline, keeping ORDS JSON parsing and delete/merge transactions bounded.
  A transport failure before any HTTP status receives one delayed fresh-transport retry; HTTP
  responses are never retried. A status received before an ESP-IDF authentication-parser error is
  preserved and classified as an HTTP/authentication failure, never as a TLS transport outage.
  Retry/recovery/final-failure logs expose only the ESP error name, trust source, bounded attempt
  counts, and window metadata.
  A failed day stops further Oracle requests for that pass while durable ADD enqueueing continues;
  sanitized status/window diagnostics are reported to ADD without response bodies or identities.
- Accepts only ADD-audited historical user-ID aliases, stores their catalog encrypted, and uses it
  to repair preserved blocked rows before retrying authoritative history.
- Reassembles bounded fragmented ADD WebSocket messages before parsing them. Every durably stored
  identity catalog advances an observable generation, immediately retries blocked-row recovery,
  and forces a fail-closed authoritative truth pass.
- Reports a reconcile successful only after ORDS truth and durable ADD enqueue both succeed; the
  serial completion marker is `complete=true`.
- Persists commands before execution and verifies fresh terminal pre/postconditions.
- Supports 72-byte certified user records; 28-byte/partial/unknown profiles remain read-only.
- Persists ten-minute administrator deadlines and revokes locally without ADD connectivity.
- Restarts the ZKT at 02:00, 12:00, and 22:00 Pakistan time, once per persisted slot, unless a lease
  is active. Protocol restart is preferred; configured telnet is a cooldown-protected fallback.

## Secure configuration

Production configuration is stored in encrypted NVS, not a compiled header. The provisioner:

1. reads the ESP Wi-Fi MAC;
2. derives a per-MAC onboarding secret from `ADD_FLEET_ROOT_SECRET` using HKDF;
3. derives a separate HMAC key domain for ESP-IDF encrypted NVS;
4. protects that HMAC key in read/write-protected eFuse BLOCK_KEY0;
5. generates an encrypted 24 KiB NVS partition in an OS temporary directory;
6. flashes firmware and NVS in one transaction; and
7. reads NVS back and verifies its SHA-256 before deleting temporary material.

The build never checks for or includes a local `zone_lite_config.h`. A non-provisioned image has
blank network and credential defaults and cannot join Wi-Fi, reach ORDS, or onboard with ADD.

Copy `tools/provisioning.example.json` to a path outside the repository and fill it there. Never add
the resulting file to git. Export the exact protected fleet root injected into production ADD,
activate ESP-IDF 5.5.3, then run. The provisioner deliberately does not fall back to
`ADD_PII_LOOKUP_KEY`: a successful flash with the wrong root produces an ESP that can join Wi-Fi but
whose signed onboarding is rejected by ADD.

```bash
. /path/to/esp-idf/export.sh
export ADD_FLEET_ROOT_SECRET='read-from-protected-secret-store'
python tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH"
```

On an empty key block the command intentionally stops and prints the detected MAC. Burning the HMAC
eFuse is irreversible and requires separate explicit authorization for that exact MAC:

```bash
python tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH" \
  --confirm-efuse-burn-for 00:00:00:00:00:00
```

Replace the example MAC only with the value read from that physical ESP after approval. For a device
previously provisioned by this fleet, `--trust-existing-derived-hmac` is permitted only when the
fleet record proves the eFuse was derived from the same root. Unknown/non-HMAC key blocks fail closed.

`--skip-firmware-flash` updates only encrypted NVS while still performing readback verification. It
does not skip eFuse safety checks.

### Exceptional split-root recovery

If a device's locked eFuse was demonstrably derived from a known wrong root, do not change the
production ADD root and do not attempt another eFuse burn. Recover it by using the production root
for the ADD bootstrap secret and the original root only to recreate HMAC-protected NVS. This mode is
valid only for an existing `HMAC_UP` block and requires both the existing-key trust flag and exact-MAC
confirmation:

```bash
export ADD_FLEET_ROOT_SECRET='read-exact-production-root-from-protected-store'
export ZONE_LITE_NVS_HMAC_ROOT_SECRET='read-proven-original-nvs-root-from-protected-store'
python tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH" \
  --skip-build \
  --skip-firmware-flash \
  --trust-existing-derived-hmac \
  --confirm-split-root-recovery-for 00:00:00:00:00:00
```

The exact MAC must be read from the attached ESP. Split-root mode refuses an empty eFuse, so it can
never be used to provision a new device. Remove both secret environment variables after recovery.

## Build-only validation

An unprovisioned CI image builds from placeholder defaults and cannot onboard:

```bash
. /path/to/esp-idf/export.sh
rm -rf build
idf.py -B build -D SDKCONFIG=/tmp/zone-lite-sdkconfig build
```

The production target is ESP32-S3, 16 MiB flash, octal PSRAM, custom OTA partition table, full CA
bundle, and HMAC-protected encrypted NVS. CI publishes only non-provisioned binaries for seven days;
site secrets are never Actions artifacts.

The current firmware version is `2.4.0`. ADD onboarding and heartbeat version fields are generated
from the ESP-IDF application descriptor, not a separately maintained string. The main task stack is
8 KiB so rebuilding thousands of persisted event UIDs cannot overflow during boot.

## Recovery safeguards

Some older ZKT devices accept TCP `4370` while their application service is stuck. Recovery is
allowed only after configured protocol failures and a 30-minute cooldown. The telnet path confirms
the expected banner/login, executes the configured restart, waits 90 seconds, and resumes normal
stability gating. It is not used for ordinary intermittent availability.

Preferred IP `0.0.0.0` enables bounded DHCP-subnet discovery. Once authenticated, the last good IP
is used directly. Serial pinning prevents attaching to a different ZKT that happens to answer first.

An OTA image may download while live capture continues, but its reboot is claimed only at an
atomic ZKT safepoint. Startup/user verification, ADD commands, administrator-lease enforcement,
time sampling, reconciliation, and scheduled terminal restarts cannot begin after that claim; OTA
waits for any one already in progress to finish. This prevents an ESP reboot from abandoning a
prepared ZKT bulk-read buffer while preserving live punch capture during the download.

## LED states

The onboard RGB LED defaults to GPIO 48. Recoverable faults latch for at most two minutes and clear
immediately after a matching success signal; fatal boot/security errors remain latched.

- white pulse — boot/storage initialization;
- blue blink — Wi-Fi connection;
- cyan pulse/solid — discovery/ZKT authenticated;
- purple pulse — startup snapshot, reconcile, or outbox drain;
- green solid/flash — live registration/punch accepted;
- amber heartbeat — durable backlog;
- orange blink — ORDS delivery failure;
- yellow blink — ZKT protocol/auth failure or bounded retry;
- red fast blink — controlled terminal restart;
- red slow blink — recoverable local storage/resource failure under automatic retry;
- red solid — fatal boot/security failure that left the connector intentionally inert;
- magenta blink — blocked/ambiguous identity row.

## Hardware release gate

Do not call a flash production-ready from a successful build alone. Complete
[`docs/hardware-acceptance.md`](../../docs/hardware-acceptance.md), including duplicate-serial
quarantine, user create/edit/delete with unchanged attendance count, offline lease expiry,
flapping/quiet-period proof, queued attendance replay, restart idempotency, and a 24-hour soak.
For this release, also retain the evidence listed in
[`docs/zone-lite-2.1.10-release.md`](../../docs/zone-lite-2.1.10-release.md).
