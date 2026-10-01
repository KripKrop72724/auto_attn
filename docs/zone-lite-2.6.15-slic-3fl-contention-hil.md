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

Run `firmware-extend-2-6-0-hil.yml` with scope `2.6.15-bld5` from green main after
deploying the backend scope support. The workflow pins the original source, artifact and
application digests, previews the extension, and atomically replaces only the
quarantine marker while retaining its backup. The signed release remains `HIL_ONLY`.
