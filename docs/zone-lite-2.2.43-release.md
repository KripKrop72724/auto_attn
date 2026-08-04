# Zone Lite 2.2.43

## Production correction

Tower 13 proved that 2.2.42 successfully delivered and received authenticated
ADD acknowledgements for all 67 Aug 3 records and all 64 Aug 4 records under
the reproduced SPIFFS pressure condition.  The cycle nevertheless ended as
failed because the earlier redundant local pending/blocked append had no free
physical block.

2.2.43 treats that exact state as recovered only when all of the following are
true:

- the authoritative ZKT dump is complete and identity-safe;
- the ZKT still retains the source records;
- ADD is enabled and authenticated;
- every authoritative batch has been accepted by the normal durable outbox or
  the bounded direct-ACK fallback;
- the full truth count matches the mapped terminal count; and
- no window overflow or downstream delivery error occurred.

No pending, blocked-identity, or ADD outbox is truncated.  If any ADD
acknowledgement is missing, the existing fail-closed retry remains unchanged.
Successful pressure recovery emits `LOCAL_RECONCILE_STORAGE_RECOVERED`, clears
the truth-repair LED fault, and records the full reconciliation checkpoint.

## Rollout gate

Publish as HIL-only and prove on SWAT before promotion.  Repeat the single-zone
Tower 13 pressure gate after promotion.  Expansion remains prohibited until
Tower 13 shows both `ADD_DIRECT_ACK_FALLBACK_SUCCEEDED` and
`LOCAL_RECONCILE_STORAGE_RECOVERED`, clean Aug 3/Aug 4 delegation, no later
`FULL_RECONCILE_FAILED`, and a steady `ONLINE/LIVE_CAPTURE/OTA_READY/CERTIFIED`
state with the 50,609-punch/63-user baseline preserved or increased.
