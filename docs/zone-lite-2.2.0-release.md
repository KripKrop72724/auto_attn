# Zone Lite 2.2.0

Zone Lite 2.2.0 introduces the OTA bootstrap required for future remote application updates.

## Included

- Secure Boot V2 and application rollback build configuration.
- Factory plus two guarded OTA application slots.
- Resumable HTTPS application download into the inactive slot.
- Encrypted-NVS download journal and first-boot confirmation state.
- Automatic bootloader rollback when the candidate cannot confirm health.
- Signed ADD capability, assignment, progress, and download requests.
- Legacy-device compatibility and a disabled-by-default server feature gate.

## Physical installation requirement

Version 2.2.0 must be physically flashed once with a signed bootloader and signed application. The provisioning command requires an exact MAC-address confirmation before the irreversible Secure Boot operation. Peshawar is first; Islamabad remains on the current firmware until physical access is available.

## Rollout hold

Do not enable remote campaigns solely because 2.2.0 has been merged or deployed. Complete the sacrificial-device test, Peshawar physical bootstrap, HIL gate, and canary observation described in `docs/zone-lite-remote-firmware-updates.md` first.
