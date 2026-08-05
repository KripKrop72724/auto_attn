# Zone Lite 2.2.47

## Production fault-latch recovery

Zone Lite 2.2.47 keeps the original ORDS pending outbox authoritative when a
disposable rewrite cannot be opened, allocated, written, flushed, synced, or
atomically installed. Every source-preserving failure now becomes a bounded,
rate-limited backlog retry and explicitly clears a stale `LOCAL_FAILURE` latch.

This closes the production gap observed on Tower-13 after 2.2.46: live capture,
direct ADD delivery, and authoritative ZKT reconciliation were healthy, but a
repeated temporary rewrite failure could keep the connector red/degraded even
though the original queue was unchanged.

The atomic replacement path now reports whether the authoritative source was
successfully preserved. A replacement failure is treated as backlog only when
the original queue is still present; failure to restore the authoritative
backup remains a real `LOCAL_FAILURE` and stays fail-closed.

No ZKT users, fingerprints, UIDs, attendance punches, blocked records, pending
ORDS events, acknowledgements, or identity evidence are deleted or rewritten
by this change.
