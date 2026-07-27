# Zone Lite 2.2.8 release record

Zone Lite 2.2.8 makes large attendance-truth reconciliation apply durable,
progress-aware backpressure instead of repeatedly filling the finite ADD
outbox.

## Failure addressed

On a terminal with roughly 95,000 retained punches, a complete truth scan could
produce receipt and attendance batches faster than ADD acknowledged them. The
4 MiB durable bulk outbox reached capacity and the fixed two-minute capacity
wait expired even when acknowledgements were still advancing. The next
15-minute truth cycle then produced the same idempotent history again, extending
the backlog.

## Durable behavior

- A heavy reconcile is deferred while any previously durable ADD bulk rows
  remain. Live capture continues and the existing rows drain before another
  historical scan can add duplicates.
- Capacity waits use a ten-minute **no-progress** deadline. Every acknowledged
  row decrease resets the deadline, allowing a finite large history to complete
  while still failing closed on a genuinely stalled transport.
- The ADD acknowledgement timeout is 60 seconds for large durable batches,
  reducing needless retransmission while keeping retries bounded.
- A deferred scan does not clear the firmware-forced truth flag, does not update
  the last-successful-reconcile timestamp, and retries on the next 15-minute
  cycle.
- Existing local outbox files and OTA completion journals survive the update.

## Rollout gates

1. Build and sign the exact main commit with ESP-IDF 5.5.3.
2. Publish 2.2.8 as `HIL_ONLY` for `ZONE-SWAT-01`.
3. Require SWAT to update once, remain ONLINE/OTA_READY, preserve its complete
   user and attendance snapshot, complete the forced truth cycle, and show no
   queue-saturation or reconcile-failure log.
4. Promote the exact canary-tested bytes.
5. Update other OTA_READY devices sequentially. A device with a pre-existing
   backlog must first log `FULL_RECONCILE_DEFERRED_OUTBOX`, drain the backlog,
   then complete a later `FULL_RECONCILE` without error.
