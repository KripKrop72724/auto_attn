# Zone Lite OTA runbook for operators and AI agents

This checklist is authoritative for remote firmware operations. Stop rather than infer missing approvals, keys, device identity, HIL evidence, or zone scope.

## Non-negotiable rules

- Never expose, print, download for inspection, or commit a firmware private key.
- Never copy a DPAPI-protected fleet key into ADD's database, containers, API, GitHub secrets, or an artifact.
- Never regenerate the signing vault because DPAPI decryption failed; restore the original runner identity and profile.
- Never change the dedicated firmware-signing runner account without a tested DPAPI migration and recovery plan.
- Never OTA a bootloader, partition table, NVS, SPIFFS, or storage image.
- Never enable the OTA feature flag before physical Peshawar acceptance.
- Never create a national or multi-zone campaign.
- Never run more than one active deployment in a zone.
- Never overwrite an existing release version.
- Never mark a deployment successful manually.
- Never change a device from legacy to capable in the database.
- Never proceed after a failure or rollback until a human reviews it.
- Never disable existing attendance, identity repair, queueing, or legacy API behavior to make OTA pass.

## Release preflight

- Confirm the candidate SHA is an exact commit on `main`.
- Confirm normal CI passed for that SHA.
- Confirm the physical HIL workflow passed for that exact SHA.
- Confirm the HIL evidence includes both physical power-loss tests.
- Confirm the version in `firmware/zone_lite/CMakeLists.txt` matches the requested release.
- Confirm the signing environment reviewer recognizes the SHA and version.
- Confirm the ADD signing-vault manifest has exactly one `ACTIVE` key and two `RESERVE` keys.
- Confirm the workflow artifact contains no `.pem` private key, `.dpapi` blob, entropy file, or private-key archive.
- Confirm the firmware store has free space and its backup is healthy.

If any item is unknown, stop without publishing.

## Campaign preflight

- Confirm the release is signed, immutable, not revoked, and visible in ADD.
- Confirm the selected zone is exactly the intended zone.
- Confirm every eligible target attests Secure Boot V2, rollback, and `zone-lite-ota-v1`.
- Report legacy and offline devices separately; do not remove them.
- Confirm an operator is available to watch attendance and deployment events.
- Perform password step-up and type the exact zone and version.

## During rollout

- Observe only one in-progress deployment in the zone.
- Preserve pending offline devices indefinitely.
- Confirm normal attendance continues while the image downloads.
- Confirm the device reconnects, reports the expected image hash and partition, and receives first-boot confirmation.
- Confirm at least one known employee punch reaches ORDS after success.
- Pause on `FAILED`, `ROLLED_BACK`, stale progress, identity regression, or attendance regression.

## Incident collection

Record the campaign ID, deployment ID, connector ID, device serial, zone, old and new versions, expected and observed SHA-256, partition, reset reason, last progress phase, ADD event history, and relevant ESP serial logs. Preserve the failed release and evidence. Do not retry by editing status rows.

## Peshawar and Islamabad sequence

Peshawar is the first bootstrap and acceptance zone because physical access is currently available. Islamabad stays on its existing firmware until physical bootstrap is possible. Older firmware remains supported for attendance indefinitely but cannot receive OTA releases.
