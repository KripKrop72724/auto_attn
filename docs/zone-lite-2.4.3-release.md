# Zone Lite 2.4.3 release

Zone Lite 2.4.3 makes baseline and append-tail reconciliation account for every
terminal ordinal without allowing an invalid record to block later punches.

- Both paths use the same signed source-record representation. ADD atomically
  commits contiguous `source_tail_chunk` ranges and sends `source_tail_ack`
  only after every ordinal, the cursor, and the chain digest are durable.
- Invalid timestamps and malformed records are encrypted as immutable source
  evidence, excluded from attendance and Oracle, and shown in ADD's
  **Reconciliation → Source exceptions** inspector. Review and password-step-up
  reveal actions never create or alter attendance.
- ADD's source checkpoint is authoritative after reconnect. ACK loss, power
  loss, or disconnect causes an exact idempotent replay. Digest mutation,
  cursor/count regression, generation change, or chain divergence invalidates
  coverage and raises a production hold.
- Source parity means the committed cursor equals the terminal record count and
  the canonical ledger has exactly one disposition per ordinal. Attendance-row
  count can be lower when a terminal row is malformed or duplicated.
- Karachi resumes from its existing certified cursor 5,043. The migration
  preserves all earlier manifest evidence, including non-canonical repeated
  scans, and refuses to choose a canonical row if old raw digests conflict.

The exact signed image must remain `HIL_ONLY` until the Karachi-only campaign
proves poison-record advancement, valid punches behind the exception, exact
replay after ACK loss, complete source parity and chain continuity, stable live
capture/commands/heap, rollback behavior, and the required 24-hour soak. Only
then may the exact tested bytes be promoted to `AVAILABLE` with an audited
`CANARY_ACCEPTED` closure. Promotion does not start fleet campaigns.
