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
