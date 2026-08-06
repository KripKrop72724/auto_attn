# Zone Lite 2.4.0 release

Zone Lite 2.4.0 adds the backward-compatible `history_stream_v2` reconciliation transport. ADD remains the only durable cursor authority. Existing jobs—including an in-progress Karachi job—resume at their latest committed ordinal and chain digest; the migration does not recreate, renumber, or reset a reconciliation job.

Each stream-v2 assignment durably reserves at most 400 source rows. Firmware prepares and validates the ZKT source once, copies the bounded raw burst to PSRAM, issues `CMD_FREE_DATA`, and only then submits up to four sequential 100-row chunks. Every chunk remains an independent ADD transaction and advances the cursor only after ADD returns its post-commit cursor and chain digest. Lost acknowledgements therefore replay safely, while conflicting evidence remains fail-closed.

Promotion requires:

- exact signed application version `2.4.0`, immutable application SHA-256, approved signing key, and the exact tested Git SHA;
- PostgreSQL migration `20260806_0013` with the pre-deployment reconciliation job IDs, cursors, and chain digests preserved or advanced after deployment;
- production Karachi HIL evidence showing at least 4× the preserved 2.3.0 records-per-second baseline;
- proof that one preparation supplies four commits, `CMD_FREE_DATA` precedes network waits, ACK cursor/chain checks reject mismatches, stale assignments are discarded, and a pending command releases unused credit at a committed boundary;
- live punches during preparation and delivery recovered exactly once by normal live capture or the certified append tail;
- stable heap/largest-block evidence and a 24-hour Karachi soak without watchdog resets, ZKT degradation, cursor regression, unsafe resolvable rows, or attendance loss;
- ADD readiness, frontend reconciliation progress, Oracle priority, device connectivity, OTA readiness, and terminal certification gates passing before each later zone;
- sequential rollout in the authorized order, stopping on the first failed gate and never forcing a reboot during an exclusive ZKT operation or administrator lease.

Firmware 2.3.0 remains the rollback target during the canary observation window. A rollback retains ADD chunks, manifests, coverage evidence, Oracle outbox rows, and the exact committed cursor.
