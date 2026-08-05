# Zone Lite 2.2.50 large-terminal truth memory hotfix

## Incident

`ZONE-PESHAWAR-02` completed its 2.2.49 OTA, restored the exact pinned ZKT
serial, 728 users, and more than 96,000 retained punches, and resumed live
capture. Its firmware-forced truth passes nevertheless failed after the large
attendance read. Smaller canaries with the same firmware and truth path
completed successfully.

The large-terminal path retained the multi-megabyte ZKT dump, the verified user
catalog and deduplication state, and an always-maximum 5,000-event reconcile
array at the same time. The final allocation could therefore fail under PSRAM
pressure even when the authoritative terminal snapshot itself was valid.

## Change

- Count valid current-month events per day during the existing first scan.
- Allocate only the largest measured daily truth window, with a one-row minimum
  and the existing 5,000-event hard ceiling.
- Preserve daily transaction boundaries, identity completeness checks,
  before/dump/after count verification, ADD durability, Oracle fail-closed
  replacement, and all live-capture behavior.
- Report the measured month, daily capacity, requested bytes, free PSRAM, and
  largest free block for large terminals; report the same bounded evidence if
  allocation still fails.
- Give every independent periodic truth cycle its own two-attempt fresh-session
  retry budget. An exhausted incident can no longer leak recovery-chunk state or
  retry counters into future cycles.

No punches, identities, queues, or ZKT records are deleted by this change.

## Production acceptance

1. Build and publish the exact main-branch 2.2.50 artifact through CI and HIL.
2. Re-run the immutable SWAT canary and require normal boot, stable identity
   snapshot, live capture, and a completed forced truth cycle.
3. Update `ZONE-PESHAWAR-02` only. Require serial `CJH9211060009`, at least 728
   users, no attendance-count regression from 96,628, a
   `TRUTH_WINDOW_MEMORY_BOUNDED` diagnostic, fresh daily truth delegation, a new
   `last_reconcile_at`, and no later `FULL_RECONCILE_FAILED`.
4. Keep `ZONE-PESHAWAR-06` and nationwide rollout paused until Peshawar-02
   satisfies every acceptance condition.
