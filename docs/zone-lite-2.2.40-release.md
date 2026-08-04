# Zone Lite 2.2.40 guarded resilience release

Zone Lite 2.2.40 fixes two production defects exposed at
`ZONE-SLICTOWER-3FL`:

- recoverable local allocation, outbox, and rewrite failures could raise the
  permanent `FATAL` LED state even while live capture and ADD heartbeats kept
  running;
- attendance batches did not carry the terminal UID and identity fingerprint
  already verified by the firmware user snapshot, so ADD could preserve an
  otherwise resolvable punch as `BLOCKED_IDENTITY`.

## Runtime invariants

- Solid red is reserved for an inert boot/security failure. Runtime storage,
  resource, ORDS, ZKT, and truth failures remain bounded and retryable.
- A matching success signal clears the corresponding recoverable LED fault.
- ADD heartbeat telemetry reports the LED controller's actual selected state,
  not an inference from ZKT online status. `FATAL` and `LOCAL_FAILURE` create
  visible connector alerts.
- Failure to start either core runtime task triggers a controlled reboot so a
  connector never remains online with half of the attendance pipeline absent.

## Identity invariants

- Firmware resolves both terminal UID and user ID when both are present and
  accepts the identity only when they identify the same verified terminal row.
- Live punches carry the UID and terminal identity fingerprint from the stable
  in-memory snapshot. Dump formats that contain a UID carry the terminal UID
  directly.
- The encrypted ADD alias/tombstone catalog may enrich a punch only when any
  available UID evidence also matches. A conflicting UID/user-ID pair remains
  fail closed.
- The backend records the first stable complete snapshot as the start of
  provable identity continuity and automatically requeues safe missing-UID
  punches after that point.
- The bounded repair query filters unrecoverable names, future captures,
  fingerprint conflicts, and unresolved duplicate-CNIC claims before applying
  its limit. Invalid newer rows therefore cannot starve an older valid punch.

Genuinely ambiguous identities remain preserved as `BLOCKED_IDENTITY` or
identity-reuse quarantine. This release does not weaken duplicate-CNIC,
tombstone, timestamp, or authoritative-truth safeguards.

## Verification completed before publication

- Ruff passes for the changed backend and tests.
- All 173 repository tests pass.
- ESP-IDF 5.5.3 builds the ESP32-S3 application successfully.
- The application image is `0x120000` bytes with 55% of the smallest app
  partition free.

## Rollout gate

Publish the exact green `main` SHA as an immutable `HIL_ONLY` candidate and
target only `ZONE-SWAT-01`. Accept that canary only after:

1. OTA reaches `SUCCEEDED` without rollback and returns to `ONLINE`,
   `OTA_READY`, `LIVE_CAPTURE`, and a certified complete snapshot;
2. terminal user and attendance counts do not regress;
3. a known post-update punch is Oracle-confirmed;
4. no new resolvable punch becomes `BLOCKED_IDENTITY` and the safe historical
   repair queue makes forward progress;
5. heartbeat `led_state` matches the physical LED and no runtime condition
   produces solid red; and
6. the normal observation window completes with no stale progress, local
   failure, identity regression, or attendance regression.

Promotion must reuse the exact canary-tested bytes. Subsequent rollout remains
one zone-scoped campaign at a time. A national or multi-zone campaign is
forbidden by the OTA agent runbook.
