# Zone Lite 2.5.0 nationwide COMM Key recovery release

> **HIL quarantine:** the immutable 2.5.0 Swat candidate downloaded completely
> and reached `BOOTED_PENDING` on 25 August 2026, then lost connector visibility
> before proving terminal/runtime health. It must never be promoted to Quetta or
> a nationwide campaign. Version 2.5.1 corrected that Swat failure and passed its
> HIL, but the first staged-recovery boot in Quetta exposed a second internal-RAM
> allocation boundary. Version 2.5.2 supersedes both candidates and must pass the
> fresh HIL and Quetta recovery gates in `zone-lite-2.5.2-comm-key-ota-release.md`.

Zone Lite 2.5.0 adds a fail-closed, audited recovery path for an ESP connector whose
configured ZKT communication key no longer matches its assigned terminal. The release
is intended for every OTA-capable Zone Lite ESP in ADD, including Faisalabad and Quetta.
Campaigns remain zone-scoped and the nationwide orchestrator advances one zone at a
time by default.

## Supported operation

`ESP_ONLY` replaces the ESP's stored ZKT COMM Key only after the candidate key
authenticates the pinned terminal serial. The new value is sealed by ADD for the exact
connector MAC, operation, revision, mode, terminal serial, and expiry. The firmware
stores it in HMAC-protected encrypted NVS using a two-phase journal and reports success
only after authenticated serial verification.

`ESP_AND_TERMINAL` is intentionally unavailable in 2.5.0. ADD exposes the reason and
will reject this mode until a terminal model/firmware adapter has passed destructive
hardware certification. This prevents an unverified remote write from locking out both
sides of the connection.

ADD stores managed values under a dedicated Fernet key. API responses, WebSocket/SSE
events, command results, logs, and audit metadata never contain the value. Break-glass
reveal requires a fresh administrator password, a reason, and exact typed confirmation;
the UI removes the value after 15 seconds, on window blur, on backgrounding, or when the
drawer closes. The response is explicitly non-cacheable.

## Deployment order

1. Back up ADD's database and deploy migration `20260825_0020` with the backend and
   frontend while COMM Key mutation and reveal flags remain disabled.
2. Configure a dedicated `ADD_COMM_KEY_SECRET_FERNET_KEY`. It must not reuse the PII
   encryption key. Retain it in the production secret store before any managed value is
   created.
3. Enable `ADD_COMM_KEY_MANAGEMENT_ENABLED`, leaving reveal disabled unless the
   break-glass workflow is approved.
4. Build the exact reviewed 2.5.0 commit in ESP-IDF 5.5.3, pass the physical OTA HIL
   workflow, sign it in the protected firmware-signing environment, and publish the
   immutable artifact.
5. Complete and accept the production canary. Confirm signed image digest, OTA slot,
   reported 2.5.0 version, ADD connectivity, ZKT certification, source-ledger parity,
   and Oracle delivery quiescence.
6. Run `Invoke-NationwideFirmwareRollout.ps1`. Its default batch size is one zone. Any
   failed/rolled-back device, offline scope member, source-integrity failure, or Oracle
   regression halts and cancels the active campaign.
7. Close the rollout only when every eligible inventory row reports 2.5.0,
   `comm_key_capable=true`, a valid signed application digest, an OTA application slot,
   normal capture activity, and certified source/Oracle state.

Faisalabad and Quetta are not special-cased or excluded. They receive the update when
their connectors are OTA-capable, online in the authoritative ADD inventory, and their
zone reaches its campaign turn. An offline or excluded device prevents that zone from
being treated as fully covered; it must be remediated and rerun.

## Quetta recovery after 2.5.0

Open the Quetta device drawer in ADD, choose **Control → COMM Key recovery**, retain
`ESP connector only`, enter `1979`, verify the pinned serial is `UFS2253100068`, provide
the operational reason, type the displayed device-and-serial confirmation exactly, and
re-authenticate. If the request was staged before the firmware update, the first 2.5.0
heartbeat materializes and delivers it automatically.

Success requires the connector to authenticate the terminal with `1979`, read exactly
the pinned serial, commit revision 1 (or the next displayed revision), and report the
postconditions to ADD. Failure leaves the previously committed key and revision intact.
Do not use terminal rotation for this recovery.

## Rollback and incident rules

- Pause/cancel the active zone campaign on any device failure or rollback.
- Do not force-reboot a remote ESP to accelerate OTA. Reboot is owned by the OTA state
  machine after its safe point; physical power cycling is HIL-only.
- A `RECONCILIATION_REQUIRED` or `INDETERMINATE` key operation is not success. Preserve
  evidence and investigate before issuing another revision.
- Never place a COMM Key in a ticket, log, screenshot, command line, GitHub secret name,
  release manifest, or audit reason.
- Losing the dedicated Fernet key makes managed reveal impossible; rotate it only with
  an explicit data re-encryption procedure.

## Required release evidence

Retain the commit SHA, signed artifact SHA-256, signing key ID, HIL run ID, canary
campaign ID, per-zone campaign IDs, device MACs, reported application digests and OTA
partitions, final firmware versions/capabilities, ZKT serial postconditions, source
coverage results, and Oracle delivery metrics. Secret values are never release evidence.
