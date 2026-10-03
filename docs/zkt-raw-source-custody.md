# Raw terminal source custody

This additive receiver component is disabled with `zkt_custody_enabled` by
default. It does not qualify a terminal profile, enable a firmware writer, or
authorize release or rollout.

An epoch-bound reconciliation baseline or tail can declare `RAW_PRESERVED`.
The row contains its ordinal, original bytes, byte digest, terminal record key
and occurrence index. It cannot also claim an attendance event, decoded time,
user reference or source error. Existing 8/16/40-byte transport layouts remain
bounded; accepting those bytes does not establish their semantic layout.

The surrounding ADD transaction commits the encrypted canonical source row,
range receipt, chain checkpoint and a `SOURCE_LEDGER` processing obligation.
Failure of any of those writes prevents an acknowledgement and rolls back the
cursor. An identical retry returns the original committed range, including
after new intake is disabled or the job is paused or ended. Its receipt retains
the original range cursor and matching chain, even when later ranges have
committed; it does not grant new scan credit through a hold.
Equal bytes at two ordinals retain separate rows
and work identities. Original legacy interpretations and attendance keys are
not overwritten by this path.

The obligation starts in `WAIT_PROFILE`, owned by `ADD_PROTOCOL`, with no timed
retry. Unchanged raw evidence does not need repeated semantic inspection. Its
immutable key binds the connector, manifest, terminal, source epoch, generation,
ordinal, layout size and source digests. A later inspection checks that binding
and the decrypted bytes. Evidence mutation and unavailable cryptographic
material are different holds. The custody status endpoint identifies a missing
source obligation without returning protected content.

Recovery-epoch prefix copying reads at most 100 manifests per page and creates
replacement obligations in the same transaction. Failure on a later page leaves
the old epoch and custody intact. This bounds materialization; a large recovery
transaction still needs scheduler/time-budget qualification before activation.

Raw custody does not create attendance or Oracle work. A sealed baseline can
prove capture while its separate Oracle gate remains
`SOURCE_INTERPRETATION_REQUIRED`. Generic exclusion reviews and clock
corrections cannot supply the missing profile proof. The new raw counters are
separate from invalid-source quarantine and tail exception counts. ADD displays
pending interpretation without a review or retry shortcut. Certification of an
older baseline cannot turn its later raw tail into current Oracle certification;
the historical certificate keeps its original cutoff.

Migration `0047` adds the nullable source-manifest work link and raw custody
counters. Existing rows default to zero; encrypted evidence, receipt identity
and work remain retained on downgrade. This data-preserving migration does not
establish semantic compatibility with an older backend: operational rollback
to a reader that does not understand raw holds remains unqualified.

`test_zkt_raw_source_custody.py` exercises SQLite and PostgreSQL transactions,
lost acknowledgement replay, concurrent PostgreSQL submissions, epoch and
capability rejection, immutable byte/identity checks, source versus Oracle
assurance, migration idempotence and retained evidence. The reconciliation UI
test checks pending interpretation and the absence of false completion/actions.

Still required before activation: qualified model fixtures, derived attendance
interpretation, live/history occurrence matching, identity evidence, ADD-owned
Oracle creation and verification, compatible bridge behavior, and the release
plan's workload, recovery, soak and field evidence.
