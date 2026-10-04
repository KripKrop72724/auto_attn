# Derived ZKT interpretation evidence

ADD retains a bounded, encrypted interpretation history for authenticated raw
custody. This is an implementation component, not model qualification or release
authority. Journal custody stays disabled on each connector until its activation
gates are satisfied. No Oracle package, attendance correction or firmware rollout
is performed by this change.

Migration `0048` adds an interpretation-version marker to processing work and an
append-only evidence table. Each step binds the work identity, original byte
digest, length, receipt revision, reported profile, decoder implementation version
and source-manifest reference. Its unique key includes work, implementation version,
input fingerprint and step number. The encrypted payload includes the previous
step's digest; a new decoder version also references the preceding completed
interpretation for the same input. Prior ciphertext, raw custody, source claims,
attendance event UIDs and Oracle keys remain unchanged. Downgrade retains evidence.

The inspector verifies the original receipt/source bytes before proposing facts.
It handles at most 128 records per transaction step, including alternative
layouts. A complete packet remains limited to 65,536 bytes, and the entire
interpretation has a fixed maximum number of steps. Each step closes with its
scheduling state in the same transaction. A failed transaction resumes from the
last committed step; a lost response cannot duplicate a committed step. Connector
then work row locks serialize interpretation with newly arriving fragments.

Independent groups defer their evidence flush until the bounded batch finishes,
allowing PostgreSQL to insert multiple steps and update their work states
together. Each group is visited at most once in that batch. The standalone
decoder helper still flushes by default. Source-association savepoints may flush
earlier within the same transaction; nothing reports progress before the outer
commit. A failed batched write rolls back all proposed steps and work changes,
while the original custody receipts remain intact.

For new work, the inspector also checks for existing interpretation history once
per bounded candidate batch while holding the connector and work locks. An empty
history proof avoids the two per-record metadata reads on the first step. It
is tied to that transaction and exact input fingerprint, consumed once, and
discarded on context exit or failure. A changed revision, reopened transaction,
pending interpretation or any retained history takes the normal chain and
correction-provenance path. Ciphertext, source binding and raw-digest validation
remain mandatory for every record. The proof never grants decoding authority.

Every size-compatible live layout is an explicit hypothesis. Layout errors retain
their byte offset, length, digest and fixed error category. Later valid records
within the same packet remain inspectable. Identical same-second records retain
separate offsets. When two layouts are plausible, the result remains ambiguous.
The old reserved `LIVE_FRAME` format has no specified header contract and remains
an explicit framing hold. A sender's reported model, plausible date, record length
or decoded header cannot grant profile, checksum or terminal-session authority.

Source records retain historical attendance UIDs separately from user references.
In particular, eight-byte records have no inferred enrollment identity. Local
Pakistan time and its UTC interpretation are encrypted with the other proposed
facts. The original encoded value stays present. Source rows retain their original
`RAW_PRESERVED` classification, even when a proposed decoder rejects their bytes.
Interpretation neither alters a source cursor nor certifies Oracle delivery.

A changed implementation version schedules each eligible work group once. The
worker does not repeatedly decrypt unchanged profile holds. New fragments carry
a new evidence revision and fingerprint; prior interpretation is shown as
historical until the new input is inspected. Disabled connectors remain idle.
Database statement/time budgets and connector rotation still apply. A large
packet can span several ticks without retaining a transaction or lock between
them. Database workload qualification remains distinct from field latency.

Migration `0049` adds committed per-connector scheduling state and partial indexes
for pending live intake and revision-held work. The worker prefers due live packet/fragment groups received by
ADD in the preceding 60 seconds, newest first. After at most eight such steps,
it services an older candidate before starting another burst. Explicit retry
deadlines take precedence over decoder changes. Revision-only holds drain in
index order through three disjoint version ranges (null, below and above the
current version); these comparisons schedule work and do not rank decoder trust.
Each range supplies a bounded page ordered by version, intake time and ID.
The merge of those pages and the oldest due page uses the original intake time
for holds without a retry timestamp. Current-version holds are skipped by the
index, and settled live records are excluded from the recent-intake index.
This uses ADD's intake time only; it does not trust a terminal timestamp or treat
an uploaded offline packet as independently verified real-time evidence.

