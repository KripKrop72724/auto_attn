# Zone Lite remote firmware updates

## Safety contract

Zone Lite OTA updates are application-only. They never rewrite the bootloader, partition table, encrypted NVS, identity queue, or storage partition. A release is eligible only when the device reports all of the following:

- Secure Boot V2 is enabled.
- Bootloader application rollback is enabled.
- The partition layout is exactly `zone-lite-ota-v1`.
- The device is running OTA-capable firmware.

Devices on older firmware remain fully operational and continue sending attendance. ADD labels them `LEGACY_MANUAL_UPDATE`; it does not send them OTA commands or treat them as unhealthy.

`ADD_FIRMWARE_OTA_ENABLED` defaults to `false`. Keep it false until Peshawar bootstrap and hardware acceptance are complete.

## Architecture

1. GitHub Actions builds an exact commit from `main` with ESP-IDF 5.5.3.
2. The protected signing and production environments publish an immutable `HIL_ONLY` package quarantined to one exact, locally attached ESP MAC. National OTA remains disabled.
3. A physical hardware-in-the-loop run proves attendance continuity, download power-loss recovery, first-boot rollback, and queue preservation for that exact SHA and exact signed package.
4. The protected `firmware-production` environment promotes the same tested bytes by removing the server-side quarantine marker. It never rebuilds, resigns, or replaces the package.
5. An administrator creates a per-zone production campaign after password step-up and typed confirmation.
6. ADD offers the release to one eligible ESP at a time in that zone.
7. The ESP downloads to the inactive OTA slot, checkpoints progress in encrypted NVS, verifies metadata and SHA-256, and switches the boot partition.
8. The new image must reconnect to ADD and receive first-boot confirmation before it marks itself valid. A crash, reset, or power loss before confirmation causes the bootloader to roll back.
9. ADD advances to the next device only after the current deployment succeeds. Failure or rollback pauses the campaign.

Offline devices remain pending indefinitely. They do not block normal attendance, and they are not skipped into an unsafe parallel rollout.

## Required GitHub configuration

Create these protected environments and require an authorized reviewer:

| Environment | Required configuration |
| --- | --- |
| `firmware-signing` | Authorized reviewers; access to the dedicated ADD Windows signing runner. Private keys are not GitHub secrets. |
| `firmware-production` | Variable `ADD_FIRMWARE_STORE_HOST_PATH`, pointing to a protected host directory mounted read-only at `/firmware` |
| `firmware-hil-peshawar` | Secret `OTA_HIL_RUNNER_COMMAND` on a runner labeled `self-hosted`, `zone-lite-hil`, `peshawar` |
| `firmware-hil-islamabad` | Secret `OTA_HIL_RUNNER_COMMAND` on a runner labeled `self-hosted`, `zone-lite-hil`, `islamabad` |

The three signing private keys are generated on the trusted ADD Windows runner and encrypted with Windows DPAPI `CurrentUser`. Their ciphertext, entropy, public keys, and manifest live under `%ProgramData%\StateLife\AttendanceDeviceDashboard\firmware-signing`, with inheritance disabled and access restricted to the runner identity, `SYSTEM`, and local administrators. Private PEM material must never enter ADD's database, containers, HTTP APIs, GitHub secrets, logs, or artifacts.

ADD needs only the manifest verification public key in `ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64`. The production host path is configured through `ADD_FIRMWARE_STORE_HOST_PATH`; Docker mounts it read-only.

## Signing-key custody

The authoritative key vault is created by the `Zone Lite signing key bootstrap` workflow after exact typed confirmation and protected-environment approval. It creates three independent RSA-3072 keys: key 1 is `ACTIVE`; keys 2 and 3 are `RESERVE`. The factory bootloader is signed by all three so their public digests are installed during first boot. Normal application and manifest releases are signed only by the active key.

DPAPI binds decryption to the dedicated Windows runner account. Preserve the ADD host's system-state backup, that account profile, DPAPI master keys, and the firmware-signing directory together. A file-only copy of the `.dpapi` blobs is not a usable disaster-recovery backup. Restore and test the protected runner identity in an isolated recovery exercise before bootstrapping additional zones.

The bootstrap workflow returns a one-day artifact containing only signed binaries, public keys, hashes, and the exact target MAC. The normal release workflow builds unsigned firmware on a hosted runner, then sends only that unsigned application to the ADD runner. The ADD runner decrypts the active key into a restricted temporary file, signs the application and release manifest, overwrites and deletes the temporary plaintext, and publishes only the signed package.

Changing the runner service account makes the existing DPAPI vault unreadable. Do not regenerate keys if a vault exists but cannot be opened; stop and restore the original account/profile. Silent regeneration would permanently split the fleet trust root.

## One-time physical bootstrap

Physical bootstrap permanently enables Secure Boot V2 and cannot be treated like an ordinary flash. Use a sacrificial ESP32-S3 of the exact deployed hardware revision first.

