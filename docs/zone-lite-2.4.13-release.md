# Zone Lite 2.4.13 non-blocking attendance settlement release

Zone Lite 2.4.13 is the firmware companion to ADD's durable per-item attendance
settlement path. It preserves the existing 2.4.12 wire compatibility while
consuming additive receipt outcome fields when the server provides them.

The outbox advances after ADD confirms a durable semantic quarantine, preventing
one malformed event from blocking newer live punches. Unacknowledged transport or
storage failures remain retryable with bounded jittered exponential backoff. The
firmware validates the complete batch envelope, bounds and syncs its corrupt-row
evidence store, and logs only safe receipt/count metadata for server-side
quarantines.

Build and sign one immutable artifact and follow
`docs/non-blocking-attendance-ingestion-rollout.md`. Backend revision
`20260824_0019` and the compatible server code must be healthy before any OTA
campaign begins. Promote through HIL, one canary, one device per region, and
graduated fleet cohorts. Do not perform an immediate nationwide push.
