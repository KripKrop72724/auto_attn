# ADD ESP32-S3 physical provisioning

## Deployment status and safety boundary

Physical provisioning is an additive ADD feature and defaults to
`ADD_PROVISIONING_ENABLED=false`. Existing fleet, attendance, reconciliation,
signed OTA, alerts and automatic `/device/v2/onboard` behavior do not depend on
it. Enable it only after both native companions and one exact immutable factory
bundle complete the destructive HIL matrix.

The supported hardware profile is `esp32s3-16mb-zone-lite-v1`: ESP32-S3,
16 MB flash and octal PSRAM. The native companion is supported on Windows
10/11 x64 and Apple-Silicon macOS. Browser Web Serial, generic ESP32, Intel Mac,
iPhone and iPad are intentionally unsupported.

## Trust and data flow

1. The companion creates an Ed25519 installation identity protected by Windows
   DPAPI or macOS Keychain and a five-minute six-digit pairing code.
2. An authenticated ADD administrator approves the code with a password
   re-entry. The companion subsequently authenticates an outbound TLS WebSocket
   by signing a server nonce; there is no localhost listener or URL bearer token.
3. For each session the companion generates a fresh in-memory X25519 key. ADD
   sends validated configuration to the private provisioner. The worker derives
   device-bound material and returns only an AES-GCM/X25519 envelope.
4. The companion downloads that session/MAC-bound artifact with an Ed25519
   signed, timestamped, one-use request. It decrypts in a child process, verifies
   the signed manifest and every hash, performs the authorized eFuse/write
   operation, and reads back every written range.
5. The existing signed onboarding endpoint correlates the physical MAC to the
   session. No manual connector registration or borrowed device token exists.

Wi-Fi password, ZKT communication key, fleet root, ORDS credentials, raw NVS,
HMAC key and connector token are forbidden from PostgreSQL, Redis, audit JSON,
SSE, URLs, receipts and retained logs. The API holds operator input only in a
bounded in-process vault until the protected worker accepts it. Plaintext worker
intermediates use tmpfs. Device plaintext exists only in the companion child
process temporary directory. Files and their job-local key are removed after
use; the system does not claim physical secure deletion on SSD media.

## Production storage and secrets

The deployment host provides:

- `ADD_FACTORY_FIRMWARE_STORE_HOST_PATH`: read-only immutable factory bundles.
- `ADD_COMPANION_RELEASE_STORE_HOST_PATH`: Ed25519-signed companion releases.
- `add-provisioning-artifacts`: short-lived, companion-encrypted artifacts shared
  read-only with the API.

When provisioning is enabled, startup fails unless these independent secrets
are explicit:

- `ADD_FLEET_ROOT_SECRET`
- `ADD_PROVISIONING_PAIRING_SECRET`
- `ADD_PROVISIONING_INTERNAL_TOKEN`
- `ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64`
- the existing firmware manifest verification public key

The legacy PII lookup-key fallback can authenticate already deployed connectors,
but `settings.provisioning_fleet_root_secret` never permits that fallback to
generate a new package.

The `add-production` GitHub environment must provide the two provisioning
secrets (`ADD_PROVISIONING_PAIRING_SECRET` and
`ADD_PROVISIONING_INTERNAL_TOKEN`), the companion Ed25519 public key, and
existing absolute Windows directories for both immutable stores. The deploy
workflow writes `ADD_PROVISIONING_ENABLED=false` unless the production variable
is explicitly `true`; when true, deployment fails before Compose if any secret,
key, or store is absent. API readiness then requires the private provisioner.
The public TLS ingress for `autoattn.slichealth.com` must route both
`/companion/v1/pairings` and the WebSocket `/companion/v1/stream` to `add-api`.

## Immutable release sequence

1. Run `factory-firmware-hil-candidate.yml` for an exact green commit on `main`,
   matching firmware SemVer and sacrificial ESP MAC. The build requires the
   protected setup-portal password and ESP-IDF 5.5.3.
2. The Windows signing runner signs the bootloader with all three RSA-3072 keys,
   the application with the active key, and the canonical full-bootstrap
   manifest with RSA-PSS. Publication is atomic and `HIL_ONLY`.
3. Execute the hardware acceptance matrix and retain its redacted receipt in the
   protected receipt store by SHA-256.
4. Run `factory-firmware-promote.yml`. It verifies that receipt and removes only
   the quarantine marker. It does not rebuild or replace any firmware byte.