The burst counter commits with the interpretation step under the existing
connector-first lock. Failed transactions cannot consume a historical turn;
restarted and concurrent workers inherit committed fairness. Each of the five
candidate queries is limited to the connector's batch quota, their union is
deduplicated, and at most that quota is inspected. A partially interpreted packet
receives at most one step in a batch. Rotation, lock skipping, statement deadlines
and the overall tick budget continue to apply. Downgrade retains the counter.
This scheduler does not establish ESP backlog catch-up, Oracle latency or fleet
capacity; those require their own integrated qualification.

The queries preserve index ordering instead of sorting all retained work by a
coalesced retry/intake expression. Connector discovery uses independent indexed
ordered one-row probes. Fixed index predicates remain visible in prepared generic
plans. PostgreSQL qualification checks the actual emitted plans against 200,000
current-version holds as well as a large due backlog.

`scripts/run_zkt_custody_load.py` drives the real receipt handler and inspector
against a fresh database in an explicitly named local PostgreSQL container. Its
default load is 17 synthetic connectors, ten single-item envelopes per second
each, for 900 seconds. It uses 2,048 synthetic user references per connector,
replays a committed response every 97 observations, and restarts the inspector
object every five minutes. A separately spawned Python process generates inputs
during a hard 900-second window, using a bounded 150-item queue per connector.
It confirms readiness before the measured window starts and shares the system's
monotonic clock with the receipt workers. Backend execution, Python garbage
collection and report serialization therefore do not share its interpreter lock.
Fixed per-connector offered/taken counters track pending inputs without relying
on platform-specific queue-size support. Emitter failure, startup failure or
unfinished process shutdown cannot produce a passing result. Receipt
waits and response replays cannot stop source arrivals. Queue refusal, missed
input, emission lag exceeding one 100-ms interval, or an incomplete input set
fails the offered-load gate. Already offered inputs have up to 15 seconds after
the window for receipt processing; that grace never creates more inputs or
changes their original scheduled time. Scheduled-input-to-commit latency includes
queueing, and the existing p95/p99 limits remain unchanged. Inspection then has
its own bounded drain interval. The runner saves
progress atomically, including failures, handler latency, scheduled-input-to-commit
latency, database counts and replay counts. Its database is removed after the
final report; a still-running worker prevents removal. Use a new output path for
each run:

```sh
python scripts/run_zkt_custody_load.py --postgres-container local-test-postgres \
  --postgres-user add_service --output /protected/evidence/custody-load.json
```

The container must expose PostgreSQL only on loopback and use a local Docker
daemon. The runner ignores deployment settings, generates a fresh encryption
key and never starts external delivery. A pass only establishes this backend
component's behavior on the measured host. Socket/TLS latency, the ESP journal,
Oracle completion, terminal models, seven-day capacity and field HIL require
separate evidence. Record the host's resources and competing workloads with
the report; a ten-second smoke run cannot replace the full burst.

The ordinary custody API returns only interpretation status, version, sampling
time and current-input/current-decoder flags. It never returns protected facts.
The UI distinguishes proposed facts, ambiguity, rejection, work in progress and
historical evidence, while keeping identity and Oracle completion separate.
Internal evidence consumers must finish the validating chain iterator before
using completeness; it checks every ciphertext and digest link. No attendance
consumer is authorized by this component.

Verification uses synthetic source/live bytes, invalid dates, two plausible
layouts, repeated same-second records, maximum-size fragmented packets,
transaction interruption, corrupted or missing steps, decoder revisions, original
receipt replay, disabled connectors, PostgreSQL row-lock concurrency, additive
migration retention and 200,000 unchanged work obligations. Component tests do
not qualify installed terminal profiles or physical power failure behavior.

Remaining dependencies: independent profile fixtures, qualified interpretation
authorization, closed source/live windows, one-to-one occurrence assignment,
historical identity evidence and ADD-owned Oracle creation. A generic review note
or this decoder's successful syntax result cannot substitute for those proofs.
