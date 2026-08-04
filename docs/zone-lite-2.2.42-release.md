# Zone Lite 2.2.42

## Production correction

Zone Lite 2.2.42 keeps authoritative attendance reconciliation available when
multiple preserved queues legitimately occupy most of the 8 MiB SPIFFS
partition.  This was reproduced on `ZONE-SLICTOWER-13FL` after the accepted
2.2.41 boot: the terminal remained online and retained 50,609 punches and 63
users, but a large blocked-identity archive and an older ADD bulk outbox left
insufficient physical blocks for another current-truth append.

The connector now uses a bounded direct ADD delivery only after the durable
bulk outbox cannot accept the truth chunk.  Every direct batch:

- is validated with the same attendance schema as the durable outbox;
- waits for an authenticated ADD acknowledgement;
- emits an auditable success or failure event in ADD;
- remains safe to retry because every event UID is idempotent;
- leaves the original bulk and blocked-identity queues untouched; and
- yields to the separately bounded live-attendance priority path.

The same helper backs the existing current-day priority fallback, removing two
independent implementations of the acknowledgement protocol.  A failed or
unacknowledged direct batch still fails the truth cycle closed and is retried
from the unchanged ZKT terminal truth.

## Rollout gate

Publish as HIL-only, prove the exact signed bytes on SWAT, and then repeat the
single-zone Tower-13 gate.  Expansion remains prohibited until Tower-13 shows
`ZKT_STABLE`, a complete preserved snapshot, current truth delegated through
ADD without `ADD_TRUTH_QUEUE_SATURATED`, and a clean steady state.
