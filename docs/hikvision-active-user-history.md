# Hikvision active-user history

ADD offers two explicit scopes. For Hikvision the UI defaults to **Active users —
all retained history**. **Full terminal audit — all records** remains available.
Existing API callers default to ALL_RECORDS; ZKT behavior is unchanged.

ACTIVE_USERS freezes the employee-number strings from a complete, stable terminal
snapshot received within 15 minutes. It requires the qualified DS-K1T342EFWX
V3.3.5 poll5 pilot profile. The scope includes registered profiles regardless of
CNIC mapping; identity holds still apply before Oracle release. Refresh terminal
users before starting if the snapshot is stale. Later profile additions/deletions
do not silently alter an existing job's scope.

ADD obtains a fixed terminal upper serial boundary, then searches each employee
using employeeNoString and serial bounds. Each employee receives two matching
bounded scans and boundary readbacks; employees without records require two
empty observations. A final terminal lower-boundary check detects retention loss
during the whole job. A response outside the employee/serial scope holds the job
before ingesting out-of-scope attendance. Checkpoints and page receipts retain
existing replay/idempotency semantics. Twenty-record pages fit the existing ESP
3.0.8 request validator and bounded response buffers; no firmware update is needed.

Certificates identify ACTIVE_USERS, the immutable user snapshot, excluded scope,
and each employee's coverage. Oracle assurance is computed only from that job's
committed SCAN1 pages, separately from source completion. This does not certify
former users' records or delete terminal, ADD, or Oracle history. A full-terminal
job must be cancelled explicitly before a replacement can start; its captured
evidence remains intact.

## Hardware observations — 2026-09-18

Read-only checks on the installed V3.3.5 terminal found 33 users with 21,025
matching access-control records, compared with 143,713 terminal-wide records at
source cutoff 173713. These are access-control record counts, not guaranteed
attendance counts. Every user's first page matched its employee ID. Multi-page
users passed serial-seek, decreasing remaining-count, and new-session replay
checks; an unknown employee returned no records. Protected local receipts retain
the per-user results. The expected volume reduction is about 85%; elapsed-time
speedup is not promised because device filtering latency varies.

Native firmware tests prove existing history requests preserve the employee
filter and both serial bounds. Backend tests cover complete multi-page scans,
empty scopes, filter rejection, new punches beyond cutoff, retention loss,
checkpoint rollback/replay, frozen membership, stale snapshots, independent Oracle
assurance, and scope-aware idempotency. UI tests exercise both scope submissions.
