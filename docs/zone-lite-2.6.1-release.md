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

## PIN and card policy

**Do not sign, publish, or campaign this image yet.** The protected signing
script explicitly rejects ZKT 2.6.1 while this work is incomplete. This
release's credential policy covers terminal PIN/password and card attendance
for every user, including administrators and users without biometrics. It does
not claim to remove palm templates or disable other terminal methods. The
terminal identity fingerprint includes the card value, so a verified
ADD/firmware transition is needed to preserve attendance identity evidence
when a nonzero card is removed. The firmware now checks the complete raw user
table on startup and hourly, attempts an acknowledged pre-write ADD snapshot,
clears only PIN and card bytes, refreshes the terminal's active user cache,
and compares every raw record before each write and after the batch. It then
sends an
acknowledged stable post-write snapshot when ADD is available. It leaves
user records intact; biometric preservation still needs physical confirmation
on each model. ADD retains the old and new card fingerprints across a bounded
transition, but only a captured fingerprint can disambiguate a punch in the
write window. A held punch without that proof remains held for review. The
ADD change must be deployed before firmware rollout. If ADD is offline, the
firmware still removes PIN/card credentials and verifies the terminal state;
it logs deferred identity continuity and resends the current snapshot on
reconnect. Attendance lacking sufficient retained identity proof stays held
for review. Offline/reconnect behavior needs HIL validation before release.
Each terminal model needs a card and fingerprint canary before wider rollout.

### G3 disposable-user evidence, 24 September 2026

On the authenticated SLICTOWER 3FL G3, a single operator-created disposable
user had one fingerprint template and a PIN. A user-scoped raw 72-byte record
write cleared its PIN while retaining the other record bytes. Readback found
the PIN absent, the fingerprint template hash unchanged, and all 167 other
user records byte-for-byte unchanged. The operator physically confirmed that
the PIN no longer works and the fingerprint still works. The disposable user
was then deleted; readback again found all 167 other records unchanged. This
is protocol and physical evidence for the known PIN field on **one G3**. The firmware's
[`zkt_credential_record` helper](../firmware/zone_lite/main/zkt_credential_record.c)
encodes the 28/72-byte PIN and card offsets and rejects other record sizes;
the startup/hourly enforcement calls it for every user, including admins.

A second disposable G3 user had a card, PIN, and fingerprint that all worked
before the write. A scoped 72-byte record write cleared PIN and card bytes;
readback after reconnect found both absent, fingerprint template slot 7
unchanged, and all 167 other user records byte-for-byte unchanged. The
operator physically confirmed that only the fingerprint still works. The
disposable user was deleted, again leaving every other record unchanged.

### SLICTOWER 13FL rollback evidence, 22 September 2026

ADD campaign `151669daef2910e907adb81a630c468c` is paused after a
2.4.12-to-2.5.4 HIL attempt. ADD recorded a verified target boot and repeated
`WAITING_FOR_RUNTIME_HEALTH` states. It then observed two fresh heartbeats on
the previous firmware and marked the deployment `PREVIOUS_FIRMWARE_OBSERVED`
at 1:51 pm PKT. The reset cause was not reported. The 2.6.1 HIL must establish
runtime-health reporting and explain any return to the previous firmware
before treating 13FL as accepted.

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
   of them, a user with no biometric, and an administrator. Verify PIN/password
   and card attendance are disabled on startup and after an hourly cycle;
   verify face and fingerprint still work.
   Check ADD identity continuity, terminal user and punch counts, held events,
   ORDS delivery, and a known employee punch.
4. Require a green main SHA, physical canary, protected signing, immutable
   `HIL_ONLY` publication, and five ordered HIL acceptances under the
   [OTA runbook](zone-lite-ota-agent-runbook.md). Promote only the identical
   signed bytes after acceptance. Nationwide rollout remains one zone at a
   time, with a pause on any rollback or attendance/identity regression.
5. The national ZKT inventory also includes uFace800, uFace800/ID, and
   uFace800 Plus/ID terminals outside the five-device HIL. Before expanding
   past the first device of each of those models, verify the same credential
   removal, biometric preservation, clock, and attendance postconditions on
   that model. Offline or degraded devices remain pending until healthy.

The generated `build-direct-test` image is unsigned test output and must never
be flashed or used as a release artifact.