1. Back up the active signing key offline and verify recovery access.
2. Build Zone Lite 2.2.0 from the exact approved commit.
3. Sign the bootloader and application with the active Secure Boot V2 key.
4. Connect one Peshawar ESP directly and read its MAC address.
5. Run `provision_zone_lite.py` with `--signed-bootloader`, `--signed-app`, and `--confirm-secure-boot-for` set to that exact MAC.
6. Verify ordinary punches, queued delivery, reboot recovery, and device identity before disconnecting it.
7. Repeat one ESP at a time in Peshawar.
8. Leave Islamabad and all other legacy ESPs running their current firmware until someone is physically present to bootstrap them.

Never remotely enable Secure Boot, replace the partition table, or bootstrap an unknown device. A mismatched MAC confirmation must stop the provisioning operation.

## HIL evidence contract

The site-owned rig command must exit successfully and write `ota-hil-evidence.json` with this shape:

```json
{
  "device_serial": "physical-device-serial",
  "firmware_sha256": "64 lowercase hexadecimal characters",
  "checks": {
    "baseline_attendance_preserved": true,
    "download_power_loss_resumed": true,
    "first_boot_power_loss_rolled_back": true,
    "new_image_confirmed": true,
    "identity_queue_preserved": true,
    "legacy_device_unaffected": true
  }
}
```

The rig must deliberately remove power during download and again during first boot. A simulated software reset is not sufficient evidence for the power-loss checks.

## Release procedure

1. Merge the firmware change to `main` only after normal CI is green.
2. Run `Zone Lite quarantined HIL candidate` with the exact main SHA, semantic version, and locally attached ESP MAC.
3. Confirm ADD shows the release as `HIL_ONLY`, national OTA remains disabled, and only that exact MAC is eligible.
4. Run `Zone Lite OTA hardware gate` with the exact main SHA and physical rig.
5. Record the successful HIL workflow run ID.
6. Run `Zone Lite signed firmware release` with the same SHA, semantic version, and HIL run ID. This promotes the exact tested package without rebuilding it.
7. Approve the protected production environment only after checking its displayed SHA, version, image hash, and HIL run.
8. Keep `ADD_FIRMWARE_OTA_ENABLED=false` until the Peshawar acceptance checklist is signed off.

Published versions are immutable. If a defect is found, revoke the release and publish a new version. Never replace a `.bin` or manifest under an existing version.

## Campaign procedure

Campaigns are always per-zone. Creation requires a recent password step-up, CSRF protection, the typed zone name, and the typed firmware version.

1. Start with one Peshawar canary ESP.
2. Observe download, reboot, first-boot confirmation, normal punch ingestion, identity reconciliation, and queued-delivery recovery.
3. Wait through the agreed observation period.
4. Continue one device at a time in Peshawar.
5. Pause immediately on any failure, rollback, unexpected identity state, or attendance regression.
6. Roll out to Islamabad only after physical bootstrap there and a separate Islamabad HIL acceptance run.

Do not convert a legacy device into an OTA campaign participant by editing the database. Eligibility must come from device capability attestation.

## Failure behavior

| Failure | Expected result |
| --- | --- |
| Power loss before or during download | Current application keeps booting; download resumes from its checkpoint |
| Corrupt or truncated image | SHA-256 or image validation fails; current slot remains active; campaign pauses |
| Power loss after setting the boot partition | Bootloader starts the candidate; missing confirmation causes rollback |
| Candidate crashes on first boot | Bootloader rolls back; ADD records `ROLLED_BACK`; campaign pauses |
| ADD unavailable | Attendance capture and local queue continue; OTA waits |
| ESP offline | Deployment remains pending indefinitely |
| Legacy ESP polls old endpoints only | Existing behavior is unchanged |
| Release revoked | No new assignment or download grant is issued |

## Key rotation

ESP32-S3 Secure Boot V2 permits a controlled multiple-key lifecycle. Rotation must be a dedicated, HIL-tested release and never an incidental application change.

1. Generate and escrow the next RSA-3072 key offline.
2. Introduce its public digest only through a physically and cryptographically reviewed process.
3. Sign a transition release according to Espressif's supported key-rotation procedure.
4. Complete per-zone canaries before broader rollout.
5. Revoke an old key only after every reachable device has proven the new trust path and an incident review approves revocation.

## Recovery

If ADD or its firmware store is lost, restore the database and immutable firmware directory from backups. The optional GitHub release is a disaster-recovery copy, not the live distribution endpoint. Verify `SHA256SUMS`, the manifest signature, and the image hash before republishing.

If a device repeatedly rolls back, leave it on the known-good image. Do not force the failed version. Collect its ADD deployment events, serial logs, reset reason, current partition, image hash, and HIL comparison before preparing a new release.
