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
