# ZKT restart handoff

The `LIVE_CAPTURE` activity label does not establish that a terminal read or
attendance write has finished. OTA previously accepted that label as a restart
safepoint. A request could therefore race a packet being received or saved.

The ZKT gateway now brackets complete live sessions, discovery, configuration,
scheduled restarts and recovery restarts with one terminal-owner guard. OTA
latches its request under the same short lock. A latched request rejects new
sessions and administrative activity; the current session finishes its active
packet and cleanup calls before releasing ownership. The live loop checks for
the request between operations, and discovery declines further probes. Every
early return from a guarded operation still passes through the ownership
handoff. Lock contention delays the handoff rather than claiming completion.
The Hikvision restart gate retains its existing worker-lock contract.

Exact ZKT 2.6.16 and 2.7.0 images then quiesce the journal storage owner. It stops
accepting requests and finishes every request already admitted, including
abandoned caller tickets. Only its own execution loop can confirm quiescence,
after releasing storage resources. Completed replies can still be collected;
they do not count as active I/O. Recovery and new proof operations remain stopped
until reboot. No mutex is retained across network activity or the wait loop.

`WAITING_FOR_JOURNAL_QUIESCE` and the runtime's `QUIESCING` phase make a blocked
handoff visible. A stalled operation is never killed or assumed complete after
a timer. Quiescence describes stopped I/O, not successful attendance persistence:
the previous write result and incident counters remain intact. Local boot and
writer readiness are false while the owner is quiescing.

Production control-flow tests request restart during each guarded operation,
refuse a concurrent session, and exercise repeated handoff-lock failures. A
real threaded owner test blocks an append, abandons a queued caller and proves
the barrier waits for both. OTA tests verify that journal drain begins after the
terminal handoff, remains disabled for older ZKT versions, and leaves Hikvision's
existing restart gate intact.

This component does not complete the transfer of legacy queues or catalog
operations to the storage owner, or the operational bridge rollback coordinator.
Those remain release gates. Physical power interruption and flash endurance
remain **NOT_PERFORMED**.
