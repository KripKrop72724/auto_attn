# Zone Lite 2.4.1 release

Zone Lite 2.4.1 is the production hardening update for the durable
`history_stream_v2` reconciliation path introduced in 2.4.0.

- ADD serializes the v2 credit-expiry timestamp before sending a WebSocket ACK.
  A chunk that is already durable can therefore always be acknowledged and the
  remaining chunks in the same credit can continue.
- Zone Lite rechecks the inbound reconciliation queue immediately before a
  legacy full truth read. A newly arrived bounded source assignment wins that
  narrow scheduler race and cannot be starved by a multi-minute terminal scan.
- The existing ADD job ID, committed ordinal, chain digest, and protected source
  evidence are retained across the update. No source cursor is recreated or
  reset.

Promotion remains fail closed. The exact signed image must pass the Karachi
hardware canary, durable non-zero resume, four-chunk credit, power-loss and
disconnect recovery, live-punch preservation, stable heap, and 24-hour soak
gates before any fleet rollout.
