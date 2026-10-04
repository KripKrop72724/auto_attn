# ZKT ADD-owned Oracle delivery component

Status: implemented component, inactive for field records. This does not release
2.7.0 or activate a connector. Oracle proof scope is explicit below.

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

## Versioned Oracle projection

New intents require `raw-captures/delivery-v2/check`, contract `2`, scope
`ORACLE_RAW_DAY_TIMES_V2` and confirmation path `ADD_ZKT_PROJECTION_V2`. The
response must bind the exact keyed request digest and event UID, contain a
SHA-256 content token, and independently confirm raw projection and downstream
state. A version-1/core-only response, an echoed UID, or a raw match with daily
processing pending cannot acknowledge delivery or authorize another insert.

The independent reader in `deploy/add/oracle/zkt_delivery_projection_v2.sql`
checks event UID, zone, device, terminal serial, user reference, employee name,
CNIC, timestamp, raw-punch flag, capture type, trust status and clock difference.
It also verifies the derived Pakistan attendance date and presence of the Oracle
receipt timestamp. UTC timestamps retain six fractional digits. Clock difference
uses the table's declared `NUMBER(10,3)` representation with explicit rounding;
null is distinct from measured zero. The original complete payload remains
frozen, including `zone_name`, `status` and `punch`, which the raw table does not
store. These three fields are **not** certified as Oracle columns.

For an ordinary punch, the same SQL statement snapshot verifies one CNIC-to-
employee mapping, the target punch's chronological check-in/out flags, DATASYNC,
and one employee/day row with the expected earliest and latest local punch times.
Same-second punches remain distinct and ordered by event UID. A single ordinary
punch requires no check-out; two occurrences at the same instant require both
times. Non-BIOMETRIC days, linked leave/OD adjustments, duplicate employee
mappings, inconsistent source dates and suspect clocks remain explicit holds.
Raw-only observations require false check-in/out flags and the separate
`RAW_ONLY` disposition. This proves raw preservation and daily punch times,
not leave, roster, holiday, payroll or effective-status policy correctness.
Those business-policy and production qualification gates remain separate.

The content token binds the observed raw fields, receipt time, derived flags,
identity multiplicity, affected-day boundaries, actual daily times and explicit
scope. It is a point-in-time observation, not a promise that another writer
cannot later change Oracle. All source values for one proof come from one SQL
statement; the reader does no DML, locking, transaction control, dynamic SQL or
repair-package invocation. Daily mismatch retains custody without replacing
Oracle rows. Unavailable verification retries; explicit conflicts require review.

Package source and a separately guarded ORDS route installer are provided for
review. Neither is run by ADD deployment. The route refuses an invalid package,
placeholder credentials or ambiguous attendance modules. Configure the distinct
ADD-only credential in a protected installation copy, never in Git. Missing
endpoint/authorization never falls back to the older checker. Existing legacy
records and their routes are unchanged. Previously frozen version-1 intents
require an explicit compatibility review; they cannot silently acquire a new
proof or have their encrypted request overwritten.

## Isolated Oracle execution

`tests/oracle/oracle_test_container.py` creates a labelled disposable Oracle
Free container on an internal-only Docker network without published ports.
`run_projection_checks.py` refuses an unlabelled, externally networked or unhealthy target
and accepts no production DSN or credential. It recreates only its synthetic
schema. CI runs this as `oracle-projection`; container smoke testing depends on
that job. The official 23.26.3.0-lite multi-platform image is pinned by digest.

The reader compiled and passed 38 database checks on Oracle Free 26ai
23.26.3.0.0 (ARM64). They cover every stored immutable field, daily pending/hold
states, null clocks, duplicate identities, same-second occurrence multiplicity,
raw-only evidence, malformed requests, alternate session timezone/numeric locale,
stable/change-sensitive tokens and preservation of the caller's transaction.
Two additional OWA tests verify wrong-credential rejection and safe errors for
authenticated malformed requests. This is not an ORDS HTTP integration test.
A 200,001-row synthetic retained set with 2,001 rows on the affected day took
20–30 ms in two local reads. This is one measured case, not a p99 guarantee.

The read-only `zkt_delivery_projection_v2_preflight.sql` check on 19c parsed the projection's actual column references with
`WHERE 1=0` on every production table; it returned zero matched rows and preserved
the synthetic timestamp's microseconds through the UTC-to-Pakistan conversion.
It did not call a user package or install/compile a stored object.

The complete reader body also passed anonymous PL/SQL compilation on Oracle
19c 19.26 through authenticated APEX SQL Commands on 4 October 2026. Only the
package wrapper was replaced: the exact helper, verification and HTTP procedure
declarations were enclosed in `DECLARE ... BEGIN NULL; END;`. The outer body
called none of those subprograms. APEX returned `Statement processed` in 0.05
seconds. This checks the full PL/SQL and static SQL against the existing column
types without executing an attendance query, creating a stored object or
changing production data. The reviewed source SHA-256 was
`9addda02f3ac0e7d3c69e6669f66810640ba6a9e4897b4997c63698ab730dd0f`.
The generated anonymous block and observed result are retained as protected
local evidence. This is anonymous compilation evidence only.

Production's inspected Oracle engine is 19c. The newer local engine does **not**
qualify stored-package installation/execution on 19c, the production schema's business rules, ORDS
authentication, downstream writer behavior or concurrent real delivery. Those
checks need an isolated matching Oracle environment before field activation.
No production Oracle data or schema was changed during this work.

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
