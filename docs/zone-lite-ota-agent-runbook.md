# Zone Lite OTA runbook for operators and AI agents

This checklist is authoritative for remote firmware operations. Stop rather than infer missing approvals, keys, device identity, HIL evidence, or zone scope.

## Non-negotiable rules

- Never expose, print, download for inspection, or commit a firmware private key.
- Never copy a DPAPI-protected fleet key into ADD's database, containers, API, GitHub secrets, or an artifact.
- Never regenerate the signing vault because DPAPI decryption failed; restore the original runner identity and profile.
- Never change the dedicated firmware-signing runner account without a tested DPAPI migration and recovery plan.
- Never OTA a bootloader, partition table, NVS, SPIFFS, or storage image.
- Never promote an exact-target candidate before a successful production
  canary has been accepted through the central ADD control plane.
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
- Confirm the exact-target production canary succeeded for that exact SHA and
  signed package.
- Confirm the canary returned to `ONLINE`, `OTA_READY`, `LIVE_CAPTURE`, and a
  certified complete ZKT snapshot without attendance-count regression.
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

## Central nationwide sequence

The trusted `WIN-80SOHNEH66P` runner hosts the release and ADD control plane; it
does not claim direct physical access to any terminal. Start with the exact
`ZONE-SWAT-01` canary, promote the identical signed bytes after remote
acceptance, then create one zone-scoped campaign at a time. A later zone starts
only after the preceding device is back to `ONLINE`, `OTA_READY`,
`LIVE_CAPTURE`, terminal parity is preserved, and every resolvable attendance
row is Oracle-confirmed.

Missing or conflicting CNIC rows are a separate fail-closed state. They remain
durably preserved as `BLOCKED_IDENTITY` or identity-reuse quarantine, do not
prevent safe OTA, and may drain only after verified identity evidence is
provided. Older non-OTA firmware remains supported for attendance and must
never be made OTA-capable by editing database state.
