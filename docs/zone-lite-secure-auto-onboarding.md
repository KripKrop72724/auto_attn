# Zone Lite secure automatic onboarding

This is the production source of truth for provisioning, reprovisioning, and recovering Zone Lite
ESP32-S3 connectors. It applies to firmware `2.1.1` and later.

## Non-negotiable production flow

There is no manual ADD connector registration. A securely provisioned ESP joins its configured
site Wi-Fi, signs `POST /device/v2/onboard`, receives a rotating connector token, stores it in
encrypted NVS, and opens the ADD device WebSocket. ADD creates or updates the connector row for the
physical Wi-Fi MAC automatically.

Never compile a connector ID or token into firmware, copy credentials from another floor, or use a
`2.0.2` header-based image for a new production ESP.

Before touching hardware, confirm:

- the source reports Zone Lite `2.1.1` or later;
- `tools/provision_zone_lite.py` exists;
- the effective build enables HMAC-protected encrypted NVS;
- production exposes `/device/v2/onboard`; and
- the provisioner's explicit `ADD_FLEET_ROOT_SECRET` comes from the same protected source injected
  into production ADD.

The provisioner must never infer the fleet root from a local `.env.add` or fall back to
`ADD_PII_LOOKUP_KEY`. The latter can create a valid encrypted flash whose onboarding signature is
rejected by production.

## What is permanent and what is updateable

The one-time eFuse burn stores a per-device HMAC key in read/write-protected `BLOCK_KEY0`. It
protects encrypted NVS; it does not bind the ESP to a ZKT terminal, zone, Wi-Fi network, or firmware
version.

- Firmware-only and OTA updates remain supported and preserve NVS.
- Wi-Fi, zone, ZKT, ORDS, and onboarding configuration can be rewritten in encrypted NVS.
- A full flash erase does not erase eFuse, but encrypted NVS must then be regenerated from the
  proven original HMAC root.
- Raw per-device HMAC files are temporary. Recovery depends on the protected root, derivation
  version, ESP MAC, and provisioning record.

Back up the production fleet root in an approved secret manager and offline recovery store. Do not
rotate it silently; rotation requires staged reprovisioning.

## Standard provisioning

Keep site configuration outside the repository with owner-only permissions. It contains Wi-Fi,
zone, ZKT, ORDS, recovery, and onboarding values. Never commit it.

```bash
. /path/to/esp-idf-v5.5.3/export.sh
export ADD_FLEET_ROOT_SECRET='read-exact-production-root-from-protected-store'
python firmware/zone_lite/tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH"
```

The provisioner reads the ESP MAC, derives domain-separated onboarding and NVS HMAC material,
generates encrypted NVS in an OS temporary directory, flashes it, reads it back, verifies SHA-256,
and destroys temporary key material.

### First use of an empty eFuse

The first command stops when `BLOCK_KEY0` is empty. Record the detected MAC, inspect the redacted
eFuse summary, and obtain explicit authorization for that exact physical ESP. Then rerun with:

```bash
python firmware/zone_lite/tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH" \
  --confirm-efuse-burn-for 00:00:00:00:00:00
```

Never guess or reuse a MAC confirmation. Verify that `KEY_PURPOSE_0` becomes `HMAC_UP`, the key
block becomes unreadable/unwritable, and encrypted-NVS readback matches.

### Existing correctly provisioned ESP

For a known ESP whose locked HMAC derives from the current production root, use
`--trust-existing-derived-hmac`. Add `--skip-firmware-flash` when changing only NVS. This never
authorizes a second eFuse burn.

## Exceptional split-root recovery

If records prove that a locked eFuse was derived from a known wrong root, the ESP is recoverable.
Do not change production to the wrong root and do not attempt another eFuse burn. Use the exact
production root for ADD onboarding and the proven original root only for NVS encryption:

```bash
export ADD_FLEET_ROOT_SECRET='read-exact-production-root-from-protected-store'
export ZONE_LITE_NVS_HMAC_ROOT_SECRET='read-proven-original-nvs-root-from-protected-store'
python firmware/zone_lite/tools/provision_zone_lite.py \
  --port /dev/cu.usbmodemXXXX \
  --config /protected/path/zone.json \
  --idf-path "$IDF_PATH" \
  --skip-build \
  --skip-firmware-flash \
  --trust-existing-derived-hmac \
  --confirm-split-root-recovery-for 00:00:00:00:00:00
```

This mode requires the exact attached MAC and refuses an empty eFuse. Back up the existing
encrypted NVS first, require the new NVS readback hash to match, remove both secret environment
variables afterwards, and retain a redacted recovery record.

## Required live verification

Provisioning is complete only after all applicable checks pass:

1. Encrypted NVS initializes and the intended zone/device identity loads.
2. Site Wi-Fi obtains an IP and SNTP establishes valid UTC time.
3. Signed onboarding completes and ADD reports onboarding generation `1` or later.
4. The ADD WebSocket connects and heartbeats remain current.
5. ZKT discovery authenticates with the configured Comm Key.
6. Terminal model, IP, serial, user count, and attendance count appear correctly in ADD.
7. The initial stability gate reaches `ONLINE`; certified profiles become `CERTIFIED`.
8. Startup reconciliation completes without clearing attendance.
9. ADD and ORDS outboxes are independently durable and either drain or raise actionable alerts.

At a flashing desk where the destination SSID is absent, Wi-Fi retries are expected. Record site
validation as pending; do not claim that automatic ADD onboarding was verified until it succeeds on
the destination network.

## Troubleshooting order

If the ESP joins Wi-Fi but does not appear in ADD, check firmware version, encrypted provisioning,
onboarding URL, SNTP, production-root provenance, DNS/TLS reachability, and backend logs—in that
order. An onboarding HTTP `401` commonly means a root/signature mismatch or invalid timestamp. Do
not work around it by creating a manual connector or copying another device token.

If a connector appears but initially reports `RECOVERING/READ_ONLY`, allow the bounded stability
window and inspect live logs. A certified terminal should transition to a stable online state after
the required observations; an unknown record profile remains read-only by design.

## Security rules

- Never print or commit fleet roots, bootstrap secrets, connector tokens, Wi-Fi/ORDS/telnet
  credentials, provisioning JSON, or generated NVS images.
- Never burn eFuse without exact-MAC authorization and a pre-burn summary.
- Never trust an existing unreadable HMAC block without a matching provisioning record.
- Never use split-root recovery without proof of the original NVS root and exact-MAC confirmation.
- Never replace signed automatic onboarding with an unauthenticated registration endpoint.
