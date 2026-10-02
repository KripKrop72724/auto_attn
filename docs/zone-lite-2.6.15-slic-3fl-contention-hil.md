# Zone Lite 2.6.15: SLICTOWER 3FL HIL

## Evidence prompting this patch

The signed 2.6.14 image installed on SLICTOWER 3FL. A fresh fingerprint
attendance event for terminal user 89 reached ADD as LIVE event 391153 on
2026-09-28 at 14:01:32 UTC and was acknowledged by Oracle. A later live
event was acknowledged by ADD after `LIVE_OUTBOX_LOCK_RECOVERED`, and ADD
briefly reported `ESP_DELIVERY_WORKER_FAULT`. The worker recovered and device
storage remained healthy. The 2.6.14 HIL campaign was paused pending a fix.

The legacy Oracle drain was found reading and settling up to 100 records while
holding the shared storage lock. This is a plausible source of contention, not
a proven root cause. 2.6.15 limits each slice to 16 records and logs
`ORDS_LEGACY_LOCK_SLOW` when a read or commit holds the lock for at least one
second. The durable queue format and delivery acknowledgement rules remain the
same. ADD reconciliation now accepts a connector release cursor behind ADD's
already committed cursor; it still rejects a cursor ahead of durable state.

## Release and trial constraints

- Publish the signed 2.6.15 image as `HIL_ONLY`, bound to the exact five
  reviewed identities in `deploy/add/hil-targets-2.6.15.json`.
- Start with SLICTOWER 3FL alone. Verify its current signed 2.6.14 application
  digest, OTA success, online state, healthy storage, no new persistent
  delivery-worker fault, and fresh fingerprint attendance acknowledged by
  Oracle. Inspect `ORDS_LEGACY_LOCK_SLOW` if it appears.
- Keep SLICTOWER 13FL on 2.4.12 until its preserved flash queue has a safe
  recovery path. A prior OTA rolled back after the persistence probe failed.
- A physical card/PIN rejection trial is still outstanding on 3FL because no
  disposable test user is available. Startup credential-policy logs and
  terminal-user readback are useful checks but do not substitute for that trial.
- Do not promote to nationwide based on this 3FL trial alone. Both SLICTOWER
  terminals and the other HIL candidates still need their required evidence.

## BLD5 scope extension, 1 October 2026

The requested BLD5 HIL uses the existing signed 2.6.15 image without rebuilding
or changing its manifest. `hil-targets-2.6.15-bld5.json` appends only the active
ZKT connector `510baddb-8eff-4817-bc48-549ee34bbd0f`, MAC
`ac:27:6e:a4:4e:d4`, and confirmed serial `PGB1261300022`. The original five
targets and global HIL configuration remain intact. BLD5 can run independently
alongside the first three sites; the later Peshawar targets still require formal
acceptance. The offline Hikvision device sharing the BLD5 zone is excluded.

BLD5 initially runs factory 2.5.2 with application digest
`27128790bde3ce3d0e5e697bb35189379cda8600f5179fab075f127c2dc9671b`, which is
not a qualified OTA predecessor. Its firmware rejects a same-version 2.5.2
offer. First install the published signed 2.4.12 bridge into an OTA slot and
verify `SUCCEEDED`, online state and digest
`cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589`. Only then
offer 2.6.15. The firmware still verifies the preserved predecessor, persistent
queues and runtime health before committing its boot. Start the saved HIL
observation only after the exact 2.6.15 application digest and healthy current
boot telemetry are confirmed. OTA success alone does not constitute HIL acceptance.

The original published 2.4.12 progress messages omit application digest and
partition fields. The BLD5 bridge adapter acknowledges its runtime-health
handshake without advancing installation state. After 2.4.12 sends a fresh
signed capability report with the exact published digest in an OTA slot, ADD
records the checked boot/reconciliation transitions. A subsequent authenticated
success report completes the bridge. Changed identity, another image, stale
telemetry or proof from another boot cannot use this adapter.

Run `firmware-extend-2-6-0-hil.yml` with scope `2.6.15-bld5` from green main after
deploying the backend scope support. The workflow pins the original source, artifact and
application digests, previews the extension, and atomically replaces only the
quarantine marker while retaining its backup. The signed release remains `HIL_ONLY`.

## Six-city scope extension, 2 October 2026

The requested city cohort consists of the two active devices each in Faisalabad,
Multan, Lahore and Peshawar, and one each in Quetta and Karachi. Spare inventory
is excluded. `hil-targets-2.6.15-cities.json` preserves the six published identities
and appends the eight exact city identities not already present. This specific
fourteen-device scope permits independent HIL campaigns, including the two
Peshawar devices, without creating acceptance evidence. The original five- and
six-device scopes retain their existing ordering rules. The general HIL parser
still limits unrelated releases to eight targets.

Faisalabad, Multan and Quetta run individually identified factory 2.4.12 images;
install the existing signed 2.5.2 bridge first. Lahore runs individually identified
factory 2.5.2 images; install the existing signed 2.4.12 bridge first. The city
factory bridge guards pin each connector, MAC, confirmed terminal serial, current
factory version and application digest, plus the full signed bridge identity.
Offer and download recheck those conditions. The legacy 2.4.12 progress adapter
also covers only the two exact Lahore targets and retains the BLD5 current-boot,
signed capability and runtime-health requirements.

Peshawar already has the qualified signed 2.5.2 predecessor. Karachi has a
qualified signed 2.4.12 predecessor but must receive its pending terminal binding
acknowledgement before HIL eligibility. An ordinary signed 2.5.2 upgrade can supply
the newer expected-serial acknowledgement protocol; do not bypass the confirmed
binding check or change terminal credentials.

After green-main checks and backend deployment, run
`firmware-extend-2-6-0-hil.yml` with scope `2.6.15-cities` and confirmation
`EXTEND-2.6.15-CITIES-HIL`. The guarded extension changes only the quarantine marker
of the exact original signed 2.6.15 image. Start city OTA campaigns from ADD,
verify each bridge before offering 2.6.15, and keep the release `HIL_ONLY`.
Installation success and a saved observation do not substitute for completed HIL
acceptance or authorize nationwide promotion.
