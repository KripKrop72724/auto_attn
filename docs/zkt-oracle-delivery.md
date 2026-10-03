# ZKT ADD-owned Oracle delivery component

Status: implemented component, inactive for field records. This does not release
2.7.0, certify all Oracle fields, or activate a connector.

`zkt_oracle_delivery.register_intent` is an internal transaction helper for future
qualified occurrence creation. It requires a new attendance UID equal to the
occurrence identity, a matching source epoch/ordinal/raw digest, and a consistent
manifest, alias, attendance and outbox relationship. It refuses legacy UIDs and
ambiguous source associations. No current ingestion path calls this helper.
Profile qualification, correct identity evidence and canonical attendance
creation remain the future caller's responsibility.

## Durable route and replay

Migration `20261004_0046` adds delivery intents and content receipts. Existing
attendance rows, Oracle keys and delivery paths retain their prior behavior.
An intent permanently selects the ADD-owned content-verification path, including
after its connector's custody feature is disabled. It is selected before both
ordinary and manually overridden delivery. Generic HTTP success, UID membership
checks and firmware receipts cannot complete it.

At claim time ADD revalidates terminal/source ownership, source occurrence
identity, current identity authorization and clock plausibility. It freezes the
Oracle payload and verification request as encrypted values with a keyed digest.
Retries retain the same bytes and identity. Changed evidence creates a hold;
unavailable encryption keys or verification services retain retryable custody.
An encryption failure cannot leave a partially frozen intent.

The worker first checks Oracle's current content. Only a verified missing record
can be posted. The send attempt is committed before network I/O, and no database
transaction is held over an Oracle request. A lost POST response is followed by
the same content check; successful HTTP transport alone cannot finish delivery.
A matching result commits the receipt and acknowledgement atomically. A content
conflict is held without replacement. Attempt fencing and row locking prevent a
stale worker from acknowledging or sending after a newer claim. Parallel lanes
share the existing worker concurrency budget.

## Exact scope of the current proof

The existing Oracle `raw-captures/identity-repairs/check` contract checks event
UID, terminal serial, terminal user reference, event timestamp, raw-punch flag,
employee name and CNIC. This component records scope `ORACLE_RAW_CORE_V1` and
confirmation path `ADD_ZKT_CORE_CHECK`.

That checker does **not** verify all transmitted zone/device fields, capture type,
clock difference or trust status. Status and punch fields are not stored in the
current Oracle raw table. The check also does not certify downstream daily
processing. A payload digest preserves ADD's complete submitted intent; it is
not a claim that Oracle checked every field. The dedicated ADD-only checker
credential is required. Missing authorization never falls back to UID checks.

The full Oracle projection contract, its live qualification, delivery latency,
failure fairness and downstream completion evidence remain release gates.
Before any intent is activated, backend rollback must also preserve this route;
an older backend that does not understand the intent is not a valid reader.

## Recovery and verification

The additive migration deliberately retains encrypted intents and receipts on
downgrade. A PostgreSQL dump/restore test preserved both ciphertexts, receipt
identity, attempt history and scope, including downgrade/upgrade and schema
comparison. Test data were synthetic; this is not an Oracle acceptance trace.

SQLite and PostgreSQL tests cover lost replies after simulated Oracle commit,
post success followed by an unavailable check, changed identity/source/epoch,
feature rollback, unavailable keys, failed receipt persistence, malformed
responses, legacy UID preservation and migration replay. A real PostgreSQL
concurrent-verifier test proves that only one receipt commits. The existing
100-record firmware-receipt test still requires one flush; route decisions are
loaded once before mutating that batch.

Field capture activation, qualified canonical occurrence creation, and the full
bridge/2.7.0 acceptance gates remain incomplete.

The complete backend unit suite passed 1,441 tests with one SQLite-only row-lock
skip; the corresponding PostgreSQL test passed against the disposable server.
