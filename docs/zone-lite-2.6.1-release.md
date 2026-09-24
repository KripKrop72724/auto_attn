# Zone Lite 2.6.1 ZKT release preparation

## Current state

This branch prepares, but does not authorize, the 2.6.1 release. The unsigned
ZKT image has a signed-manifest contract for direct upgrades from **exactly**
2.4.12 and 2.5.2. It reads the existing queue lanes and continues legacy
queue writes so either installed predecessor can read a rollback image's data.
Both ADD preflight and the new image's first-boot guard match the exact ESP
application hashes of the signed predecessor releases, not their version
strings alone. A missing or mismatched predecessor image is ineligible.
The ESP also sets the authenticated terminal clock to Pakistan Standard Time
on session start and every hour, only after a recent NTP sample. It verifies
the terminal clock by rereading it and sends `ZKT_TIME_SYNC_OK`,
`ZKT_TIME_SYNC_DEFERRED`, or `ZKT_TIME_SYNC_FAILED` to ADD's device logs.

## Blocking credential work

**Do not sign, publish, or campaign this image yet.** The protected signing
script explicitly rejects ZKT 2.6.1 while this work is incomplete. The current ZKT user
record parser exposes password and card fields, but has no validated way to
enumerate or remove palm templates or verify all per-user authentication modes
on the G3, SilkBio-101TC/ID, and MB40-VL/ID terminals. The terminal identity
fingerprint intentionally includes the card value. Clearing a nonzero card
would therefore change the fingerprint used to match attendance events and
could strand previously captured events in identity quarantine. A verified
ADD/firmware transition for that identity change and terminal-model-specific
readback tests are required before enabling the cleanup for every user,
including administrators and users without biometrics. The required cadence
is startup and hourly; the terminal user record must remain while non-biometric
credentials are removed and face/fingerprint templates remain usable.

## Qualification after the credential work lands

1. Use the exact [five target identities](../deploy/add/hil-targets-2.6.1.json)
   in the configured ADD HIL scope and signing workflow. Start with Swat's
   2.5.2 direct-jump canary; then test SLICTOWER-13FL and 3FL from 2.4.12;
   then Peshawar-02 and -06 from 2.5.2. Run one active device per zone.
2. Before 13FL, preserve and review the prior rollback evidence. Do not infer
   that the new predecessor guard fixes its earlier missing runtime-health
   report. Close or cancel old active Peshawar campaigns through ADD before a
   new campaign.
3. For each model, test a user with face and fingerprint, a user with only one
   of them, a user with no biometric, and an administrator. Verify PIN/password,
   card, palm (where supported), and other non-biometric methods are gone on
   startup and after an hourly cycle; verify face and fingerprint still work.
   Check ADD identity continuity, terminal user and punch counts, held events,
   ORDS delivery, and a known employee punch.
4. Require a green main SHA, physical canary, protected signing, immutable
   `HIL_ONLY` publication, and five ordered HIL acceptances under the
   [OTA runbook](zone-lite-ota-agent-runbook.md). Promote only the identical
   signed bytes after acceptance. Nationwide rollout remains one zone at a
   time, with a pause on any rollback or attendance/identity regression.

The generated `build-direct-test` image is unsigned test output and must never
be flashed or used as a release artifact.