5. Run `provisioning-companion-release.yml` natively on Windows x64 and macOS
   arm64. Publish the unsigned Windows installer executable and macOS application
   ZIP through the protected publication runner into immutable
   `{platform}/{version}` catalog entries. The packages are intentionally not
   Authenticode/Apple code-signed; ADD verifies every stored Ed25519 manifest and
   selects the highest SemVer before displaying the exact SHA-256 and OS warning
   steps.

ADD chooses the highest SemVer `AVAILABLE` bundle for the exact hardware profile,
requires protected setup-password evidence, and rejects `HIL_ONLY`, `REVOKED`,
duplicate-version, layout, signature, size or SHA mismatches.

The promotion receipt is canonical JSON with `schema_version: 1`, `status:
"PASSED"`, the exact `bundle_id`, the lowercase SHA-256 of `manifest.json` in
`manifest_sha256`, and a `checks` object. Every required check in the promotion
workflow must be literal `true`: blank-device Secure Boot, managed no-reburn,
foreign-root rejection, every-range readback, power-loss recovery, same-MAC
resume, different-MAC refusal, signed onboarding, terminal read-only binding,
and managed storage/outbox preservation. A receipt hash alone cannot promote
bytes belonging to a different bundle.

## Operator flow and recovery

The UI is **Firmware → Prepare device**. It guides environment/pairing, automatic
USB inspection, configuration, review, password step-up, exact-MAC confirmation
for irreversible paths, package preparation, eFuse verification, range write and
readback, boot evidence, signed onboarding and terminal validation.

Blank devices require physical-label acknowledgement and exact-MAC typing before
the HMAC/Secure Boot boundary. Known managed devices never re-burn eFuse and only
rewrite encrypted NVS plus the signed recovery application; storage/outbox and
OTA partitions are preserved. Unknown locked keys or foreign Secure Boot digests
end in `RECOVERY_REQUIRED` with no standard-wizard override.

If USB or network is lost during a destructive stage, keep the device out of
service and reconnect the same MAC to the same session. A still-running companion
retains job-local proof of a completed eFuse step, re-reads the eFuse purpose, and
continues without re-burning it. A different MAC is rejected. If the companion
process itself exits at an ambiguous eFuse boundary, the standard wizard fails
closed to recovery instead of inferring that the operation completed.
Cancellation is disabled after the irreversible boundary.

Known legacy devices also fail closed in the current standard wizard. The
repository documents the required RAM-only HMAC challenge, but does not yet
contain a production diagnostic image and protected comparison implementation.
Do not enable legacy classification from a browser/companion boolean. That
diagnostic must be implemented, signature-bound, and destructively HIL-validated
before legacy-root provisioning can be released.

An empty expected ZKT serial keeps the terminal read-only. After authenticated
discovery, ADD displays model, serial and IP. The administrator confirms the
serial with password step-up; firmware persists `PIN_TERMINAL_SERIAL` in encrypted
NVS and reports it on heartbeat. Certification can enable mutations only after
that confirmation and the existing stability/snapshot gates.

The additive migration does not revoke capabilities from terminals that were
already authenticated before this feature existed: their observed/expected
serial is recorded as `MIGRATED_PREEXISTING`. Only connectors created by the new
empty-serial provisioning path enter `SERIAL_CONFIRMATION_REQUIRED`.

Locally verified devices that cannot reach the destination network become
`SITE_VALIDATION_PENDING` at session expiry and remain correlated by MAC. Later
signed onboarding resumes the session automatically. Only verified onboarding,
heartbeat, serial binding and certification can produce `VERIFIED_ONLINE`.

## Verification commands

Run before a staging deployment:

```text
python -m ruff check apps/add_backend apps/add_provisioner apps/provisioning_companion/src scripts tests firmware/zone_lite/tools
python -m pytest -q tests/unit tests/firmware tests/companion
pnpm --dir apps/add_frontend test
pnpm --dir apps/add_frontend build
pnpm --dir apps/add_frontend budget
docker compose -f docker-compose.add.yml config
```

Software tests do not replace the mandatory physical matrix: blank and managed
boards, foreign keys, power loss at every boundary, same/different-MAC recovery,
all readback ranges, Wi-Fi and ZKT variants, signed onboarding, serial persistence,
read-only-before-confirmation, stability, live attendance, ORDS delivery and
managed-reflash storage/outbox preservation. The legacy-board matrix remains
blocked on the RAM-only diagnostic described above.
